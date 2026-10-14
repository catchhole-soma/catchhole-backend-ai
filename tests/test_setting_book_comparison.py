import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
)
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)
from tests.test_character_fact_batch import _candidate, _decision_payload
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_setting_book_source_contract import SOURCE


@pytest.mark.parametrize("batch", [False, True])
def test_nonordered_world_book_recomparison_retains_document_context(batch):
    from app.schemas.worker import WorkerWorldSettingCandidatePayload
    source = WorkerAnalysisSource.model_validate(SOURCE)
    decision = {"operation": "ADD", "consolidation_status": "SINGLE", "target_ref": "T1", "proposed_scope_name": None,
        "proposed_setting_name": "수명", "proposed_value": "300년", "comparison_reason": "설정집에 수명이 명시되어 있습니다."}
    target = WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="엘프", properties=[], version=0)
    if batch:
        candidate = WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(),
            subject_name="엘프", setting_name="수명", extracted_value="300년", temporal_scope="PRESENT", evidence_spans=[{"quote": "수명: 300년"}],
            raw_extraction_json={"epistemic_status": "ASSERTED"})
        decision.update(source_candidate_refs=["C1"], consolidation_status="SINGLE")
        response = {"decisions": [decision]}
    else:
        candidate = WorkerWorldSettingCandidatePayload(candidate_id=uuid4(), work_id=uuid4(), source_episode_id=None,
            source=source, temporal_scope="PRESENT", category="RACE", subject_name="엘프", setting_name="수명", extracted_value="300년",
            evidence_spans=[{"quote": "수명: 300년"}], raw_extraction_json={"epistemic_status": "ASSERTED"})
        response = decision
    provider = RecordingClient(response)
    comparator = WorldSettingComparator(llm_client=provider, model="test-model", max_attempts=1)
    result, _ = asyncio.run(comparator.compare_batch("RACE", [candidate], [target], analysis_source=source)
        if batch else comparator.compare(candidate, [target]))
    assert (result.decisions[0] if batch else result).operation == "ADD"
    request = provider.requests[0]
    assert "설정집" in request["system_prompt"] and "미래" in request["system_prompt"]
    assert '"source_kind": "SETTING_BOOK"' in request["user_prompt"]
    assert str(source.setting_book_id) not in request["user_prompt"]


def test_book_character_segments_keep_source_on_projected_slots_and_dependencies():
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidates = [
        _candidate("C1", "Q1", "다리를 다쳤다").model_copy(update={"source_episode_no": None, "source": source}),
        _candidate("C2", "Q2", "더 크게 다쳤다").model_copy(update={"source_episode_no": None, "source": source}),
    ]
    provider = RecordingClient(
        {"decisions": [{**_decision_payload("C1", operation="ADD", resolved_key="status.다리_상태", value="다리를 다쳤다"), "effective_from_episode_no": 1}]},
        {"decisions": [{**_decision_payload("C2", operation="UPDATE", resolved_key="status.다리_상태", target_ref="Q1", value="더 크게 다쳤다"), "effective_from_episode_no": 2}]},
    )
    comparator = CharacterFactComparator(llm_client=provider, model="test-model", batch_max_candidates=1)
    result = asyncio.run(execute_character_fact_comparison_batch(comparator,
        matched_character_name="아린", canonical_fact_type="STATUS", candidates=candidates,
        snapshot_entries=[], ordered_context=True))
    assert len(result.decisions) == 2
    assert result.failures == []
    assert result.decisions[1].dependency_candidate_refs == ["C1"]
    second_prompt = json.loads(provider.requests[1]["user_prompt"])
    projected = second_prompt["snapshot_entries"][0]
    assert projected["source_kind"] == "SETTING_BOOK"
    assert projected["source_episode_no"] is None
    assert projected["source_setting_book_name"] == "인물 설정.docx"
    assert all('"source_kind": "SETTING_BOOK"' in request["user_prompt"] for request in provider.requests)
    assert all("설정집" in request["system_prompt"] and "미래" in request["system_prompt"] for request in provider.requests)
    assert str(source.setting_book_id) not in json.dumps(provider.requests, default=str)


def test_world_book_comparison_has_source_context_without_transport_ids():
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidate = WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(),
        subject_name="엘프", setting_name="수명", extracted_value="300년", temporal_scope="PRESENT", evidence_spans=[{"quote": "수명: 300년"}],
        raw_extraction_json={"epistemic_status": "ASSERTED"})
    target = WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="엘프", properties=[], version=0)
    provider = RecordingClient({"decisions": [{
        "source_candidate_refs": ["C1"], "target_ref": "T1",
        "operation": "ADD", "consolidation_status": "SINGLE",
        "matched_property_ref": None, "proposed_scope_name": None, "proposed_setting_name": "수명",
        "proposed_value": "300년", "existing_root_property_names_to_move": [],
        "review_reason": None, "comparison_reason": "설정집에 종족의 수명이 명시되어 있습니다.",
    }]})
    result, _raw = asyncio.run(WorldSettingComparator(llm_client=provider, model="test-model").compare_batch(
        "RACE", [candidate], [target], ordered_context=True, analysis_source=source))
    assert result.decisions[0].operation == "ADD"
    request = provider.requests[0]
    assert json.loads(request["user_prompt"])["source"] == {
        "source_kind": "SETTING_BOOK", "setting_book_name": "인물 설정.docx", "revision_no": 2,
        "reference_time_kind": "UNSPECIFIED", "reference_episode_no": None}
    assert "미래" in request["system_prompt"] and "설정집" in request["system_prompt"]
    assert str(source.setting_book_id) not in request["user_prompt"]
    assert str(candidate.candidate_id) not in request["user_prompt"]
    assert str(target.world_setting_id) not in request["user_prompt"]


def test_world_book_independent_recovery_keeps_frozen_source_in_every_request():
    from app.analysis.exceptions import ComparisonValidationError
    from app.analysis.world_setting_batch_recovery import recover_world_batch
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidates = [WorkerWorldSettingComparisonBatchCandidate(candidate_ref=f"C{index}", candidate_id=uuid4(),
        subject_name="엘프", setting_name=name, extracted_value=value, temporal_scope="PRESENT", evidence_spans=[{"quote": f"{name}: {value}"}],
        raw_extraction_json={"epistemic_status": "ASSERTED"})
        for index, (name, value) in enumerate([("수명", "300년"), ("키", "180cm")], 1)]
    responses = [{"decisions": [{"source_candidate_refs": [candidate.candidate_ref], "target_ref": "T1",
        "operation": "ADD", "consolidation_status": "SINGLE", "matched_property_ref": None,
        "proposed_scope_name": None, "proposed_setting_name": candidate.setting_name,
        "proposed_value": candidate.extracted_value, "existing_root_property_names_to_move": [],
        "review_reason": None, "comparison_reason": f"엘프의 {candidate.setting_name}이 명시되어 있습니다."}]}
        for candidate in candidates]
    provider = RecordingClient(*responses)
    result = asyncio.run(recover_world_batch(WorldSettingComparator(llm_client=provider, model="test-model"),
        "RACE", candidates, [WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="엘프", properties=[], version=0)],
        ComparisonValidationError("Synthetic invalid combined response"), analysis_source=source))
    assert len(result.decisions) == 2 and result.failures == [] and result.recovery_calls == 2
    assert all(json.loads(request["user_prompt"])["source"] == source.prompt_data() for request in provider.requests)
    assert all("미래" in request["system_prompt"] and "설정집" in request["system_prompt"] for request in provider.requests)


def test_book_character_batch_rejects_foreign_source_before_comparison():
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
    from app.analysis.exceptions import OrderedInputContextError
    from app.schemas.worker import WorkerCharacterFactComparisonBatchPayload
    from tests.test_character_fact_batch import FakeBatchSpring, _context
    from tests.test_ordered_analysis_runtime import _input_context
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidate = _candidate("C1", "Q1", "부상").model_copy(update={"source_episode_no": None, "source": source})
    batch = WorkerCharacterFactComparisonBatchPayload(comparison_batch_id=uuid4(), work_id=uuid4(), source_episode_id=None,
        source=source, character_ref="K1", matched_character_name="아린", canonical_fact_type="STATUS",
        analysis_context=_input_context(), candidates=[candidate])
    context = _context(batch).model_copy(update={"analysis_context": batch.analysis_context})
    provider = RecordingClient()
    pipeline = CharacterFactComparisonPipeline(FakeBatchSpring(batch, context), CharacterFactComparator(llm_client=provider, model="test-model"))
    with pytest.raises(OrderedInputContextError):
        asyncio.run(pipeline.process_all(uuid4(), uuid4(), analysis_context=batch.analysis_context,
            analysis_source=source.model_copy(update={"revision_id": uuid4()}), continue_on_candidate_failure=True))
    assert provider.requests == []


def test_book_character_subject_fallback_keeps_source_and_uses_adjacent_text():
    from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
    from app.analysis.ordered_character_subjects import (
        OrderedSubjectTarget,
        resolve_ordered_character_subjects,
    )
    from app.analysis.schemas import ExtractedSettingCandidate
    from app.schemas.analysis_context import AnalysisStateProvenance
    from tests.test_setting_extractor import _valid_setting_payload
    source = WorkerAnalysisSource.model_validate(SOURCE)
    data = _valid_setting_payload()
    data.update(entity_name="미상", raw_entity_mention="그", evidence_spans=[{"quote": "그는 강하다."}])
    character_id = uuid4()
    provider = RecordingClient({"resolutions": [{"candidate_ref": "C1", "target_ref": "K1"}]})
    resolved, _ = asyncio.run(resolve_ordered_character_subjects(client=provider, model="test-model", max_attempts=1,
        max_output_tokens=1000, context=SubjectResolutionChunkContext("아린에 관한 표", "그는 강하다.", None),
        candidates=[ExtractedSettingCandidate.model_validate(data)], episode_no=None, analysis_source=source,
        subjects=[OrderedSubjectTarget(name="아린", actual_character_id=character_id,
            provenance=AnalysisStateProvenance.from_source(confirmation_status="CONFIRMED", source=source))]))
    assert resolved[0].binding.actual_character_id == character_id
    prompt = json.loads(provider.requests[0]["user_prompt"])
    assert prompt["source"] == source.prompt_data()
    assert prompt["targets"][0]["source_kind"] == "SETTING_BOOK"
    assert "설정집" in provider.requests[0]["system_prompt"]
    assert str(character_id) not in provider.requests[0]["user_prompt"]
