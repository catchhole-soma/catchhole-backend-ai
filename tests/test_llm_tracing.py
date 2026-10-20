import asyncio
import json
from uuid import UUID

import httpx
import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry

from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.responses import LlmTextResponse
from app.monitoring.worker_metrics import WorkerMetrics
from app.monitoring.worker_tracing import (
    WorkerTracingRuntime,
    get_process_tracing,
    set_process_tracing,
    worker_span,
)
from app.usage import metering
from app.usage.metering import MeteredEmbeddingClient, MeteredTextGenerationClient

MODEL = "gpt-5.6-sol"
PRIVATE = "PRIVATE-manuscript-response-credential-canary"
RESPONSE = LlmTextResponse("{}", input_token_count=10, output_token_count=5)


@pytest.fixture
def traces(monkeypatch):
    # No network exporter, global OTel provider, tokenizer download, or real ledger.
    monkeypatch.setattr(metering, "_estimate_text_token_upper_bound", lambda *args: 100)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = WorkerTracingRuntime(provider, allowed_models={MODEL})
    previous = set_process_tracing(runtime)
    try:
        yield exporter
    finally:
        set_process_tracing(previous)
        runtime.shutdown()


@pytest.fixture
def metrics():
    return WorkerMetrics(
        environment="local",
        worker_kind="analysis",
        allowed_models={MODEL},
        registry=CollectorRegistry(),
    )


class Ledger:
    def __init__(self, clock=None):
        self.reservations = []
        self.settlements = []
        self.releases = []
        self.clock = clock

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)
        if self.clock is not None:
            self.clock[0] += 13_000_000_000

    async def settle_ai_tokens(self, request_id, *args, **kwargs):
        self.settlements.append(request_id)
        if self.clock is not None:
            self.clock[0] += 17_000_000_000

    async def release_ai_tokens(self, request_id, outcome):
        self.releases.append((request_id, outcome))


class Provider:
    def __init__(self, results, clock=None):
        self.results = iter(results)
        self.clock = clock
        self.call_count = 0

    async def create_text_response(self, **kwargs):
        self.call_count += 1
        if self.clock is not None:
            self.clock[0] += 2_000_000_000
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


def make_client(provider, ledger, **kwargs):
    return MeteredTextGenerationClient(
        delegate=provider,
        ledger=ledger,
        analysis_job_id=UUID(int=1),
        lease_token=UUID(int=2),
        purpose="SETTING_EXTRACTION",
        default_model=MODEL,
        **kwargs,
    )


def status_error(code):
    request = httpx.Request("POST", f"https://provider.invalid/?secret={PRIVATE}")
    return httpx.HTTPStatusError(
        PRIVATE, request=request, response=httpx.Response(code, request=request),
    )


def named(exporter, name):
    return [span for span in exporter.get_finished_spans() if span.name == name]


def assert_private_data_absent(exporter):
    for span in exporter.get_finished_spans():
        assert PRIVATE not in json.dumps({
            "name": span.name,
            "attributes": dict(span.attributes),
            "description": span.status.description,
            "events": [dict(event.attributes) for event in span.events],
        })
        assert not span.events  # No implicit exception.message/stacktrace events.


def metric(metrics, name, **labels):
    return metrics.registry.get_sample_value(name, {
        "application": "catchhole-ai", "environment": "local",
        "worker_kind": "analysis", "purpose": "SETTING_EXTRACTION",
        "model": MODEL, **labels,
    })


async def drain_cleanup():
    pending = tuple(metering._detached_ledger_tasks)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=1)


def test_retry_spans_preserve_hierarchy_call_metrics_and_semaphore_boundaries(traces, metrics):
    clock = [0]
    ledger = Ledger(clock)
    provider = Provider([status_error(429), RESPONSE], clock)
    semaphore = asyncio.Semaphore(1)
    sleeps = []

    async def sleep(delay):
        assert not semaphore.locked()
        sleeps.append(delay)
        clock[0] += 19_000_000_000

    client = make_client(
        provider, ledger, metrics=metrics, max_retries=1,
        request_semaphore=semaphore, sleeper=sleep,
        random_source=lambda: 0.0, monotonic_ns=lambda: clock[0],
    )

    async def run():
        with worker_span("worker.attempt", root=True):
            return await client.create_text_response(PRIVATE, PRIVATE)

    assert asyncio.run(run()) is RESPONSE
    assert sleeps == [2.0]
    assert len(ledger.reservations) == 2
    assert len(ledger.releases) == len(ledger.settlements) == 1
    assert provider.call_count == 2
    assert not semaphore.locked()
    assert metric(metrics, "catchhole_llm_retries_total", error_type="429") == 1
    for outcome in ("failure", "success"):
        assert metric(metrics, "catchhole_llm_call_duration_seconds_sum", outcome=outcome) == 2

    root, = named(traces, "worker.attempt")
    request, = named(traces, "llm.request")
    assert request.parent.span_id == root.context.span_id
    assert request.attributes["outcome"] == "success"
    calls = sorted(named(traces, "llm.call"), key=lambda s: s.attributes["llm.retry_index"])
    assert len(calls) == 2
    assert calls[0].status.status_code is StatusCode.ERROR
    assert calls[0].attributes["error.type"] == "429"
    assert calls[1].attributes["outcome"] == "success"
    assert calls[1].status.status_code is StatusCode.UNSET
    assert len(named(traces, "llm.semaphore.wait")) == 2
    assert len(named(traces, "llm.retry.backoff")) == 1
    for span in traces.get_finished_spans():
        assert span.context.trace_id == root.context.trace_id
        if span.name not in {"worker.attempt", "llm.request"}:
            assert span.parent.span_id == request.context.span_id
    for wait, reserve in zip(
        sorted(named(traces, "llm.semaphore.wait"), key=lambda s: s.start_time),
        sorted(named(traces, "llm.tokens.reserve"), key=lambda s: s.start_time),
    ):
        assert wait.end_time <= reserve.start_time
    assert_private_data_absent(traces)


def test_rejected_retry_reservation_does_not_create_provider_span_or_retry_metric(traces, metrics):
    class RejectSecondReserve(Ledger):
        async def reserve_ai_tokens(self, **kwargs):
            if self.reservations:
                raise RuntimeError(PRIVATE)
            await super().reserve_ai_tokens(**kwargs)

    async def sleep(delay):
        pass

    client = make_client(
        Provider([status_error(429)]), RejectSecondReserve(), metrics=metrics,
        max_retries=1, sleeper=sleep,
    )
    with pytest.raises(RuntimeError, match=PRIVATE):
        asyncio.run(client.create_text_response(PRIVATE, PRIVATE))
    assert len(named(traces, "llm.call")) == 1
    reserves = named(traces, "llm.tokens.reserve")
    assert len(reserves) == 2
    assert reserves[-1].attributes["error.type"] == "ledger_error"
    assert metric(metrics, "catchhole_llm_retries_total", error_type="429") is None
    assert_private_data_absent(traces)


@pytest.mark.parametrize("phase", ["reserve", "finalize"])
def test_owner_cancellation_preserves_actual_ledger_lifetime_and_parent(traces, phase):
    async def run():
        started, finish, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class BlockingLedger(Ledger):
            async def reserve_ai_tokens(self, **kwargs):
                if phase == "reserve":
                    started.set()
                    await finish.wait()
                await super().reserve_ai_tokens(**kwargs)

            async def settle_ai_tokens(self, request_id, *args, **kwargs):
                if phase == "finalize":
                    started.set()
                    await finish.wait()
                await super().settle_ai_tokens(request_id, *args, **kwargs)

            async def release_ai_tokens(self, request_id, outcome):
                await super().release_ai_tokens(request_id, outcome)
                released.set()

        ledger, provider = BlockingLedger(), Provider([RESPONSE])
        semaphore = asyncio.Semaphore(1)
        client = make_client(provider, ledger, request_semaphore=semaphore)

        async def job():
            with worker_span("worker.attempt", root=True):
                await client.create_text_response(PRIVATE, PRIVATE)

        owner = asyncio.create_task(job())
        await asyncio.wait_for(started.wait(), timeout=1)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=0.1)
        assert not semaphore.locked()
        span_name = f"llm.tokens.{phase}"
        assert not named(traces, span_name)
        root, = named(traces, "worker.attempt")
        request, = named(traces, "llm.request")
        assert request.attributes["outcome"] == "canceled"

        finish.set()
        if phase == "reserve":
            await asyncio.wait_for(released.wait(), timeout=1)
        await drain_cleanup()
        completed, = named(traces, span_name)
        assert completed.end_time > root.end_time
        assert completed.context.trace_id == root.context.trace_id
        assert completed.parent.span_id == request.context.span_id
        assert completed.attributes["outcome"] == "success"
        if phase == "reserve":
            assert provider.call_count == 0
            release, = named(traces, "llm.tokens.release")
            assert release.parent.span_id == request.context.span_id
            assert len(ledger.releases) == 1
        else:
            assert provider.call_count == 1
            assert len(ledger.settlements) == 1
            assert not ledger.releases

    asyncio.run(run())
    assert_private_data_absent(traces)


def test_provider_and_ledger_failure_keep_original_exception_without_secret_events(traces):
    provider_error = status_error(503)

    class BrokenRelease(Ledger):
        async def release_ai_tokens(self, request_id, outcome):
            raise RuntimeError(PRIVATE)

    client = make_client(Provider([provider_error]), BrokenRelease(), max_retries=0)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(client.create_text_response(PRIVATE, PRIVATE))
    assert raised.value is provider_error
    call, = named(traces, "llm.call")
    release, = named(traces, "llm.tokens.release")
    assert call.attributes["error.type"] == "5xx"
    assert release.attributes["error.type"] == "ledger_error"
    assert call.status.status_code is release.status.status_code is StatusCode.ERROR
    assert_private_data_absent(traces)


def test_successful_provider_and_failed_settlement_are_distinct(traces, metrics):
    settlement_error = RuntimeError(PRIVATE)

    class BrokenSettlement(Ledger):
        async def settle_ai_tokens(self, request_id, *args, **kwargs):
            raise settlement_error

    provider = Provider([RESPONSE])
    client = make_client(provider, BrokenSettlement(), metrics=metrics)
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(client.create_text_response(PRIVATE, PRIVATE))
    assert raised.value is settlement_error
    assert provider.call_count == 1
    call, = named(traces, "llm.call")
    finalize, = named(traces, "llm.tokens.finalize")
    request, = named(traces, "llm.request")
    assert call.attributes["outcome"] == "success"
    assert call.status.status_code is StatusCode.UNSET
    assert finalize.attributes["error.type"] == "ledger_error"
    assert finalize.attributes["ledger.outcome"] == "SUCCESS"
    assert request.attributes["outcome"] == "failure"
    assert metric(metrics, "catchhole_llm_calls_total", outcome="success", error_type="none") == 1
    assert not named(traces, "llm.retry.backoff")
    assert_private_data_absent(traces)


def test_canceling_backoff_does_not_start_retry_or_hold_semaphore(traces, metrics):
    async def run():
        started = asyncio.Event()
        semaphore = asyncio.Semaphore(1)
        ledger, provider = Ledger(), Provider([status_error(429), RESPONSE])

        async def sleep(delay):
            assert not semaphore.locked()
            started.set()
            await asyncio.Event().wait()

        client = make_client(
            provider, ledger, metrics=metrics, request_semaphore=semaphore,
            sleeper=sleep, max_retries=1,
        )
        owner = asyncio.create_task(client.create_text_response(PRIVATE, PRIVATE))
        await asyncio.wait_for(started.wait(), timeout=1)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert not semaphore.locked()
        assert provider.call_count == len(ledger.reservations) == len(ledger.releases) == 1

    asyncio.run(run())
    backoff, = named(traces, "llm.retry.backoff")
    assert backoff.attributes["outcome"] == "canceled"
    assert metric(metrics, "catchhole_llm_retries_total", error_type="429") is None


def test_non_exception_base_exception_propagates_and_releases_slot(traces):
    class Abort(BaseException):
        pass

    error = Abort(PRIVATE)
    ledger, provider = Ledger(), Provider([error])
    semaphore = asyncio.Semaphore(1)
    client = make_client(provider, ledger, request_semaphore=semaphore)
    with pytest.raises(Abort) as raised:
        asyncio.run(client.create_text_response(PRIVATE, PRIVATE))
    assert raised.value is error
    assert not semaphore.locked()
    assert provider.call_count == 1
    # Preserve the existing policy: only CancelledError and ordinary provider
    # Exceptions enter ledger cleanup; tracing must not invent another retry.
    assert ledger.releases == ledger.settlements == []
    call, = named(traces, "llm.call")
    assert call.attributes["outcome"] == "failure"
    assert_private_data_absent(traces)


def test_waiting_and_running_cancellation_do_not_leak_slots_or_cross_job_context(traces):
    async def run():
        started = asyncio.Event()
        semaphore = asyncio.Semaphore(1)
        ledger = Ledger()

        class BlockingProvider:
            async def create_text_response(self, **kwargs):
                started.set()
                await asyncio.Event().wait()

        async def job(provider):
            with worker_span("worker.attempt", root=True):
                await make_client(
                    provider, ledger, request_semaphore=semaphore,
                ).create_text_response(PRIVATE, PRIVATE)

        running = asyncio.create_task(job(BlockingProvider()))
        await asyncio.wait_for(started.wait(), timeout=1)
        waiting_provider = Provider([RESPONSE])
        waiting = asyncio.create_task(job(waiting_provider))
        await asyncio.sleep(0)  # Let the second Task enter semaphore.acquire().
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert semaphore.locked()  # The running Job still owns its permit.
        assert waiting_provider.call_count == 0
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await drain_cleanup()
        assert not semaphore.locked()
        await asyncio.wait_for(job(Provider([RESPONSE])), timeout=1)
        assert not semaphore.locked()
        assert len(ledger.reservations) == 2
        assert len(ledger.releases) == len(ledger.settlements) == 1

    asyncio.run(run())
    roots = named(traces, "worker.attempt")
    assert len({s.context.trace_id for s in roots}) == 3
    requests = {s.context.trace_id: s for s in named(traces, "llm.request")}
    assert set(requests) == {s.context.trace_id for s in roots}
    for span in traces.get_finished_spans():
        if span.name not in {"worker.attempt", "llm.request"}:
            assert span.parent.span_id == requests[span.context.trace_id].context.span_id
    waits = named(traces, "llm.semaphore.wait")
    assert sum(s.attributes.get("outcome") == "canceled" for s in waits) == 1
    assert_private_data_absent(traces)


def test_tracing_without_metrics_preserves_configured_model_and_normalizes_override(traces):
    client = make_client(Provider([RESPONSE, RESPONSE]), Ledger(), metrics=None)
    asyncio.run(client.create_text_response(PRIVATE, PRIVATE))
    asyncio.run(client.create_text_response(PRIVATE, PRIVATE, model=PRIVATE))
    requests = named(traces, "llm.request")
    assert [s.attributes["llm.model"] for s in requests] == [MODEL, "other"]
    assert_private_data_absent(traces)


def test_configured_model_override_is_preserved_when_metrics_are_disabled(traces):
    configured_override = "configured-comparison-model"
    get_process_tracing().allowed_models = frozenset({MODEL, configured_override})
    client = make_client(Provider([RESPONSE]), Ledger(), metrics=None)

    asyncio.run(client.create_text_response(PRIVATE, PRIVATE, model=configured_override))

    assert named(traces, "llm.call")[0].attributes["llm.model"] == configured_override
    assert named(traces, "llm.request")[0].attributes["llm.model"] == configured_override


def test_embedding_shared_helpers_do_not_emit_text_llm_spans(traces):
    class EmbeddingProvider:
        version = "test"

        async def create_embeddings(self, inputs):
            return EmbeddingBatchResponse([[0.1]], "embedding-model", input_token_count=1)

    ledger = Ledger()
    client = MeteredEmbeddingClient(
        delegate=EmbeddingProvider(), ledger=ledger, analysis_job_id=UUID(int=1),
        lease_token=UUID(int=2), model_name="embedding-model",
        request_semaphore=asyncio.Semaphore(1),
    )

    async def run():
        with worker_span("worker.attempt", root=True):
            await client.create_embeddings([PRIVATE])

    asyncio.run(run())
    assert len(ledger.reservations) == len(ledger.settlements) == 1
    assert [s.name for s in traces.get_finished_spans()] == ["worker.attempt"]


def test_span_start_failure_preserves_provider_ledger_and_slot_cleanup(traces, monkeypatch):
    def fail_start(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(get_process_tracing().tracer, "start_span", fail_start)
    semaphore = asyncio.Semaphore(1)
    ledger, provider = Ledger(), Provider([RESPONSE])
    client = make_client(provider, ledger, request_semaphore=semaphore)
    assert asyncio.run(client.create_text_response(PRIVATE, PRIVATE)) is RESPONSE
    assert not semaphore.locked()
    assert provider.call_count == len(ledger.reservations) == len(ledger.settlements) == 1
    assert not traces.get_finished_spans()
