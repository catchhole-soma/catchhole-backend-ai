"""Current-conflict wire propagation, separate from semantic model judgment."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.domain.enums import CharacterFactTemporalScope, SettingBookReferenceTimeKind
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonBatchContextResponse,
    WorkerWorldSettingComparisonBatchPayload,
    WorkerWorldSettingComparisonExactTarget,
    WorkerWorldSettingComparisonTarget,
)
from tests.test_world_setting_batch_pipeline import (
    ANALYSIS_JOB_ID,
    LEASE_TOKEN,
    FakeBatchSpringApi,
    FakeBatchSubjectResolver,
    _batch,
)


class ReplyClient:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text=json.dumps(self.reply, ensure_ascii=False))


def current_conflict_inputs():
    source = WorkerAnalysisSource.model_validate({
        "sourceKind": "SETTING_BOOK", "settingBookId": UUID(int=1),
        "settingBookName": "종족 설정집", "revisionId": UUID(int=2),
        "revisionNo": 1, "contentHash": "a" * 64,
        "referenceTimeKind": "UNSPECIFIED", "referenceEpisodeNo": None,
    })
    candidate = WorkerWorldSettingCandidatePayload.model_validate({
        "candidateId": UUID(int=3), "workId": UUID(int=4),
        "sourceEpisodeId": None, "source": source,
        "category": "RACE", "subjectName": "고블린", "scopeName": None,
        "settingName": "거주지", "extractedValue": "남쪽에서만 산다.",
        "evidenceSpans": [{"quote": "고블린은 남쪽에서만 산다."}],
        "extractionConfidence": 0.95, "temporalScope": "PRESENT",
        "rawExtractionJson": {"epistemic_status": "ASSERTED"},
    })
    target = WorkerWorldSettingComparisonTarget.model_validate({
        "worldSettingId": UUID(int=5), "subjectName": "고블린", "version": 1,
        "properties": [{
            "scopeName": None, "settingName": "서식지", "value": "북쪽에서만 산다.",
            "provenance": {
                "confirmationStatus": "CONFIRMED", "sourceKind": "EPISODE",
                "sourceEpisodeNo": 7, "sourceCandidateIds": [UUID(int=6)],
                "currentSourceTimes": [{
                    "sourceKind": "EPISODE", "sourceEpisodeNo": 7,
                    "temporalScope": "PRESENT",
                }],
            },
        }],
    })
    decision = {
        "consolidation_status": "CONFLICT", "operation": "REVIEW_REQUIRED",
        "review_reason": "GENERAL_UNCERTAINTY", "target_ref": "T1",
        "matched_scope_name": None, "matched_property_name": "서식지",
        "proposed_scope_name": None, "proposed_setting_name": "서식지",
        "proposed_value": "북쪽에서만 산다.\n남쪽에서만 산다.",
        "comparison_reason": "같은 고블린의 현재 서식지가 서로 양립할 수 없어 확인이 필요하다.",
    }
    return source, candidate, target, decision


def compare_reply(route, source, candidate, target, decision):
    if route == "singleton":
        client = ReplyClient(decision)
        result, raw = asyncio.run(WorldSettingComparator(
            llm_client=client, max_attempts=1,
        ).compare(candidate, [target]))
    else:
        response_decision = {
            **decision, "source_candidate_refs": ["C1"],
            "existing_root_property_names_to_move": [],
        }
        if route == "ordered":
            response_decision.pop("matched_scope_name")
            response_decision.pop("matched_property_name")
            response_decision["matched_property_ref"] = (
                "T1.P1" if decision["matched_property_name"] is not None else None
            )
        client = ReplyClient({"decisions": [response_decision]})
        batch_candidate = WorkerWorldSettingComparisonBatchCandidate.model_validate({
            **candidate.model_dump(by_alias=True), "candidateRef": "C1",
        })
        batch, raw = asyncio.run(WorldSettingComparator(
            llm_client=client, max_attempts=1,
        ).compare_batch("RACE", [batch_candidate], [target],
                        ordered_context=route == "ordered", analysis_source=source))
        result = batch.decisions[0]
        raw = raw["decisions"][0]
    return result, raw, client.requests[0]


@pytest.mark.parametrize("source_kind", ["SETTING_BOOK", "EPISODE"])
@pytest.mark.parametrize("route", ["singleton", "batch", "ordered"])
def test_current_conflict_retains_current_and_incoming_without_mutation(route, source_kind):
    source, candidate, target, decision = current_conflict_inputs()
    if source_kind == "EPISODE":
        source = WorkerAnalysisSource(source_kind="EPISODE", episode_id=UUID(int=7), episode_no=8)
        candidate = candidate.model_copy(update={
            "source": source, "source_episode_id": UUID(int=7), "raw_extraction_json": {},
        })
    before = target.model_dump(mode="json")
    result, raw, request = compare_reply(route, source, candidate, target, decision)
    assert result.operation == "REVIEW_REQUIRED"
    assert result.consolidation_status == "CONFLICT"
    assert result.review_reason == "GENERAL_UNCERTAINTY"
    assert result.target_ref == "T1"
    assert result.matched_property_name == result.proposed_setting_name == "서식지"
    assert result.proposed_value == "북쪽에서만 산다.\n남쪽에서만 산다."
    assert raw["proposed_value"] == result.proposed_value
    assert target.model_dump(mode="json") == before
    payload = json.loads(request["user_prompt"])
    incoming = payload["candidate"] if route == "singleton" else payload["candidates"][0]
    assert incoming["setting_name"] == "거주지"
    assert incoming["extracted_value"] == candidate.extracted_value
    assert incoming["evidence_spans"][0]["quote"] == candidate.evidence_spans[0].quote
    assert payload["targets"][0]["properties"][0]["current_source_times"][0]["temporal_scope"] == "PRESENT"
    assert str(UUID(int=6)) not in request["user_prompt"]
    if route != "singleton":
        assert result.source_candidate_refs == ["C1"]
        assert result.existing_root_property_names_to_move == []


@pytest.mark.parametrize("route", ["singleton", "batch", "ordered"])
def test_ordinary_uncertainty_remains_single_source_review(route):
    source, candidate, target, decision = current_conflict_inputs()
    decision.update(consolidation_status="SINGLE", review_reason="GENERAL_UNCERTAINTY",
                    proposed_setting_name="거주지", proposed_value="모델의 추측",
                    comparison_reason="전승의 지시 대상이 불명확해 확인이 필요하다.")
    candidate = candidate.model_copy(update={"raw_extraction_json": {"epistemic_status": "UNRESOLVED_CLAIM"}})
    result, _, _ = compare_reply(route, source, candidate, target, decision)
    assert result.operation == "REVIEW_REQUIRED"
    assert result.consolidation_status == "SINGLE"
    assert result.review_reason == "GENERAL_UNCERTAINTY"
    assert result.proposed_value == candidate.extracted_value
    assert result.proposed_setting_name == candidate.setting_name


@pytest.mark.parametrize("uncertain", [False, True])
def test_current_conflict_or_uncertainty_reaches_existing_batch_completion(uncertain):
    source, candidate, target, decision = current_conflict_inputs()
    if uncertain:
        decision.update(consolidation_status="SINGLE", review_reason="GENERAL_UNCERTAINTY",
                        proposed_setting_name="거주지", proposed_value=candidate.extracted_value)
    batch_candidate = WorkerWorldSettingComparisonBatchCandidate.model_validate({
        **candidate.model_dump(by_alias=True), "candidateRef": "C1",
    })
    batch = WorkerWorldSettingComparisonBatchPayload.model_validate({
        **_batch().model_dump(by_alias=True), "sourceEpisodeId": None, "source": source,
        "rawScopeName": None, "candidates": [batch_candidate],
    })
    spring = FakeBatchSpringApi([batch])
    spring.get_world_setting_comparison_batch_context = AsyncMock(return_value=
        WorkerWorldSettingComparisonBatchContextResponse(
            comparison_batch_id=batch.comparison_batch_id, source=source,
            candidates=[batch_candidate], targets=[target], exact_targets=[
                WorkerWorldSettingComparisonExactTarget(candidate_ref="C1", world_setting_id=target.world_setting_id),
            ],
        ))
    client = ReplyClient({"decisions": [{
        **decision, "source_candidate_refs": ["C1"], "existing_root_property_names_to_move": [],
    }]})
    result = asyncio.run(WorldSettingComparisonPipeline(
        spring, FakeBatchSubjectResolver(target.world_setting_id, target.subject_name),
        WorldSettingComparator(llm_client=client, max_attempts=1),
    ).process_all(ANALYSIS_JOB_ID, LEASE_TOKEN, analysis_source=source))

    assert result.completed_count == 1 and result.failed_count == 0
    assert len(client.requests) == len(spring.completions) == 1
    wire = spring.completions[0].model_dump(by_alias=True)["decisions"][0]
    assert wire["suggestedOperation"] == "REVIEW_REQUIRED"
    assert wire["consolidationStatus"] == ("SINGLE" if uncertain else "CONFLICT")
    assert wire["comparisonReviewReason"] == "GENERAL_UNCERTAINTY"
    assert wire["targetWorldSettingId"] == target.world_setting_id
    assert wire["matchedPropertyName"] == "서식지"
    assert wire["sourceCandidateRefs"] == ["C1"]
    assert wire["existingRootPropertyNamesToMove"] == []
    assert wire["proposedValue"] == (candidate.extracted_value if uncertain else
                                    target.properties[0].value + "\n" + candidate.extracted_value)


@pytest.mark.parametrize("invalid", ["multiple-incoming", "property-move"])
def test_current_conflict_review_does_not_expand_source_or_move_contract(invalid):
    source, candidate, target, decision = current_conflict_inputs()
    candidates = [WorkerWorldSettingComparisonBatchCandidate.model_validate({
        **candidate.model_dump(by_alias=True), "candidateRef": "C1",
    })]
    decision.update(source_candidate_refs=["C1"], existing_root_property_names_to_move=[])
    if invalid == "multiple-incoming":
        candidates.append(candidates[0].model_copy(update={"candidate_ref": "C2", "candidate_id": UUID(int=8)}))
        decision["source_candidate_refs"] = ["C1", "C2"]
    else:
        decision["existing_root_property_names_to_move"] = ["서식지"]
    client = ReplyClient({"decisions": [decision]})
    with pytest.raises(ComparisonValidationError):
        asyncio.run(WorldSettingComparator(llm_client=client, max_attempts=1).compare_batch(
            "RACE", candidates, [target], analysis_source=source,
        ))


@pytest.mark.parametrize("invalid", [
    "null-review-reason", "no-target", "unknown-target", "no-match", "missing-property", "wrong-proposed-path", "wrong-source-scope",
    "past", "hypothetical", "unknown", "unresolved-claim", "anchor-inconsistency",
    "missing-current-times", "nonpresent-current-time",
])
def test_current_conflict_rejects_unproven_or_wrong_current_anchor(invalid):
    source, candidate, target, decision = current_conflict_inputs()
    if invalid == "null-review-reason":
        decision["review_reason"] = None
    elif invalid == "no-target":
        decision.update(target_ref=None, matched_property_name=None)
    elif invalid == "unknown-target":
        decision["target_ref"] = "T9"
    elif invalid == "no-match":
        decision["matched_property_name"] = None
    elif invalid == "missing-property":
        target = target.model_copy(update={"properties": []})
    elif invalid == "wrong-proposed-path":
        decision["proposed_setting_name"] = "전투 방식"
    elif invalid == "wrong-source-scope":
        candidate = candidate.model_copy(update={"scope_name": "겨울"})
    elif invalid in {"past", "hypothetical", "unknown"}:
        candidate = candidate.model_copy(update={"temporal_scope": CharacterFactTemporalScope(invalid.upper())})
    elif invalid == "unresolved-claim":
        candidate = candidate.model_copy(update={"raw_extraction_json": {"epistemic_status": "UNRESOLVED_CLAIM"}})
    elif invalid == "anchor-inconsistency":
        source = source.model_copy(update={"reference_time_kind": SettingBookReferenceTimeKind.WORK_START})
        candidate = candidate.model_copy(update={"source": source, "effective_from_episode_no": 8})
    else:
        prop = target.properties[0]
        times = [] if invalid == "missing-current-times" else [
            prop.provenance.current_source_times[0].model_copy(update={"temporal_scope": CharacterFactTemporalScope.PAST}),
        ]
        target = target.model_copy(update={"properties": [prop.model_copy(update={
            "provenance": prop.provenance.model_copy(update={"current_source_times": times}),
        })]})
    with pytest.raises(ComparisonValidationError):
        compare_reply("batch", source, candidate, target, decision)
