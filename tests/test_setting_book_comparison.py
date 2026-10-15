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
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerEvidenceSpan,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)
from tests.test_character_fact_batch import _candidate, _decision_payload
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_setting_book_source_contract import SOURCE


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("operation,scope,value,quote", [
    ("ADD", "PRESENT", "아직 결정되지 않음",
     "정식 임명 여부는 아직 결정되지 않았으며, 다인은 임시라는 두 글자를 싫어하면서도 동료의 장부에는 그 두 글자를 정확히 남긴다."),
    ("EXCLUDE", "PRESENT", "아직 결정되지 않음", "정식 임명 여부는 아직 결정되지 않았다."),
    ("REVIEW_REQUIRED", "UNKNOWN", "정식 임명됐다는 소문",
     "다인이 정식 임명됐다는 소문만 있고 사실인지, 언제 일인지 알 수 없다."),
    ("REVIEW_REQUIRED", "HYPOTHETICAL", "정식 임명될 가능성",
     "다인이 다음 심사를 통과한다면 정식 임명될 수도 있다."),
], ids=["explicit_current_undecided", "repeated_undecided", "unverified_claim", "future_possibility"])
def test_book_character_fixed_replies_preserve_undecided_value_and_uncertainty_guards(
    batch, operation, scope, value, quote,
):
    # Fixed replies test typed value/evidence propagation and nonprojection, not model semantics.
    from app.analysis.character_fact_projection import CharacterProjectionEntry, CharacterProjectionState
    from app.schemas.analysis_context import AnalysisStateProvenance
    from app.schemas.worker import WorkerCharacterSnapshotEntry
    from tests.test_character_fact_comparator import _candidate as single_candidate

    source = WorkerAnalysisSource.model_validate(SOURCE)
    key = "profile.정식_임명_여부"
    occupation = "지난겨울 결원 자리를 메운 임시 수문 기록관"
    snapshots = [CharacterProjectionEntry(
        reference="P1", fact_type="PROFILE", fact_key="profile.occupation",
        fact_value=occupation, value_json={"value": occupation},
        provenance=AnalysisStateProvenance.from_source(confirmation_status="PROVISIONAL", source=source),
    )]
    if operation == "EXCLUDE":
        snapshots.append(CharacterProjectionEntry(
            reference="P2", fact_type="PROFILE", fact_key=key,
            fact_value="아직 결정되지 않음", value_json={"value": "아직 결정되지 않음"},
            provenance=snapshots[0].provenance,
        ))
    payload = {"operation": operation, "target_ref": None, "removed_snapshot_refs": [],
        "proposed_fact_value": value if operation == "ADD" else None,
        "proposed_value_json": {"value": value} if operation == "ADD" else None,
        "temporal_scope": scope, "comparison_reason": "현재 상태를 보존하거나 근거가 부족한 주장은 확인합니다."}
    if batch:
        candidate = WorkerCharacterFactComparisonBatchCandidate(
            candidate_ref="C1", projected_snapshot_ref="Q1", source=source,
            raw_fact_key=key, initial_canonical_fact_key=key, canonical_key_resolution="PATTERN",
            attribute_value=value, value_json={"value": value}, value_type="STRING", evidence_spans=[{"quote": quote}],
        )
        payload.update(candidate_ref="C1", resolved_canonical_fact_key=key)
        client = RecordingClient({"decisions": [payload]})
        result, _ = asyncio.run(CharacterFactComparator(llm_client=client, max_attempts=1).compare_batch(
            matched_character_name="강다인", canonical_fact_type="PROFILE", candidates=[candidate],
            snapshot_entries=snapshots, ordered_context=True,
        ))
        decision = result.decisions[0]
        input_candidate = json.loads(client.requests[0]["user_prompt"])["candidates"][0]
    else:
        candidate = single_candidate().model_copy(update={
            "source": source, "source_episode_id": None, "entity_name": "강다인", "matched_character_name": "강다인",
            "canonical_fact_type": "PROFILE", "canonical_fact_key": key, "attribute_name": key,
            "attribute_value": value, "value_json": {"value": value}, "value_type": "STRING",
            "evidence_spans": [WorkerEvidenceSpan(quote=quote)],
        })
        client = RecordingClient(payload)
        decision, _ = asyncio.run(CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            candidate, [WorkerCharacterSnapshotEntry(
                fact_type=x.fact_type, fact_key=x.fact_key, fact_value=x.fact_value,
                value_json=x.value_json, provenance=x.provenance,
            ) for x in snapshots],
        ))
        input_candidate = json.loads(client.requests[0]["user_prompt"])["candidate"]

    state = CharacterProjectionState(snapshots)
    application = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key=key, value_type=candidate.value_type, candidate_value_json=candidate.value_json,
        decision=decision, source=source)
    assert decision.operation == operation and decision.temporal_scope == scope
    assert input_candidate["attribute_value"] == value
    assert input_candidate["evidence_spans"][0]["quote"] == quote
    assert state.entries_by_ref["P1"].fact_value == occupation
    assert decision.target_ref is None and decision.removed_snapshot_refs == []
    assert decision.proposed_value_json == ({"value": "아직 결정되지 않음"} if operation == "ADD" else None)
    if operation == "ADD":
        assert application.projected_entry.fact_value == "아직 결정되지 않음"
    else:
        assert application.projected_entry is None and state.entries == snapshots


def test_book_character_internal_conflicts_are_not_projected_into_unrelated_candidate(monkeypatch):
    monkeypatch.setattr("app.analysis.character_fact_comparator._estimate_prompt_tokens",
        lambda system, user, model: 300 if len(json.loads(user)["candidates"]) > 2 else 100)
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidates = [WorkerCharacterFactComparisonBatchCandidate(
        candidate_ref=ref, projected_snapshot_ref=projected, source=source,
        raw_fact_key=key, initial_canonical_fact_key=key, canonical_key_resolution="EXACT",
        attribute_value=str(value), value_json={"value": value}, value_type=value_type,
        evidence_spans=[{"quote": quote}],
    ) for ref, projected, key, value, value_type, quote in [
        ("C1", "Q1", "profile.height", 168, "NUMBER", "인물 소개: 아린의 키는 168cm이다."),
        ("C2", "Q2", "profile.height", 178, "NUMBER", "외형 정리: 아린의 키는 178cm이다."),
        ("C3", "Q3", "profile.favorite_food", "사과", "STRING", "아린은 사과를 좋아한다."),
    ]]
    reason = "같은 설정집의 키가 서로 달라 두 근거 모두 확인이 필요합니다."
    provider = RecordingClient({"decisions": [{
        "candidate_ref": ref, "resolved_canonical_fact_key": "profile.height",
        "operation": "REVIEW_REQUIRED", "target_ref": None, "removed_snapshot_refs": [],
        "proposed_fact_value": None, "proposed_value_json": None, "temporal_scope": "PRESENT",
        "comparison_reason": reason,
    } for ref in ["C1", "C2"]]}, {"decisions": [{
        "candidate_ref": "C3", "resolved_canonical_fact_key": "profile.favorite_food",
        "operation": "ADD", "target_ref": None, "removed_snapshot_refs": [],
        "proposed_fact_value": "사과", "proposed_value_json": {"value": "사과"},
        "temporal_scope": "PRESENT", "comparison_reason": "선호 음식은 키와 무관한 새 설정입니다.",
    }]})

    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=1, batch_max_input_tokens=200),
        matched_character_name="아린", canonical_fact_type="PROFILE", candidates=candidates,
        snapshot_entries=[], ordered_context=True,
    ))

    assert result.failures == []
    assert [decision.operation for decision in result.decisions] == ["REVIEW_REQUIRED", "REVIEW_REQUIRED", "ADD"]
    assert [decision.dependency_candidate_refs for decision in result.decisions] == [[], [], []]
    assert [decision.comparison_reason for decision in result.decisions[:2]] == [reason, reason]
    assert all(decision.proposed_fact_value is None and decision.proposed_value_json is None
               for decision in result.decisions[:2])
    assert result.decisions[2].proposed_fact_value == "사과"
    assert result.provider_segment_count == 2 and result.singleton_fallback_count == 0
    assert json.loads(provider.requests[1]["user_prompt"])["snapshot_entries"] == []


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    ("operation", "before", "value", "proposal", "quote", "reason"),
    [
        ("REVIEW_REQUIRED", "모든 마법은 주문이 필요하다.", "모든 마법은 주문 없이 쓸 수 있다.", "임의 선택값",
         "모든 마법은 주문 없이 쓸 수 있다.", "같은 조건의 주문 규칙이 양립할 수 없어 확인이 필요합니다."),
        ("REVIEW_REQUIRED", "모든 마법은 주문이 필요하다.", "모든 마법은 주문 없이 쓸 수 있다.", "임의 선택값",
         "기존 주문 규칙은 오류다. 모든 마법은 주문 없이 쓸 수 있다.", "기존 규칙을 정정하려는 주장으로 작가의 확인이 필요합니다."),
        ("MERGE", "마법은 주문이 필요하다.", "치유 마법은 주문 없이 쓴다.", "마법은 주문이 필요하되 치유 마법만 명시적 예외로 주문 없이 쓴다.",
         "치유 마법에만 일반 규칙의 명시적 예외를 적용한다.", "일반 규칙과 치유 마법에만 적용되는 예외를 함께 보존합니다."),
        ("EXCLUDE", "마법은 주문이 필요하다.", "마법을 쓸 때 주문을 외운다.", "임의 요약값",
         "마법을 쓸 때 주문을 외운다.", "이미 저장된 주문 규칙과 같은 내용입니다."),
    ],
    ids=["value_difference", "correction_declaration", "bounded_exception", "paraphrase"],
)
def test_world_book_fixed_policy_response_preserves_review_and_compatible_values(
    batch, operation, before, value, proposal, quote, reason,
):
    # Fixed replies prove DTO propagation and original-value guards; semantic judgement is separate.
    source = WorkerAnalysisSource.model_validate({**SOURCE, "referenceTimeKind": "EPISODE", "referenceEpisodeNo": 2})
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=uuid4(), subject_name="마법", version=1,
        properties=[{"setting_name": "주문 필요 여부", "value": before,
                     "provenance": {"confirmationStatus": "CONFIRMED", "sourceEpisodeNo": 1}}],
    )
    candidate_fields = {"subject_name": "마법", "setting_name": "주문 필요 여부", "extracted_value": value,
        "temporal_scope": "PRESENT", "evidence_spans": [{"quote": quote}],
        "raw_extraction_json": {"epistemic_status": "ASSERTED"}}
    payload = {"operation": operation, "consolidation_status": "SINGLE", "target_ref": "T1",
        "matched_scope_name": None, "matched_property_name": "주문 필요 여부", "proposed_scope_name": None,
        "proposed_setting_name": "주문 필요 여부", "proposed_value": proposal, "comparison_reason": reason,
        "review_reason": "GENERAL_UNCERTAINTY" if operation == "REVIEW_REQUIRED" else None}
    if batch:
        candidate = WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(), **candidate_fields)
        payload.update(source_candidate_refs=["C1"], existing_root_property_names_to_move=[])
        response = {"decisions": [payload]}
    else:
        candidate = WorkerWorldSettingCandidatePayload(candidate_id=uuid4(), work_id=uuid4(), source=source,
            source_episode_id=None, category="POWER_SYSTEM", **candidate_fields)
        response = payload
    provider = RecordingClient(response)
    comparator = WorldSettingComparator(llm_client=provider, model="test-model", max_attempts=1)
    result, _ = asyncio.run(comparator.compare_batch("POWER_SYSTEM", [candidate], [target], analysis_source=source)
        if batch else comparator.compare(candidate, [target]))
    decision = result.decisions[0] if batch else result

    assert decision.operation == operation
    assert decision.consolidation_status == "SINGLE"
    assert decision.review_reason == ("GENERAL_UNCERTAINTY" if operation == "REVIEW_REQUIRED" else None)
    assert decision.target_ref == "T1" and decision.matched_property_name == "주문 필요 여부"
    assert decision.proposed_value == (proposal if operation == "MERGE" else value)
    assert decision.comparison_reason == reason
    assert target.properties[0].value == before
    if batch:
        assert decision.source_candidate_refs == ["C1"]
        assert decision.existing_root_property_names_to_move == []


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


def test_book_character_segments_keep_source_on_projected_slots_and_dependencies(monkeypatch):
    monkeypatch.setattr("app.analysis.character_fact_comparator._estimate_prompt_tokens",
        lambda system, user, model: 300 if len(json.loads(user)["candidates"]) > 1 else 100)
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidates = [
        _candidate("C1", "Q1", "다리를 다쳤다").model_copy(update={"source_episode_no": None, "source": source}),
        _candidate("C2", "Q2", "더 크게 다쳤다").model_copy(update={"source_episode_no": None, "source": source}),
    ]
    provider = RecordingClient(
        {"decisions": [{**_decision_payload("C1", operation="ADD", resolved_key="status.다리_상태", value="다리를 다쳤다"), "effective_from_episode_no": 1}]},
        {"decisions": [{**_decision_payload("C2", operation="UPDATE", resolved_key="status.다리_상태", target_ref="Q1", value="더 크게 다쳤다"), "effective_from_episode_no": 2}]},
    )
    comparator = CharacterFactComparator(llm_client=provider, model="test-model", batch_max_input_tokens=200)
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
    initial = ComparisonValidationError("Synthetic invalid independent decisions")
    initial.world_batch_response_payload = {"decisions": [
        {**response["decisions"][0], "operation": "EXCLUDE", "matched_property_ref": "T1.P99"}
        for response in responses]}
    initial.world_batch_request_payload = {"targets": [{"ref": "T1", "properties": []}]}
    result = asyncio.run(recover_world_batch(WorldSettingComparator(llm_client=provider, model="test-model"),
        "RACE", candidates, [WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="엘프", properties=[], version=0)],
        initial, ordered_context=True, analysis_source=source))
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
