"""Synthetic evidence obligations and correction recovery, not planner accuracy."""

import asyncio
import copy
import logging
import socket

import pytest
from pydantic import ValidationError

from app.chatbot.qa import response_schema
from app.schemas.chat_execution import ConversationTurn
from app.schemas.chat_hybrid import HybridPlan
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import answer, claim, search_with

SOURCE = "세린은 자신을 남자라고 소개했다."


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Authority regressions must not call external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def current_case(*, mixed=False, required=True):
    case = scenario()
    case.update(question="현재 세린의 성별은?" + (" 원문에서는 어떻게 나와?" if mixed else ""))
    case["outputs"] = [
        {
            "disposition": "PROCEED",
            "rdbMode": "CURRENT",
            "sourceNeed": "SCENE" if mixed else "NONE",
            "registeredCurrentRequired": required,
            "subjects": [
                {
                    "domain": "CHARACTER",
                    "name": "세린",
                    "focus": {"factType": "PROFILE", "attribute": "성별"},
                }
            ],
            "searchTerms": ["세린"],
            "includeEvidence": mixed,
            "episodeFrom": None,
            "episodeTo": None,
            "sourceFallback": False,
            "requestedFacets": [],
            "requestedParts": [{"ref": "P1", "description": "원문에 나타난 세린의 성별"}]
            if mixed
            else [],
            "includeRegisteredValues": False,
            "eventContext": None,
            "eventScope": None,
        }
    ]
    case.update(keyword=[], vector=[])
    return case


def current_catalog(spring):
    row = item(701, "세린", category="PROFILE", key="profile.gender", label="성별", value="여자")
    row["origin"] = "USER_INPUT"
    spring.catalog = [catalog_subject(701, "세린", current=[row])]


def missing_catalog(spring):
    # An apparently related but differently named character is not an alias proof.
    row = item(
        702, "다른 이름", category="PROFILE", key="profile.gender", label="성별", value="여자"
    )
    spring.catalog = [catalog_subject(702, "다른 이름", current=[row])]


def source_answer():
    return answer(claim(SOURCE, SOURCE, parts=("P1",), event_match=None))


def run(tmp_path, case, setup=current_catalog, **kwargs):
    return asyncio.run(replay(tmp_path, case, spring_setup=setup, **kwargs))


def result_text(result):
    assert result["finish"]["failure"] is None, result["finish"]
    return result["finish"]["result"]["text"]


def test_required_current_decision_is_strict_and_cannot_silently_default():
    plan = current_case()["outputs"][0]
    assert "registeredCurrentRequired" in response_schema(HybridPlan).schema["required"]
    for value in (None, "true", 1):
        with pytest.raises(ValidationError):
            HybridPlan.model_validate({**plan, "registeredCurrentRequired": value})
    del plan["registeredCurrentRequired"]
    with pytest.raises(ValidationError, match="registeredCurrentRequired"):
        HybridPlan.model_validate(plan)


@pytest.mark.parametrize("changes", [{"rdbMode": "NONE"}, {"rdbMode": "HISTORY"}])
def test_current_requirement_cannot_use_an_incompatible_route(changes):
    with pytest.raises(ValidationError, match="Required current information"):
        HybridPlan.model_validate({**current_case(mixed=True)["outputs"][0], **changes})


def test_current_requirement_drops_source_fallback_instead_of_rejecting():
    # 계획기가 등록값 확인 요구에 원문 보완까지 붙이면 거절(실행 실패)이 아니라 보완만 끈다.
    plan = HybridPlan.model_validate(
        {**current_case(mixed=True)["outputs"][0], "sourceFallback": True}
    )
    assert plan.registered_current_required is True and plan.source_fallback is False


def test_current_value_uses_manual_snapshot_without_manuscript_fallback(tmp_path):
    result = run(tmp_path, current_case(), search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    text = result_text(result)
    assert "여자" in text and "직접 입력" in text
    assert "남자" not in text
    assert not result["search"].calls and not result["embedding"].inputs
    assert [q["tool"] for q in result["spring"].queries] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_CURRENT",
    ]


def test_missing_current_subject_is_not_replaced_by_source_or_another_character(tmp_path):
    result = run(
        tmp_path,
        current_case(),
        missing_catalog,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
    )
    assert "대상을 찾지 못" in result_text(result)
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert [q["tool"] for q in result["spring"].queries] == ["FIND_CHARACTERS"]
    assert not result["search"].calls and not result["embedding"].inputs


def test_manuscript_only_answer_does_not_inherit_the_current_snapshot(tmp_path):
    case = current_case(mixed=True, required=False)
    case["question"] = "원문에서 세린의 성별은 어떻게 나와?"
    case["outputs"][0]["rdbMode"] = "NONE"
    case["outputs"].append(source_answer())
    result = run(tmp_path, case, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    text = result_text(result)
    assert SOURCE in text and "원문에서 확인한 내용" in text
    assert "여자" not in text and "현재 등록" not in text
    assert not result["spring"].queries


def test_mixed_answer_keeps_manual_current_and_source_conflict_separate(tmp_path):
    case = current_case(mixed=True)
    response = source_answer()
    response.update(
        coverage="PARTIAL",
        conflicts=[
            {
                "rowRefs": ["R1"],
                "evidenceRefs": ["E1"],
                "supports": [{"evidenceRef": "E1", "quote": SOURCE}],
            }
        ],
    )
    case["outputs"].append(response)
    result = run(tmp_path, case, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    text = result_text(result)
    assert SOURCE in text and "현재 성별은 여자" in text
    assert "직접 입력" in text and "등록된 성별 설정과 원문의 내용이 다르게" in text
    payload = result["raw"].requests[-1]["input"]
    assert payload["registeredCurrent"] == {"required": True, "confirmed": True}
    assert payload["rows"][0]["value"] == "여자"
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


def test_mixed_missing_current_remains_unconfirmed_after_complete_source_answer(tmp_path, caplog):
    case = current_case(mixed=True)
    case["outputs"].append(source_answer())
    with caplog.at_level(logging.INFO, logger="app.chatbot.hybrid"):
        result = run(
            tmp_path, case, missing_catalog, search_setup=search_with(keyword=[passage(SOURCE, 1)])
        )
    text = result_text(result)
    assert SOURCE in text and "현재 등록 설정은 확인하지 못" in text
    payload = result["raw"].requests[-1]["input"]
    assert payload["registeredCurrent"] == {"required": True, "confirmed": False}
    assert payload["rows"] == []
    assert "stage=published data={'coverage': 'PARTIAL'" in caplog.text
    assert "다른 이름" not in text


def test_empty_history_does_not_erase_a_confirmed_current_read(tmp_path):
    case = current_case(mixed=True)
    case["outputs"][0]["rdbMode"] = "CURRENT_AND_HISTORY"
    case["outputs"].append(source_answer())
    result = run(tmp_path, case, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    assert "현재 성별은 여자" in result_text(result)
    assert result["raw"].requests[-1]["input"]["registeredCurrent"]["confirmed"] is True


def test_truncated_current_value_cannot_satisfy_current_obligation(tmp_path):
    def truncated(spring):
        current_catalog(spring)
        spring.catalog[0]["current"][0]["valueTruncated"] = True

    case = current_case(mixed=True)
    case["outputs"].append(source_answer())
    result = run(tmp_path, case, truncated, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    assert "현재 등록 설정은 확인하지 못" in result_text(result)
    assert result["raw"].requests[-1]["input"]["registeredCurrent"]["confirmed"] is False


@pytest.mark.parametrize("per_item", [False, True])
def test_mixed_missing_source_preserves_the_current_answer(tmp_path, per_item):
    case = current_case(mixed=True)
    case["outputs"][0]["perItem"] = per_item
    result = run(tmp_path, case)
    text = result_text(result)
    assert "현재 성별은 여자" in text and "원문 내용은 이번 조회에서 확인하지 못" in text
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


def correction_catalog(spring):
    current_catalog(spring)
    spring.context = spring.context.model_copy(
        update={
            "conversation": [
                ConversationTurn(
                    question="지금 세린은 남자야 여자야?",
                    answer="남자입니다.",
                )
            ]
        }
    )


@pytest.mark.parametrize("value", ["여자", "남자"])
def test_value_only_correction_repairs_unobserved_attribute_then_maps_real_field(tmp_path, value):
    case = current_case()
    case["question"] = f"{value}일 텐데?"
    invalid = copy.deepcopy(case["outputs"][0])
    repaired = copy.deepcopy(invalid)
    repaired["subjects"][0]["focus"]["attribute"] = value
    case["outputs"] = [invalid, repaired, {"outcome": "ANSWER", "fieldRefs": ["F1"]}]
    result = run(tmp_path, case, correction_catalog)
    assert "여자" in result_text(result)  # The correction's value is not accepted as evidence.
    assert len(result["raw"].requests) == 3
    repair = result["raw"].requests[1]["system"]
    assert "UNOBSERVED_FOCUS_ATTRIBUTE" in repair and "$.subjects[].focus.attribute" in repair
    field_payload = result["raw"].requests[2]["input"]
    assert field_payload["attribute"] == value
    assert field_payload["fields"][0]["displayName"] == "성별"
    assert not result["search"].calls
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3


def test_repeated_focus_failure_clarifies_without_changing_scope_or_leaking_terms(tmp_path, caplog):
    case = current_case()
    case["question"] = "여자일 텐데?"
    private = "private-unobserved-attribute"
    case["outputs"][0]["subjects"][0]["focus"]["attribute"] = private
    case["outputs"] *= 2
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = run(tmp_path, case, correction_catalog)
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "CLARIFICATION"
    assert not result["spring"].queries and not result["search"].calls
    assert len(result["raw"].requests) == 2
    assert private not in caplog.text and private not in result["raw"].requests[1]["system"]


def test_ambiguous_registered_name_stops_before_current_or_source_lookup(tmp_path):
    def ambiguous(spring):
        current_catalog(spring)
        spring.catalog.append(catalog_subject(702, "세린"))

    result = run(
        tmp_path,
        current_case(mixed=True),
        ambiguous,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
    )
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "CLARIFICATION"
    assert not result["search"].calls
    assert [q["tool"] for q in result["spring"].queries] == ["FIND_CHARACTERS"]
