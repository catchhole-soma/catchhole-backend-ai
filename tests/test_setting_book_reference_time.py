"""Book reference time is source data, never an episode or permission to overwrite."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonDecision
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
)
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.clients.spring_worker_client import SpringWorkerClient
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper
from app.models.episode_chunk import EpisodeChunk
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerAnalysisJobPayload,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)
from app.worker.analysis_job_worker import AnalysisJobWorker
from app.worker.character_fact_comparison_worker import CharacterFactComparisonWorker
from app.worker.world_setting_comparison_worker import WorldSettingComparisonWorker
from tests.test_character_fact_batch import _candidate, _decision_payload
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_setting_book_source_contract import SOURCE, book_payload_data


def book_source(kind="WORK_START", number=None):
    return WorkerAnalysisSource.model_validate({
        **SOURCE, "referenceTimeKind": kind, "referenceEpisodeNo": number,
    })


@pytest.mark.parametrize("kind,number", [
    ("EPISODE", None), ("EPISODE", 0), ("EPISODE", -1),
    ("WORK_START", 3), ("UNSPECIFIED", 3), (None, None), ("UPLOAD_TIME", None),
])
def test_book_reference_pair_rejects_invalid_time_before_analysis(kind, number):
    data = book_payload_data()
    data["settingBook"].update(referenceTimeKind=kind, referenceEpisodeNo=number)
    data["analysisContext"].update(referenceTimeKind=kind, referenceEpisodeNo=number)
    with pytest.raises(ValidationError):
        WorkerAnalysisJobPayload.model_validate(data)
    with pytest.raises(ValidationError):
        book_source(kind, number)


@pytest.mark.parametrize("kind,number", [("UNSPECIFIED", None), ("WORK_START", None), ("EPISODE", 7)])
def test_book_reference_survives_claim_source_provenance_without_episode(kind, number):
    data = book_payload_data()
    data["settingBook"].update(referenceTimeKind=kind, referenceEpisodeNo=number)
    data["analysisContext"].update(referenceTimeKind=kind, referenceEpisodeNo=number)
    payload = WorkerAnalysisJobPayload.model_validate(data)
    source = payload.frozen_source()
    assert source.prompt_data()["reference_time_kind"] == kind
    assert source.prompt_data()["reference_episode_no"] == number
    assert source.episode_id is None and source.episode_no is None
    assert payload.setting_book.prompt_data()["reference_time_kind"] == kind
    provenance = AnalysisStateProvenance.from_source(confirmation_status="PROVISIONAL", source=source)
    assert provenance.prompt_data()["source_reference_time_kind"] == kind
    assert provenance.prompt_data()["source_reference_episode_no"] == number
    assert provenance.source_episode_no is None
    assert str(source.revision_id) not in json.dumps(provenance.prompt_data())


def test_reference_change_cannot_equal_a_frozen_source_with_the_same_raw_hash():
    start = book_source()
    later = book_source("EPISODE", 7)
    assert start.content_hash == later.content_hash
    assert start.identity() != later.identity()


def test_book_claim_rejects_reference_mismatched_with_frozen_ordered_context():
    data = book_payload_data()
    data["settingBook"].update(referenceTimeKind="EPISODE", referenceEpisodeNo=7)
    data["analysisContext"].update(referenceTimeKind="WORK_START", referenceEpisodeNo=None)
    with pytest.raises(ValidationError):
        WorkerAnalysisJobPayload.model_validate(data)


def test_legacy_episode_provenance_keeps_its_original_prompt_and_no_book_reference():
    provenance = AnalysisStateProvenance(confirmation_status="CONFIRMED", source_episode_no=7)
    assert provenance.prompt_data() == {"confirmation_status": "CONFIRMED", "source_episode_no": 7}
    source = WorkerAnalysisSource(source_kind="EPISODE", episode_id=uuid4(), episode_no=7)
    assert source.prompt_data() == {"source_kind": "EPISODE", "episode_no": 7}
    with pytest.raises(ValidationError):
        WorkerAnalysisSource(source_kind="EPISODE", episode_id=uuid4(), episode_no=7,
                             referenceTimeKind="WORK_START")


@pytest.mark.parametrize("worker_type", [AnalysisJobWorker, CharacterFactComparisonWorker, WorldSettingComparisonWorker])
def test_every_real_worker_claim_requires_reference_time_capability(worker_type):
    requests = []

    async def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    async def scenario():
        client = SpringWorkerClient("http://test.invalid", "test-only",
                                    httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        worker = worker_type(spring_client=client)
        try:
            await worker.claim_next()
            await client.claim(allowed_job_types=["SETTING_EXTRACTION"])
        finally:
            await worker.aclose()
            await client.aclose()

    asyncio.run(scenario())
    assert requests[0]["supportedSourceKinds"] == ["EPISODE", "SETTING_BOOK"]
    assert requests[0]["supportsSettingBookReferenceTime"] is True
    assert "supportsSettingBookReferenceTime" not in requests[1]


def world_candidate_data():
    return {"category": "WORLD_RULE_HISTORY", "subject_name": "세계", "scope_name": None,
            "setting_name": "부활", "extracted_value": "부활할 수 없다", "confidence": 0.9,
            "evidence_spans": [{"quote": "부활할 수 없다"}]}


def test_book_world_extraction_retries_missing_temporal_metadata_and_preserves_future_through_publish():
    data = book_payload_data()
    data["settingBook"].update(referenceTimeKind="EPISODE", referenceEpisodeNo=3)
    data["analysisContext"].update(referenceTimeKind="EPISODE", referenceEpisodeNo=3)
    book = WorkerAnalysisJobPayload.model_validate(data).setting_book
    raw = world_candidate_data()
    provider = RecordingClient({"candidates": [raw]}, {"candidates": [{
        **raw, "temporal_scope": "HYPOTHETICAL", "effective_from_episode_no": 8,
        "epistemic_status": "ASSERTED",
    }]})
    result = asyncio.run(WorldSettingExtractor(llm_client=provider, model="test-model", max_attempts=2)
                         .extract_from_chunk("8화 이후 부활할 수 없다", setting_book=book))
    assert len(provider.requests) == 2
    chunk = EpisodeChunk(id=uuid4(), episode_id=uuid4(), chunk_index=0,
                         chunk_text="8화 이후 부활할 수 없다", start_offset=0, end_offset=14)
    published = WorldSettingCandidateMapper.to_publish_item(result.candidates[0], chunk)
    wire = published.model_dump(by_alias=True, exclude_none=False)
    assert wire["temporalScope"] == "HYPOTHETICAL"
    assert wire["effectiveFromEpisodeNo"] == 8
    metadata = json.loads(provider.requests[0]["user_prompt"])["metadata"]
    assert metadata["reference_episode_no"] == 3
    assert "episode_no" not in metadata


def test_world_legacy_response_keeps_present_but_book_missing_time_is_unknown():
    payload = {"candidateId": str(uuid4()), "workId": str(uuid4()), "category": "WORLD_RULE_HISTORY",
               "subjectName": "세계", "settingName": "부활", "extractedValue": "없다",
               "evidenceSpans": [{"quote": "없다"}]}
    episode = WorkerWorldSettingCandidatePayload.model_validate({**payload, "sourceEpisodeId": str(uuid4())})
    book = WorkerWorldSettingCandidatePayload.model_validate({**payload, "sourceEpisodeId": None, "source": SOURCE})
    assert episode.temporal_scope == "PRESENT"
    assert book.temporal_scope == "UNKNOWN"


def current_entry(*, episode_no=7, protected=False, review_source="AUTOMATIC"):
    return CharacterProjectionEntry(reference="P1", fact_type="PROFILE", fact_key="profile.힘",
        fact_value="20", value_json={"value": 20}, provenance=AnalysisStateProvenance.model_validate({
            "confirmationStatus": "CONFIRMED", "sourceKind": "EPISODE", "sourceEpisodeNo": episode_no,
            "reviewSource": review_source, "currentValueProtected": protected,
        }))


def apply_number(state, source, *, operation="UPDATE", target_ref="P1", value=10):
    return state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": value},
        source=source, decision=CharacterFactComparisonDecision(operation=operation, target_ref=target_ref,
            temporal_scope="PRESENT", proposed_fact_value=str(value), proposed_value_json={"value": value},
            comparison_reason="원문에는 다른 수치가 명시되어 있습니다."))


@pytest.mark.parametrize("kind,number", [("WORK_START", None), ("EPISODE", 3), ("UNSPECIFIED", None)])
def test_later_episode_current_cannot_be_replaced_in_memory_by_earlier_or_unanchored_book(kind, number):
    current = current_entry()
    state = CharacterProjectionState([current])
    with pytest.raises(ValueError, match="reference time"):
        apply_number(state, book_source(kind, number))
    assert state.entries == [current]


def test_persisted_protection_prevents_book_projection_without_treating_all_human_review_as_protected():
    protected = current_entry(protected=True)
    state = CharacterProjectionState([protected])
    with pytest.raises(ValueError, match="protected current"):
        apply_number(state, book_source("EPISODE", 8))
    assert state.entries == [protected]
    unprotected_human_review = CharacterProjectionState([current_entry(review_source="HUMAN")])
    result = apply_number(unprotected_human_review, book_source("EPISODE", 8))
    assert result.projected_entry.fact_value == "10"


@pytest.mark.parametrize("current_kind,current_no,incoming_kind,incoming_no,allowed", [
    ("EPISODE", 7, "EPISODE", 7, False),
    ("EPISODE", 7, "EPISODE", 8, True),
    ("WORK_START", None, "WORK_START", None, False),
    ("WORK_START", None, "EPISODE", 1, True),
    ("UNSPECIFIED", None, "EPISODE", 8, False),
])
def test_book_to_book_conflicts_require_strictly_later_known_time(current_kind, current_no, incoming_kind, incoming_no, allowed):
    current = current_entry()
    current = CharacterProjectionEntry(**{**current.__dict__, "provenance": AnalysisStateProvenance.from_source(
        confirmation_status="CONFIRMED", source=book_source(current_kind, current_no),
    )})
    state = CharacterProjectionState([current])
    if allowed:
        assert apply_number(state, book_source(incoming_kind, incoming_no)).projected_entry.fact_value == "10"
    else:
        with pytest.raises(ValueError, match="reference time"):
            apply_number(state, book_source(incoming_kind, incoming_no))
        assert state.entries == [current]


@pytest.mark.parametrize("episode_no,allowed", [(6, False), (7, False), (8, True)])
def test_episode_after_book_cannot_rollback_same_or_later_book_current(episode_no, allowed):
    current = current_entry()
    current = CharacterProjectionEntry(**{**current.__dict__, "provenance": AnalysisStateProvenance.from_source(
        confirmation_status="CONFIRMED", source=book_source("EPISODE", 7),
    )})
    state = CharacterProjectionState([current])
    source = WorkerAnalysisSource(source_kind="EPISODE", episode_id=uuid4(), episode_no=episode_no)
    if allowed:
        assert apply_number(state, source).projected_entry.fact_value == "10"
    else:
        with pytest.raises(ValueError, match="reference time"):
            apply_number(state, source)
        assert state.entries == [current]


def test_book_new_attribute_and_duplicate_exclusion_remain_eligible():
    state = CharacterProjectionState([])
    assert apply_number(state, book_source(), operation="ADD", target_ref=None).projected_entry.fact_value == "10"
    current = current_entry(protected=True)
    state = CharacterProjectionState([current])
    application = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 20},
        source=book_source(), decision=CharacterFactComparisonDecision(operation="EXCLUDE",
            temporal_scope="PRESENT", comparison_reason="기존 수치와 같습니다."))
    assert application.projected_entry is None and state.entries == [current]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE"])
@pytest.mark.parametrize("scope,effective,kind,number", [
    ("HYPOTHETICAL", 8, "EPISODE", 3), ("PAST", 1, "EPISODE", 3),
    ("UNKNOWN", None, "UNSPECIFIED", None), ("PRESENT", 8, "EPISODE", 3),
    ("PRESENT", 1, "WORK_START", None),
])
def test_world_noncurrent_or_anchor_conflict_preserves_review_instead_of_applying_or_dropping(batch, operation, scope, effective, kind, number):
    source = book_source(kind, number)
    payload = {"subject_name": "세계", "setting_name": "부활", "extracted_value": "가능",
               "evidence_spans": [{"quote": "8화 이후 가능해질 예정이다"}],
               "temporal_scope": scope, "effective_from_episode_no": effective,
               "rawExtractionJson": {"epistemic_status": "ASSERTED"}}
    if batch:
        candidate = WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(), **payload)
    else:
        candidate = WorkerWorldSettingCandidatePayload(candidate_id=uuid4(), work_id=uuid4(),
            source=source, source_episode_id=None, category="WORLD_RULE_HISTORY", **payload)
    concrete = {"operation": operation, "consolidation_status": "SINGLE", "target_ref": None,
                "proposed_setting_name": "부활", "proposed_value": "가능",
                "comparison_reason": "원문에 부활 가능성이 적혀 있습니다."}
    review = {**concrete, "operation": "REVIEW_REQUIRED", "review_reason": "GENERAL_UNCERTAINTY"}
    if batch:
        concrete["source_candidate_refs"] = ["C1"]
        review["source_candidate_refs"] = ["C1"]
    provider = RecordingClient(*([{"decisions": [concrete]}, {"decisions": [review]}] if batch else [concrete, review]))
    comparator = WorldSettingComparator(llm_client=provider, model="test-model", max_attempts=2)
    result, _ = asyncio.run(comparator.compare_batch("WORLD_RULE_HISTORY", [candidate], [], analysis_source=source)
                           if batch else comparator.compare(candidate, []))
    assert (result.decisions[0] if batch else result).operation == "REVIEW_REQUIRED"
    assert len(provider.requests) == 2
    prompt = json.loads(provider.requests[0]["user_prompt"])
    candidate_prompt = prompt["candidates"][0] if batch else prompt["candidate"]
    assert candidate_prompt["temporal_scope"] == scope
    assert candidate_prompt["effective_from_episode_no"] == effective


@pytest.mark.parametrize("incoming_book", [False, True])
@pytest.mark.parametrize("incoming_no", [6, 7, 8])
def test_world_bidirectional_same_or_earlier_conflict_does_not_escape_as_update(incoming_book, incoming_no):
    source = (book_source("EPISODE", incoming_no) if incoming_book else
              WorkerAnalysisSource(source_kind="EPISODE", episode_id=uuid4(), episode_no=incoming_no))
    current_provenance = (AnalysisStateProvenance(confirmation_status="CONFIRMED", source_kind="EPISODE", source_episode_no=7)
        if incoming_book else AnalysisStateProvenance.from_source(confirmation_status="CONFIRMED", source=book_source("EPISODE", 7)))
    candidate = WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(),
        subject_name="세계", setting_name="부활", extracted_value="가능", evidence_spans=[{"quote": "부활 가능"}], temporal_scope="PRESENT",
        raw_extraction_json={"epistemic_status": "ASSERTED"} if incoming_book else None)
    target = WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="세계", version=1,
        properties=[{"settingName": "부활", "value": "불가능", "provenance": current_provenance}])
    update = {"source_candidate_refs": ["C1"], "operation": "UPDATE", "consolidation_status": "SINGLE",
              "target_ref": "T1", "matched_property_name": "부활", "proposed_setting_name": "부활",
              "proposed_value": "가능", "comparison_reason": "원문에는 부활이 가능하다고 적혀 있습니다."}
    review = {**update, "operation": "REVIEW_REQUIRED", "review_reason": "GENERAL_UNCERTAINTY"}
    provider = RecordingClient({"decisions": [update]}, {"decisions": [review]})
    result, _ = asyncio.run(WorldSettingComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare_batch("WORLD_RULE_HISTORY", [candidate], [target], analysis_source=source))
    assert result.decisions[0].operation == ("UPDATE" if incoming_no > 7 else "REVIEW_REQUIRED")
    assert len(provider.requests) == (1 if incoming_no > 7 else 2)


def test_book_character_future_body_start_retries_without_losing_the_review_time():
    candidate = _candidate("C1", "Q1", "다리를 다치게 될 예정이다").model_copy(update={
        "source": book_source("EPISODE", 3), "source_episode_no": None,
    })
    concrete = {**_decision_payload("C1", operation="ADD", resolved_key="status.부상", value="다리 부상"),
                "effective_from_episode_no": 8}
    review = {**_decision_payload("C1", operation="REVIEW_REQUIRED", resolved_key="status.부상", value=None),
              "effective_from_episode_no": 8}
    provider = RecordingClient({"decisions": [concrete]}, {"decisions": [review]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare_batch(matched_character_name="아린", canonical_fact_type="STATUS",
                       candidates=[candidate], snapshot_entries=[], ordered_context=True))
    assert result.decisions[0].operation == "REVIEW_REQUIRED"
    assert result.decisions[0].effective_from_episode_no == 8
    assert len(provider.requests) == 2


@pytest.mark.parametrize("number", [0, -1, True, "8"])
def test_book_character_body_time_must_be_a_positive_typed_number(number):
    with pytest.raises(ValidationError):
        CharacterFactComparisonDecision(operation="REVIEW_REQUIRED", temporal_scope="PRESENT",
            comparison_reason="시점 확인이 필요합니다.", effective_from_episode_no=number)


def test_earlier_body_start_is_not_itself_a_conflict_with_later_as_of_reference():
    decision = CharacterFactComparisonDecision(operation="UPDATE", target_ref="P1", temporal_scope="PRESENT",
        proposed_fact_value="10", proposed_value_json={"value": 10}, effective_from_episode_no=1,
        comparison_reason="3화 종료 시점에도 이 수치가 유지됩니다.")
    state = CharacterProjectionState([current_entry(episode_no=2)])
    result = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 10},
        source=book_source("EPISODE", 3), decision=decision)
    assert result.projected_entry.fact_value == "10"


def test_unanchored_book_can_use_explicit_body_episode_time_to_update_an_earlier_current():
    decision = CharacterFactComparisonDecision(operation="UPDATE", target_ref="P1", temporal_scope="PRESENT",
        proposed_fact_value="10", proposed_value_json={"value": 10}, effective_from_episode_no=8,
        comparison_reason="8화부터 수치가 달라졌습니다.")
    state = CharacterProjectionState([current_entry(episode_no=7)])
    result = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 10},
        source=book_source("UNSPECIFIED"), decision=decision)
    assert result.projected_entry.fact_value == "10"


@pytest.mark.parametrize("extra_time", [
    {"sourceKind": "EPISODE", "sourceEpisodeNo": 9, "temporalScope": "PRESENT"},
    {"sourceKind": "SETTING_BOOK", "referenceTimeKind": "EPISODE", "referenceEpisodeNo": 8, "temporalScope": "PRESENT"},
    {"sourceKind": None, "temporalScope": "UNKNOWN"},
])
def test_every_current_source_is_checked_instead_of_the_earliest_representative(extra_time):
    provenance = AnalysisStateProvenance.model_validate({
        "confirmationStatus": "CONFIRMED", "sourceKind": "EPISODE", "sourceEpisodeNo": 2,
        "currentSourceTimes": [{"sourceKind": "EPISODE", "sourceEpisodeNo": 2, "temporalScope": "PRESENT"}, extra_time],
    })
    current = current_entry()
    current = CharacterProjectionEntry(**{**current.__dict__, "provenance": provenance})
    state = CharacterProjectionState([current])
    with pytest.raises(ValueError, match="reference time"):
        apply_number(state, book_source("EPISODE", 8))
    assert state.entries == [current]


def test_projected_explicit_body_time_survives_an_unanchored_book_between_segments():
    state = CharacterProjectionState([])
    decision = CharacterFactComparisonDecision(operation="ADD", temporal_scope="PRESENT",
        proposed_fact_value="10", proposed_value_json={"value": 10}, effective_from_episode_no=1,
        comparison_reason="1화부터 힘이 10입니다.")
    first = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 10},
        source=book_source("UNSPECIFIED"), decision=decision)
    assert first.projected_entry.provenance.effective_from_episode_no == 1
    later = CharacterFactComparisonDecision(operation="UPDATE", target_ref="Q1", temporal_scope="PRESENT",
        proposed_fact_value="20", proposed_value_json={"value": 20}, effective_from_episode_no=2,
        comparison_reason="2화부터 힘이 20입니다.")
    second = state.apply(candidate_ref="C2", projected_snapshot_ref="Q2", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 20},
        source=book_source("UNSPECIFIED"), decision=later)
    assert second.projected_entry.fact_value == "20" and second.dependency_candidate_refs == ("C1",)


def test_protected_absent_character_slot_cannot_be_recreated_by_book_add():
    state = CharacterProjectionState([], protected_slots=[{"factType": "PROFILE", "factKey": "profile.힘"}])
    with pytest.raises(ValueError, match="protected current"):
        apply_number(state, book_source(), operation="ADD", target_ref=None)
    assert state.entries == []


def test_protected_absent_world_path_is_target_scoped_and_normalized_before_book_add():
    source = book_source()
    candidate = WorkerWorldSettingComparisonBatchCandidate(candidate_ref="C1", candidate_id=uuid4(),
        subject_name="세계", setting_name="힘", extracted_value="10", evidence_spans=[{"quote": "힘: 10"}], temporal_scope="PRESENT")
    target = WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="세계", version=1,
        properties=[], protectedPaths=[{"scopeName": None, "settingName": "힘"}])
    add = {"source_candidate_refs": ["C1"], "operation": "ADD", "consolidation_status": "SINGLE",
           "target_ref": "T1", "proposed_setting_name": " 힘 ", "proposed_value": "10",
           "comparison_reason": "원문에 힘이 명시되어 있습니다."}
    review = {**add, "operation": "REVIEW_REQUIRED", "review_reason": "GENERAL_UNCERTAINTY"}
    provider = RecordingClient({"decisions": [add]}, {"decisions": [review]})
    result, _ = asyncio.run(WorldSettingComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare_batch("WORLD_RULE_HISTORY", [candidate], [target], analysis_source=source))
    assert result.decisions[0].operation == "REVIEW_REQUIRED"
    assert len(provider.requests) == 2


def test_character_batch_keeps_deleted_slot_in_review_and_projects_only_independent_new_slot():
    source = book_source()
    candidates = [
        _candidate("C1", "Q1", "기존 부상").model_copy(update={"source": source, "source_episode_no": None}),
        _candidate("C2", "Q2", "독립 상태").model_copy(update={"source": source, "source_episode_no": None}),
    ]
    add1 = _decision_payload("C1", operation="ADD", resolved_key="status.부상", value="기존 부상")
    add2 = _decision_payload("C2", operation="ADD", resolved_key="status.독립_상태", value="독립 상태")
    review = _decision_payload("C1", operation="REVIEW_REQUIRED", resolved_key="status.부상", value=None)
    provider = RecordingClient({"decisions": [add1, add2]}, {"decisions": [review, add2]})
    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=2),
        matched_character_name="아린", canonical_fact_type="STATUS", candidates=candidates,
        snapshot_entries=[], ordered_context=True,
        protected_slots=[{"factType": "STATUS", "factKey": "status.부상"}],
    ))
    assert [decision.operation for decision in result.decisions] == ["REVIEW_REQUIRED", "ADD"]
    assert result.failures == []
    assert len(provider.requests) == 2
    assert json.loads(provider.requests[0]["user_prompt"])["protected_slots"] == [
        {"fact_type": "STATUS", "fact_key": "status.부상"},
    ]


def test_episode_merge_preserves_book_and_all_current_sources_before_later_projection():
    old = current_entry()
    old = CharacterProjectionEntry(**{**old.__dict__, "provenance":
        AnalysisStateProvenance.from_source(confirmation_status="CONFIRMED", source=book_source())})
    state = CharacterProjectionState([old])
    source = WorkerAnalysisSource(source_kind="EPISODE", episode_id=uuid4(), episode_no=3)
    merge = CharacterFactComparisonDecision(operation="MERGE", target_ref="P1", temporal_scope="PRESENT",
        proposed_fact_value="30", proposed_value_json={"value": 30}, comparison_reason="3화에서 힘을 보완했습니다.")
    merged = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 30},
        source_episode_no=3, source=source, decision=merge)
    assert [(item.source_kind, item.source_episode_no) for item in merged.projected_entry.provenance.current_source_times] == [
        ("SETTING_BOOK", None), ("EPISODE", 3),
    ]
    earlier = WorkerAnalysisSource(source_kind="EPISODE", episode_id=uuid4(), episode_no=2)
    with pytest.raises(ValueError, match="reference time"):
        apply_number(state, earlier, target_ref="Q1")
    assert state.entries == [merged.projected_entry]


def test_character_singleton_book_deleted_slot_retries_into_preserved_review():
    from tests.test_character_fact_comparator import _candidate as singleton_candidate
    candidate = singleton_candidate().model_copy(update={"source": book_source(), "source_episode_id": None})
    concrete = {"operation": "ADD", "temporal_scope": "PRESENT", "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": True}, "comparison_reason": "설정집에 회복 내용이 있습니다."}
    review = {**concrete, "operation": "REVIEW_REQUIRED"}
    provider = RecordingClient(concrete, review)
    result, _ = asyncio.run(CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare(candidate, [], protected_slots=[{"factType": "STATUS", "factKey": "status.회복"}]))
    assert result.operation == "REVIEW_REQUIRED" and len(provider.requests) == 2
    assert json.loads(provider.requests[0]["user_prompt"])["protected_slots"] == [
        {"fact_type": "STATUS", "fact_key": "status.회복"}]


def test_character_singleton_checks_all_current_times_and_exposes_only_safe_provenance():
    from tests.test_character_fact_comparator import _candidate as singleton_candidate
    from tests.test_character_fact_comparator import _snapshot_entry
    candidate = singleton_candidate().model_copy(update={"source": book_source("EPISODE", 8), "source_episode_id": None})
    snapshot = _snapshot_entry("STATUS", "status.회복", {"active": True}).model_copy(update={
        "provenance": AnalysisStateProvenance(confirmation_status="CONFIRMED", source_episode_no=2,
            currentSourceTimes=[{"sourceKind": "EPISODE", "sourceEpisodeNo": 2, "temporalScope": "PRESENT"},
                {"sourceKind": "EPISODE", "sourceEpisodeNo": 9, "temporalScope": "PRESENT"}])})
    update = {"operation": "UPDATE", "target_ref": "P1", "temporal_scope": "PRESENT",
        "proposed_fact_value": "완전히 회복됨", "proposed_value_json": {"active": True},
        "comparison_reason": "원문에는 회복 내용이 있습니다."}
    review = {**update, "operation": "REVIEW_REQUIRED", "target_ref": None}
    provider = RecordingClient(update, review)
    result, _ = asyncio.run(CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare(candidate, [snapshot]))
    assert result.operation == "REVIEW_REQUIRED" and len(provider.requests) == 2
    prompt = json.loads(provider.requests[0]["user_prompt"])
    assert [time["source_episode_no"] for time in prompt["snapshot_entries"][0]["current_source_times"]] == [2, 9]
    assert str(candidate.candidate_id) not in json.dumps(prompt)


@pytest.mark.parametrize("kind,number,effective", [("EPISODE", 3, 4), ("WORK_START", None, 1)])
@pytest.mark.parametrize("prior_decision", [False, True])
def test_character_exclude_anchor_conflict_cannot_discard_review_from_projected_state(kind, number, effective, prior_decision):
    current = current_entry(protected=True)
    if prior_decision:
        current = CharacterProjectionEntry(**{**current.__dict__, "reference": "Q1",
            "origin": "PRIOR_DECISION", "source_candidate_ref": "C1"})
    state = CharacterProjectionState([current])
    decision = CharacterFactComparisonDecision(operation="EXCLUDE", temporal_scope="PRESENT",
        effective_from_episode_no=effective, comparison_reason="이미 알려진 계획과 같습니다.")
    with pytest.raises(ValueError, match="reference time"):
        state.apply(candidate_ref="C2", projected_snapshot_ref="Q2", fact_type="PROFILE",
            resolved_fact_key="profile.힘", value_type="NUMBER", candidate_value_json={"value": 20},
            source=book_source(kind, number), decision=decision)
    assert state.entries == [current]


@pytest.mark.parametrize("kind,number,effective,expected", [
    ("EPISODE", 3, 4, "REVIEW_REQUIRED"), ("WORK_START", None, 1, "REVIEW_REQUIRED"),
    ("EPISODE", 3, 1, "EXCLUDE"), ("WORK_START", None, None, "EXCLUDE"),
])
def test_character_singleton_exclude_retries_anchor_conflict_but_keeps_present_duplicate(kind, number, effective, expected):
    from tests.test_character_fact_comparator import _candidate as singleton_candidate
    from tests.test_character_fact_comparator import _snapshot_entry
    candidate = singleton_candidate().model_copy(update={"source": book_source(kind, number), "source_episode_id": None})
    snapshot = _snapshot_entry("STATUS", "status.회복", {"active": True})
    exclude = {"operation": "EXCLUDE", "temporal_scope": "PRESENT", "effective_from_episode_no": effective,
        "comparison_reason": "이미 알려진 회복 내용과 같습니다."}
    review = {**exclude, "operation": "REVIEW_REQUIRED", "comparison_reason": "본문과 기준시점이 달라 확인이 필요합니다."}
    provider = RecordingClient(exclude, review)
    result, _ = asyncio.run(CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare(candidate, [snapshot]))
    assert result.operation == expected and result.effective_from_episode_no == effective
    assert len(provider.requests) == (2 if expected == "REVIEW_REQUIRED" else 1)


@pytest.mark.parametrize("ordered", [False, True])
@pytest.mark.parametrize("kind,number,effective,expected", [
    ("EPISODE", 3, 4, "REVIEW_REQUIRED"), ("WORK_START", None, 1, "REVIEW_REQUIRED"),
    ("EPISODE", 3, 1, "EXCLUDE"), ("WORK_START", None, None, "EXCLUDE"),
])
def test_character_batch_exclude_retries_anchor_conflict_against_current_or_provisional(kind, number, effective, expected, ordered):
    source = book_source(kind, number)
    candidate = _candidate("C1", "Q1", "다리 부상").model_copy(update={"source": source, "source_episode_no": None})
    snapshot = CharacterProjectionEntry(reference="P1", fact_type="STATUS", fact_key="status.부상",
        fact_value="다리 부상", value_json=candidate.value_json, origin="PROVISIONAL" if ordered else "PERSISTED",
        provenance=AnalysisStateProvenance.from_source(confirmation_status="PROVISIONAL" if ordered else "CONFIRMED",
            source=book_source("WORK_START")))
    exclude = {**_decision_payload("C1", operation="EXCLUDE", resolved_key="status.부상", value=None,
        reason="이미 알려진 다리 부상과 같습니다."), "effective_from_episode_no": effective}
    review = {**exclude, "operation": "REVIEW_REQUIRED", "comparison_reason": "본문과 기준시점이 달라 확인이 필요합니다."}
    provider = RecordingClient({"decisions": [exclude]}, {"decisions": [review]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=2)
        .compare_batch(matched_character_name="아린", canonical_fact_type="STATUS", candidates=[candidate],
            snapshot_entries=[snapshot], ordered_context=ordered))
    assert result.decisions[0].operation == expected and result.decisions[0].effective_from_episode_no == effective
    assert len(provider.requests) == (2 if expected == "REVIEW_REQUIRED" else 1)
