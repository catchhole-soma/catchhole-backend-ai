"""Optional, process-owned Worker traces; never a business-operation dependency.

Only explicit domain/client spans are emitted. No global provider, auto instrumentation,
exception bodies, HTTP headers or source text are collected here.
"""

# Telemetry failures must not replace a business BaseException already unwinding.
# The worker_span business-body handler always re-raises the original exception.
# ruff: noqa: BLE001

import asyncio
import logging
import math
import re
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.core.config import Settings
from app.monitoring.worker_metrics import ERROR_TYPES, JOB_TYPES, PURPOSES

logger = logging.getLogger(__name__)
_process_tracing: "WorkerTracingRuntime | None" = None
_worker_execution_scope: ContextVar["WorkerTracingRuntime | None"] = ContextVar(
    "catchhole_worker_execution_scope", default=None,
)
_trace_context_propagator = TraceContextTextMapPropagator()
# Match only known route shapes. Neither the original URL nor its IDs become attributes.
_UUID_PATH = re.compile(r"/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?=/|$)")
_SPRING_OPERATIONS = {
    "/relationship-jobs/claim": "relationship.job.claim",
    "/relationship-jobs/{id}/complete": "relationship.job.complete",
    "/relationship-jobs/{id}/fail": "relationship.job.fail",
    "/relationship-jobs/{id}/heartbeat": "relationship.job.heartbeat",
    "/analysis-jobs/{id}/relationship-analysis": "relationship.context",
    "/analysis-jobs/{id}/relationship-analysis/prepare": "relationship.prepare",
    "/analysis-jobs/{id}/relationship-analysis/complete": "relationship.complete",
    "/analysis-jobs/{id}/relationship-analysis/fail": "relationship.fail",
    "/analysis-jobs/{id}/progress": "job.progress",
    "/analysis-jobs/{id}/complete": "job.complete",
    "/analysis-jobs/{id}/fail": "job.fail",
    "/analysis-jobs/{id}/heartbeat": "job.heartbeat",
    "/analysis-jobs/{id}/character-fact-comparisons/claim-next": "character.claim",
    "/analysis-jobs/{id}/character-fact-comparison-batches/claim-next": "character.batch.claim",
    "/analysis-jobs/{id}/character-fact-comparison-batches/{id}/context": "character.batch.context",
    "/analysis-jobs/{id}/character-fact-comparison-batches/{id}/complete": "character.batch.complete",
    "/analysis-jobs/{id}/character-fact-comparison-batches/{id}/fail": "character.batch.fail",
    "/analysis-jobs/{id}/setting-candidates/{id}/character-fact-comparison-context": "character.context",
    "/analysis-jobs/{id}/setting-candidates/{id}/character-fact-comparison-complete": "character.complete",
    "/analysis-jobs/{id}/setting-candidates/{id}/character-fact-comparison-fail": "character.fail",
    "/analysis-jobs/{id}/world-setting-candidates": "world.publish",
    "/analysis-jobs/{id}/world-setting-comparisons/claim-next": "world.claim",
    "/analysis-jobs/{id}/world-setting-comparison-batches/claim-next": "world.batch.claim",
    "/analysis-jobs/{id}/world-setting-subject-resolutions/pending": "world.subject.pending",
    "/analysis-jobs/{id}/world-setting-subject-resolutions": "world.subject.complete",
    "/analysis-jobs/{id}/world-setting-comparison-batches/{id}/reset-stale-subject-resolution": "world.subject.reset",
    "/analysis-jobs/{id}/world-setting-comparison-batches/{id}/context": "world.batch.context",
    "/analysis-jobs/{id}/world-setting-comparison-batches/{id}/complete": "world.batch.complete",
    "/analysis-jobs/{id}/world-setting-comparison-batches/{id}/fail": "world.batch.fail",
    "/analysis-jobs/{id}/world-setting-subjects": "world.subject.list",
    "/analysis-jobs/{id}/world-setting-candidates/{id}/comparison-context": "world.context",
    "/analysis-jobs/{id}/world-setting-candidates/{id}/comparison-complete": "world.complete",
    "/analysis-jobs/{id}/world-setting-candidates/{id}/comparison-fail": "world.fail",
    "/ai-token-usages/reserve": "tokens.reserve",
    "/ai-token-usages/{id}/settle": "tokens.settle",
    "/ai-token-usages/{id}/release": "tokens.release",
}
_OUTCOMES = frozenset({
    "success", "failure", "canceled", "lease_lost", "unknown", "returned", "skipped",
    "handed_off", "partial",
})
_ERROR_TYPES = ERROR_TYPES | {
    "ledger_error", "failure", "lease_lost", "unknown", "http_4xx", "http_5xx",
    "network", "protocol",
}
_ENUM_ATTRIBUTES = {
    "spring.operation": frozenset(_SPRING_OPERATIONS.values()),
    "http.request.method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}),
    "analysis.job.type": JOB_TYPES,
    "analysis.source.kind": frozenset({"EPISODE", "SETTING_BOOK"}),
    "analysis.mode": frozenset({"CONFIRMED_ONLY", "ORDERED_PROVISIONAL"}),
    "analysis.review_mode": frozenset({"MANUAL", "AUTOMATIC"}),
    "analysis.checkpoint": frozenset({
        "none", "CHUNKS_READY", "CHARACTER_CANDIDATES_SAVED",
        "CHARACTER_COMPARISONS_HANDED_OFF", "CHARACTER_COMPARISONS_FINISHED",
        "WORLD_CANDIDATES_PUBLISHED", "WORLD_COMPARISONS_FINISHED",
    }),
    "llm.purpose": PURPOSES,
    "ledger.outcome": frozenset({"SUCCESS", "FAILURE", "USAGE_UNAVAILABLE"}),
    "outcome": _OUTCOMES,
    "error.type": _ERROR_TYPES,
}
_COUNT_ATTRIBUTES = frozenset({
    "analysis.job.attempt", "analysis.source.char_count", "analysis.chunk.index",
    "analysis.chunk.char_count", "analysis.known_character.count", "analysis.chunk.count",
    "analysis.candidate.count", "analysis.comparison.completed_count",
    "analysis.comparison.failed_count", "analysis.comparison.batch_count",
    "analysis.embedding.completed_count", "analysis.embedding.failed_count",
    "analysis.embedding.skipped_count", "analysis.world.reused_chunk_count", "llm.retry_index",
})


def _warn() -> None:
    # Even a faulty logging handler must not mask a business exception or cancellation.
    try:
        logger.warning("Worker tracing operation failed.")
    except BaseException:  # noqa: S110 - retrying the broken logger would recurse.
        pass


def _safe_attribute(name: str, value, allowed_models: frozenset[str]):
    if name in _ENUM_ATTRIBUTES:
        return value if isinstance(value, str) and value in _ENUM_ATTRIBUTES[name] else "other"
    if name == "llm.model":
        return value if isinstance(value, str) and value in allowed_models else "other"
    if name == "http.response.status_code":
        return value if type(value) is int and 100 <= value <= 599 else None
    if name == "analysis.job.id":
        if isinstance(value, (UUID, str)):
            try:
                return str(UUID(str(value)))
            except (ValueError, AttributeError):
                return None
        return None
    if name in _COUNT_ATTRIBUTES:
        return value if type(value) is int and value >= 0 else None
    if (
        name == "llm.retry_delay_seconds" and type(value) in (int, float)
        and math.isfinite(value) and value >= 0
    ):
        return value
    return None


class SafeWorkerSpan:
    """Small, bounded facade. It never records exception messages or arbitrary keys."""

    def __init__(self, span=None, *, allowed_models: Iterable[str] = ()) -> None:
        self._span = span
        self._allowed_models = frozenset(allowed_models)
        self._outcome_marked = False

    def set_attribute(self, name: str, value) -> None:
        if self._span is None:
            return
        try:
            safe_value = _safe_attribute(name, value, self._allowed_models)
            if safe_value is not None:
                self._span.set_attribute(name, safe_value)
        except BaseException:
            _warn()

    def set_attributes(self, attributes: Mapping) -> None:
        try:
            for name, value in attributes.items():
                self.set_attribute(name, value)
        except BaseException:
            _warn()

    def set_outcome(self, outcome: str, *, error: bool = False) -> None:
        self._outcome_marked = True
        self.set_attribute("outcome", outcome)
        if error and self._span is not None:
            try:
                self._span.set_status(Status(StatusCode.ERROR))
            except BaseException:
                _warn()

    def set_error(self, error_type: str) -> None:
        self.set_attribute("error.type", error_type)
        self.set_outcome("failure", error=True)


class WorkerTracingRuntime:
    """Owns one provider, suitable for direct InMemory exporter injection in tests."""

    def __init__(self, provider: TracerProvider, *, allowed_models: Iterable[str] = ()) -> None:
        self.provider = provider
        self.tracer = provider.get_tracer("catchhole.worker")
        self.allowed_models = frozenset(allowed_models)
        self._closed = False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.provider.force_flush(timeout_millis=5000)
        except BaseException:
            _warn()
        finally:
            try:
                self.provider.shutdown()
            except BaseException:
                _warn()


def create_worker_tracing(
    settings: Settings, worker_kind: str, model_names: Iterable[str | None] = (),
) -> WorkerTracingRuntime | None:
    if not settings.ai_worker_tracing_enabled:
        return None
    provider = None
    exporter = None
    processor = None
    processor_registered = False
    try:
        service_name = settings.otel_service_name
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", service_name):
            service_name = "catchhole-ai-worker"
        worker_kind = worker_kind if worker_kind in {
            "analysis", "character-comparison", "world-comparison", "relationship",
        } else "other"
        provider = TracerProvider(
            resource=Resource({
                "service.name": service_name,
                "service.instance.id": str(uuid4()),
                "deployment.environment.name": settings.app_env if settings.app_env in {
                    "local", "prod",
                } else "other",
                "worker.kind": worker_kind,
            }),
            sampler=ParentBased(ALWAYS_ON),
            shutdown_on_exit=False,
        )
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_traces_endpoint,
            timeout=5,
        )
        processor = BatchSpanProcessor(
            exporter, schedule_delay_millis=1000, max_queue_size=1024,
            max_export_batch_size=256, export_timeout_millis=5000,
        )
        provider.add_span_processor(processor)
        processor_registered = True
        return WorkerTracingRuntime(provider, allowed_models={
            model for model in (
                settings.llm_model, settings.effective_llm_extraction_model,
                settings.effective_llm_subject_resolution_model,
                settings.effective_llm_comparison_model, *model_names,
            ) if isinstance(model, str) and model
        })
    except BaseException as exc:
        if isinstance(exc, Exception):
            _warn()
        if provider is not None:
            try:
                provider.shutdown()
            except BaseException:
                _warn()
        # Before registration the provider does not yet own this processor/exporter.
        if not processor_registered and (processor is not None or exporter is not None):
            try:
                (processor if processor is not None else exporter).shutdown()
            except BaseException:
                _warn()
        if not isinstance(exc, Exception):
            # Initialization is not business-error cleanup: honor process exit.
            raise
        return None


def get_process_tracing() -> WorkerTracingRuntime | None:
    return _process_tracing


def set_process_tracing(runtime: WorkerTracingRuntime | None) -> WorkerTracingRuntime | None:
    global _process_tracing
    previous, _process_tracing = _process_tracing, runtime
    return previous


@contextmanager
def worker_span(
    name: str, *, attributes: Mapping | None = None, root: bool = False,
    kind: SpanKind = SpanKind.INTERNAL,
):
    runtime = get_process_tracing()
    raw_span = None
    token = None
    execution_token = None
    safe_span = SafeWorkerSpan()
    if root:
        try:
            # A failed/disabled nested root must not borrow another execution's scope.
            execution_token = _worker_execution_scope.set(None)
        except BaseException:
            _warn()
    if runtime is not None:
        try:
            # Caller-owned names must be fixed identifiers, never source content or IDs.
            if not re.fullmatch(r"[a-z][a-z0-9_.]{0,95}", name):
                name = "worker.operation"
            parent = context.Context() if root else None
            raw_span = runtime.tracer.start_span(name, context=parent, kind=kind)
            safe_span = SafeWorkerSpan(raw_span, allowed_models=runtime.allowed_models)
            safe_span.set_attributes(attributes if attributes is not None else {})
            token = context.attach(trace.set_span_in_context(raw_span, parent))
            if root and execution_token is not None:
                _worker_execution_scope.set(runtime)
        except BaseException:
            _warn()
    try:
        yield safe_span
    except BaseException as exc:
        if not safe_span._outcome_marked:
            canceled = isinstance(exc, asyncio.CancelledError)
            safe_span.set_outcome("canceled" if canceled else "failure", error=not canceled)
        raise
    finally:
        if execution_token is not None:
            try:
                _worker_execution_scope.reset(execution_token)
            except BaseException:
                _warn()
        if token is not None:
            try:
                context.detach(token)
            except BaseException:
                _warn()
        if raw_span is not None:
            try:
                raw_span.end()
            except BaseException:
                _warn()


def _has_worker_trace_context() -> bool:
    try:
        runtime = get_process_tracing()
        return (
            runtime is not None and _worker_execution_scope.get() is runtime
            and trace.get_current_span().get_span_context().is_valid
        )
    except BaseException:
        _warn()
        return False


@contextmanager
def spring_http_span(method: str, url: str, *, enabled: bool = True):
    """A bounded HTTP CLIENT span within a claimed execution, never an idle root."""
    if not enabled or not _has_worker_trace_context():
        yield SafeWorkerSpan()
        return
    operation = "other"
    try:
        path = _UUID_PATH.sub("/{id}", urlsplit(url).path)
        operation = _SPRING_OPERATIONS.get(path.removeprefix("/api/internal/v1"), "other")
    except BaseException:
        _warn()
    with worker_span("spring.http.request", kind=SpanKind.CLIENT, attributes={
        "http.request.method": method, "spring.operation": operation,
    }) as span:
        yield span


def safe_inject_traceparent(headers, *, enabled: bool = True):
    """Copy request headers; telemetry failures keep the original request untouched."""
    if not enabled or not _has_worker_trace_context():
        return headers
    try:
        copied = httpx.Headers(headers)
        # Explicit W3C propagator: no baggage, auth, body or URL in telemetry.
        _trace_context_propagator.inject(copied)
        return copied
    except BaseException:
        _warn()
        return headers


def job_trace_attributes(payload) -> dict:
    """Read only known metadata; test doubles and older payloads may omit fields."""
    try:
        source_kind = getattr(payload, "source_kind", None)
        source = getattr(payload, "setting_book" if source_kind == "SETTING_BOOK" else "episode", None)
        return {
            "analysis.job.id": getattr(payload, "analysis_job_id", None),
            "analysis.job.type": getattr(payload, "job_type", None),
            "analysis.job.attempt": getattr(payload, "claim_attempt_count", None),
            "analysis.source.kind": source_kind,
            "analysis.source.char_count": getattr(source, "char_count", None),
            "analysis.mode": getattr(payload, "analysis_mode", None),
            "analysis.review_mode": getattr(payload, "review_mode", None),
            "analysis.checkpoint": getattr(payload, "checkpoint_stage", None) or "none",
        }
    except BaseException:
        _warn()
        return {}
