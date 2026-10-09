"""Bounded, best-effort metrics for one CLI worker process.

Token counters are a usage reference; Spring's reservation/settlement ledger is
still the source of truth. Cached input is already included in input tokens.
"""

import asyncio
import logging
from collections.abc import Iterable
from functools import wraps
from time import time

import httpx
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from app.clients.exceptions import WorkerLeaseExpiredError
from app.core.config import Settings
from app.llm.exceptions import LlmOutputTruncatedError, LlmResponseValidationError

logger = logging.getLogger(__name__)
PURPOSES = frozenset(
    {
        "SETTING_EXTRACTION",
        "SUBJECT_RESOLUTION",
        "WORLD_SETTING_EXTRACTION",
        "CHARACTER_FACT_COMPARISON",
        "WORLD_SETTING_SUBJECT_RESOLUTION",
        "WORLD_SETTING_COMPARISON",
    }
)
JOB_TYPES = frozenset(
    {
        "SETTING_EXTRACTION",
        "CHARACTER_FACT_COMPARISON",
        "WORLD_SETTING_COMPARISON",
    }
)
JOB_OUTCOMES = frozenset({"success", "failure", "canceled", "lease_lost", "unknown"})
LLM_OUTCOMES = frozenset({"success", "failure", "canceled", "unknown"})
ERROR_TYPES = frozenset(
    {
        "none",
        "400",
        "401",
        "403",
        "4xx",
        "429",
        "5xx",
        "timeout",
        "network",
        "truncated",
        "validation",
        "canceled",
        "other",
    }
)
_process_metrics: "WorkerMetrics | None" = None


def _bounded(value: str, allowed: frozenset[str], fallback: str = "other") -> str:
    return value if value in allowed else fallback


def _best_effort(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:  # noqa: BLE001 - metrics must never alter the business operation.
            # Never log provider data, dynamic labels, or exception messages.
            logger.warning("Worker metrics operation failed.")
            return None

    return guarded


def _exception_chain(exc: BaseException):
    visited: set[int] = set()
    while exc is not None and id(exc) not in visited:
        yield exc
        visited.add(id(exc))
        exc = exc.__cause__ or exc.__context__


def llm_error_type(exc: BaseException) -> str:
    errors = tuple(_exception_chain(exc))
    if any(isinstance(error, asyncio.CancelledError) for error in errors):
        return "canceled"
    for error in errors:
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status == 429:
                return "429"
            if 500 <= status <= 599:
                return "5xx"
            if status == 408:
                return "timeout"
            if status in {400, 401, 403}:
                return str(status)
            if 400 <= status <= 499:
                return "4xx"
    if any(isinstance(error, (TimeoutError, httpx.TimeoutException)) for error in errors):
        return "timeout"
    if any(isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)) for error in errors):
        return "network"
    if any(isinstance(error, LlmOutputTruncatedError) for error in errors):
        return "truncated"
    if any(isinstance(error, LlmResponseValidationError) for error in errors):
        return "validation"
    return "other"


def job_error_outcome(exc: BaseException) -> str:
    errors = tuple(_exception_chain(exc))
    if any(isinstance(error, WorkerLeaseExpiredError) for error in errors):
        return "lease_lost"
    if any(isinstance(error, asyncio.CancelledError) for error in errors):
        return "canceled"
    return "failure" if isinstance(exc, Exception) else "unknown"


class WorkerMetrics:
    def __init__(
        self,
        environment: str,
        worker_kind: str,
        allowed_models: Iterable[str],
        registry: CollectorRegistry | None = None,
    ) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        self.allowed_models = frozenset(allowed_models)
        self.common = (
            "catchhole-ai",
            _bounded(environment, frozenset({"local", "prod"})),
            _bounded(
                worker_kind, frozenset({"analysis", "character-comparison", "world-comparison"})
            ),
        )
        labels = ("application", "environment", "worker_kind")
        self.jobs_active = Gauge(
            "catchhole_worker_jobs_active",
            "Jobs currently executing in this process.",
            labels,
            registry=self.registry,
        )
        self.job_attempts = Counter(
            "catchhole_worker_job_attempts_total",
            "Finished process execution attempts.",
            (*labels, "job_type", "outcome"),
            registry=self.registry,
        )
        self.job_duration = Histogram(
            "catchhole_worker_job_duration_seconds",
            "Time spent processing a claimed job.",
            (*labels, "job_type", "outcome"),
            registry=self.registry,
            buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600),
        )
        self.last_finished = Gauge(
            "catchhole_worker_last_job_finished_timestamp_seconds",
            "Last local job finish time.",
            labels,
            registry=self.registry,
        )
        self.llm_calls = Counter(
            "catchhole_llm_calls_total",
            "Individual delegate provider calls.",
            (*labels, "purpose", "model", "outcome", "error_type"),
            registry=self.registry,
        )
        self.llm_duration = Histogram(
            "catchhole_llm_call_duration_seconds",
            "Delegate call time excluding ledger and waits.",
            (*labels, "purpose", "model", "outcome"),
            registry=self.registry,
            buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
        )
        self.llm_retries = Counter(
            "catchhole_llm_retries_total",
            "Additional provider attempts actually started.",
            (*labels, "purpose", "model", "error_type"),
            registry=self.registry,
        )
        self.llm_tokens = Counter(
            "catchhole_llm_usage_tokens_total",
            "Reported usage; input includes cached input.",
            (*labels, "purpose", "model", "token_type"),
            registry=self.registry,
        )
        self.llm_usage_unavailable = Counter(
            "catchhole_llm_usage_unavailable_total",
            "Provider calls without reported usage.",
            (*labels, "purpose", "model"),
            registry=self.registry,
        )
        self.jobs_active.labels(*self.common).set(0)
        self._active_jobs = 0
        self.last_finished.labels(*self.common).set(0)
        self._server = None
        self._server_thread = None

    def _llm_labels(self, purpose: str, model: str) -> tuple[str, ...]:
        return (*self.common, _bounded(purpose, PURPOSES), _bounded(model, self.allowed_models))

    @_best_effort
    def record_job_started(self) -> None:
        self._active_jobs += 1
        self.jobs_active.labels(*self.common).set(self._active_jobs)

    @_best_effort
    def record_job_finished(self, job_type: str, outcome: str, duration_seconds: float) -> None:
        # Restore active even when recording another metric fails.
        self._active_jobs -= 1
        self.jobs_active.labels(*self.common).set(self._active_jobs)
        labels = (
            *self.common,
            _bounded(job_type, JOB_TYPES),
            _bounded(outcome, JOB_OUTCOMES, "unknown"),
        )
        self.job_attempts.labels(*labels).inc()
        self.job_duration.labels(*labels).observe(max(0, duration_seconds))
        self.last_finished.labels(*self.common).set(time())

    @_best_effort
    def record_llm_call(
        self,
        purpose: str,
        model: str,
        outcome: str,
        error_type: str,
        duration_seconds: float,
        usage: tuple[int, int, int] | None,
    ) -> None:
        labels = self._llm_labels(purpose, model)
        outcome = _bounded(outcome, LLM_OUTCOMES, "unknown")
        self.llm_calls.labels(*labels, outcome, _bounded(error_type, ERROR_TYPES)).inc()
        self.llm_duration.labels(*labels, outcome).observe(max(0, duration_seconds))
        if usage is None:
            self.llm_usage_unavailable.labels(*labels).inc()
        else:
            for token_type, count in zip(("input", "cached_input", "output"), usage, strict=True):
                self.llm_tokens.labels(*labels, token_type).inc(count)

    @_best_effort
    def record_llm_retry(self, purpose: str, model: str, error_type: str) -> None:
        self.llm_retries.labels(
            *self._llm_labels(purpose, model),
            _bounded(error_type, ERROR_TYPES),
        ).inc()

    @_best_effort
    def start_exporter(self, host: str, port: int) -> bool:
        if self._server is None:
            self._server, self._server_thread = start_http_server(
                port=port,
                addr=host,
                registry=self.registry,
            )
        return True

    @_best_effort
    def stop_exporter(self) -> None:
        if self._server is None:
            return
        server, thread = self._server, self._server_thread
        self._server = self._server_thread = None
        try:
            server.shutdown()
        finally:
            server.server_close()
            thread.join(timeout=1)


def create_worker_metrics(
    settings: Settings,
    worker_kind: str,
    model_names: Iterable[str | None] = (),
) -> WorkerMetrics | None:
    if not settings.ai_worker_metrics_enabled:
        return None
    try:
        return WorkerMetrics(
            environment=settings.app_env,
            worker_kind=worker_kind,
            allowed_models={
                settings.llm_model,
                settings.effective_llm_extraction_model,
                settings.effective_llm_subject_resolution_model,
                settings.effective_llm_comparison_model,
                *(model for model in model_names if model),
            },
        )
    except Exception:  # noqa: BLE001 - registry initialization is optional monitoring.
        logger.warning("Worker metrics operation failed.")
        return None


def get_process_metrics() -> WorkerMetrics | None:
    return _process_metrics


def set_process_metrics(metrics: WorkerMetrics | None) -> WorkerMetrics | None:
    global _process_metrics
    previous, _process_metrics = _process_metrics, metrics
    return previous
