"""Offline recovery contracts; authored model responses do not measure model accuracy."""

import asyncio
import copy
import json
import logging
import socket

import pytest

from app.chatbot.qa_render import NOTICES
from app.chatbot.qa_validation import RULES
from tests.test_chat_current_authority import current_case
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    BEHAVIOR,
    EMOTION,
    answer,
    assert_answer,
    claim,
    parts_case,
    search_with,
)

PRIVATE = "rejected-private-value-5cfaf980-8f54-4b0a-a8c6-01e9daff9f61"
REGISTERED_PLAN = {
    "registeredCurrentRequired": False,
    "rdbMode": "CURRENT",
    "sourceNeed": "NONE",
    "subjects": [
        {
            "domain": "CHARACTER",
            "name": "미라",
            "worldCategory": None,
            "focus": {"factType": "SKILL", "attribute": None},
        }
    ],
    "requestedParts": [],
    "requestedFacets": [],
    "eventScope": None,
    "eventContext": None,
}


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Plan recovery tests cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


# 등록 전용 계획에 남은 requestedParts(PLAN_PARTS_MODE)와 요구 항목 없는 sourceFallback
# (PLAN_FALLBACK_PARTS_REQUIRED)은 재시도 대신 정규화한다 — tests/test_chat_plan_normalization.py.
@pytest.mark.parametrize(
    ("changes", "code", "path"),
    [
        ({"requestedFacets": ["TIME"]}, "PLAN_PARTS_MODE", "$.requestedParts"),
        ({"requestedParts": []}, "PLAN_PARTS_REQUIRED", "$.requestedParts"),
        (
            {"requestedParts": [{"ref": "P2", "description": PRIVATE}]},
            "PLAN_PARTS_REFERENCES",
            "$.requestedParts",
        ),
        (
            {"requestedParts": [{"ref": "P1", "description": " "}]},
            "PLAN_PARTS_REFERENCES",
            "$.requestedParts",
        ),
        (
            {**REGISTERED_PLAN, "requestedFacets": ["P1"]},
            "PLAN_PARTS_FIELD",
            "$.requestedFacets",
        ),
        (
            {**REGISTERED_PLAN, "eventContext": PRIVATE},
            "PLAN_EVENT_CONTEXT",
            "$.eventContext",
        ),
        ({"eventContext": " "}, "PLAN_EVENT_CONTEXT", "$.eventContext"),
        ({"disposition": "GREETING"}, "PLAN_CONVERSATION_ONLY", "$"),
    ],
)
def test_cross_field_failure_provides_specific_safe_feedback_then_recovers(
    tmp_path, caplog, changes, code, path
):
    case = parts_case()
    valid_plan = case["outputs"][0]
    invalid_plan = {**copy.deepcopy(valid_plan), **copy.deepcopy(changes)}
    text = f"{EMOTION} {BEHAVIOR}"
    case["outputs"] = [invalid_plan, valid_plan, answer(claim(text, text))]

    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = asyncio.run(
            replay(tmp_path, case, search_setup=search_with(keyword=[passage(text, 120)]))
        )

    assert_answer(result)
    records = [record for record in caplog.records if record.name == "app.chatbot.qa"]
    assert len(records) == 1
    assert records[0].validation_stage == "RESPONSE_SCHEMA"
    assert records[0].validation_errors == [{"code": code, "path": path}]
    assert PRIVATE not in repr(records[0].__dict__)
    retry = result["raw"].requests[1]["system"]
    assert code in retry and path in retry and RULES[code] in retry
    assert PRIVATE not in retry
    assert len(result["raw"].requests) == len(result["spring"].reservations) == 3
    assert len(result["spring"].reports) == 3
    assert all(report["outcome"] == "SUCCESS" for report in result["spring"].reports)


def test_unobserved_focus_is_retried_once_then_dropped_before_any_search(tmp_path, caplog):
    # 질문에 없는 항목 이름은 한 번 재전달하고, 마지막 시도도 같은 오류만 남으면 그 이름을 비운 뒤
    # 유형(SKILL)으로 진행한다. 거절값은 로그·지침에 남지 않고 검색은 시작하지 않는다.
    case = parts_case()
    invalid = copy.deepcopy(case["outputs"][0])
    invalid["subjects"] = [
        {
            "domain": "CHARACTER",
            "name": "미라",
            "worldCategory": None,
            "focus": {"factType": "SKILL", "attribute": PRIVATE},
        }
    ]
    case["outputs"] = [invalid, invalid]

    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = asyncio.run(replay(tmp_path, case))

    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert NOTICES["FOCUS_ATTRIBUTE_DROPPED"] in result["finish"]["result"]["limitations"]
    records = [record for record in caplog.records if record.name == "app.chatbot.qa"]
    assert len(records) == 2
    assert all(record.validation_stage == "RESPONSE_CONTRACT" for record in records)
    assert all(
        record.validation_errors
        == [{"code": "UNOBSERVED_FOCUS_ATTRIBUTE", "path": "$.subjects[].focus.attribute"}]
        for record in records
    )
    assert all("qa_lookup.validate_focus:" in record.origin for record in records)
    assert PRIVATE not in caplog.text
    assert "UNOBSERVED_FOCUS_ATTRIBUTE" in result["raw"].requests[1]["system"]
    assert PRIVATE not in result["raw"].requests[1]["system"]
    # 계획 호출은 두 번뿐이고, 비운 항목 이름은 이후 조회·검색 어디에도 전달되지 않는다.
    assert len(result["raw"].requests) == 2
    assert PRIVATE not in json.dumps(result["spring"].queries, ensure_ascii=False)
    assert all(PRIVATE not in str(call) for call in result["search"].calls)


@pytest.mark.parametrize(
    "field,code,path",
    [
        ("searchTerms", "UNOBSERVED_SEARCH_TERM", "$.searchTerms[]"),
        ("subjects", "UNOBSERVED_SUBJECT_NAME", "$.subjects[].name"),
    ],
)
def test_unobserved_search_vocabulary_repairs_exact_field_and_preserves_requests(
    tmp_path,
    caplog,
    field,
    code,
    path,
):
    case = parts_case()
    valid = copy.deepcopy(case["outputs"][0])
    invalid = copy.deepcopy(valid)
    invalid[field] = (
        [PRIVATE]
        if field == "searchTerms"
        else [
            {
                "domain": "CHARACTER",
                "name": PRIVATE,
                "focus": {"factType": None, "attribute": None},
            }
        ]
    )
    case["outputs"] = [
        invalid,
        valid,
        answer(claim(EMOTION, EMOTION, parts=("P1",)), claim(BEHAVIOR, BEHAVIOR, parts=("P2",))),
    ]
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = asyncio.run(
            replay(
                tmp_path,
                case,
                search_setup=search_with(keyword=[passage(f"{EMOTION} {BEHAVIOR}", 1)]),
            )
        )
    assert_answer(result)
    assert result["raw"].requests[-1]["input"]["plan"]["requestedParts"] == valid["requestedParts"]
    feedback = result["raw"].requests[1]["system"]
    assert code in feedback and path in feedback
    assert PRIVATE not in feedback and PRIVATE not in caplog.text
    assert len(result["raw"].requests) == len(result["spring"].reservations) == 3
    assert result["search"].calls
    assert result["spring"].receipts


@pytest.mark.parametrize("source_need", ["NONE", "REASON"])
def test_duplicate_subject_repair_preserves_multi_field_history_and_explanation(
    tmp_path, source_need
):
    case = current_case(required=False)
    case["question"] = "세린의 장비 등급과 훈련 단계 이력을 구분해서 설명해줘."
    valid = case["outputs"][0]
    valid.update(
        rdbMode="HISTORY",
        sourceNeed=source_need,
        answerShape="SUMMARY",
        subjects=[
            {
                "domain": "CHARACTER",
                "name": "세린",
                "focus": {"factType": None, "attribute": "장비 등급과 훈련 단계"},
            }
        ],
        requestedParts=[{"ref": "P1", "description": "두 기록의 차이"}]
        if source_need != "NONE"
        else [],
    )
    invalid = copy.deepcopy(valid)
    invalid["subjects"] = [
        {
            "domain": "CHARACTER",
            "name": "세린",
            "focus": {"factType": category, "attribute": attribute},
        }
        for category, attribute in [("ITEM", "장비 등급"), ("LEVEL", "훈련 단계")]
    ]
    explanation = "장비 등급과 훈련 단계는 서로 다른 기록이다."
    response = {"claims": [{"text": explanation, "rowRefs": ["R1", "R2"]}]}
    if source_need != "NONE":
        response = answer(
            {
                **claim(explanation, explanation, parts=("P1",), event_match=None),
                "rowRefs": ["R1", "R2"],
            }
        )
    case["outputs"] = [invalid, valid, {"outcome": "ANSWER", "fieldRefs": ["F1", "F2"]}, response]

    def setup(spring):
        history = [
            item(
                701,
                "세린",
                kind="HISTORY",
                category=category,
                key=key,
                label=label,
                value=value,
                number=n,
                episode=1,
            )
            for n, (category, key, label, value) in enumerate(
                [
                    ("ITEM", "item.grade", "장비 등급", "4"),
                    ("LEVEL", "level.stage", "훈련 단계", "7"),
                    ("SKILL", "skill.unrelated", "무관한 기술", "제외되어야 하는 값"),
                ],
                901,
            )
        ]
        spring.catalog = [catalog_subject(701, "세린", history=history)]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            search_setup=search_with(keyword=[passage(explanation, 1)]),
        )
    )
    assert explanation in assert_answer(result)["text"]
    assert "제외되어야" not in result["finish"]["result"]["text"]
    feedback = result["raw"].requests[1]["system"]
    assert "PLAN_SUBJECTS" in feedback and RULES["PLAN_SUBJECTS"] in feedback
    field_payload = result["raw"].requests[2]["input"]
    assert field_payload["attribute"] == "장비 등급과 훈련 단계"
    answer_payload = result["raw"].requests[-1]["input"]
    assert len(answer_payload["rows"]) == 2
    assert all(row["kind"] == "HISTORY" for row in answer_payload["rows"])
    assert [q["tool"] for q in result["spring"].queries] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_HISTORY",
        "CHARACTER_HISTORY",
    ]
    assert bool(result["search"].calls) == (source_need != "NONE")
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 4
