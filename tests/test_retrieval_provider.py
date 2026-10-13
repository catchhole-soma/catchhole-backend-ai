import asyncio
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
