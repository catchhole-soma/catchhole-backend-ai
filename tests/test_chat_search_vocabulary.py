"""Observed keyword combinations and authored source-only plans; no model quality claim."""

import asyncio
import copy
import logging
import socket

import pytest

from app.schemas.chat_execution import ConversationTurn
from tests.test_chat_focused_lookup import catalog_subject
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


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Search vocabulary regressions cannot use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def execute(tmp_path, case, *, spring_setup=None):
    text = f"{EMOTION} {BEHAVIOR}"
    case["outputs"].append(answer(claim(text, text)))
    return asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=spring_setup,
            search_setup=search_with(keyword=[passage(text, 1)]),
        )
    )


@pytest.mark.parametrize("terms", [["봉화", "행동"], ["봉화 행동"], ["봉화\t행동"], ["봉화\n행동"]])
def test_observed_words_can_be_grouped_without_a_plan_retry(tmp_path, terms):
    case = parts_case()
    case["outputs"][0]["searchTerms"] = terms
    result = execute(tmp_path, case)

    assert_answer(result)
    assert len(result["raw"].requests) == 2
    assert result["raw"].requests[-1]["input"]["plan"]["searchTerms"] == terms
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2
    assert not result["spring"].queries


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("searchTerms", "봉화 신호탑", "UNOBSERVED_SEARCH_TERM"),
        ("searchTerms", "봉화 달라졌다", "UNOBSERVED_SEARCH_TERM"),
        ("name", "미라 봉화", "UNOBSERVED_SUBJECT_NAME"),
        ("attribute", "봉화 행동", "UNOBSERVED_FOCUS_ATTRIBUTE"),
    ],
)
def test_unobserved_word_and_recombined_subject_or_focus_still_require_repair(
    tmp_path, caplog, field, value, code
):
    case = parts_case()
    valid = case["outputs"][0]
    invalid = copy.deepcopy(valid)
    if field == "searchTerms":
        invalid[field] = [value]
    else:
        invalid["subjects"] = [
            {
                "domain": "CHARACTER",
                "name": value if field == "name" else "미라",
                "focus": {"factType": None, "attribute": value if field == "attribute" else None},
            }
        ]
    case["outputs"] = [invalid, valid]
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = execute(tmp_path, case)

    assert_answer(result)
    records = [record for record in caplog.records if record.name == "app.chatbot.qa"]
    assert len(records) == 1 and records[0].validation_errors[0]["code"] == code
    assert code in result["raw"].requests[1]["system"]
    assert len(result["raw"].requests) == 3
    assert not result["spring"].queries


@pytest.mark.parametrize("has_history", [False, True])
def test_keyword_combination_uses_only_currently_observed_conversation(tmp_path, has_history):
    case = parts_case()
    valid = case["outputs"][0]
    combined = copy.deepcopy(valid)
    combined["searchTerms"] = ["봉화 새벽"]
    case["outputs"] = [combined] if has_history else [combined, valid]

    def setup(spring):
        if has_history:
            spring.context = spring.context.model_copy(
                update={
                    "conversation": [
                        ConversationTurn(question="새벽에 있었던 일이야?", answer="확인 중입니다.")
                    ]
                }
            )

    result = execute(tmp_path, case, spring_setup=setup)
    assert_answer(result)
    assert len(result["raw"].requests) == (2 if has_history else 3)
    assert not result["spring"].queries


@pytest.mark.parametrize("source_need", ["SCENE", "REASON", "PROCESS"])
def test_source_only_plan_does_not_resolve_or_read_a_mentioned_character(tmp_path, source_need):
    case = parts_case()
    case["outputs"][0].update(
        sourceNeed=source_need,
        subjects=[
            {"domain": "CHARACTER", "name": "미라", "focus": {"factType": None, "attribute": None}}
        ],
    )

    def setup(spring):
        # A needless RDB lookup would clarify these same-name candidates early.
        spring.catalog = [catalog_subject(701, "미라"), catalog_subject(702, "미라")]

    result = execute(tmp_path, case, spring_setup=setup)
    assert_answer(result)
    assert not result["spring"].queries
    assert result["raw"].requests[-1]["input"]["rows"] == []
    assert result["search"].calls and result["spring"].receipts
