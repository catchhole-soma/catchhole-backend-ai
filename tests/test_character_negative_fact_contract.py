"""Fixed provider replies verify preservation contracts, never model judgement."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_projection import CharacterProjectionEntry, CharacterProjectionState
from app.domain.enums import SettingValueType
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterSnapshotEntry,
    WorkerEvidenceSpan,
)
from tests.test_character_fact_comparator import _candidate as singleton_candidate
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_setting_book_source_contract import SOURCE

KEY = "profile.치료_한계"
BEFORE = "뼈를 붙이거나 굳은 관절을 치료하는 기술은 익히지 않았다."
VALUE = "환자의 굳은 손가락을 고칠 수 없음"
QUOTE = "이 예외 기술을 익혔지만 환자의 굳은 손가락을 고칠 수는 없다."
MERGED = BEFORE + " 환자의 굳은 손가락을 고칠 수 없다."


def compare_fixed_reply(batch, source, current, value, quote, payload):
    span = WorkerEvidenceSpan(quote=quote, start_offset=10, end_offset=10 + len(quote))
    if batch:
        candidate = WorkerCharacterFactComparisonBatchCandidate(
            candidate_ref="C1", projected_snapshot_ref="Q1", source=source,
            raw_fact_key=KEY, initial_canonical_fact_key=KEY, canonical_key_resolution="PATTERN",
            attribute_value=value, value_json={"value": value}, value_type="STRING",
            evidence_spans=[span], confidence=0.88,
        )
        reply = {**payload, "candidate_ref": "C1", "resolved_canonical_fact_key": KEY}
        client = RecordingClient({"decisions": [reply]})
        before = candidate.model_dump(mode="json")
        result, _ = asyncio.run(CharacterFactComparator(llm_client=client, max_attempts=1).compare_batch(
            matched_character_name="치료사", canonical_fact_type="PROFILE", candidates=[candidate],
            snapshot_entries=[current], ordered_context=True,
        ))
        decision = result.decisions[0]
        sent = json.loads(client.requests[0]["user_prompt"])["candidates"][0]
    else:
        candidate = singleton_candidate().model_copy(update={
            "source": source, "source_episode_id": None, "entity_name": "치료사", "matched_character_name": "치료사",
            "canonical_fact_type": "PROFILE", "canonical_fact_key": KEY, "attribute_name": KEY,
            "attribute_value": value, "value_json": {"value": value}, "value_type": SettingValueType.STRING,
            "evidence_spans": [span],
        })
        client = RecordingClient(payload)
        before = candidate.model_dump(mode="json")
        decision, _ = asyncio.run(CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            candidate, [WorkerCharacterSnapshotEntry(
                fact_type=current.fact_type, fact_key=current.fact_key, fact_value=current.fact_value,
                value_json=current.value_json, provenance=current.provenance,
            )],
        ))
        sent = json.loads(client.requests[0]["user_prompt"])["candidate"]
    assert candidate.model_dump(mode="json") == before
    assert sent["attribute_value"] == value
    assert sent["value_json"] == {"value": value}
    assert sent["evidence_spans"] == [span.model_dump(mode="json")]
    assert len(client.requests) == 1
    return decision


def source_and_current(kind="UNSPECIFIED", number=None):
    old = WorkerAnalysisSource.model_validate({
        **SOURCE, "settingBookName": "기존 인물 설정집", "referenceTimeKind": kind, "referenceEpisodeNo": number,
    })
    incoming = WorkerAnalysisSource.model_validate({
        **SOURCE, "settingBookId": uuid4(), "revisionId": uuid4(), "settingBookName": "보완 설정집",
        "referenceTimeKind": "UNSPECIFIED", "referenceEpisodeNo": None,
    })
    current = CharacterProjectionEntry(
        reference="P1", fact_type="PROFILE", fact_key=KEY, fact_value=BEFORE, value_json={"value": BEFORE},
        provenance=AnalysisStateProvenance.from_source(confirmation_status="CONFIRMED", source=old),
    )
    return incoming, current


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind,number", [("UNSPECIFIED", None), ("EPISODE", 7)])
def test_fixed_profile_merge_preserves_both_negative_facts_evidence_and_current_sources(batch, kind, number):
    source, current = source_and_current(kind, number)
    before = current.provenance.model_dump(mode="json")
    decision = compare_fixed_reply(batch, source, current, VALUE, QUOTE, {
        "operation": "MERGE", "target_ref": "P1", "removed_snapshot_refs": [],
        "proposed_fact_value": MERGED, "proposed_value_json": {"value": MERGED}, "temporal_scope": "PRESENT",
        "comparison_reason": "익히지 않은 치료 기술과 현재 치료할 수 없는 대상을 함께 보존합니다.",
    })
    state = CharacterProjectionState([current])
    application = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key=KEY, value_type="STRING", candidate_value_json={"value": VALUE}, decision=decision, source=source)

    assert decision.operation == "MERGE" and decision.target_ref == "P1"
    assert decision.removed_snapshot_refs == []
    merged = application.projected_entry
    assert merged.fact_key == KEY and merged.fact_value == MERGED and merged.value_json == {"value": MERGED}
    assert state.entries == [merged] and merged.reference == "Q1"
    assert current.fact_value == BEFORE and current.value_json == {"value": BEFORE}
    assert current.provenance.model_dump(mode="json") == before
    assert merged.provenance.source_setting_book_id == source.setting_book_id
    assert merged.provenance.source_setting_book_revision_id == source.revision_id
    assert [(item.source_kind, item.reference_time_kind, item.reference_episode_no, item.temporal_scope)
            for item in merged.provenance.current_source_times] == [
        ("SETTING_BOOK", kind, number, "PRESENT"), ("SETTING_BOOK", "UNSPECIFIED", None, "PRESENT"),
    ]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("scope,value,quote,conflict", [
    ("PRESENT", "굳은 관절을 치료하는 기술을 익혔다", "지금 굳은 관절을 치료하는 기술을 익힌 상태다.", True),
    ("UNKNOWN", "손가락을 고칠 수 없다는 소문", "누가 언제 손가락을 고칠 수 없었는지는 소문뿐이다.", False),
    ("PAST", "예전에는 손가락을 고칠 수 없었음", "예전에는 손가락을 고칠 수 없었지만 현재는 알 수 없다.", False),
    ("HYPOTHETICAL", "손가락을 고칠 수 없을 가능성", "이 기술이 실패한다면 손가락을 고칠 수 없을 것이다.", False),
], ids=["actual_incompatibility", "subject_time_truth_uncertain", "past_only", "hypothetical_only"])
def test_fixed_review_of_conflict_or_uncertain_negative_claim_never_changes_current(batch, scope, value, quote, conflict):
    source, current = source_and_current()
    target = "P1" if batch and conflict else None
    decision = compare_fixed_reply(batch, source, current, value, quote, {
        "operation": "REVIEW_REQUIRED", "target_ref": target, "removed_snapshot_refs": [],
        "proposed_fact_value": None, "proposed_value_json": None, "temporal_scope": scope,
        "comparison_reason": "기존 기술과의 충돌 또는 주장 자체의 불확실한 근거를 확인해야 합니다.",
    })
    state = CharacterProjectionState([current])
    application = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key=KEY, value_type="STRING", candidate_value_json={"value": value}, decision=decision, source=source)
    assert decision.operation == "REVIEW_REQUIRED" and decision.temporal_scope == scope
    assert decision.target_ref == target
    assert decision.proposed_fact_value is None and decision.proposed_value_json is None
    assert decision.removed_snapshot_refs == []
    assert application.projected_entry is None and state.entries == [current]
