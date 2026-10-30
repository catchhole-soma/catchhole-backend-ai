import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.analysis.exceptions import (
    ComparisonValidationError,
    LlmExtractionError,
    OrderedInputContextError,
)
from app.analysis.relationship_observations import (
    LocatedRelationshipEvidence,
    RelationshipExtractionResult,
    RelationshipObservation,
)
from app.analysis.relationship_pipeline import RelationshipAnalysisPipeline
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerTransportError
from app.clients.spring_worker_client import SpringWorkerClient
from app.llm.responses import LlmTextResponse


def setup_pipeline(*, saved=None, observations=()):
    payload = SimpleNamespace(
        analysis_job_id=uuid4(), lease_token=uuid4(),
        episode=SimpleNamespace(episode_no=5, title="다섯 번째 이야기"),
        frozen_source=lambda: SimpleNamespace(content_hash="a" * 64),
    )
    context = {
        "formatVersion": 1, "sourceHash": "a" * 64, "sourceEpisodeNo": 5,
        "contextToken": "frozen-token", "status": "PREPARED",
        "observations": list(observations), "nodes": [], "relationships": [], "heldObservations": [],
    }
    spring = SimpleNamespace(
        get_relationship_analysis=AsyncMock(return_value=saved),
        prepare_relationship_analysis=AsyncMock(return_value=context),
        complete_relationship_analysis=AsyncMock(), fail_relationship_analysis=AsyncMock(),
    )
    extractor = SimpleNamespace(extract_from_chunk=AsyncMock())
    comparator = SimpleNamespace(compare=AsyncMock(return_value=[]))
    pipeline = RelationshipAnalysisPipeline(
        spring_client=spring, payload=payload, extractor=extractor, comparator=comparator,
    )
    return pipeline, spring, extractor, comparator, context


def test_restored_prepared_context_does_not_repeat_paid_extraction():
    pipeline, spring, extractor, comparator, context = setup_pipeline(observations=[{"ref": "O1"}])
    spring.get_relationship_analysis.return_value = context
    comparator.compare.return_value = [{"observationRefs": ["O1"], "operation": "HOLD"}]

    async def run():
        await pipeline.load()
        await pipeline.extract_chunk(SimpleNamespace(id=uuid4()))
        return await pipeline.finish([])

    result = asyncio.run(run())
    extractor.extract_from_chunk.assert_not_called()
    spring.prepare_relationship_analysis.assert_not_called()
    comparator.compare.assert_awaited_once_with(context)
    assert result["relationshipAnalysisStatus"] == "COMPLETED"
    assert spring.complete_relationship_analysis.call_args.args[2]["contextToken"] == "frozen-token"


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "APPLIED"])
def test_terminal_results_are_not_reextracted_or_recompared(status):
    pipeline, spring, extractor, comparator, context = setup_pipeline()
    context["status"] = status
    spring.get_relationship_analysis.return_value = context

    async def run():
        await pipeline.load()
        return await pipeline.finish([SimpleNamespace(id=uuid4())])

    assert asyncio.run(run()) == {"relationshipAnalysisStatus": status}
    extractor.extract_from_chunk.assert_not_called()
    comparator.compare.assert_not_called()
    spring.complete_relationship_analysis.assert_not_called()


def test_empty_observation_set_preserves_existing_edges_without_comparison():
    pipeline, spring, _, comparator, context = setup_pipeline()
    context["relationships"] = [{"ref": "R1", "status": "ACTIVE"}]
    asyncio.run(pipeline.finish([]))
    comparator.compare.assert_not_called()
    assert spring.complete_relationship_analysis.call_args.args[2]["decisions"] == []


def test_chunk_evidence_becomes_episode_unicode_offsets_and_keeps_rejection_count():
    pipeline, spring, extractor, _, _ = setup_pipeline()
    chunk = SimpleNamespace(id=uuid4(), start_offset=8, chunk_text="가와 나가 협력했다. 😀")
    observation = RelationshipObservation(
        source={"name": "가", "mention": "가와"}, target={"name": "나", "mention": "나가"},
        summary="함께 협력했다", evidence_span_ids=["S1"], evidence_quotes=[chunk.chunk_text], source_chunk_id=chunk.id,
        evidence_spans=[LocatedRelationshipEvidence(quote=chunk.chunk_text, start_offset=0, end_offset=len(chunk.chunk_text))],
    )
    extractor.extract_from_chunk.return_value = RelationshipExtractionResult(
        source_chunk_id=chunk.id, raw_observations=[], observations=[observation],
        rejections=[SimpleNamespace(reason="INVALID_ITEM")], response=LlmTextResponse(text="{}"),
    )
    asyncio.run(pipeline.finish([chunk]))
    request = spring.prepare_relationship_analysis.call_args.args[2]
    assert request["rejectedObservationCount"] == 1
    assert request["observations"][0]["evidence"] == [{
        "quote": chunk.chunk_text, "startOffset": 8, "endOffset": 8 + len(chunk.chunk_text),
    }]
    assert request["observations"][0]["ref"] == "O1"


@pytest.mark.parametrize("field,value", [
    ("formatVersion", True), ("formatVersion", 2), ("sourceHash", "b" * 64),
    ("sourceEpisodeNo", 6), ("status", "INVALIDATED"), ("contextToken", None),
])
def test_stale_or_unsupported_context_is_fatal(field, value):
    pipeline, spring, _, _, context = setup_pipeline()
    context[field] = value
    spring.get_relationship_analysis.return_value = context
    with pytest.raises(OrderedInputContextError):
        asyncio.run(pipeline.load())
    spring.fail_relationship_analysis.assert_not_called()


def test_bad_relation_output_is_isolated_without_saving_raw_error():
    pipeline, spring, extractor, comparator, _ = setup_pipeline()
    extractor.extract_from_chunk.side_effect = LlmExtractionError("private manuscript")
    chunks = [SimpleNamespace(id=uuid4(), chunk_text="원문", start_offset=0) for _ in range(2)]
    result = asyncio.run(pipeline.finish(chunks))
    assert result == {"relationshipAnalysisStatus": "FAILED"}
    assert extractor.extract_from_chunk.await_count == 1
    comparator.compare.assert_not_called()
    body = spring.fail_relationship_analysis.call_args.args[2]
    assert body == {"formatVersion": 1, "sourceHash": "a" * 64, "contextToken": None,
                    "failureCode": "EXTRACTION_FAILED"}


def test_invalid_comparison_becomes_internal_failure():
    pipeline, spring, _, comparator, context = setup_pipeline(observations=[{"ref": "O1"}])
    spring.get_relationship_analysis.return_value = context
    comparator.compare.side_effect = ComparisonValidationError("invalid relation")

    async def run():
        await pipeline.load()
        return await pipeline.finish([])

    assert asyncio.run(run())["relationshipAnalysisStatus"] == "FAILED"
    assert spring.fail_relationship_analysis.call_args.args[2]["failureCode"] == "COMPARISON_FAILED"
    spring.complete_relationship_analysis.assert_not_called()


@pytest.mark.parametrize("error", [
    AiTokenQuotaExhaustedError(), SpringWorkerTransportError("transport"),
    RuntimeError("unexpected"),
    httpx.HTTPStatusError("provider auth", request=httpx.Request("POST", "https://example.invalid"),
                          response=httpx.Response(401)),
    asyncio.CancelledError(),
])
def test_authority_quota_unexpected_and_cancellation_are_never_swallowed(error):
    pipeline, spring, extractor, _, _ = setup_pipeline()
    extractor.extract_from_chunk.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(pipeline.finish([SimpleNamespace(id=uuid4(), chunk_text="원문", start_offset=0)]))
    spring.fail_relationship_analysis.assert_not_called()


def test_spring_relationship_requests_preserve_lease_and_claim_capability():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204 if request.method == "GET" else 200, json={"data": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = SpringWorkerClient("http://test", "internal", http)
            job, lease = uuid4(), uuid4()
            assert await client.get_relationship_analysis(job, lease) is None
            await client.prepare_relationship_analysis(job, lease, {"formatVersion": 1})
            await client.complete_relationship_analysis(job, lease, {"decisions": []})
            await client.fail_relationship_analysis(job, lease, {"failureCode": "INVALID_RESPONSE"})
            assert all(r.headers["X-Worker-Lease-Token"] == str(lease) for r in requests)
            assert all(r.headers["X-Internal-Api-Key"] == "internal" for r in requests)
    asyncio.run(run())
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["relationship-analysis", "prepare", "complete", "fail"]
