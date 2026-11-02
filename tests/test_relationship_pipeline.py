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
from app.analysis.relationship_grouped_comparator import RelationshipComparisonOutcome
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
    diagnostic = body.pop("failureDiagnostics")
    assert diagnostic["stage"] == "RELATIONSHIP_EXTRACTION"
    assert diagnostic["reasonCode"] == "COMPARISON_RESULT_INVALID"
    assert "private manuscript" not in str(diagnostic)
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


def test_isolated_failure_sends_actual_comparison_values_without_stopping_core_analysis():
    from app.analysis.relationship_diagnostics import annotate_relationship_failure
    from app.schemas.worker import WorkerFailureExcerpt

    pipeline, spring, _, comparator, context = setup_pipeline(observations=[{"ref": "O1"}])
    spring.get_relationship_analysis.return_value = context
    comparator.compare.side_effect = annotate_relationship_failure(
        ComparisonValidationError("unapproved raw error"), stage="RELATIONSHIP_COMPARE",
        reason="RELATIONSHIP_MERGE_CONTENT_CHANGED", excerpts=[
            WorkerFailureExcerpt(kind="EXPECTED", field="relationshipSummary", text="서로 경계한다."),
            WorkerFailureExcerpt(kind="RESPONSE", field="relationshipSummary", text="서로 신뢰한다."),
        ],
    )

    async def run():
        await pipeline.load()
        return await pipeline.finish([])

    assert asyncio.run(run()) == {"relationshipAnalysisStatus": "FAILED"}
    body = spring.fail_relationship_analysis.call_args.args[2]
    assert body["failureDiagnostics"]["reasonCode"] == "RELATIONSHIP_MERGE_CONTENT_CHANGED"
    assert body["failureDiagnostics"]["stage"] == "RELATIONSHIP_COMPARE"
    assert [part["text"] for part in body["failureDiagnostics"]["excerpts"]] == ["서로 경계한다.", "서로 신뢰한다."]
    assert "unapproved raw error" not in str(body)


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


def test_partial_completion_sends_valid_decisions_and_typed_failures_atomically():
    pipeline, spring, extractor, comparator, context = setup_pipeline(observations=[{"ref": "O1"}, {"ref": "O2"}])
    pipeline.payload.relationship_analysis_contract = {"formatVersion": 1, "supportsPartialResults": True}
    context.update(supportsPartialResults=True, blockedPairs=[])
    spring.get_relationship_analysis.return_value = context
    decisions = [{"observationRefs": ["O2"], "operation": "ADD"}]
    failures = [{"observationRefs": ["O1"], "reasonCode": "RELATIONSHIP_OBSERVATION_REFS_INVALID"}]
    comparator.compare_partial = AsyncMock(return_value=RelationshipComparisonOutcome(decisions, failures))

    async def run():
        await pipeline.load()
        return await pipeline.finish([])

    summary = asyncio.run(run())
    assert summary["relationshipAnalysisStatus"] == "PARTIAL"
    assert summary["relationshipFailureCount"] == summary["relationshipFailedGroupCount"] == 1
    assert spring.complete_relationship_analysis.call_args.args[2] == {
        "formatVersion": 1, "contextToken": "frozen-token", "decisions": decisions, "failures": failures,
    }
    spring.complete_relationship_analysis.assert_awaited_once()
    spring.fail_relationship_analysis.assert_not_called()
    extractor.extract_from_chunk.assert_not_called()
    comparator.compare.assert_not_called()


@pytest.mark.parametrize("status", ["COMPLETED", "APPLIED"])
def test_terminal_partial_resume_preserves_failed_count_without_paid_calls(status):
    pipeline, spring, extractor, comparator, context = setup_pipeline(observations=[{"ref": "O1"}])
    pipeline.payload.relationship_analysis_contract = {"formatVersion": 1, "supportsPartialResults": True}
    context.update(status=status, supportsPartialResults=True,
                   failures=[{"observationRefs": ["O1"], "reasonCode": "RELATIONSHIP_OBSERVATION_REFS_INVALID"}])
    spring.get_relationship_analysis.return_value = context
    comparator.compare_partial = AsyncMock()

    async def run():
        await pipeline.load()
        return await pipeline.finish([])

    assert asyncio.run(run()) == {"relationshipAnalysisStatus": "PARTIAL", "relationshipFailureCount": 1,
                                  "relationshipFailedGroupCount": 1}
    extractor.extract_from_chunk.assert_not_called()
    comparator.compare.assert_not_called()
    comparator.compare_partial.assert_not_called()
    spring.complete_relationship_analysis.assert_not_called()


@pytest.mark.parametrize("patch", [
    {"supportsPartialResults": True}, {"supportsPartialResults": "true"},
])
def test_context_cannot_enable_partial_without_frozen_claim_capability(patch):
    pipeline, spring, _, comparator, context = setup_pipeline(observations=[{"ref": "O1"}])
    context.update(patch)
    spring.get_relationship_analysis.return_value = context
    with pytest.raises(OrderedInputContextError):
        asyncio.run(pipeline.load())
    comparator.compare.assert_not_called()


@pytest.mark.parametrize("failures", [None, [{"observationRefs": ["O999"], "reasonCode": "RELATIONSHIP_OBSERVATION_REFS_INVALID"}],
                                        [{"observationRefs": ["O1"], "reasonCode": "private text"}]])
def test_terminal_partial_missing_or_malformed_failures_cannot_become_success(failures):
    pipeline, spring, _, _, context = setup_pipeline(observations=[{"ref": "O1"}])
    pipeline.payload.relationship_analysis_contract = {"formatVersion": 1, "supportsPartialResults": True}
    context.update(status="COMPLETED", supportsPartialResults=True, failures=failures)
    spring.get_relationship_analysis.return_value = context
    with pytest.raises(OrderedInputContextError):
        asyncio.run(pipeline.load())


def test_partial_spring_completion_error_does_not_fall_back_to_hold_or_failure_completion():
    pipeline, spring, _, comparator, context = setup_pipeline(observations=[{"ref": "O1"}])
    pipeline.payload.relationship_analysis_contract = {"formatVersion": 1, "supportsPartialResults": True}
    context.update(supportsPartialResults=True)
    spring.get_relationship_analysis.return_value = context
    comparator.compare_partial = AsyncMock(return_value=RelationshipComparisonOutcome([], [
        {"observationRefs": ["O1"], "reasonCode": "RELATIONSHIP_OBSERVATION_REFS_INVALID"},
    ]))
    spring.complete_relationship_analysis.side_effect = SpringWorkerTransportError("private")

    async def run():
        await pipeline.load()
        await pipeline.finish([])

    with pytest.raises(SpringWorkerTransportError):
        asyncio.run(run())
    spring.complete_relationship_analysis.assert_awaited_once()
    spring.fail_relationship_analysis.assert_not_called()


def test_restored_failed_context_explains_missing_original_diagnostics():
    from app.exceptions.failure_classification import failure_diagnostics

    pipeline, spring, _, _, context = setup_pipeline()
    context["status"] = "FAILED"
    spring.get_relationship_analysis.return_value = context

    async def run():
        await pipeline.load()
        return await pipeline.finish([])

    assert asyncio.run(run()) == {"relationshipAnalysisStatus": "FAILED"}
    assert pipeline.failure_exception is not None
    diagnostic = failure_diagnostics(pipeline.failure_exception, stage=pipeline.failure_stage)
    assert diagnostic.reason_code == "RELATIONSHIP_PREVIOUS_FAILURE"
    assert diagnostic.stage == "RELATIONSHIP_RESUME"
