"""Authored current/source comparison recovery; no external calls or model quality score."""

import asyncio
import copy
import logging
import socket

import pytest

from tests.test_chat_current_authority import current_case
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import answer, assert_answer, claim, search_with

SOURCE = "도윤은 자신의 레벨이 5라고 말했다."
COMPARISON = "현재 등록된 도윤의 레벨은 17이고, 원문에서 말한 레벨은 5로 서로 다릅니다."


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Comparison regressions cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def comparison_case():
    case = current_case(mixed=True)
    case["question"] = "현재 도윤의 레벨과 원문의 레벨을 비교해줘."
    case["outputs"][0].update(
        subjects=[
            {
                "domain": "CHARACTER",
                "name": "도윤",
                "focus": {"factType": "LEVEL", "attribute": None},
            }
        ],
        searchTerms=["도윤", "레벨"],
        requestedParts=[
            {"ref": "P1", "description": "현재 등록된 레벨"},
            {"ref": "P2", "description": "원문의 레벨과 현재값의 차이"},
        ],
    )
    return case


def current_catalog(spring):
    row = item(801, "도윤", category="LEVEL", key="level", label="레벨", value="17")
    spring.catalog = [catalog_subject(801, "도윤", current=[row])]


def comparison_answer():
    response = answer(
        {**claim(COMPARISON, SOURCE, event_match=None), "rowRefs": ["R1"]},
        coverage="PARTIAL",
    )
    response["conflicts"] = [
        {
            "rowRefs": ["R1"],
            "evidenceRefs": ["E1"],
            "supports": [{"evidenceRef": "E1", "quote": SOURCE}],
        }
    ]
    return response


@pytest.mark.parametrize(
    "problem,code,path",
    [
        ("source_only", "REQUESTED_PARTS_INCOMPLETE", "$.missingFacets"),
        ("complete_conflict", "ANSWER_COVERAGE_MISMATCH", "$.coverage"),
        ("complete_missing", "ANSWER_COVERAGE_MISMATCH", "$.coverage"),
    ],
)
def test_comparison_recovers_each_contract_error_without_losing_either_source(
    tmp_path, caplog, problem, code, path
):
    case = comparison_case()
    valid = comparison_answer()
    invalid = copy.deepcopy(valid)
    if problem == "source_only":
        invalid = answer(claim(SOURCE, SOURCE, parts=("P2",), event_match=None))
    elif problem == "complete_conflict":
        invalid["coverage"] = "COMPLETE"
    else:
        invalid = answer(
            claim(SOURCE, SOURCE, parts=("P2",), event_match=None),
            missing=("P1",),
            coverage="COMPLETE",
        )
    case["outputs"] += [invalid, valid]
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = asyncio.run(
            replay(
                tmp_path,
                case,
                spring_setup=current_catalog,
                search_setup=search_with(keyword=[passage(SOURCE, 1)]),
            )
        )

    response = assert_answer(result)
    assert COMPARISON in response["text"]
    assert "등록 정보와 원문을 구분해 확인" in response["text"]
    records = [r for r in caplog.records if r.name == "app.chatbot.qa"]
    assert len(records) == 1 and records[0].validation_errors == [{"code": code, "path": path}]
    feedback = result["raw"].requests[-1]["system"]
    assert code in feedback and path in feedback
    assert SOURCE not in feedback and COMPARISON not in feedback
    payload = result["raw"].requests[-1]["input"]
    assert payload["registeredCurrent"] == {"required": True, "confirmed": True}
    assert payload["rows"][0]["value"] == "17"
    assert len(result["raw"].requests) == 3
    reservations = result["spring"].reservations
    assert len(reservations) == len(result["spring"].reports) == 4
    assert sum(call["purpose"] == "CHAT_QUERY_EMBEDDING" for call in reservations) == 1
    assert result["search"].calls and result["spring"].receipts
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


@pytest.mark.parametrize("problem", ["unknown_row", "blank_support"])
def test_comparison_still_rejects_unknown_rows_and_missing_source_support(tmp_path, problem):
    case = comparison_case()
    invalid = comparison_answer()
    if problem == "unknown_row":
        invalid["claims"][0]["rowRefs"] = ["R999"]
    else:
        invalid["claims"][0]["supports"] = []
    case["outputs"] += [invalid, invalid]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=current_catalog,
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
            hybrid={"max_rounds": 1},
        )
    )
    response = assert_answer(result)
    assert COMPARISON not in response["text"]
    assert "17" in response["text"] and "원문 내용은 이번 조회에서 확인하지 못" in response["text"]
    assert len(result["raw"].requests) == len(result["spring"].reports) == 3
