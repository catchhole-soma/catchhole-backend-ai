import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonDecision,
)
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerCharacterFactComparisonCandidatePayload,
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
    assert "논리적으로 절대 양립 불가능하다는 수준까지 요구하지 않는다" in (
        client.requests[0]["system_prompt"]
    )
    assert "의미상 가까운 여러 STATUS를 함께 해소" in (
        client.requests[0]["system_prompt"]
    )
    assert "독립적·잠재적 상태까지 연쇄적으로 제거하지 않는다" in (
        client.requests[0]["system_prompt"]
    )
    assert client.requests[0]["prompt_cache_key"] == "character-fact-comparison:v4"
    assert prompt_payload["snapshot_entries"][0]["ref"] == "P1"
    assert prompt_payload["snapshot_entries"][0]["fact_value"] == "출혈 중"
    assert prompt_payload["exact_target_ref"] is None
    assert prompt_payload["allowed_operations"] == [
        "ADD",
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


def test_comparator_allows_only_status_snapshot_removal() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": ["P2"],
        "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": False},
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
        "proposed_value_json": {"active": False},
        "temporal_scope": "PRESENT",
        "comparison_reason": "P1을 갱신한다.",
    }
    valid = {**invalid, "target_ref": "P2"}
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
    assert raw["comparison_reason"] == decision.comparison_reason
    assert len(client.requests) == 2
    first_prompt = json.loads(client.requests[0]["user_prompt"])
    retry_prompt = json.loads(client.requests[1]["user_prompt"])
    assert first_prompt["exact_target_ref"] == "P2"
    assert first_prompt["allowed_operations"] == [
        "UPDATE",
        "MERGE",
        "HISTORY_ONLY",
        "EXCLUDE",
        "REVIEW_REQUIRED",
    ]
    assert "validation_feedback" not in first_prompt
    assert retry_prompt["validation_feedback"]["previous_response_rejected"] is True
    assert "exact_target_ref" in retry_prompt["validation_feedback"]["correction"]
    assert "canonical Fact key" in retry_prompt["validation_feedback"]["reason"]


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
    assert "ADD와 removed_snapshot_refs" in retry_prompt["validation_feedback"]["correction"]


def test_comparator_retries_add_when_canonical_slot_already_exists() -> None:
    invalid = {
        "operation": "ADD",
        "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": "완전히 회복됨",
        "proposed_value_json": {"active": False},
        "temporal_scope": "PRESENT",
        "comparison_reason": "새 항목으로 추가한다.",
    }
    valid = {
        **invalid,
        "operation": "UPDATE",
        "target_ref": "P1",
        "comparison_reason": "같은 canonical 항목의 값을 갱신한다.",
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


@pytest.mark.parametrize("operation", ["HISTORY_ONLY", "EXCLUDE", "REVIEW_REQUIRED"])
def test_non_snapshot_operations_discard_irrelevant_proposed_values(operation: str) -> None:
    decision = CharacterFactComparisonDecision.model_validate(
        {
            "operation": operation,
            "target_ref": None,
            "removed_snapshot_refs": [],
            "proposed_fact_value": "provider가 잘못 덧붙인 제안 표시값",
            "proposed_value_json": {"active": False},
            "temporal_scope": "PRESENT",
            "comparison_reason": "현재 snapshot을 자동 변경하지 않는다.",
        }
    )

    assert decision.proposed_fact_value is None
    assert decision.proposed_value_json is None


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
            "valueJson": {"active": False},
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
