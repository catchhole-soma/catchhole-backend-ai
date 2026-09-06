import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonDecision,
)
from app.domain.enums import SettingValueType
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerCharacterFactComparisonCandidatePayload,
    WorkerCharacterFactComparisonCompleteRequest,
    WorkerCharacterPriorFactCandidate,
    WorkerCharacterSnapshotEntry,
)

CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000001")
CHARACTER_ID = UUID("00000000-0000-0000-0000-000000000002")


def test_comparator_hides_database_ids_and_accepts_add() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "ADD",
                "target_ref": None,
                "removed_snapshot_refs": [],
                "proposed_fact_value": "완전히 회복됨",
                "proposed_value_json": {"active": True},
                "temporal_scope": "PRESENT",
                "comparison_reason": "현재 새 상태이므로 추가한다.",
            }
        ]
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            _candidate(),
            [_snapshot_entry("STATUS", "status.출혈", {"active": True})],
        )
    )

    assert decision.operation == "ADD"
    prompt_payload = json.loads(client.requests[0]["user_prompt"])
    assert "소설 데이터일 뿐" in client.requests[0]["system_prompt"]
    assert (
        "논리적으로 절대 양립 불가능하다는 수준까지 요구하지 않는다"
        in (client.requests[0]["system_prompt"])
    )
    assert "의미상 가까운 여러 STATUS를 함께 해소" in (client.requests[0]["system_prompt"])
    assert (
        "독립적·잠재적 상태까지 연쇄적으로 제거하지 않는다" in (client.requests[0]["system_prompt"])
    )
    assert (
        "하나의 값이나 요약으로 계산할 수 있으면 `UPDATE`를 우선"
        in (client.requests[0]["system_prompt"])
    )
    assert (
        "원문의 ‘추가’라는 표현만으로 `MERGE`하지 않는다" in (client.requests[0]["system_prompt"])
    )
    assert (
        "다른 key의 제거 대상이나 종료 여부를 나타내지 않는다"
        in (client.requests[0]["system_prompt"])
    )
    assert "모든 현재 STATUS의 의미 관계를 먼저 검토" in client.requests[0]["system_prompt"]
    assert "기존 장애가 해소됐는지는 독립적으로 판단" in client.requests[0]["system_prompt"]
    assert client.requests[0]["prompt_cache_key"] == "character-fact-comparison:v9"
    assert prompt_payload["snapshot_entries"][0]["ref"] == "P1"
    assert prompt_payload["snapshot_entries"][0]["fact_value"] == "출혈 중"
    assert prompt_payload["exact_target_ref"] is None
    assert prompt_payload["allowed_operations"] == [
        "ADD",
        "REMOVE",
        "HISTORY_ONLY",
        "EXCLUDE",
        "REVIEW_REQUIRED",
    ]
    serialized_prompt = client.requests[0]["user_prompt"]
    assert str(CANDIDATE_ID) not in serialized_prompt
    assert str(CHARACTER_ID) not in serialized_prompt
    assert "00000000-0000-0000-0000-000000000010" not in serialized_prompt
    assert "00000000-0000-0000-0000-000000000011" not in serialized_prompt
    assert prompt_payload["candidate"]["evidence_spans"][0]["quote"] == "상처가 완전히 나았다."


def test_comparator_passes_prior_same_slot_candidates_as_unconfirmed_chronology() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "ADD",
                "target_ref": None,
                "removed_snapshot_refs": [],
                "proposed_fact_value": "36",
                "proposed_value_json": {"value": 36},
                "temporal_scope": "PRESENT",
                "comparison_reason": "앞선 35에서 1 상승한 최종값이다.",
            }
        ]
    )
    prior_candidate = WorkerCharacterPriorFactCandidate.model_validate(
        {
            "sourceEpisodeNo": 2,
            "attributeName": "stats.mental",
            "attributeValue": "35",
            "valueJson": {"value": 35},
            "evidenceSpans": [],
            "comparisonStatus": "COMPLETED",
            "suggestedOperation": "ADD",
            "proposedFactValue": "35",
            "proposedValueJson": {"value": 35},
        }
    )

    asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            _candidate(),
            [],
            [prior_candidate],
        )
    )

    prompt_payload = json.loads(client.requests[0]["user_prompt"])
    assert prompt_payload["prior_candidates"] == [
        {
            "source_episode_no": 2,
            "attribute_name": "stats.mental",
            "attribute_value": "35",
            "value_json": {"value": 35},
            "evidence_spans": [],
            "comparison_status": "COMPLETED",
            "suggested_operation": "ADD",
            "proposed_fact_value": "35",
            "proposed_value_json": {"value": 35},
        }
    ]


def test_comparator_retries_invalid_number_json_and_normalizes_display_value() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "정신 36",
        "proposed_value_json": {"value": "36"},
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 정신 수치를 추가한다.",
    }
    valid = {**invalid, "proposed_value_json": {"value": 36}}
    client = FakeTextClient([invalid, valid])
    candidate = _candidate().model_copy(
        update={
            "attribute_name": "stats.mental",
            "attribute_value": "35",
            "value_json": {"value": 35},
            "value_type": SettingValueType.NUMBER,
            "canonical_fact_type": "STAT",
            "canonical_fact_key": "stats.mental",
        }
    )

    decision, raw = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(candidate, [])
    )

    assert decision.proposed_fact_value == "36"
    assert raw["proposed_fact_value"] == "36"
    assert len(client.requests) == 2


def test_comparator_allows_only_status_snapshot_removal() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": ["P2"],
        "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": True},
        "temporal_scope": "PRESENT",
        "comparison_reason": "P2도 제거한다.",
    }
    valid = {**invalid, "removed_snapshot_refs": ["P1"]}
    client = FakeTextClient([invalid, valid])

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            _candidate(),
            [
                _snapshot_entry("STATUS", "status.출혈", {"active": True}),
                _snapshot_entry("STAT", "stats.strength", {"value": 10}),
            ],
        )
    )

    assert decision.removed_snapshot_refs == ["P1"]
    assert len(client.requests) == 2


def test_comparator_retries_when_past_candidate_updates_current_snapshot() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": True},
        "temporal_scope": "PAST",
        "comparison_reason": "과거 상태를 현재에 추가한다.",
    }
    valid = {
        **invalid,
        "operation": "HISTORY_ONLY",
        "proposed_fact_value": None,
        "proposed_value_json": None,
        "comparison_reason": "회상 속 과거 상태라 이력으로만 남긴다.",
    }
    client = FakeTextClient([invalid, valid])

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(_candidate(), [])
    )

    assert decision.operation == "HISTORY_ONLY"
    assert decision.temporal_scope == "PAST"
    assert len(client.requests) == 2


def test_comparator_requires_update_target_to_match_canonical_fact_key() -> None:
    invalid = {
        "operation": "UPDATE",
        "target_ref": "P1",
        "removed_snapshot_refs": [],
        "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": True},
        "temporal_scope": "PRESENT",
        "comparison_reason": "P1을 갱신한다.",
    }
    valid = {**invalid, "target_ref": "P2", "comparison_reason": "P2를 갱신한다."}
    client = FakeTextClient([invalid, valid])

    decision, raw = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            _candidate(),
            [
                _snapshot_entry("STATUS", "status.출혈", {"active": True}),
                _snapshot_entry("STATUS", "status.회복", {"active": True}),
            ],
        )
    )

    assert decision.target_ref == "P2"
    assert "P2" not in decision.comparison_reason
    assert "status.회복" not in decision.comparison_reason
    assert "현재 '현재 표시값' 설정을 갱신한다." == decision.comparison_reason
    assert raw["comparison_reason"] == decision.comparison_reason
    assert len(client.requests) == 2
    first_prompt = json.loads(client.requests[0]["user_prompt"])
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    assert first_prompt["exact_target_ref"] == "P2"
    assert first_prompt["allowed_operations"] == [
        "UPDATE",
        "MERGE",
        "REMOVE",
        "HISTORY_ONLY",
        "EXCLUDE",
        "REVIEW_REQUIRED",
    ]
    assert "validation_feedback" not in first_prompt
    assert retry_prompt["validation_feedback"]["previous_response_rejected"] is True
    assert "exact_target_ref" in retry_prompt["validation_feedback"]["correction"]
    assert "canonical Fact key" in retry_prompt["validation_feedback"]["reason"]


def test_comparator_retries_fabricated_reason_ref_and_sanitizes_known_ref() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "UPDATE",
                "target_ref": "P1",
                "removed_snapshot_refs": [],
                "proposed_fact_value": "회복 중",
                "proposed_value_json": {"active": True},
                "temporal_scope": "PRESENT",
                "comparison_reason": "P1을 갱신하되 P10은 별도 근거다.",
            },
            {
                "operation": "UPDATE",
                "target_ref": "P1",
                "removed_snapshot_refs": [],
                "proposed_fact_value": "회복 중",
                "proposed_value_json": {"active": True},
                "temporal_scope": "PRESENT",
                "comparison_reason": "P1을 갱신한다.",
            },
        ]
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            _candidate(),
            [_snapshot_entry("STATUS", "status.회복", {"active": True})],
        )
    )

    assert decision.comparison_reason == "현재 '현재 표시값' 설정을 갱신한다."
    assert "status.회복" not in decision.comparison_reason
    assert "P10" not in decision.comparison_reason
    assert len(client.requests) == 2
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    assert (
        "Unknown snapshot refs in comparison reason"
        in (retry_prompt["validation_feedback"]["reason"])
    )


def test_comparator_uses_neutral_label_when_snapshot_fact_value_is_missing() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "UPDATE",
                "target_ref": "P1",
                "removed_snapshot_refs": [],
                "proposed_fact_value": "바바리안",
                "proposed_value_json": {"value": "바바리안"},
                "temporal_scope": "PRESENT",
                "comparison_reason": "P1을 유지한다.",
            }
        ]
    )
    candidate = _candidate().model_copy(
        update={
            "canonical_fact_type": "PROFILE",
            "canonical_fact_key": "profile.species",
            "attribute_value": "바바리안",
            "value_json": {"value": "바바리안"},
        }
    )
    legacy_entry = WorkerCharacterSnapshotEntry(
        fact_type="PROFILE",
        fact_key="profile.species",
        fact_value=None,
        value_json={"value": "바바리안"},
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            candidate,
            [legacy_entry],
        )
    )

    assert decision.comparison_reason == "현재 관련 설정을 유지한다."
    assert "P1" not in decision.comparison_reason
    assert "species" not in decision.comparison_reason


def test_comparator_guides_different_status_key_to_add_and_remove_on_retry() -> None:
    invalid = {
        "operation": "UPDATE",
        "target_ref": "P1",
        "removed_snapshot_refs": [],
        "proposed_fact_value": "오른발이 심하게 다친 상태",
        "proposed_value_json": {"name": "오른발 부상"},
        "temporal_scope": "PRESENT",
        "comparison_reason": "비슷한 기존 부상 상태를 갱신한다.",
    }
    valid = {
        **invalid,
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": ["P1"],
        "comparison_reason": "오른발 부상을 추가하고 기존의 포괄적인 부상 상태를 대체한다.",
    }
    client = FakeTextClient([invalid, valid])
    candidate = _candidate().model_copy(
        update={
            "attribute_name": "status.오른발_부상",
            "attribute_value": "오른발이 심하게 다친 상태",
            "canonical_fact_key": "status.오른발_부상",
        }
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            candidate,
            [_snapshot_entry("STATUS", "status.부상", {"name": "부상"})],
        )
    )

    assert decision.operation == "ADD"
    assert decision.removed_snapshot_refs == ["P1"]
    first_prompt = json.loads(client.requests[0]["user_prompt"])
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    assert first_prompt["exact_target_ref"] is None
    assert "UPDATE" not in first_prompt["allowed_operations"]
    assert (
        "ADD/UPDATE/MERGE와 removed_snapshot_refs"
        in (retry_prompt["validation_feedback"]["correction"])
    )


def test_comparator_retries_add_when_canonical_slot_already_exists() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": True},
        "temporal_scope": "PRESENT",
        "comparison_reason": "새 항목으로 추가한다.",
    }
    valid = {
        **invalid,
        "operation": "UPDATE",
        "target_ref": "P1",
        "comparison_reason": "현재 회복 상태를 새 내용으로 바꾼다.",
    }
    client = FakeTextClient([invalid, valid])

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            _candidate(),
            [_snapshot_entry("STATUS", "status.회복", {"active": True})],
        )
    )

    assert decision.operation == "UPDATE"
    assert decision.target_ref == "P1"
    assert len(client.requests) == 2


def test_comparator_retries_internal_reason_terms_with_user_facing_explanation() -> None:
    internal_reason = {
        "operation": "UPDATE",
        "target_ref": "P1",
        "removed_snapshot_refs": [],
        "proposed_fact_value": "회복 완료",
        "proposed_value_json": {"active": True},
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 snapshot의 status.회복 canonical Fact를 UPDATE한다.",
    }
    user_facing_reason = {
        **internal_reason,
        "comparison_reason": "현재 회복 중인 상태를 회복 완료로 변경합니다.",
    }
    client = FakeTextClient([internal_reason, user_facing_reason])

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            _candidate(),
            [_snapshot_entry("STATUS", "status.회복", {"active": True})],
        )
    )

    assert decision.comparison_reason == "현재 회복 중인 상태를 회복 완료로 변경합니다."
    assert len(client.requests) == 2
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    assert "internal" in retry_prompt["validation_feedback"]["reason"]


def test_comparator_removes_same_status_slot_when_current_state_ended() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "REMOVE",
                "target_ref": None,
                "removed_snapshot_refs": ["P1"],
                "proposed_fact_value": None,
                "proposed_value_json": None,
                "temporal_scope": "PRESENT",
                "comparison_reason": "오른발이 완전히 회복되어 현재 부상 상태를 종료합니다.",
            }
        ]
    )
    candidate = _candidate().model_copy(
        update={
            "attribute_name": "status.오른발_부상",
            "attribute_value": "오른발이 완전히 회복됨",
            "canonical_fact_key": "status.오른발_부상",
            "value_json": {"active": False},
        }
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            candidate,
            [_snapshot_entry("STATUS", "status.오른발_부상", {"active": True})],
        )
    )

    assert decision.operation == "REMOVE"
    assert decision.target_ref is None
    assert decision.removed_snapshot_refs == ["P1"]
    assert decision.proposed_fact_value is None
    assert decision.proposed_value_json is None


def test_comparator_removes_multiple_related_cross_key_statuses() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "REMOVE",
                "target_ref": None,
                "removed_snapshot_refs": ["P1", "P2"],
                "proposed_fact_value": None,
                "proposed_value_json": None,
                "temporal_scope": "PRESENT",
                "comparison_reason": "다시 두 발로 걸을 수 있어 발 부상과 마비 영향이 끝났습니다.",
            }
        ]
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            _candidate().model_copy(update={"value_json": {"active": False}}),
            [
                _snapshot_entry("STATUS", "status.오른발_부상", {"active": True}),
                _snapshot_entry("STATUS", "status.마비독", {"active": True}),
            ],
        )
    )

    assert decision.operation == "REMOVE"
    assert decision.target_ref is None
    assert decision.removed_snapshot_refs == ["P1", "P2"]
    prompt_payload = json.loads(client.requests[0]["user_prompt"])
    assert prompt_payload["exact_target_ref"] is None
    assert "REMOVE" in prompt_payload["allowed_operations"]


def test_comparator_does_not_allow_remove_without_current_status() -> None:
    client = FakeTextClient(
        [
            {
                "operation": "EXCLUDE",
                "target_ref": None,
                "removed_snapshot_refs": [],
                "proposed_fact_value": None,
                "proposed_value_json": None,
                "temporal_scope": "PRESENT",
                "comparison_reason": "종료할 현재 상태가 없어 별도 설정으로 반영하지 않습니다.",
            }
        ]
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=1).compare(
            _candidate().model_copy(update={"value_json": {"active": False}}),
            [_snapshot_entry("ITEM", "item.포션", {"name": "포션"})],
        )
    )

    assert decision.operation == "EXCLUDE"
    prompt_payload = json.loads(client.requests[0]["user_prompt"])
    assert prompt_payload["allowed_operations"] == [
        "HISTORY_ONLY",
        "EXCLUDE",
        "REVIEW_REQUIRED",
    ]


def test_comparator_retries_inactive_candidate_upsert_as_canonical_remove() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "회복 완료",
        "proposed_value_json": {"active": True},
        "temporal_scope": "PRESENT",
        "comparison_reason": "회복 결과를 현재 상태로 추가합니다.",
    }
    valid = {
        **invalid,
        "operation": "REMOVE",
        "removed_snapshot_refs": ["P1"],
        "proposed_fact_value": None,
        "proposed_value_json": None,
        "comparison_reason": "회복이 끝나 현재 부상 상태를 종료합니다.",
    }
    client = FakeTextClient([invalid, valid])
    candidate = _candidate().model_copy(update={"value_json": {"active": False}})

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            candidate,
            [_snapshot_entry("STATUS", "status.부상", {"active": True})],
        )
    )

    assert decision.operation == "REMOVE"
    assert decision.removed_snapshot_refs == ["P1"]
    assert len(client.requests) == 2
    first_prompt = json.loads(client.requests[0]["user_prompt"])
    assert first_prompt["allowed_operations"] == [
        "REMOVE",
        "HISTORY_ONLY",
        "EXCLUDE",
        "REVIEW_REQUIRED",
    ]
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    assert "inactive STATUS" in retry_prompt["validation_feedback"]["reason"]


@pytest.mark.parametrize("invalid_active", [False, "false"])
def test_comparator_retries_inactive_or_non_boolean_proposal_upsert(
    invalid_active: object,
) -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "회복 중",
        "proposed_value_json": {"active": invalid_active},
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 회복 상태를 추가합니다.",
    }
    valid = {**invalid, "proposed_value_json": {"active": True}}
    client = FakeTextClient([invalid, valid])

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(_candidate(), [])
    )

    assert decision.operation == "ADD"
    assert decision.proposed_value_json == {"active": True}
    assert len(client.requests) == 2
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    reason = retry_prompt["validation_feedback"]["reason"]
    if invalid_active is False:
        assert "inactive STATUS" in reason
    else:
        assert "JSON boolean" in reason


def test_comparator_rejects_non_boolean_candidate_active_before_llm_call() -> None:
    client = FakeTextClient([])
    candidate = _candidate().model_copy(update={"value_json": {"active": "false"}})

    with pytest.raises(ValueError, match="candidate.value_json.active must be a JSON boolean"):
        asyncio.run(
            CharacterFactComparator(llm_client=client, max_attempts=1).compare(candidate, [])
        )

    assert client.requests == []


@pytest.mark.parametrize(
    "payload_update",
    [
        {"target_ref": "P1", "removed_snapshot_refs": ["P1"]},
        {"target_ref": None, "removed_snapshot_refs": []},
        {"target_ref": None, "removed_snapshot_refs": ["P1"], "temporal_scope": "PAST"},
        {"target_ref": None, "removed_snapshot_refs": ["P1", "P1"]},
        {
            "target_ref": None,
            "removed_snapshot_refs": ["P1"],
            "proposed_fact_value": "현재값으로 저장하면 안 됨",
            "proposed_value_json": {"active": False},
        },
    ],
)
def test_remove_requires_canonical_removal_set(payload_update: dict) -> None:
    payload = {
        "operation": "REMOVE",
        "target_ref": None,
        "removed_snapshot_refs": ["P1"],
        "proposed_fact_value": None,
        "proposed_value_json": None,
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 상태를 종료한다.",
        **payload_update,
    }

    with pytest.raises(ValueError):
        CharacterFactComparisonDecision.model_validate(payload)


def test_comparator_rejects_remove_from_non_status_candidate() -> None:
    invalid = {
        "operation": "REMOVE",
        "target_ref": None,
        "removed_snapshot_refs": ["P1"],
        "proposed_fact_value": None,
        "proposed_value_json": None,
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 부상 상태를 종료한다.",
    }
    valid = {
        **invalid,
        "operation": "ADD",
        "removed_snapshot_refs": [],
        "proposed_fact_value": "포션",
        "proposed_value_json": {"name": "포션"},
        "comparison_reason": "현재 포션 소지를 추가한다.",
    }
    client = FakeTextClient([invalid, valid])
    item_candidate = _candidate().model_copy(
        update={
            "attribute_name": "item.포션",
            "attribute_value": "포션",
            "canonical_fact_type": "ITEM",
            "canonical_fact_key": "item.포션",
            "value_json": {"name": "포션"},
        }
    )

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            item_candidate,
            [_snapshot_entry("STATUS", "status.부상", {"active": True})],
        )
    )

    assert decision.operation == "ADD"
    prompt_payload = json.loads(client.requests[0]["user_prompt"])
    assert "REMOVE" not in prompt_payload["allowed_operations"]


def test_comparator_retries_unknown_removed_snapshot_ref() -> None:
    invalid = {
        "operation": "REMOVE",
        "target_ref": None,
        "removed_snapshot_refs": ["P9"],
        "proposed_fact_value": None,
        "proposed_value_json": None,
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 부상 상태를 종료한다.",
    }
    valid = {**invalid, "removed_snapshot_refs": ["P1"]}
    client = FakeTextClient([invalid, valid])

    decision, _ = asyncio.run(
        CharacterFactComparator(llm_client=client, max_attempts=2).compare(
            _candidate(),
            [_snapshot_entry("STATUS", "status.부상", {"active": True})],
        )
    )

    assert decision.removed_snapshot_refs == ["P1"]
    assert len(client.requests) == 2


def test_snapshot_operation_requires_final_display_value() -> None:
    with pytest.raises(ValueError, match="proposed_fact_value"):
        CharacterFactComparisonDecision.model_validate(
            {
                "operation": "MERGE",
                "target_ref": "P1",
                "removed_snapshot_refs": [],
                "proposed_value_json": {"active": False},
                "temporal_scope": "PRESENT",
                "comparison_reason": "현재값과 신규 정보를 합친다.",
            }
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("target_ref", "P1"),
        ("removed_snapshot_refs", ["P1"]),
    ],
)
@pytest.mark.parametrize("operation", ["HISTORY_ONLY", "EXCLUDE", "REVIEW_REQUIRED"])
def test_non_snapshot_operations_still_reject_target_and_removals(
    operation: str,
    field_name: str,
    field_value: object,
) -> None:
    payload = {
        "operation": operation,
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": None,
        "proposed_value_json": None,
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 snapshot을 자동 변경하지 않는다.",
        field_name: field_value,
    }

    with pytest.raises(ValueError):
        CharacterFactComparisonDecision.model_validate(payload)


@pytest.mark.parametrize(
    ("operation", "temporal_scope"),
    [
        ("HISTORY_ONLY", "PAST"),
        ("EXCLUDE", "PRESENT"),
        ("REVIEW_REQUIRED", "PRESENT"),
    ],
)
def test_non_snapshot_operations_discard_irrelevant_proposed_values(
    operation: str,
    temporal_scope: str,
) -> None:
    decision = CharacterFactComparisonDecision.model_validate(
        {
            "operation": operation,
            "target_ref": None,
            "removed_snapshot_refs": [],
            "proposed_fact_value": "provider가 잘못 덧붙인 제안 표시값",
            "proposed_value_json": {"active": False},
            "temporal_scope": temporal_scope,
            "comparison_reason": "현재 snapshot을 자동 변경하지 않는다.",
        }
    )

    assert decision.proposed_fact_value is None
    assert decision.proposed_value_json is None


def test_present_non_persistent_event_can_be_stored_as_history_only() -> None:
    decision = CharacterFactComparisonDecision.model_validate(
        {
            "operation": "HISTORY_ONLY",
            "target_ref": None,
            "removed_snapshot_refs": [],
            "proposed_fact_value": None,
            "proposed_value_json": None,
            "temporal_scope": "PRESENT",
            "comparison_reason": "포션을 사용한 사건은 이력으로만 둔다.",
        }
    )

    assert decision.operation == "HISTORY_ONLY"
    assert decision.temporal_scope == "PRESENT"


def test_proposed_value_json_requires_an_object() -> None:
    decision_payload = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "36",
        "proposed_value_json": "36",
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 수치를 추가한다.",
    }
    with pytest.raises(ValueError):
        CharacterFactComparisonDecision.model_validate(decision_payload)

    with pytest.raises(ValueError):
        WorkerCharacterFactComparisonCompleteRequest.model_validate(
            {
                "operation": "ADD",
                "removedSnapshotEntries": [],
                "proposedFactValue": "36",
                "proposedValueJson": "36",
                "temporalScope": "PRESENT",
                "comparisonReason": "현재 수치를 추가한다.",
                "contextToken": "snapshot-v1",
                "rawComparisonJson": decision_payload,
            }
        )


class FakeTextClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(text=json.dumps(self.responses.pop(0), ensure_ascii=False))


def _candidate() -> WorkerCharacterFactComparisonCandidatePayload:
    return WorkerCharacterFactComparisonCandidatePayload.model_validate(
        {
            "candidateId": str(CANDIDATE_ID),
            "workId": "00000000-0000-0000-0000-000000000010",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000011",
            "entityName": "비요른",
            "attributeName": "status.회복",
            "attributeValue": "완전히 회복됨",
            "valueJson": {"active": True},
            "valueType": "JSON",
            "evidenceSpans": [
                {"quote": "상처가 완전히 나았다.", "startOffset": 10, "endOffset": 22}
            ],
            "matchedCharacterId": str(CHARACTER_ID),
            "matchedCharacterName": "비요른",
            "canonicalFactType": "STATUS",
            "canonicalFactKey": "status.회복",
            "confidence": 0.95,
        }
    )


def _snapshot_entry(
    fact_type: str,
    fact_key: str,
    value_json: dict,
) -> WorkerCharacterSnapshotEntry:
    return WorkerCharacterSnapshotEntry(
        fact_type=fact_type,
        fact_key=fact_key,
        fact_value="출혈 중" if fact_key == "status.출혈" else "현재 표시값",
        value_json=value_json,
    )
