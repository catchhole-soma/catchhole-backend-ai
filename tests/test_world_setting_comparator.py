import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
)

TARGET_ID = UUID("00000000-0000-0000-0000-000000000004")


@pytest.mark.parametrize(
    ("operation", "candidate_setting_name", "candidate_value", "decision_overrides"),
    [
        ("ADD", "사회 구조", "부족 단위", {"target_ref": "T1"}),
        (
            "UPDATE",
            "서식지",
            "극지방",
            {"target_ref": "T1", "matched_property_name": "서식지"},
        ),
        (
            "MERGE",
            "서식지",
            "극지방",
            {
                "target_ref": "T1",
                "matched_property_name": "서식지",
                "proposed_value": "혹한 지역과 극지방",
            },
        ),
        ("EXCLUDE", "현재 소유자", "수아", {}),
        (
            "EXCLUDE",
            "새 서식지 설명",
            "혹한 지역에 산다.",
            {"target_ref": "T1", "matched_property_name": "서식지"},
        ),
    ],
)
def test_comparator_accepts_each_supported_operation(
    operation: str,
    candidate_setting_name: str,
    candidate_value: str,
    decision_overrides: dict,
) -> None:
    candidate = _candidate(candidate_setting_name, candidate_value)
    decision = {
        "consolidation_status": "SINGLE",
        "operation": operation,
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": candidate_setting_name,
        "proposed_value": candidate_value,
        "comparison_reason": "테스트 비교 이유",
        **decision_overrides,
    }
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.operation == operation
    user_prompt = text_client.requests[0]["user_prompt"]
    assert "T1" in user_prompt
    assert str(TARGET_ID) not in user_prompt
    prompt_payload = json.loads(user_prompt)
    assert prompt_payload["candidate"]["evidence_spans"] == [
        {"quote": "원문 근거", "start_offset": None, "end_offset": None}
    ]


def test_comparator_restores_single_extracted_value_without_retry() -> None:
    candidate = _candidate("현재 소유자", "수아")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "EXCLUDE",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "현재 소유자",
        "proposed_value": "다른 값",
        "comparison_reason": "일시적 소유 상태다.",
    }
    text_client = FakeTextClient([invalid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.proposed_value == "수아"
    assert result.consolidation_status == "SINGLE"
    assert len(text_client.requests) == 1


def test_comparator_preserves_single_candidate_merge_value_when_conflict_is_normalized() -> None:
    candidate = _candidate("서식지", "극지방")
    merged_value = "혹한 지역과 극지방"
    text_client = FakeTextClient([{
        "consolidation_status": "CONFLICT",
        "operation": "MERGE",
        "target_ref": "T1",
        "matched_property_name": "서식지",
        "proposed_setting_name": "서식지",
        "proposed_value": merged_value,
        "comparison_reason": "기존 서식지 설명과 신규 정보를 함께 보존한다.",
    }])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.consolidation_status == "SINGLE"
    assert result.operation == "MERGE"
    assert result.proposed_value == merged_value


def test_comparator_restores_add_identity_fields_without_retry() -> None:
    candidate = _candidate("도달 가능성", "미궁 진입 직후 외곽으로 떨어질 수 있다.")
    invalid = {
        "consolidation_status": "MERGED",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_scope_name": "다른 범위",
        "proposed_setting_name": "다듬은 설정명",
        "proposed_value": "미궁 외곽으로 이동할 가능성이 있다.",
        "comparison_reason": "기존에 없는 설정이다.",
    }
    text_client = FakeTextClient([invalid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.consolidation_status == "SINGLE"
    assert result.proposed_scope_name == candidate.scope_name
    assert result.proposed_setting_name == candidate.setting_name
    assert result.proposed_value == candidate.extracted_value


def test_comparator_passes_validation_reason_to_retry() -> None:
    candidate = _candidate("서식지", "극지방")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "UPDATE",
        "target_ref": "T1",
        "matched_property_name": "존재하지 않는 속성",
        "proposed_setting_name": "존재하지 않는 속성",
        "proposed_value": "극지방",
        "comparison_reason": "기존 서식지를 바꾼다.",
    }
    valid = {
        **invalid,
        "matched_property_name": "서식지",
        "proposed_setting_name": "서식지",
    }
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [_target()])
    )

    assert result.matched_property_name == "서식지"
    first_prompt = json.loads(text_client.requests[0]["user_prompt"])
    retry_prompt = json.loads(text_client.requests[1]["user_prompt"])
    assert "validation_feedback" not in first_prompt
    assert retry_prompt["validation_feedback"]["previous_response_rejected"] is True
    assert "does not exist" in retry_prompt["validation_feedback"]["reason"]


def test_comparator_consolidates_multiple_same_key_values_for_add() -> None:
    source_values = [
        "공명시킨 메시지 스톤끼리 대화할 수 있다.",
        "짧게 읊조려 신호를 보낼 수 있다.",
        "조작해 연락 내용을 수신할 수 있다.",
    ]
    candidate = _candidate("기능", "\n".join(source_values))
    merged_value = (
        "공명시킨 메시지 스톤끼리 대화할 수 있으며, 짧게 읊조려 신호를 보내고 "
        "조작해 연락 내용을 수신할 수 있다."
    )
    text_client = FakeTextClient([{
        "consolidation_status": "MERGED",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "기능",
        "proposed_value": merged_value,
        "comparison_reason": "같은 기능을 설명하는 원문 근거를 하나의 설정으로 정리했다.",
    }])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [])
    )

    assert result.proposed_value == merged_value
    prompt_payload = json.loads(text_client.requests[0]["user_prompt"])
    assert prompt_payload["candidate"]["extracted_values"] == source_values


def test_comparator_retries_when_exclude_matches_property_without_target() -> None:
    candidate = _candidate("새 서식지 설명", "혹한 지역에 산다.")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "EXCLUDE",
        "target_ref": None,
        "matched_property_name": "서식지",
        "proposed_setting_name": "새 서식지 설명",
        "proposed_value": "혹한 지역에 산다.",
        "comparison_reason": "기존 서식지와 같은 내용이다.",
    }
    valid = {**invalid, "target_ref": "T1"}
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [_target()])
    )

    assert result.target_ref == "T1"
    assert result.matched_property_name == "서식지"
    assert len(text_client.requests) == 2


def test_comparator_replaces_internal_target_reference_in_user_facing_reason() -> None:
    candidate = _candidate("서식지", "극지방")
    text_client = FakeTextClient([
        {
            "consolidation_status": "SINGLE",
            "operation": "MERGE",
            "target_ref": "T1",
            "matched_property_name": "서식지",
            "proposed_setting_name": "서식지",
            "proposed_value": "혹한 지역과 극지방",
            "comparison_reason": "T1의 기존 서식지와 모순되지 않아 병합한다.",
        }
    ])

    result, raw_result = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.comparison_reason == "기존 '바바리안' 설정의 기존 서식지와 모순되지 않아 병합한다."
    assert raw_result["comparison_reason"] == result.comparison_reason
    assert "T1" not in result.comparison_reason


def test_comparator_preserves_conflicting_values_for_user_review() -> None:
    source_values = ["통신 반경은 약 300m다.", "통신 반경은 약 3km다."]
    candidate = _candidate("통신 반경", "\n".join(source_values))
    text_client = FakeTextClient([{
        "consolidation_status": "CONFLICT",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "통신 반경",
        "proposed_value": "\n".join(source_values),
        "comparison_reason": "원문마다 통신 반경이 달라 최종값 확인이 필요하다.",
    }])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [])
    )

    assert result.consolidation_status == "CONFLICT"
    assert result.proposed_value == candidate.extracted_value


def test_comparator_restores_conflicting_source_values_without_retry() -> None:
    candidate = _candidate("통신 반경", "약 300m\n약 3km")
    invalid = {
        "consolidation_status": "CONFLICT",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "통신 반경",
        "proposed_value": "약 300m 또는 약 3km",
        "comparison_reason": "두 수치가 달라 확인이 필요하다.",
    }
    text_client = FakeTextClient([invalid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [])
    )

    assert result.proposed_value == candidate.extracted_value
    assert len(text_client.requests) == 1


def test_comparator_never_matches_same_setting_name_from_a_different_scope() -> None:
    candidate = _candidate(
        "방향별 몬스터 출몰 규칙",
        "동쪽에서 고블린이 출몰한다.",
        scope_name="1층",
    )
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "UPDATE",
        "target_ref": "T1",
        "matched_scope_name": "2층",
        "matched_property_name": "방향별 몬스터 출몰 규칙",
        "proposed_scope_name": "2층",
        "proposed_setting_name": "방향별 몬스터 출몰 규칙",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "다른 층의 기존 설정을 갱신한다.",
    }
    valid = {
        **invalid,
        "matched_scope_name": "1층",
        "proposed_scope_name": "1층",
        "comparison_reason": "1층의 기존 설정을 갱신한다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="미궁",
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="방향별 몬스터 출몰 규칙",
                value="동쪽에서 슬라임이 출몰한다.",
            ),
            WorkerWorldSettingProperty(
                scope_name="2층",
                setting_name="방향별 몬스터 출몰 규칙",
                value="동쪽에서 오크가 출몰한다.",
            ),
        ],
        version=3,
    )
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [target])
    )

    assert result.matched_scope_name == "1층"
    assert result.proposed_scope_name == "1층"
    prompt_payload = json.loads(text_client.requests[0]["user_prompt"])
    assert prompt_payload["candidate"]["scope_name"] == "1층"
    assert [property["scope_name"] for property in prompt_payload["targets"][0]["properties"]] == [
        "1층",
        "2층",
    ]
    assert len(text_client.requests) == 2


class FakeTextClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(
            text=json.dumps(self.responses.pop(0), ensure_ascii=False),
        )


def _candidate(
    setting_name: str,
    extracted_value: str,
    scope_name: str | None = None,
) -> WorkerWorldSettingCandidatePayload:
    return WorkerWorldSettingCandidatePayload.model_validate(
        {
            "candidateId": "00000000-0000-0000-0000-000000000003",
            "workId": "00000000-0000-0000-0000-000000000010",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000011",
            "category": "RACE",
            "subjectName": "바바리안",
            "scopeName": scope_name,
            "settingName": setting_name,
            "extractedValue": extracted_value,
            "evidenceSpans": [{"quote": "원문 근거"}],
            "extractionConfidence": 0.95,
        }
    )


def _target() -> WorkerWorldSettingComparisonTarget:
    return WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="바바리안",
        properties=[
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="서식지",
                value="혹한 지역",
            )
        ],
        version=3,
    )
