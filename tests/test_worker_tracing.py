import argparse
import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.core.config import Settings
from app.monitoring import worker_tracing as tracing
from app.monitoring.worker_metrics import WorkerMetrics, get_process_metrics
from app.worker.blocking_io import BlockingIoExecutor
from scripts import run_analysis_worker as runner


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = tracing.WorkerTracingRuntime(provider, allowed_models={"allowed-model"})
    previous = tracing.set_process_tracing(runtime)
    try:
        yield exporter
    finally:
        tracing.set_process_tracing(previous)
        runtime.shutdown()


def _payload():
    return SimpleNamespace(
        analysis_job_id=uuid4(), job_type="SETTING_EXTRACTION", claim_attempt_count=2,
        source_kind="EPISODE", episode=SimpleNamespace(char_count=1200),
        analysis_mode="ORDERED_PROVISIONAL", review_mode="AUTOMATIC",
        checkpoint_stage="CHUNKS_READY", work_title="private title", lease_token=uuid4(),
    )


def test_disabled_tracing_creates_no_provider_exporter_or_resources(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled tracing created a resource")

    monkeypatch.setattr(tracing, "TracerProvider", forbidden)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", forbidden)
    monkeypatch.setattr(tracing, "Resource", forbidden)
    settings = Settings(_env_file=None, ai_worker_tracing_enabled=False)
    assert tracing.create_worker_tracing(settings, "analysis") is None
    previous = tracing.set_process_tracing(None)
    try:
        with tracing.worker_span("analysis.job.execute", root=True) as span:
            span.set_outcome("success")
    finally:
        tracing.set_process_tracing(previous)


def test_runtime_owns_provider_without_changing_global_provider(monkeypatch):
    exporter = InMemorySpanExporter()
    exporter_options = {}

    def create_exporter(**kwargs):
        exporter_options.update(kwargs)
        return exporter

    monkeypatch.setattr(tracing, "OTLPSpanExporter", create_exporter)
    global_provider = trace.get_tracer_provider()
    settings = Settings(
        _env_file=None, ai_worker_tracing_enabled=True, app_env="prod",
        otel_service_name="catchhole-ai-worker",
        otel_exporter_otlp_traces_endpoint="http://collector.invalid:4318/v1/traces",
    )
    runtime = tracing.create_worker_tracing(settings, "analysis", ("cli-model",))
    assert runtime is not None
    assert trace.get_tracer_provider() is global_provider
    assert exporter_options == {
        "endpoint": "http://collector.invalid:4318/v1/traces", "timeout": 5,
    }
    assert runtime.provider.resource.attributes["service.name"] == "catchhole-ai-worker"
    assert runtime.provider.resource.attributes["deployment.environment.name"] == "prod"
    assert runtime.provider.resource.attributes["worker.kind"] == "analysis"
    assert "cli-model" in runtime.allowed_models
    previous = tracing.set_process_tracing(runtime)
    try:
        with tracing.worker_span("analysis.job.execute", root=True):
            pass
        runtime.provider.force_flush()
        assert len(exporter.get_finished_spans()) == 1
    finally:
        tracing.set_process_tracing(previous)
        runtime.shutdown()
        runtime.shutdown()


def test_initialization_failure_keeps_tracing_optional_without_raw_error(monkeypatch, caplog):
    def fail(**kwargs):
        raise RuntimeError("SECRET collector settings")

    monkeypatch.setattr(tracing, "OTLPSpanExporter", fail)
    runtime = tracing.create_worker_tracing(
        Settings(_env_file=None, ai_worker_tracing_enabled=True), "analysis",
    )
    assert runtime is None
    assert "Worker tracing operation failed." in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("termination", [KeyboardInterrupt(), SystemExit(2)])
def test_initialization_preserves_process_exit_after_resource_cleanup(monkeypatch, termination):
    closed = []

    class Provider:
        def __init__(self, **kwargs):
            pass

        def shutdown(self):
            closed.append(True)

    def interrupt(**kwargs):
        raise termination

    monkeypatch.setattr(tracing, "TracerProvider", Provider)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", interrupt)
    with pytest.raises(type(termination)) as raised:
        tracing.create_worker_tracing(
            Settings(_env_file=None, ai_worker_tracing_enabled=True), "analysis",
        )
    assert raised.value is termination
    assert closed == [True]


@pytest.mark.parametrize("fail_at", ["processor", "registration"])
def test_partial_initialization_closes_unowned_exporter(monkeypatch, fail_at):
    calls = []

    class Exporter:
        def shutdown(self):
            calls.append("exporter.close")

    class Processor:
        def __init__(self, exporter, **kwargs):
            if fail_at == "processor":
                raise RuntimeError("processor creation failed")
            self.exporter = exporter

        def shutdown(self):
            calls.append("processor.close")
            self.exporter.shutdown()

    class Provider:
        def __init__(self, **kwargs):
            pass

        def add_span_processor(self, processor):
            raise RuntimeError("registration failed")

        def shutdown(self):
            calls.append("provider.close")

    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda **kwargs: Exporter())
    monkeypatch.setattr(tracing, "BatchSpanProcessor", Processor)
    monkeypatch.setattr(tracing, "TracerProvider", Provider)
    assert tracing.create_worker_tracing(
        Settings(_env_file=None, ai_worker_tracing_enabled=True), "analysis",
    ) is None
    assert calls.count("provider.close") == 1
    assert calls.count("exporter.close") == 1
    assert ("processor.close" in calls) == (fail_at == "registration")


def test_attributes_are_explicitly_allowed_and_never_capture_raw_payload(spans):
    payload = _payload()
    with tracing.worker_span(
        "analysis.job.execute", root=True, attributes=tracing.job_trace_attributes(payload),
    ) as span:
        span.set_attributes({
            "prompt": "SECRET prompt", "exception.message": "SECRET error",
            "lease_token": "SECRET lease", "analysis.job.id": str(payload.analysis_job_id),
            "llm.model": "unrecognized SECRET model", "llm.purpose": "SECRET purpose",
            "analysis.chunk.count": 3, "analysis.chunk.index": -1,
            "llm.retry_delay_seconds": float("nan"),
        })
        span.set_outcome("success")
    saved = spans.get_finished_spans()[0]
    assert saved.attributes["analysis.job.id"] == str(payload.analysis_job_id)
    assert saved.attributes["analysis.job.attempt"] == 2
    assert saved.attributes["analysis.source.char_count"] == 1200
    assert saved.attributes["llm.model"] == "other"
    assert saved.attributes["llm.purpose"] == "other"
    assert saved.attributes["analysis.chunk.count"] == 3
    assert "analysis.chunk.index" not in saved.attributes
    assert "llm.retry_delay_seconds" not in saved.attributes
    assert "SECRET" not in str(saved.attributes)
    assert "private title" not in str(saved.attributes)
    assert not saved.events


@pytest.mark.parametrize("error", [RuntimeError("SECRET failure"), asyncio.CancelledError("SECRET cancel")])
def test_business_exception_identity_and_cancellation_are_preserved(spans, error):
    with pytest.raises(type(error)) as caught, tracing.worker_span("analysis.job.execute", root=True):
        raise error
    assert caught.value is error
    saved = spans.get_finished_spans()[0]
    canceled = isinstance(error, asyncio.CancelledError)
    assert saved.attributes["outcome"] == ("canceled" if canceled else "failure")
    assert saved.status.status_code == (StatusCode.UNSET if canceled else StatusCode.ERROR)
    assert saved.status.description is None
    assert not saved.events


def test_explicit_error_is_not_overwritten_by_generic_exception(spans):
    with pytest.raises(RuntimeError), tracing.worker_span("llm.call") as span:
        span.set_error("429")
        raise RuntimeError("SECRET response")
    saved = spans.get_finished_spans()[0]
    assert saved.attributes["error.type"] == "429"
    assert saved.attributes["outcome"] == "failure"
    assert not saved.events


def test_trace_operations_cannot_replace_business_exception(monkeypatch, caplog):
    class BrokenSpan:
        def set_attribute(self, *args):
            raise RuntimeError("SECRET attribute")

        def set_status(self, *args):
            raise RuntimeError("SECRET status")

        def end(self):
            raise RuntimeError("SECRET end")

    span = BrokenSpan()
    runtime = SimpleNamespace(
        tracer=SimpleNamespace(start_span=lambda *args, **kwargs: span), allowed_models=frozenset(),
    )
    monkeypatch.setattr(context, "attach", lambda value: (_ for _ in ()).throw(RuntimeError("SECRET attach")))
    previous = tracing.set_process_tracing(runtime)
    original = asyncio.CancelledError("business cancellation")
    try:
        with pytest.raises(asyncio.CancelledError) as caught, tracing.worker_span("analysis.job.execute") as facade:
            facade.set_outcome("failure", error=True)
            raise original
        assert caught.value is original
        assert "SECRET" not in caplog.text
    finally:
        tracing.set_process_tracing(previous)


def test_start_span_failure_still_runs_business_operation(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("start failed")

    runtime = SimpleNamespace(tracer=SimpleNamespace(start_span=fail), allowed_models=frozenset())
    previous = tracing.set_process_tracing(runtime)
    try:
        result = []
        with tracing.worker_span("analysis.job.execute"):
            result.append("ran")
        assert result == ["ran"]
    finally:
        tracing.set_process_tracing(previous)


def test_end_failure_does_not_leave_context_attached(spans, monkeypatch):
    runtime = tracing.get_process_tracing()
    original_start = runtime.tracer.start_span
    original_context = context.get_current()

    def start_broken_span(*args, **kwargs):
        span = original_start(*args, **kwargs)

        def fail_end(*args, **kwargs):
            raise RuntimeError("end failed")

        monkeypatch.setattr(span, "end", fail_end)
        return span

    with monkeypatch.context() as patch:
        patch.setattr(runtime.tracer, "start_span", start_broken_span)
        with tracing.worker_span("analysis.job.execute", root=True):
            pass
    assert context.get_current() is original_context
    with tracing.worker_span("analysis.prepare"):
        pass
    assert spans.get_finished_spans()[0].parent is None


def test_root_per_execution_and_child_context_are_isolated_between_jobs(spans):
    async def scenario():
        class Worker:
            async def process_claimed(self, payload):
                with tracing.worker_span("analysis.prepare"):
                    await asyncio.sleep(0)
                return payload.analysis_job_id

        payloads = [_payload(), _payload()]
        with tracing.worker_span("outer.operation", root=True):
            result = await asyncio.gather(*(
                runner._run_claimed_with_metrics(Worker(), payload, None) for payload in payloads
            ))
        assert result == [p.analysis_job_id for p in payloads]

    asyncio.run(scenario())
    saved = spans.get_finished_spans()
    roots = [s for s in saved if s.name == "analysis.job.execute"]
    children = [s for s in saved if s.name == "analysis.prepare"]
    assert len(roots) == len(children) == 2
    assert len({s.context.trace_id for s in roots}) == 2
    assert all(root.parent is None for root in roots)
    assert all(root.attributes["outcome"] == "success" for root in roots)
    assert {child.parent.span_id for child in children} == {root.context.span_id for root in roots}
    outer = next(s for s in saved if s.name == "outer.operation")
    assert outer.context.trace_id not in {root.context.trace_id for root in roots}


@pytest.mark.parametrize("error,outcome", [(RuntimeError("failure"), "failure"), (asyncio.CancelledError(), "canceled")])
def test_root_preserves_metrics_finally_on_failed_execution(spans, error, outcome):
    metrics = WorkerMetrics("local", "analysis", ())

    class Worker:
        async def process_claimed(self, payload):
            raise error

    with pytest.raises(type(error)) as caught:
        asyncio.run(runner._run_claimed_with_metrics(Worker(), _payload(), metrics))
    assert caught.value is error
    labels = {"application": "catchhole-ai", "environment": "local", "worker_kind": "analysis"}
    assert metrics.registry.get_sample_value("catchhole_worker_jobs_active", labels) == 0
    assert metrics.registry.get_sample_value(
        "catchhole_worker_job_attempts_total", {**labels, "job_type": "SETTING_EXTRACTION", "outcome": outcome},
    ) == 1
    assert spans.get_finished_spans()[0].attributes["outcome"] == outcome


def test_blocking_executor_copies_each_jobs_trace_context(spans):
    async def scenario():
        executor = BlockingIoExecutor(2)
        barrier = threading.Barrier(2)

        def work():
            barrier.wait(timeout=3)
            with tracing.worker_span("analysis.prepare"):
                return trace.get_current_span().get_span_context().trace_id

        async def job():
            with tracing.worker_span("analysis.job.execute", root=True):
                parent_id = trace.get_current_span().get_span_context().trace_id
                assert await executor.run(work) == parent_id

        try:
            await asyncio.gather(job(), job())
        finally:
            await executor.aclose()

    asyncio.run(scenario())
    children = [s for s in spans.get_finished_spans() if s.name == "analysis.prepare"]
    assert len(children) == 2
    assert len({s.context.trace_id for s in children}) == 2


@pytest.mark.parametrize("termination", [KeyboardInterrupt(), SystemExit(2)])
def test_cli_restores_process_state_when_tracing_initialization_is_interrupted(monkeypatch, termination):
    previous_metrics = get_process_metrics()
    previous_tracing = tracing.get_process_tracing()
    stopped = []
    metrics = SimpleNamespace(stop_exporter=lambda: stopped.append(True))

    def interrupt(*args):
        raise termination

    monkeypatch.setattr(runner, "create_worker_metrics", lambda *args: metrics)
    monkeypatch.setattr(runner, "create_worker_tracing", interrupt)
    args = argparse.Namespace(
        worker_kind="analysis", once=True, model_name=None, extraction_model_name=None,
        subject_resolution_model_name=None, comparison_model_name=None,
    )
    with pytest.raises(type(termination)) as raised:
        asyncio.run(runner._async_main(args, Settings(_env_file=None)))
    assert raised.value is termination
    assert stopped == [True]
    assert get_process_metrics() is previous_metrics
    assert tracing.get_process_tracing() is previous_tracing


@pytest.mark.parametrize("close_fails", [False, True])
def test_cli_runtime_shutdown_is_off_loop_even_when_worker_close_fails(monkeypatch, close_fails):
    calls = []
    loop_thread = threading.get_ident()

    class Runtime:
        def shutdown(self):
            calls.append(("shutdown", threading.get_ident()))

    runtime = Runtime()

    class Worker:
        async def claim_next(self):
            return None

        async def aclose(self):
            calls.append(("close", threading.get_ident()))
            if close_fails:
                raise RuntimeError("close failure")

    previous = tracing.get_process_tracing()
    monkeypatch.setattr(runner, "create_worker_tracing", lambda *args: runtime)
    monkeypatch.setattr(runner, "create_worker_metrics", lambda *args: None)
    monkeypatch.setattr(runner, "AnalysisJobWorker", lambda **kwargs: Worker())
    args = argparse.Namespace(
        worker_kind="analysis", once=True, model_name=None, extraction_model_name=None,
        subject_resolution_model_name=None, comparison_model_name=None,
    )
    if close_fails:
        with pytest.raises(RuntimeError, match="close failure"):
            asyncio.run(runner._async_main(args, Settings(_env_file=None)))
    else:
        asyncio.run(runner._async_main(args, Settings(_env_file=None)))
    assert calls[0] == ("close", loop_thread)
    assert calls[1][0] == "shutdown" and calls[1][1] != loop_thread
    assert tracing.get_process_tracing() is previous


def test_flush_and_shutdown_failure_remain_best_effort(caplog):
    calls = []

    class Provider:
        def get_tracer(self, name):
            return object()

        def force_flush(self, **kwargs):
            calls.append("flush")
            raise RuntimeError("flush failure")

        def shutdown(self):
            calls.append("shutdown")
            raise RuntimeError("SECRET shutdown failure")

    runtime = tracing.WorkerTracingRuntime(Provider())
    runtime.shutdown()
    runtime.shutdown()
    assert calls == ["flush", "shutdown"]
    assert "SECRET" not in caplog.text
