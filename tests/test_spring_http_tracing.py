import asyncio
from uuid import uuid4

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased
from opentelemetry.trace import SpanKind, StatusCode

from app.clients.exceptions import SpringWorkerHttpError, SpringWorkerTransportError
from app.clients.spring_worker_client import (
    INTERNAL_API_KEY_HEADER,
    WORKER_LEASE_TOKEN_HEADER,
    SpringWorkerClient,
)
from app.monitoring import worker_tracing as tracing


@pytest.fixture
def tracing_runtime():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = tracing.WorkerTracingRuntime(provider)
    previous = tracing.set_process_tracing(runtime)
    try:
        yield runtime, exporter
    finally:
        tracing.set_process_tracing(previous)
        runtime.shutdown()


def _client(handler):
    return SpringWorkerClient(
        "http://spring.invalid", "PRIVATE_API_KEY",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _http_spans(exporter):
    return [span for span in exporter.get_finished_spans() if span.name == "spring.http.request"]


def _traceparent(request):
    version, trace_id, span_id, flags = request.headers["traceparent"].split("-")
    assert version == "00"
    assert len(trace_id) == 32 and len(span_id) == 16
    return int(trace_id, 16), int(span_id, 16), flags


def _assert_private_values_absent(exporter, *values):
    for span in exporter.get_finished_spans():
        rendered = span.to_json()
        assert not span.events
        assert span.status.description is None
        for value in values:
            assert value not in rendered


def test_concurrent_requests_use_client_parent_and_preserve_private_headers(tracing_runtime):
    _, exporter = tracing_runtime
    requests = []
    job_ids = [uuid4(), uuid4()]
    leases = [uuid4(), uuid4()]
    original_headers = []

    async def scenario():
        both_started = asyncio.Event()

        async def respond(request):
            requests.append(request)
            if len(requests) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
            return httpx.Response(202)

        client = _client(respond)
        client.http_client.headers["X-Client-Default"] = "PRIVATE_DEFAULT"

        async def job(index):
            headers = client._headers(leases[index])
            original_headers.append(headers)
            with tracing.worker_span("analysis.job.execute", root=True):
                return await client._request(
                    "PATCH", client._url(f"/api/internal/v1/analysis-jobs/{job_ids[index]}/progress"),
                    headers=headers, params={"private": "PRIVATE_QUERY"},
                    json={"currentStep": "PRIVATE_BODY"},
                )

        try:
            responses = await asyncio.gather(job(0), job(1))
            assert [response.status_code for response in responses] == [202, 202]
            assert "traceparent" not in client.http_client.headers
        finally:
            await client.aclose()

    asyncio.run(scenario())
    clients = _http_spans(exporter)
    roots = [s for s in exporter.get_finished_spans() if s.name == "analysis.job.execute"]
    assert len(clients) == len(roots) == len(requests) == 2
    assert len({span.context.trace_id for span in clients}) == 2
    assert all("traceparent" not in headers for headers in original_headers)
    for index, request in enumerate(requests):
        trace_id, client_id, flags = _traceparent(request)
        span = next(span for span in clients if span.context.span_id == client_id)
        root = next(span for span in roots if span.context.trace_id == trace_id)
        assert span.kind == SpanKind.CLIENT
        assert span.parent.span_id == root.context.span_id
        assert int(flags, 16) & 1 == 1
        assert span.attributes["spring.operation"] == "job.progress"
        assert span.attributes["http.request.method"] == "PATCH"
        assert span.attributes["http.response.status_code"] == 202
        assert request.headers[INTERNAL_API_KEY_HEADER] == "PRIVATE_API_KEY"
        assert request.headers[WORKER_LEASE_TOKEN_HEADER] == str(leases[index])
        assert request.headers["X-Client-Default"] == "PRIVATE_DEFAULT"
    _assert_private_values_absent(
        exporter, "PRIVATE", "spring.invalid", *map(str, job_ids), *map(str, leases),
    )


@pytest.mark.parametrize("inside_root", [False, True])
def test_initial_claim_never_creates_client_span_or_propagates(tracing_runtime, inside_root):
    _, exporter = tracing_runtime
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    async def scenario():
        client = _client(respond)
        try:
            if inside_root:
                with tracing.worker_span("analysis.job.execute", root=True):
                    assert await client.claim(["SETTING_EXTRACTION"]) is None
            else:
                assert await client.claim(["SETTING_EXTRACTION"]) is None
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(requests) == 1
    assert "traceparent" not in requests[0].headers
    assert not _http_spans(exporter)


@pytest.mark.parametrize("scope", ["outside", "unrelated_span", "disabled", "after_root"])
def test_no_http_root_or_header_without_active_worker_execution(tracing_runtime, scope):
    _, exporter = tracing_runtime
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    async def request(client):
        await client.report_progress(uuid4(), uuid4(), "PRIVATE")

    async def scenario():
        client = _client(respond)
        try:
            if scope == "unrelated_span":
                with tracing.worker_span("unrelated.operation"):
                    await request(client)
            elif scope == "disabled":
                previous = tracing.set_process_tracing(None)
                try:
                    with tracing.worker_span("analysis.job.execute", root=True):
                        await request(client)
                finally:
                    tracing.set_process_tracing(previous)
            else:
                if scope == "after_root":
                    with tracing.worker_span("analysis.job.execute", root=True):
                        pass
                await request(client)
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(requests) == 1
    assert "traceparent" not in requests[0].headers
    assert not _http_spans(exporter)


def test_valid_unsampled_worker_context_propagates_without_exporting(tracing_runtime):
    _, exporter = tracing_runtime
    provider = TracerProvider(
        resource=Resource({}), sampler=ParentBased(ALWAYS_OFF), shutdown_on_exit=False,
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = tracing.WorkerTracingRuntime(provider)
    previous = tracing.set_process_tracing(runtime)
    requests = []
    root_contexts = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    async def scenario():
        client = _client(respond)
        try:
            with tracing.worker_span("analysis.job.execute", root=True):
                assert not trace.get_current_span().is_recording()
                root_contexts.append(trace.get_current_span().get_span_context())
                await client.report_progress(uuid4(), uuid4(), "PRIVATE")
        finally:
            await client.aclose()

    try:
        asyncio.run(scenario())
    finally:
        tracing.set_process_tracing(previous)
        runtime.shutdown()
    trace_id, client_id, flags = _traceparent(requests[0])
    assert trace_id == root_contexts[0].trace_id
    assert client_id != root_contexts[0].span_id
    assert int(flags, 16) & 1 == 0
    assert not exporter.get_finished_spans()


def test_inherited_cleanup_task_keeps_trace_after_owner_root_ends(tracing_runtime):
    _, exporter = tracing_runtime
    requests = []
    job_id, lease = uuid4(), uuid4()

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    async def scenario():
        continue_cleanup = asyncio.Event()
        client = _client(respond)

        async def cleanup():
            await continue_cleanup.wait()
            with tracing.worker_span("llm.tokens.finalize"):
                await client._request(
                    "POST", client._url(f"/api/internal/v1/ai-token-usages/{job_id}/settle"),
                    headers=client._headers(lease), json={},
                )

        try:
            with tracing.worker_span("analysis.job.execute", root=True):
                task = asyncio.create_task(cleanup())
            continue_cleanup.set()
            await asyncio.shield(task)
            await client.report_progress(job_id, lease, "outside owner")
        finally:
            await client.aclose()

    asyncio.run(scenario())
    root = next(s for s in exporter.get_finished_spans() if s.name == "analysis.job.execute")
    ledger = next(s for s in exporter.get_finished_spans() if s.name == "llm.tokens.finalize")
    client_span = _http_spans(exporter)[0]
    assert client_span.context.trace_id == ledger.context.trace_id == root.context.trace_id
    assert client_span.parent.span_id == ledger.context.span_id
    assert client_span.end_time > root.end_time
    assert client_span.attributes["spring.operation"] == "tokens.settle"
    assert "traceparent" in requests[0].headers
    assert "traceparent" not in requests[1].headers


def test_subclaim_within_execution_is_traced(tracing_runtime):
    _, exporter = tracing_runtime
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    async def scenario():
        client = _client(respond)
        try:
            with tracing.worker_span("analysis.job.execute", root=True):
                assert await client.claim_next_character_fact_comparison(uuid4(), uuid4()) is None
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(_http_spans(exporter)) == 1
    assert _http_spans(exporter)[0].attributes["spring.operation"] == "character.claim"
    assert "traceparent" in requests[0].headers


@pytest.mark.parametrize("status", [400, 403, 500, 503])
def test_http_error_keeps_existing_translation_and_safe_status(tracing_runtime, status):
    _, exporter = tracing_runtime
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"private": "PRIVATE_RESPONSE"})

    async def scenario():
        client = _client(respond)
        try:
            with tracing.worker_span("analysis.job.execute", root=True):
                with pytest.raises(SpringWorkerHttpError) as caught:
                    await client.report_progress(uuid4(), uuid4(), "PRIVATE_BODY")
                assert caught.value.response.status_code == status
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(requests) == 1
    span = _http_spans(exporter)[0]
    assert span.attributes["http.response.status_code"] == status
    assert span.attributes["error.type"] == ("http_5xx" if status >= 500 else "http_4xx")
    assert span.status.status_code == StatusCode.ERROR
    _assert_private_values_absent(exporter, "PRIVATE")


@pytest.mark.parametrize("error_class,error_type", [
    (httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network"),
    (httpx.RemoteProtocolError, "protocol"),
])
def test_transport_error_keeps_original_cause_without_exporting_it(
    tracing_runtime, error_class, error_type,
):
    _, exporter = tracing_runtime
    original = error_class("PRIVATE transport detail")
    requests = []

    def respond(request):
        requests.append(request)
        raise original

    async def scenario():
        client = _client(respond)
        try:
            with tracing.worker_span("analysis.job.execute", root=True):
                with pytest.raises(SpringWorkerTransportError) as caught:
                    await client.report_progress(uuid4(), uuid4(), "PRIVATE")
                assert caught.value.__cause__ is original
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(requests) == 1
    span = _http_spans(exporter)[0]
    assert span.attributes["error.type"] == error_type
    assert span.status.status_code == StatusCode.ERROR
    assert "http.response.status_code" not in span.attributes
    _assert_private_values_absent(exporter, "PRIVATE")


def test_cancellation_remains_cancellation_and_http_span_ends(tracing_runtime):
    _, exporter = tracing_runtime
    original = asyncio.CancelledError("PRIVATE cancel reason")
    calls = []

    async def respond(request):
        calls.append(request)
        raise original

    async def scenario():
        client = _client(respond)
        try:
            with tracing.worker_span("analysis.job.execute", root=True):
                with pytest.raises(asyncio.CancelledError) as caught:
                    await client.report_progress(uuid4(), uuid4(), "PRIVATE")
                assert caught.value is original
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(calls) == 1
    span = _http_spans(exporter)[0]
    assert span.attributes["outcome"] == "canceled"
    assert span.status.status_code == StatusCode.UNSET
    _assert_private_values_absent(exporter, "PRIVATE")


@pytest.mark.parametrize("failure", ["start", "scope", "inject", "attribute", "end"])
@pytest.mark.parametrize("business_result", ["success", "transport_error", "canceled"])
def test_telemetry_failures_do_not_repeat_request_or_replace_business_error(
    tracing_runtime, monkeypatch, failure, business_result, caplog,
):
    runtime, exporter = tracing_runtime
    calls = []
    original = (
        asyncio.CancelledError("PRIVATE business cancellation")
        if business_result == "canceled" else httpx.ConnectError("PRIVATE business cause")
    )

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE telemetry failure")

    def respond(request):
        calls.append(request)
        if business_result != "success":
            raise original
        return httpx.Response(200)

    async def scenario():
        client = _client(respond)
        try:
            with (
                tracing.worker_span("analysis.job.execute", root=True),
                monkeypatch.context() as patch,
            ):
                if failure == "start":
                    patch.setattr(runtime.tracer, "start_span", fail)
                elif failure == "scope":
                    patch.setattr(tracing.trace, "get_current_span", fail)
                elif failure == "inject":
                    patch.setattr(tracing._trace_context_propagator, "inject", fail)
                else:
                    start = runtime.tracer.start_span

                    def broken_span(*args, **kwargs):
                        span = start(*args, **kwargs)
                        patch.setattr(
                            span, "set_attribute" if failure == "attribute" else "end", fail,
                        )
                        return span

                    patch.setattr(runtime.tracer, "start_span", broken_span)
                if business_result == "transport_error":
                    with pytest.raises(SpringWorkerTransportError) as caught:
                        await client.report_progress(uuid4(), uuid4(), "PRIVATE")
                    assert caught.value.__cause__ is original
                elif business_result == "canceled":
                    with pytest.raises(asyncio.CancelledError) as caught:
                        await client.report_progress(uuid4(), uuid4(), "PRIVATE")
                    assert caught.value is original
                else:
                    await client.report_progress(uuid4(), uuid4(), "PRIVATE")
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0].headers[INTERNAL_API_KEY_HEADER] == "PRIVATE_API_KEY"
    assert "PRIVATE" not in caplog.text
    _assert_private_values_absent(exporter, "PRIVATE")


def test_failed_header_copy_or_partial_injection_preserves_original_headers(
    tracing_runtime, monkeypatch,
):
    headers = {"X-Internal-Api-Key": "PRIVATE_AUTH", "X-Worker-Lease-Token": "PRIVATE_LEASE"}

    def partial_inject(carrier):
        carrier["traceparent"] = "bad partial telemetry"
        raise RuntimeError("PRIVATE injection detail")

    with tracing.worker_span("analysis.job.execute", root=True):
        with monkeypatch.context() as patch:
            patch.setattr(tracing._trace_context_propagator, "inject", partial_inject)
            assert tracing.safe_inject_traceparent(headers) is headers
        with monkeypatch.context() as patch:
            patch.setattr(tracing.httpx, "Headers", lambda *_: (_ for _ in ()).throw(ValueError()))
            assert tracing.safe_inject_traceparent(headers) is headers
    assert headers == {
        "X-Internal-Api-Key": "PRIVATE_AUTH", "X-Worker-Lease-Token": "PRIVATE_LEASE",
    }


def test_usage_retry_retains_one_client_span_per_actual_http_attempt(tracing_runtime):
    _, exporter = tracing_runtime
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200)

    async def scenario():
        client = _client(respond)
        try:
            with (
                tracing.worker_span("analysis.job.execute", root=True),
                tracing.worker_span("llm.tokens.finalize"),
            ):
                await client.settle_ai_tokens(uuid4(), 1, 0, 1, "SUCCESS")
        finally:
            await client.aclose()

    asyncio.run(scenario())
    clients = _http_spans(exporter)
    assert len(requests) == len(clients) == 2
    assert len({span.context.trace_id for span in clients}) == 1
    assert len({span.context.span_id for span in clients}) == 2
    assert len({span.parent.span_id for span in clients}) == 1
    assert [span.attributes["http.response.status_code"] for span in clients] == [503, 200]
    assert requests[0].content == requests[1].content


def test_unknown_route_and_extra_attributes_cannot_export_private_values(tracing_runtime):
    _, exporter = tracing_runtime
    with (
        tracing.worker_span("analysis.job.execute", root=True),
        tracing.spring_http_span(
            "PRIVATE_METHOD", "http://private.invalid/PRIVATE_ID?q=PRIVATE",
        ) as span,
    ):
        span.set_attributes({
            "url.full": "PRIVATE_URL", "spring.operation": "PRIVATE_OPERATION",
            "http.request.header.authorization": "PRIVATE_AUTH", "http.response.status_code": 999,
        })
    saved = _http_spans(exporter)[0]
    assert saved.attributes["spring.operation"] == "other"
    assert saved.attributes["http.request.method"] == "other"
    assert "http.response.status_code" not in saved.attributes
    _assert_private_values_absent(exporter, "PRIVATE", "private.invalid")
