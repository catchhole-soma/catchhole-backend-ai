"""Missing general quotes must not become CURRENT/identity proof on replay."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.analysis.narrative_time import narrative_prompt_data, project_advisory_narrative_time
from app.analysis.ordered_character_subjects import initial_ordered_subjects
from app.analysis.ordered_context import OrderedExtractionContext
from app.analysis.semantic_current_context import SemanticCurrentContext, prepare_semantic_current_context
from app.schemas.analysis_context import WorkerAnalysisContext, WorkerAnalysisReference
from app.schemas.worker import (
    WorkerAnalysisKnownCharacterPayload, WorkerAnalysisProvisionalCharacterPayload,
    WorkerCharacterFactComparisonBatchCandidate, WorkerCharacterFactComparisonCandidatePayload,
    WorkerEvidenceSpan,
)

KNOWN = UUID(int=1)
RUN = UUID(int=2)
PROVISIONAL = "provisional-character:00000000-0000-0000-0000-000000000003"
LOCATED = {"quote": "😀의 기록", "startOffset": 11, "endOffset": 16}
UNLOCATED = {"quote": "확인할 수 없는 인용", "startOffset": None, "endOffset": None}


@pytest.mark.parametrize("spans,reason", [
    ([UNLOCATED], "OFFSETS_UNAVAILABLE"),
    ([{"quote": "불완전", "startOffset": 1, "endOffset": None}], "INVALID_OFFSETS"),
    ([{"quote": "길이 불일치", "startOffset": 1, "endOffset": 2}], "INVALID_OFFSETS"),
    ([UNLOCATED, {"quote": "역순", "startOffset": 3, "endOffset": 1}], "EVIDENCE_UNAVAILABLE"),
    ([], "EVIDENCE_UNAVAILABLE"),
])
def test_legacy_verified_current_is_only_downgraded_in_provider_projection(spans, reason):
    frozen = {"currentSlots": [{"factValue": "기존 현재값", "supportingSources": [{
        "availability": "VERIFIED", "unavailableReason": None, "evidenceSpans": spans,
        "quoteRelation": "SOURCE_CONTEXT_ONLY", "sourceCandidateId": str(KNOWN),
        "sourceHash": "a" * 64,
    }]}]}
    before = deepcopy(frozen)
    projected = narrative_prompt_data(frozen)
    source = projected["currentSlots"][0]["supportingSources"][0]
    assert projected["currentSlots"][0]["factValue"] == "기존 현재값"
    assert source["availability"] == "UNAVAILABLE"
    assert source["unavailableReason"] == reason
    assert source["evidenceSpans"] == []
    assert source["sourceRef"] == "S1"
    assert frozen == before
    assert "확인할 수 없는 인용" not in json.dumps(projected, ensure_ascii=False)


def test_partial_current_keeps_only_located_quote_and_unavailable_never_regains_it():
    source = {"availability": "VERIFIED", "evidenceSpans": [UNLOCATED, LOCATED]}
    assert narrative_prompt_data(source)["evidenceSpans"] == [LOCATED]
    unavailable = {**source, "availability": "UNAVAILABLE", "unavailableReason": "INVALID_OFFSETS"}
    result = narrative_prompt_data(unavailable)
    assert result["evidenceSpans"] == []
    assert result["availability"] == "UNAVAILABLE"
    assert result["unavailableReason"] == "INVALID_OFFSETS"
    assert len(source["evidenceSpans"]) == 2


@pytest.mark.parametrize("operation", ["ADD", "MERGE", "HISTORY_ONLY"])
def test_local_batch_projection_cannot_reintroduce_unlocated_candidate_quote(operation):
    candidate = SimpleNamespace(candidate_ref="C1", source=None, narrative_time=None,
        evidence_spans=[WorkerEvidenceSpan.model_validate(UNLOCATED)],
        attribute_value="기존 정보", value_json={"value": "기존 정보"})
    decision = SimpleNamespace(advance_current_narrative_time=False, operation=operation,
        removed_snapshot_refs=[], target_ref=None, resolved_canonical_fact_key="profile.역할",
        proposed_fact_value="기존 정보", proposed_value_json={"value": "기존 정보"}, temporal_scope="PRESENT")
    result = project_advisory_narrative_time({}, candidate, decision, fact_type="PROFILE")
    source = (result["historyFacts"][0]["supportingSource"] if operation == "HISTORY_ONLY"
              else result["currentSlots"][0]["supportingSources"][0])
    assert source["evidenceSpans"] == []
    assert source["availability"] == "UNAVAILABLE"
    assert source["unavailableReason"] == "OFFSETS_UNAVAILABLE"
    assert candidate.evidence_spans[0].quote == UNLOCATED["quote"]


def test_frozen_identity_and_pending_reference_hide_unlocated_quotes_without_mutation():
    evidence = [UNLOCATED, LOCATED]
    known = WorkerAnalysisKnownCharacterPayload.model_validate({
        "characterId": str(KNOWN), "name": "새벽", "identityEvidence": evidence})
    provisional = WorkerAnalysisProvisionalCharacterPayload.model_validate({
        "provisionalSubjectKey": PROVISIONAL, "name": "노을", "sourceEpisodeNo": 1,
        "identityEvidence": evidence})
    reference = WorkerAnalysisReference.model_validate({"domain": "characters", "subjectName": "새벽",
        "sourceEpisodeNo": 1, "reason": "판단 보류", "value": "기존 보류값", "evidenceSpans": evidence})
    context = WorkerAnalysisContext.model_validate({"runId": str(RUN), "generation": 1,
        "inputStateHash": "a" * 64, "sourceHash": "b" * 64, "formatVersion": 1,
        "unresolvedReferences": [reference.model_dump(by_alias=True)]})
    before = [item.model_dump_json() for item in (known, provisional, context)]
    ordered = OrderedExtractionContext(context, (known,), (provisional,))
    assert ordered.prompt_data()["characters"][0]["identity_evidence"] == [LOCATED["quote"]]
    assert [target.evidence for target in initial_ordered_subjects(ordered)] == [(LOCATED["quote"],)] * 2
    pending = reference.prompt_data()
    assert pending["evidence"] == [LOCATED["quote"]]
    assert pending["value"] == "기존 보류값"
    assert pending["confirmation_status"] == "UNRESOLVED"
    assert pending["applied_to_current_state"] is False
    carrier = SemanticCurrentContext.model_validate({"formatVersion": 1, "runId": str(RUN),
        "generation": 1, "inputStateHash": "a" * 64, "sourceHash": "b" * 64, "complete": True,
        "characters": [{"characterId": str(KNOWN), "provisionalSubjectKey": None, "currentSlots": []},
                       {"characterId": None, "provisionalSubjectKey": PROVISIONAL, "currentSlots": []}],
        "worldSubjects": []})
    prepared = prepare_semantic_current_context(carrier, analysis_context=context, known_characters=[known],
        provisional_characters=[provisional], source_hash="b" * 64)
    assert [item["identity_evidence"] for item in json.loads(prepared.character_prompt_json())["characters"]] == [
        [LOCATED["quote"]], [LOCATED["quote"]]]
    assert [item.model_dump_json() for item in (known, provisional, context)] == before


def test_character_comparison_transport_accepts_empty_quotes_without_changing_value():
    batch = WorkerCharacterFactComparisonBatchCandidate.model_validate({"candidateRef": "C1",
        "projectedSnapshotRef": "Q1", "sourceEpisodeNo": 1, "attributeValue": "내용",
        "valueType": "STRING", "evidenceSpans": [], "rawFactKey": "profile.역할",
        "initialCanonicalFactKey": "profile.역할", "canonicalKeyResolution": "EXACT"})
    single = WorkerCharacterFactComparisonCandidatePayload.model_validate({"candidateId": str(KNOWN),
        "workId": str(RUN), "entityName": "새벽", "attributeName": "profile.역할",
        "attributeValue": "내용", "evidenceSpans": [], "matchedCharacterId": str(KNOWN),
        "matchedCharacterName": "새벽", "canonicalFactType": "PROFILE", "canonicalFactKey": "profile.역할"})
    assert batch.evidence_spans == single.evidence_spans == []
    assert batch.attribute_value == single.attribute_value == "내용"
