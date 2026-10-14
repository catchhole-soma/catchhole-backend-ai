"""Persisted epistemic marker never relies on process-local state."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingComparator, WorldSettingSubjectResolver
from app.analysis.world_setting_schemas import BookExtractedWorldSettingCandidate
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper
from app.models.episode_chunk import EpisodeChunk
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import WorkerWorldSettingComparisonBatchCandidate, WorkerWorldSettingSubjectResolutionCandidate
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import SOURCE


def candidate(raw):
    return WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(),
        subject_name="검은 깃", setting_name="정체 주장", extracted_value="사람인지 배인지 미확인",
        evidence_spans=[{"quote": "검은 깃의 정체는 미확인이다."}], temporal_scope="PRESENT",
        rawExtractionJson=raw)


def decision(operation, *, ref="C1"):
    return {"source_candidate_refs": [ref], "target_ref": None, "operation": operation,
        "consolidation_status": "SINGLE", "matched_property_ref": None,
        "proposed_scope_name": None, "proposed_setting_name": "정체 주장",
        "proposed_value": "사람인지 배인지 미확인", "existing_root_property_names_to_move": [],
        "review_reason": "GENERAL_UNCERTAINTY" if operation == "REVIEW_REQUIRED" else None,
        "comparison_reason": "정체에 관한 서로 다른 주장을 확인해야 합니다."}


@pytest.mark.parametrize("raw", [None, {}, {"epistemic_status": None},
    {"epistemic_status": "invented"}, {"epistemic_status": "UNRESOLVED_CLAIM"},
    {"epistemic_status": "ASSERTED", "sourceCandidates": []},
    {"epistemic_status": "ASSERTED", "sourceCandidates": [None]},
    {"epistemic_status": "ASSERTED", "sourceCandidates": [{"epistemic_status": "ASSERTED"}, {}]},
    {"epistemic_status": "ASSERTED", "sourceCandidates": [{"epistemic_status": "UNRESOLVED_CLAIM"}]},
])
@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE"])
def test_persisted_book_unresolved_or_missing_marker_rejects_concrete_operations(raw, operation):
    client = RecordingClient({"decisions": [decision(operation)]})
    with pytest.raises(ComparisonValidationError):
        asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=1).compare_batch(
            "WORLD_RULE_HISTORY", [candidate(raw)], [], ordered_context=True,
            analysis_source=WorkerAnalysisSource.model_validate(SOURCE)))


def test_book_unresolved_claim_retries_as_valid_review_with_original_value():
    client = RecordingClient({"decisions": [decision("ADD")]}, {"decisions": [decision("REVIEW_REQUIRED")]})
    result, _ = asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=2).compare_batch(
        "WORLD_RULE_HISTORY", [candidate({"epistemic_status": "UNRESOLVED_CLAIM"})], [], ordered_context=True,
        analysis_source=WorkerAnalysisSource.model_validate(SOURCE)))
    assert len(client.requests) == 2
    assert result.decisions[0].operation == "REVIEW_REQUIRED"
    payload = json.loads(client.requests[1]["user_prompt"])
    assert payload["validation_feedback"]["reason_code"] == "BOOK_CLAIM_REQUIRES_REVIEW"
    assert payload["candidates"][0]["epistemic_status"] == "UNRESOLVED_CLAIM"


def test_asserted_current_origin_still_allows_new_current_fact_and_ignores_unrelated_json_keys():
    client = RecordingClient({"decisions": [decision("ADD")]})
    raw = {"epistemic_status": "ASSERTED", "value_json": {"epistemic_status": "UNRESOLVED_CLAIM"}}
    result, _ = asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=1).compare_batch(
        "WORLD_RULE_HISTORY", [candidate(raw)], [], ordered_context=True,
        analysis_source=WorkerAnalysisSource.model_validate(SOURCE)))
    assert result.decisions[0].operation == "ADD"


def test_book_unresolved_topic_creates_no_selectable_subject_and_never_calls_identity_provider():
    client = RecordingClient({"selected_subject_refs": [], "ambiguous": False})
    raw = {"epistemic_status": "UNRESOLVED_CLAIM"}
    item = WorkerWorldSettingSubjectResolutionCandidate(candidate_id=uuid4(), category="WORLD_RULE_HISTORY",
        subject_name="검은 깃", evidence_spans=[{"quote": "검은 깃의 정체는 미확인이다."}],
        source=WorkerAnalysisSource.model_validate(SOURCE), rawExtractionJson=raw)
    selected, ambiguous = asyncio.run(WorldSettingSubjectResolver(llm_client=client, model="test-model").select_ordered_subjects(item, []))
    assert selected == [] and ambiguous is True
    assert client.requests == []


def test_consolidation_propagates_every_source_marker_and_keeps_independent_sibling():
    chunk = EpisodeChunk(id=uuid4(), episode_id=uuid4(), chunk_index=0,
        chunk_text="깃은 붉다. 정체는 미확인이다. 기원은 고대 의식이다.", start_offset=0, end_offset=36)
    def publish(name, value, quote, marker):
        item = BookExtractedWorldSettingCandidate(category="WORLD_RULE_HISTORY", subject_name="검은 깃",
            setting_name=name, extracted_value=value, evidence_spans=[{"quote": quote}], confidence=0.95,
            temporal_scope="PRESENT", effective_from_episode_no=None, epistemic_status=marker)
        return WorldSettingCandidateMapper.to_publish_item(item, chunk)
    items = [publish("색", "붉다", "깃은 붉다.", "ASSERTED"),
             publish("색", "색과 정체 미확인", "정체는 미확인이다.", "UNRESOLVED_CLAIM"),
             publish("기원", "고대 의식", "기원은 고대 의식이다.", "ASSERTED")]
    merged, sibling = WorldSettingCandidateMapper.consolidate_by_key(items)
    assert merged.raw_extraction_json.get("epistemic_status") == "UNRESOLVED_CLAIM"
    assert [v.get("epistemic_status") for v in merged.raw_extraction_json["sourceCandidates"]] == ["ASSERTED", "UNRESOLVED_CLAIM"]
    assert sibling.raw_extraction_json.get("epistemic_status") == "ASSERTED"


def test_unresolved_review_cannot_swallow_an_independent_asserted_sibling():
    first = candidate({"epistemic_status": "UNRESOLVED_CLAIM"})
    sibling = WorkerWorldSettingComparisonBatchCandidate.model_validate({
        **candidate({"epistemic_status": "ASSERTED"}).model_dump(),
        "candidate_ref": "C2", "setting_name": "유래", "extracted_value": "옛 의식",
        "evidence_spans": [{"quote": "유래는 옛 의식이다."}]})
    invalid = {**decision("REVIEW_REQUIRED"), "source_candidate_refs": ["C1", "C2"]}
    asserted_add = {**decision("ADD", ref="C2"), "proposed_setting_name": "유래", "proposed_value": "옛 의식"}
    client = RecordingClient({"decisions": [invalid]}, {"decisions": [decision("REVIEW_REQUIRED"), asserted_add]})
    result, _ = asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=2).compare_batch(
        "WORLD_RULE_HISTORY", [first, sibling], [], ordered_context=True,
        analysis_source=WorkerAnalysisSource.model_validate(SOURCE)))
    assert len(client.requests) == 2
    assert [item.operation for item in result.decisions] == ["REVIEW_REQUIRED", "ADD"]
    assert [item.source_candidate_refs for item in result.decisions] == [["C1"], ["C2"]]
