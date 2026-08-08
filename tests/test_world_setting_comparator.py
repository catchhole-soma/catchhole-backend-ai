import json
from uuid import UUID

import pytest

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonTarget,
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
        "operation": operation,
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": candidate_setting_name,
        "proposed_value": candidate_value,
        "comparison_reason": "테스트 비교 이유",
        **decision_overrides,
    }
    text_client = FakeTextClient([decision])

    result, _ = WorldSettingComparator(
        llm_client=text_client,
        max_attempts=1,
    ).compare(candidate, [_target()])

    assert result.operation == operation
    user_prompt = text_client.requests[0]["user_prompt"]
    assert "T1" in user_prompt
    assert str(TARGET_ID) not in user_prompt


def test_comparator_retries_when_exclude_rewrites_extracted_value() -> None:
    candidate = _candidate("현재 소유자", "수아")
    invalid = {
        "operation": "EXCLUDE",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "현재 소유자",
        "proposed_value": "다른 값",
        "comparison_reason": "일시적 소유 상태다.",
    }
    valid = {**invalid, "proposed_value": "수아"}
    text_client = FakeTextClient([invalid, valid])

    result, _ = WorldSettingComparator(
        llm_client=text_client,
        max_attempts=2,
    ).compare(candidate, [_target()])

    assert result.proposed_value == "수아"
    assert len(text_client.requests) == 2


class FakeTextClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(
            text=json.dumps(self.responses.pop(0), ensure_ascii=False),
        )


def _candidate(setting_name: str, extracted_value: str) -> WorkerWorldSettingCandidatePayload:
    return WorkerWorldSettingCandidatePayload.model_validate(
        {
            "candidateId": "00000000-0000-0000-0000-000000000003",
            "workId": "00000000-0000-0000-0000-000000000010",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000011",
            "category": "RACE",
            "subjectName": "바바리안",
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
        properties_json={"서식지": "혹한 지역"},
        version=3,
    )
