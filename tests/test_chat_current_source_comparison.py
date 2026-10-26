"""Authored current/source comparison recovery; no external calls or model quality score."""

import asyncio
import copy
import logging
import socket

import pytest

from app.chatbot.hybrid import _HybridSession
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
    assert len(reservations) == len(result["spring"].reports) == 3
    assert not result["embedding"].inputs
    assert not any(call["purpose"] == "CHAT_QUERY_EMBEDDING" for call in reservations)
    assert result["search"].calls and result["spring"].receipts
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


@pytest.mark.parametrize("coverage", ["COMPLETE", "PARTIAL", "NONE"])
def test_comparison_corrects_coverage_without_rewriting_supported_facts(
    tmp_path, caplog, monkeypatch, coverage
):
    case = comparison_case()
    response = comparison_answer()
    response["coverage"] = coverage
    # No replacement answer is scripted: metadata repair must not pay for a new
    # generation that can substitute another subject or a similarly named value.
    case["outputs"] += [response]
    captured = []
    finish = _HybridSession.finish_answer

    async def capture(self, answer):
        captured.append(answer)
        return await finish(self, answer)

    monkeypatch.setattr(_HybridSession, "finish_answer", capture)
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = asyncio.run(
            replay(
                tmp_path,
                case,
                spring_setup=current_catalog,
                search_setup=search_with(keyword=[passage(SOURCE, 1)]),
                hybrid={"max_rounds": 2},
            )
        )

    rendered = assert_answer(result)
    assert COMPARISON in rendered["text"]
    assert "등록 정보와 원문을 구분해 확인" in rendered["text"]
    assert captured[0].coverage == "PARTIAL"
    assert captured[0].conflicts
    assert not captured[0].missing_facets
    assert not [r for r in caplog.records if r.name == "app.chatbot.qa"]
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2
    assert not result["embedding"].inputs
    assert "vector" not in result["search"].calls
    assert result["search"].calls[-1] == "revalidate"
    assert result["spring"].receipts
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


def test_comparison_keeps_missing_requirement_when_correcting_complete_metadata(tmp_path):
    case = comparison_case()
    response = answer(
        claim(SOURCE, SOURCE, parts=("P2",), event_match=None),
        missing=("P1",),
        coverage="COMPLETE",
    )
    case["outputs"] += [response]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=current_catalog,
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
            hybrid={"max_rounds": 2},
        )
    )

    rendered = assert_answer(result)
    assert SOURCE in rendered["text"]
    assert "17" in rendered["text"]
    assert COMPARISON not in rendered["text"]
    assert len(result["raw"].requests) == 2
    assert len(result["embedding"].inputs) == 1
    assert "vector" in result["search"].calls
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3


def test_related_context_does_not_end_search_for_unanswered_comparison(tmp_path, monkeypatch):
    case = comparison_case()
    related = claim(SOURCE, SOURCE, parts=(), event_match=None)
    related["relatedFacets"] = ["P1", "P2"]
    case["outputs"] += [answer(related, missing=("P1", "P2"), coverage="PARTIAL")]
    captured = []
    finish = _HybridSession.finish_answer

    async def capture(self, answer):
        captured.append(answer)
        return await finish(self, answer)

    monkeypatch.setattr(_HybridSession, "finish_answer", capture)
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=current_catalog,
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
            hybrid={"max_rounds": 2},
        )
    )

    rendered = assert_answer(result)
    assert SOURCE in rendered["text"] and COMPARISON not in rendered["text"]
    assert captured[0].coverage == "PARTIAL"
    assert captured[0].missing_facets == ["P1", "P2"]
    assert captured[0].claims[0].answered_facets == []
    assert len(result["embedding"].inputs) == 1
    assert "vector" in result["search"].calls
    assert result["search"].calls[-1] == "revalidate"
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3


def test_layout_repair_publishes_original_quote_without_a_replacement_answer(tmp_path, monkeypatch):
    case = comparison_case()
    source = SOURCE.replace(" ", "\n", 1)
    case["outputs"] += [comparison_answer()]
    captured = []
    finish = _HybridSession.finish_answer

    async def capture(self, answer):
        captured.append(answer)
        return await finish(self, answer)

    monkeypatch.setattr(_HybridSession, "finish_answer", capture)
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=current_catalog,
            search_setup=search_with(keyword=[passage(source, 1)]),
            hybrid={"max_rounds": 1},
        )
    )

    rendered = assert_answer(result)
    assert COMPARISON in rendered["text"]
    assert captured[0].claims[0].supports[0].quote == source
    assert captured[0].conflicts[0].supports[0].quote == source
    assert rendered["retrievalSources"][0]["quote"] == source
    assert len(result["raw"].requests) == len(result["spring"].reports) == 2


def test_layout_repair_cannot_bypass_original_support_length_limit(tmp_path):
    case = comparison_case()
    source = SOURCE.replace(" ", "\n" * 1001, 1)
    case["outputs"] += [comparison_answer(), comparison_answer()]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=current_catalog,
            search_setup=search_with(keyword=[passage(source, 1)]),
            hybrid={"max_rounds": 1},
        )
    )

    rendered = assert_answer(result)
    assert COMPARISON not in rendered["text"]
    assert "17" in rendered["text"] and "원문 내용은 이번 조회에서 확인하지 못" in rendered["text"]
    assert len(result["raw"].requests) == len(result["spring"].reports) == 3


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
