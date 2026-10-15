import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.chatbot.client import ChatExecutionClient
from app.chatbot.outbox import Outbox
from app.embeddings.client import OpenAIEmbeddingsClient
from app.retrieval.contracts import IndexSpec, SourceChanged
from app.retrieval.provider import IndexProviderFailure, OperationalEmbeddingProvider
from app.schemas.chat_execution import Dispatch


@pytest.mark.parametrize(
    "scenario", ["success", "malformed", "auth", "retryable", "preflight", "unknown"]
)
def test_real_http_adapters_reserve_each_call_and_preserve_failure_usage(tmp_path, scenario):
    records, calls = [], []
    work = uuid4()
    deadline = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()

    def spring(req):
        import json

        body = json.loads(req.content or b"null")
        path = req.url.path
        records.append((path, body))
        if "/operations/" in path:
            assert body["policy"] == "RETRIEVAL_INDEX_SERVICE_PAID_V1"
            data = dict(
                schemaVersion=1,
                attemptId=path.split("/")[-1],
                capability="a" * 64,
                deadline=deadline,
            )
        elif path.endswith("/start"):
            data = dict(
                schemaVersion=1,
                questionId=None,
                attemptId=path.split("/")[-2],
                workId=str(work),
                memberId=1,
                question=None,
                episodeFrom=None,
                episodeTo=None,
                deadline=deadline,
                tokenBudget=next(b["tokenBudget"] for p, b in records if "/operations/" in p),
                maxCalls=8,
                maxToolCalls=20,
                operational=True,
            )
        elif path.endswith("/calls"):
            assert body["purpose"] == "OPERATIONAL_BACKFILL"
            data = dict(
                callId=body["callId"],
                status="RESERVED",
                reservedTokens=body["reservedTokens"],
                inputTokens=None,
                cachedInputTokens=None,
                outputTokens=None,
            )
        else:
            data = {}
        return httpx.Response(200, json={"success": True, "data": data})

    def provider(req):
        calls.append(req)
        if scenario in {"auth", "retryable"}:
            return httpx.Response(401 if scenario == "auth" else 429, json={"secret": "private"})
        payload = {
            "model": "text-embedding-3-small",
            "data": [{"index": 0, "embedding": [1.0] * (2 if scenario == "malformed" else 1536)}],
        }
        if scenario != "unknown":
            payload["usage"] = {"prompt_tokens": 7}
        return httpx.Response(200, json=payload)

    async def execute():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(spring)) as spring_http,
            httpx.AsyncClient(transport=httpx.MockTransport(provider)) as embedding_http,
        ):
            raw = OpenAIEmbeddingsClient(
                "fake",
                "text-embedding-3-small",
                1536,
                "v1",
                "https://provider.invalid/embeddings",
                embedding_http,
            )
            outbox = Outbox(str(tmp_path / "reports.db"))
            adapter = OperationalEmbeddingProvider(
                ChatExecutionClient("http://spring.invalid", "fake", spring_http),
                raw,
                outbox,
                IndexSpec(),
                "test",
            )

            async def preflight(_):
                if scenario == "preflight":
                    raise SourceChanged()

            try:
                if scenario in {"malformed", "auth", "retryable"}:
                    with pytest.raises(IndexProviderFailure) as error:
                        await adapter.embed(
                            SimpleNamespace(work_id=work),
                            [SimpleNamespace(chunk_text="합성 문장")],
                            preflight,
                        )
                    assert error.value.retryable == (scenario == "retryable")
                elif scenario == "preflight":
                    with pytest.raises(SourceChanged):
                        await adapter.embed(
                            SimpleNamespace(work_id=work),
                            [SimpleNamespace(chunk_text="합성 문장")],
                            preflight,
                        )
                else:
                    assert (
                        len(
                            await adapter.embed(
                                SimpleNamespace(work_id=work),
                                [SimpleNamespace(chunk_text="합성 문장")],
                                preflight,
                            )
                        )
                        == 1
                    )
                assert outbox.count() == 0
            finally:
                outbox.close()

    asyncio.run(execute())
    reports = [b for p, b in records if "/calls/" in p]
    assert len(reports) == 1
    assert len(calls) == (0 if scenario == "preflight" else 1)
    assert reports[0]["kind"] == (
        "RELEASE"
        if scenario == "preflight"
        else "UNKNOWN"
        if scenario in {"auth", "retryable", "unknown"}
        else "SETTLE"
    )
    if scenario in {"success", "malformed"}:
        assert reports[0]["inputTokens"] == 7
        assert reports[0]["outcome"] == ("SUCCESS" if scenario == "success" else "FAILURE")
    assert "private" not in str(records)


def reporting_case(tmp_path, *, short_deadline=False):
    work = uuid4()
    report_started, release_reports = asyncio.Event(), asyncio.Event()
    reports = []

    class Ledger:
        async def operation(self, operation_id, work_id, actor, policy, maximum):
            self.maximum = maximum
            return Dispatch(
                schemaVersion=1,
                attemptId=operation_id,
                capability="a" * 64,
                deadline=datetime.now(UTC) + timedelta(seconds=0.1 if short_deadline else 120),
            )

        async def start(self, dispatch, runner):
            return SimpleNamespace(
                operational=True,
                work_id=work,
                question_id=None,
                token_budget=self.maximum,
                max_calls=1,
            )

        async def reserve(self, *args):
            pass

        async def request(self, dispatch, runner, action, payload):
            report_started.set()
            await release_reports.wait()
            reports.append((action, payload))

    path = str(tmp_path / "interrupted.sqlite")
    return SimpleNamespace(
        client=Ledger(),
        outbox=Outbox(path),
        path=path,
        source=SimpleNamespace(work_id=work),
        chunks=[SimpleNamespace(chunk_text="합성 문장")],
        report_started=report_started,
        release_reports=release_reports,
        reports=reports,
    )


@pytest.mark.parametrize("stage", ["preflight", "provider"])
@pytest.mark.parametrize("stop", ["cancel", "outer_timeout", "dispatch_timeout"])
def test_interrupted_attempt_persists_reports_without_waiting_for_delivery(tmp_path, stage, stop):
    async def execute():
        case = reporting_case(tmp_path, short_deadline=stop == "dispatch_timeout")
        reached = asyncio.Event()

        async def pause():
            reached.set()
            await asyncio.Event().wait()

        async def before_call(_):
            if stage == "preflight":
                await pause()

        class Embedding:
            async def create_embeddings(self, values):
                await pause()

        adapter = OperationalEmbeddingProvider(
            case.client, Embedding(), case.outbox, IndexSpec(), "test"
        )
        timers = []

        async def run():
            async with asyncio.timeout(None) as timer:
                timers.append(timer)
                await adapter.embed(case.source, case.chunks, before_call)

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(reached.wait(), timeout=1)
            if stop == "cancel":
                task.cancel()
            elif stop == "outer_timeout":
                timers[0].reschedule(asyncio.get_running_loop().time())
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, "Cancellation must not wait for report delivery"
            with pytest.raises(asyncio.CancelledError if stop == "cancel" else TimeoutError):
                await task
            assert not case.report_started.is_set()
            assert case.outbox.count() == 2
            expected_usage = "RELEASE" if stage == "preflight" else "UNKNOWN"
            pending = [
                json.loads(payload)
                for (payload,) in case.outbox.db.execute(
                    "SELECT payload FROM reports WHERE action != 'finish'"
                )
            ]
            assert [payload["kind"] for payload in pending] == [expected_usage]
            assert adapter.attempts == (0 if stage == "preflight" else 1)

            # A new process can recover the durable reports without another embedding call.
            case.outbox.close()
            case.outbox = Outbox(case.path)
            case.release_reports.set()
            await case.outbox.flush(case.client)
            assert case.outbox.count() == 0
            assert [p["kind"] for action, p in case.reports if action != "finish"] == [
                expected_usage
            ]
        finally:
            task.cancel()
            case.release_reports.set()
            await asyncio.gather(task, return_exceptions=True)
            case.outbox.close()

    asyncio.run(execute())


@pytest.mark.parametrize("stop", ["outer_timeout", "dispatch_timeout"])
def test_slow_success_report_delivery_obeys_deadline_and_recovers_known_usage(tmp_path, stop):
    async def execute():
        case = reporting_case(tmp_path, short_deadline=stop == "dispatch_timeout")

        async def before_call(_):
            pass

        class Embedding:
            async def create_embeddings(self, values):
                return SimpleNamespace(
                    model="text-embedding-3-small",
                    input_token_count=7,
                    embeddings=[[1.0] * 1536],
                )

        adapter = OperationalEmbeddingProvider(
            case.client, Embedding(), case.outbox, IndexSpec(), "test"
        )
        timers = []

        async def run():
            async with asyncio.timeout(None) as timer:
                timers.append(timer)
                await adapter.embed(case.source, case.chunks, before_call)

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(case.report_started.wait(), timeout=1)
            if stop == "outer_timeout":
                timers[0].reschedule(asyncio.get_running_loop().time())
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, "Report delivery must remain inside the execution deadline"
            with pytest.raises(TimeoutError):
                await task
            assert case.outbox.count() == 2
            assert not case.reports
            assert adapter.attempts == 1 and adapter.input_tokens == 7

            case.outbox.close()
            case.outbox = Outbox(case.path)
            case.release_reports.set()
            await case.outbox.flush(case.client)
            assert case.outbox.count() == 0
            usage = [p for action, p in case.reports if action != "finish"]
            assert len(usage) == 1
            assert usage[0]["kind"] == "SETTLE"
            assert usage[0]["inputTokens"] == 7 and usage[0]["outcome"] == "SUCCESS"
        finally:
            task.cancel()
            case.release_reports.set()
            await asyncio.gather(task, return_exceptions=True)
            case.outbox.close()

    asyncio.run(execute())
