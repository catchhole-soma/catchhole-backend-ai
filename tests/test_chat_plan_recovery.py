"""Offline recovery contracts; authored model responses do not measure model accuracy."""

import asyncio
import copy
import logging
import socket

import pytest

from app.chatbot.qa_validation import RULES
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


@pytest.mark.parametrize(
    ("changes", "code", "path"),
    [
        ({"requestedFacets": ["TIME"]}, "PLAN_PARTS_MODE", "$.requestedParts"),
        (
            {**REGISTERED_PLAN, "requestedParts": [{"ref": "P1", "description": PRIVATE}]},
            "PLAN_PARTS_MODE",
            "$.requestedParts",
        ),
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
        (
            {**REGISTERED_PLAN, "sourceFallback": True},
            "PLAN_FALLBACK_PARTS_REQUIRED",
            "$.requestedParts",
        ),
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


def test_unobserved_focus_is_still_rejected_before_any_search_with_bounded_retries(
    tmp_path, caplog
):
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

    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    records = [record for record in caplog.records if record.name == "app.chatbot.qa"]
    assert len(records) == 2
    assert all(record.validation_stage == "RESPONSE_CONTRACT" for record in records)
    assert all(
        record.validation_errors == [{"code": "UNOBSERVED_LOOKUP_TERM", "path": "$"}]
        for record in records
    )
    assert all("qa_lookup.validate_focus:" in record.origin for record in records)
    assert PRIVATE not in caplog.text
    assert "UNOBSERVED_LOOKUP_TERM" in result["raw"].requests[1]["system"]
    assert PRIVATE not in result["raw"].requests[1]["system"]
    assert len(result["raw"].requests) == len(result["spring"].reservations) == 2
    assert result["search"].calls == []
    assert result["spring"].receipts == []
