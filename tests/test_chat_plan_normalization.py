"""합성 회귀: 계획기 응답의 흔한 계약 위반을 거절 대신 정규화한다."""

import pytest
from pydantic import ValidationError

from app.schemas.chat_hybrid import HybridPlan


def plan(**overrides):
    payload = {
        "disposition": "PROCEED",
        "registeredCurrentRequired": True,
        "rdbMode": "CURRENT",
        "sourceNeed": "NONE",
        "subjects": [
            {
                "domain": "CHARACTER",
                "name": "세린",
                "focus": {"factType": "PROFILE", "attribute": "종족"},
            },
            {
                "domain": "CHARACTER",
                "name": "라온",
                "focus": {"factType": "PROFILE", "attribute": "종족"},
            },
        ],
        "searchTerms": ["종족"],
        "includeEvidence": False,
        "episodeFrom": None,
        "episodeTo": None,
        "requestedParts": [{"ref": "P1", "description": "두 인물의 종족이 같은지"}],
        "sourceFallback": True,
    }
    payload.update(overrides)
    return payload


def test_required_current_with_fallback_is_normalized_instead_of_rejected():
    # 이전에는 'Required current information needs a current lookup without fallback'로 두 번 거절돼 실행이 실패했다.
    result = HybridPlan.model_validate(plan())
    assert result.registered_current_required is True
    assert result.source_fallback is False
    # 원문 요구가 없으면 보완용 요구 항목도 비운다.
    assert result.requested_parts == []


def test_required_current_with_source_need_keeps_parts_and_only_drops_fallback():
    result = HybridPlan.model_validate(plan(sourceNeed="SCENE", eventContext="두 인물의 정수 획득"))
    assert result.source_fallback is False
    assert [p.ref for p in result.requested_parts] == ["P1"]


def test_other_contract_errors_are_still_rejected():
    with pytest.raises(ValidationError):
        HybridPlan.model_validate(
            plan(registeredCurrentRequired=False, rdbMode="NONE", sourceNeed="NONE")
        )


def test_stray_requested_parts_without_source_need_are_dropped():
    # PLAN_PARTS_MODE: 원문 요구도 보완도 없는데 요구 항목만 남긴 응답은 재시도 대신 항목을 비운다.
    result = HybridPlan.model_validate(plan(registeredCurrentRequired=False, sourceFallback=False))
    assert result.requested_parts == [] and result.source_fallback is False


def test_fallback_without_parts_is_turned_off():
    # PLAN_FALLBACK_PARTS_REQUIRED: 보완할 요구 항목이 없으면 보완을 끈다.
    result = HybridPlan.model_validate(
        plan(registeredCurrentRequired=False, sourceFallback=True, requestedParts=[])
    )
    assert result.source_fallback is False
