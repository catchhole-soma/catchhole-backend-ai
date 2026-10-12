import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.chatbot.http_app import create_app
from app.chatbot.outbox import Outbox
from app.chatbot.runtime import ChatRuntime, ProviderResult, ProviderFailure, BusyError
from app.schemas.chat_execution import Dispatch, ExecutionContext, Finish, ChatResult, Usage


@pytest.fixture
def anyio_backend():
    return "asyncio"


def dispatch(seconds=60):
    return Dispatch(
        schema_version=1,
        attempt_id=uuid4(),
        capability="a" * 64,
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def context(d):
    return ExecutionContext(
        schema_version=1,
        question_id=uuid4(),
        attempt_id=d.attempt_id,
        work_id=uuid4(),
        member_id=1,
        question="비공개 질문",
        episode_from=2,
        episode_to=5,
        deadline=d.deadline,
        token_budget=32000,
        max_calls=8,
        max_tool_calls=20,
        operational=False,
    )


def usage():
    return Usage(
        kind="SETTLE", input_tokens=20, cached_input_tokens=10, output_tokens=5, outcome="SUCCESS"
    )


def answer():
    return Finish(
        result=ChatResult(
            outcome="INSUFFICIENT_EVIDENCE", text="근거 부족", sources=[], limitations=[]
        )
    )


class Spring:
    def __init__(self):
        self.owners = {}
        self.contexts = {}
        self.reports = []
        self.reservations = []
        self.fail_delivery = False
        self.deny_reserve = False
        self.deleted = False
        self.lose_start = False

    async def start(self, d, runner):
        if d.attempt_id in self.owners and self.owners[d.attempt_id] != runner:
            raise ChatTransportError(409)
        self.owners[d.attempt_id] = runner
        if self.lose_start:
            raise ChatTransportError()
        return self.contexts.setdefault(d.attempt_id, context(d))

    async def reserve(self, d, runner, call_id, purpose, model, amount):
        if self.deny_reserve:
            raise ChatTransportError(409, "QUOTA_EXHAUSTED")
        self.reservations.append((call_id, purpose, amount))

    async def request(self, d, runner, action, body, **kwargs):
        if self.deleted:
            raise ChatTransportError(403)
        if self.fail_delivery:
            raise ChatTransportError(503)
        self.reports.append((d.attempt_id, action, body))
        return {}


async def finished(runtime):
    await asyncio.gather(*list(runtime.tasks.values()))
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_paid_call_and_answer_recover_without_model_retry(tmp_path):
    spring = Spring()
    spring.fail_delivery = True
    path = str(tmp_path / "outbox.db")
    outbox = Outbox(path)
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        return ProviderResult("value", usage())

    async def handler(execution):
        assert await execution.call("CHAT_ANSWER", "fake", 100, provider) == "value"
        return answer()

    runtime = ChatRuntime(spring, outbox, handler=handler)
    d = dispatch()
    runtime.submit(d)
    runtime.submit(d)
    await finished(runtime)
    await runtime.deliver()
    assert calls == 1 and outbox.count() == 2
    outbox.close()
    # Process restart only replays reports. A second start cannot own this attempt.
    recovered = Outbox(path)
    runtime2 = ChatRuntime(spring, recovered, handler=handler)
    runtime2.submit(d)
    await finished(runtime2)
    spring.fail_delivery = False
    await runtime2.deliver()
    assert calls == 1 and recovered.count() == 0
    assert len(spring.reservations) == 1
    assert spring.reports[0][2]["inputTokens"] == 20
    recovered.close()


@pytest.mark.anyio
async def test_quota_denial_never_calls_provider(tmp_path):
    spring = Spring()
    spring.deny_reserve = True
    called = False

    async def provider():
        nonlocal called
        called = True

    async def handler(e):
        await e.call("CHAT_ANSWER", "fake", 100, provider)
        return answer()

    box = Outbox(str(tmp_path / "box"))
    r = ChatRuntime(spring, box, handler=handler)
    r.submit(dispatch())
    await finished(r)
    await r.deliver()
    assert not called
    assert spring.reports[0][2]["failure"] == "QUOTA_EXHAUSTED"
    box.close()


@pytest.mark.anyio
@pytest.mark.parametrize("known", [True, False])
async def test_failure_records_known_usage_or_unknown(tmp_path, known):
    spring = Spring()

    async def provider():
        raise ProviderFailure(usage() if known else None)

    async def handler(e):
        await e.call("CHAT_ANSWER", "fake", 100, provider)

    box = Outbox(str(tmp_path / "box"))
    r = ChatRuntime(spring, box, handler=handler)
    r.submit(dispatch())
    await finished(r)
    await r.deliver()
    report = spring.reports[0][2]
    assert report["kind"] == ("SETTLE" if known else "UNKNOWN")
    assert report["outcome"] == ("FAILURE" if known else None)
    box.close()


@pytest.mark.anyio
async def test_capacity_shutdown_and_unknown_usage(tmp_path):
    spring = Spring()
    started = asyncio.Event()

    async def provider():
        started.set()
        await asyncio.Event().wait()

    async def handler(e):
        await e.call("CHAT_ANSWER", "fake", 100, provider)

    box = Outbox(str(tmp_path / "box"))
    r = ChatRuntime(spring, box, capacity=1, handler=handler)
    r.submit(dispatch())
    await started.wait()
    with pytest.raises(BusyError):
        r.submit(dispatch())
    await r.shutdown(0)
    assert spring.reports[0][2]["kind"] == "UNKNOWN"
    assert spring.reports[1][2]["failure"] == "INTERRUPTED"
    assert not r.accepting and not r.tasks
    box.close()


@pytest.mark.anyio
async def test_deadline_does_not_publish_late_answer(tmp_path):
    spring = Spring()

    async def handler(e):
        await asyncio.sleep(1)
        return answer()

    box = Outbox(str(tmp_path / "box"))
    r = ChatRuntime(spring, box, handler=handler)
    r.submit(dispatch(0.01))
    await finished(r)
    await r.deliver()
    assert spring.reports == []
    box.close()


@pytest.mark.anyio
async def test_lost_start_response_does_not_call_handler(tmp_path):
    spring = Spring()
    spring.lose_start = True
    called = False

    async def handler(e):
        nonlocal called
        called = True

    box = Outbox(str(tmp_path / "box"))
    r = ChatRuntime(spring, box, handler=handler)
    r.submit(dispatch())
    await finished(r)
    assert not called
    box.close()


@pytest.mark.anyio
async def test_deleted_work_discards_local_payload(tmp_path):
    spring = Spring()
    spring.deleted = True
    box = Outbox(str(tmp_path / "box"))
    d = dispatch()
    box.put(
        "result", d, uuid4(), "finish", answer().model_dump(mode="json"), d.deadline.timestamp()
    )
    await box.flush(spring)
    assert box.count() == 0
    box.close()
    assert "근거 부족".encode() not in (tmp_path / "box").read_bytes()


@pytest.mark.anyio
async def test_auth_and_body_limits_do_not_echo_secrets(tmp_path):
    box = Outbox(str(tmp_path / "box"))
    r = ChatRuntime(Spring(), box)
    key = "test-private-api-key"
    app = create_app(runtime=r, api_key=key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.post("/internal/v1/chat-executions", json={})).status_code == 401
        headers = {"X-Internal-Api-Key": key}
        invalid = await http.post(
            "/internal/v1/chat-executions", headers=headers, json={"question": "비밀"}
        )
        assert invalid.status_code == 422 and "비밀" not in invalid.text
        large = await http.post("/internal/v1/chat-executions", headers=headers, content="x" * 4097)
        assert large.status_code == 413
        expired = await http.post(
            "/internal/v1/chat-executions",
            headers=headers,
            json=dispatch(-1).model_dump(by_alias=True, mode="json"),
        )
        assert expired.status_code == 409
        accepted = await http.post(
            "/internal/v1/chat-executions",
            headers=headers,
            json=dispatch().model_dump(by_alias=True, mode="json"),
        )
        assert accepted.status_code == 202
        await finished(r)
    box.close()


@pytest.mark.anyio
async def test_scoped_tool_client_does_not_send_member_header(tmp_path):
    from app.clients.setting_tools_client import (
        SettingToolsClient,
        SettingToolContext,
        SettingToolClientError,
    )
    from app.schemas.setting_tools import SettingToolQuery

    d = dispatch()
    runner = uuid4()

    async def transport(request):
        assert request.url.path == f"/api/internal/v1/chat-runs/{d.attempt_id}/tools"
        assert request.headers["X-Chat-Runner"] == str(runner)
        assert "X-Setting-Tool-Member-Id" not in request.headers
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = SettingToolsClient(
            "http://spring",
            "key",
            SettingToolContext(1, uuid4(), d.attempt_id, d.capability, runner),
            http,
        )
        with pytest.raises(SettingToolClientError):
            await client.query(SettingToolQuery(tool="FIND_CHARACTERS"))


@pytest.mark.parametrize(
    "data",
    [
        {
            "kind": "SETTLE",
            "inputTokens": 10,
            "cachedInputTokens": 11,
            "outputTokens": 0,
            "outcome": "SUCCESS",
        },
        {"kind": "SETTLE", "inputTokens": 10, "outputTokens": 0, "outcome": "SUCCESS"},
        {"kind": "UNKNOWN", "inputTokens": 0},
    ],
)
def test_missing_usage_is_never_zero(data):
    with pytest.raises(ValidationError):
        Usage.model_validate(data)


def test_versioned_java_python_fixture():
    import json
    from pathlib import Path
    from app.schemas.chat_execution import CallView

    fixture = json.loads(Path("docs/contracts/chat-execution-v1.json").read_text())
    for name, model in {
        "dispatch": Dispatch,
        "context": ExecutionContext,
        "usage": Usage,
        "call": CallView,
        "finish": Finish,
        "executionLimitFinish": Finish,
    }.items():
        assert (
            model.model_validate(fixture[name]).model_dump(by_alias=True, mode="json")
            == fixture[name]
        )


@pytest.mark.anyio
@pytest.mark.parametrize("pending_calls", [1, 16, 32])
async def test_failed_settlement_does_not_block_result_storage(tmp_path, pending_calls):
    class SettlementBroken(Spring):
        async def request(self, d, runner, action, body, **kwargs):
            if action.startswith("calls/"):
                raise ChatTransportError(503)
            return await super().request(d, runner, action, body, **kwargs)

    spring = SettlementBroken()
    box = Outbox(str(tmp_path / "box"))
    d, runner = dispatch(), uuid4()
    for i in range(pending_calls):
        box.put(
            f"call-{i}",
            d,
            runner,
            f"calls/{i}",
            usage().model_dump(mode="json"),
            d.deadline.timestamp() + 86400,
        )
    box.put("result", d, runner, "finish", answer().model_dump(mode="json"), d.deadline.timestamp())
    await box.flush(spring)
    assert box.count() == pending_calls
    assert spring.reports[0][1] == "finish"
    box.close()


@pytest.mark.anyio
async def test_stalled_usage_requests_leave_capacity_for_finish(tmp_path):
    unblock_usage, delivered_finish = asyncio.Event(), asyncio.Event()

    class Stalled(Spring):
        async def request(self, d, runner, action, body, **kwargs):
            if action.startswith("calls/"):
                await unblock_usage.wait()
                raise ChatTransportError(503)
            delivered_finish.set()
            return await super().request(d, runner, action, body, **kwargs)

    box = Outbox(str(tmp_path / "box"))
    d, runner = dispatch(), uuid4()
    for i in range(16):
        box.put(
            f"call-{i}",
            d,
            runner,
            f"calls/{i}",
            usage().model_dump(mode="json"),
            d.deadline.timestamp() + 86400,
        )
    box.put("finish", d, runner, "finish", answer().model_dump(mode="json"), d.deadline.timestamp())
    delivery = asyncio.create_task(box.flush(Stalled()))
    try:
        await asyncio.wait_for(delivered_finish.wait(), timeout=1)
    finally:
        unblock_usage.set()
        await delivery
        box.close()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["finish", "calls/x"])
async def test_failed_reports_rotate_after_reopening_legacy_outbox(tmp_path, action):
    import json
    import sqlite3

    path = str(tmp_path / "legacy.db")
    d, runner = dispatch(), uuid4()
    payload = (answer() if action == "finish" else usage()).model_dump(mode="json")
    # A pre-fix outbox must remain readable without discarding pending reports.
    with sqlite3.connect(path) as old:
        old.execute("""CREATE TABLE reports (id TEXT PRIMARY KEY, dispatch TEXT NOT NULL,
            runner TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
            expires REAL NOT NULL)""")
        for i in range(33):
            body = payload if i == 32 else {**payload, "blocked": True}
            old.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?,?)",
                (
                    str(i),
                    d.model_dump_json(by_alias=True),
                    str(runner),
                    action,
                    json.dumps(body),
                    d.deadline.timestamp(),
                ),
            )
    old.close()

    class PartialOutage(Spring):
        async def request(self, d, runner, action, body, **kwargs):
            if body.get("blocked"):
                raise ChatTransportError(503)
            return await super().request(d, runner, action, body, **kwargs)

    spring = PartialOutage()
    # Reopening between batches also checks durable retry order and idempotent upgrade.
    for _ in range(5):
        box = Outbox(path)
        await box.flush(spring)
        box.close()
    assert len(spring.reports) == 1
    assert spring.reports[0][1] == action
    box = Outbox(path)
    assert box.count() == 32
    box.close()


@pytest.mark.anyio
async def test_expired_reports_are_removed_even_behind_failed_backlog(tmp_path):
    box = Outbox(str(tmp_path / "box"))
    d, runner = dispatch(), uuid4()
    for i in range(32):
        box.put(
            f"call-{i}",
            d,
            runner,
            f"calls/{i}",
            usage().model_dump(mode="json"),
            d.deadline.timestamp() + 86400,
        )
    box.put("expired", d, runner, "finish", answer().model_dump(mode="json"), 0)
    spring = Spring()
    spring.fail_delivery = True
    await box.flush(spring)
    assert box.count() == 32
    box.close()


@pytest.mark.anyio
async def test_local_call_limit_keeps_measured_usage_and_separate_failure(tmp_path):
    spring = Spring()
    d = dispatch()
    spring.contexts[d.attempt_id] = context(d).model_copy(update={"max_calls": 1})
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        return ProviderResult("answer", usage())

    async def handler(execution):
        await execution.call("CHAT_ANSWER", "fake", 100, provider)
        await execution.call("CHAT_ANSWER", "fake", 100, provider)
        return answer()

    box = Outbox(str(tmp_path / "box"))
    runtime = ChatRuntime(spring, box, handler=handler)
    runtime.submit(d)
    await finished(runtime)
    await runtime.deliver()
    assert calls == 1 and len(spring.reservations) == 1
    assert (
        next(body for _, action, body in spring.reports if action.startswith("calls/"))["kind"]
        == "SETTLE"
    )
    assert (
        next(body for _, action, body in spring.reports if action == "finish")["failure"]
        == "EXECUTION_LIMIT_EXCEEDED"
    )
    box.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status, error, expected",
    [
        (409, {"error": {"code": "AI_TOKEN_QUOTA_EXHAUSTED"}}, "QUOTA_EXHAUSTED"),
        (429, {"error": {"code": "CHAT_LIMIT_EXCEEDED"}}, "EXECUTION_LIMIT_EXCEEDED"),
        (429, {"error": {"code": "UNRECOGNIZED"}}, "NETWORK_FAILURE"),
        (429, {"error": None}, "NETWORK_FAILURE"),
    ],
)
async def test_reservation_error_code_controls_failure_not_http_status(
    tmp_path, status, error, expected
):
    d = dispatch()
    invoked = False

    async def transport(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(
                200,
                json={"success": True, "data": context(d).model_dump(by_alias=True, mode="json")},
            )
        assert request.url.path.endswith("/calls")
        return httpx.Response(status, json=error)

    async def provider():
        nonlocal invoked
        invoked = True

    async def handler(execution):
        await execution.call("CHAT_ANSWER", "fake", 100, provider)
        return answer()

    box = Outbox(str(tmp_path / "box"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = ChatExecutionClient("http://spring", "test-key", http)
        runtime = ChatRuntime(client, box, handler=handler)
        runtime.submit(d)
        await finished(runtime)
        reports = Spring()
        await box.flush(reports)
    assert not invoked
    assert reports.reports[0][2]["failure"] == expected
    box.close()


@pytest.mark.anyio
async def test_provider_cannot_release_after_it_was_called(tmp_path):
    spring = Spring()

    async def provider():
        return ProviderResult("invalid", Usage(kind="RELEASE"))

    async def handler(e):
        await e.call("CHAT_ANSWER", "fake", 100, provider)
        return answer()

    box = Outbox(str(tmp_path / "box"))
    runtime = ChatRuntime(spring, box, handler=handler)
    runtime.submit(dispatch())
    await finished(runtime)
    await runtime.deliver()
    assert spring.reports[0][2]["kind"] == "UNKNOWN"
    assert spring.reports[1][2]["failure"] == "PROVIDER_FAILURE"
    box.close()


@pytest.mark.anyio
@pytest.mark.parametrize("variant", ["success", "truncated", "missing", "embedding"])
async def test_existing_provider_response_usage_is_captured(tmp_path, variant):
    from app.chatbot.providers import ChatTextClient, ChatEmbeddingClient
    from app.llm.responses import LlmTextResponse
    from app.llm.exceptions import LlmOutputTruncatedError
    from app.embeddings.responses import EmbeddingBatchResponse

    class Provider:
        version = "test"

        async def create_text_response(self, *args, **kwargs):
            if variant == "truncated":
                raise LlmOutputTruncatedError(
                    "private provider detail",
                    incomplete_reason="max_output_tokens",
                    max_output_tokens=5,
                    input_token_count=12,
                    cached_input_token_count=4,
                    output_token_count=5,
                )
            if variant == "missing":
                return LlmTextResponse("answer")
            return LlmTextResponse("answer", 12, 4, 5)

        async def create_embeddings(self, inputs):
            return EmbeddingBatchResponse(embeddings=[[1.0]], model="test", input_token_count=12)

    async def handler(e):
        if variant == "embedding":
            await ChatEmbeddingClient(Provider(), e, model_name="test").create_embeddings(["query"])
        else:
            await ChatTextClient(
                Provider(), e, purpose="CHAT_ANSWER", default_model="test"
            ).create_text_response("system", "question", max_output_tokens=5)
        return answer()

    spring = Spring()
    box = Outbox(str(tmp_path / "box"))
    runtime = ChatRuntime(spring, box, handler=handler)
    runtime.submit(dispatch())
    await finished(runtime)
    await runtime.deliver()
    report = spring.reports[0][2]
    assert report["kind"] == ("UNKNOWN" if variant == "missing" else "SETTLE")
    if variant != "missing":
        assert report["inputTokens"] == 12
        assert report["outputTokens"] == (0 if variant == "embedding" else 5)
        assert report["outcome"] == ("FAILURE" if variant == "truncated" else "SUCCESS")
    assert len(spring.reservations) == 1
    box.close()


@pytest.mark.anyio
async def test_concurrent_provider_calls_reserve_outbox_space_before_spending(tmp_path):
    spring = Spring()
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return ProviderResult("value", usage())

    async def handler(e):
        results = await asyncio.gather(
            *(e.call("CHAT_ANSWER", "fake", 100, provider) for _ in range(3)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        return answer()

    box = Outbox(str(tmp_path / "box"), max_rows=3)
    runtime = ChatRuntime(spring, box, handler=handler)
    runtime.submit(dispatch())
    await finished(runtime)
    assert calls == 2 and box.count() == 3
    await runtime.deliver()
    assert len(spring.reports) == 3 and box.count() == 0
    box.close()
