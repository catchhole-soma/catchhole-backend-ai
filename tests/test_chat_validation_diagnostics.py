"""Offline validation diagnostics must not expose rejected model data."""

import copy
import logging
import socket
from pathlib import Path

import pytest

from app.chatbot.replay import (
    ScriptedTextClient,
    check_expected,
    load_fixture,
    replay_case,
)


FIXTURE = load_fixture(Path(__file__).parent / "fixtures/chat-qa-v1.json")
PRIVATE = "private-response-원문-89e11c19-1d84-436c-b528-e071343b29df"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Validation diagnostics tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def scenario():
    case = copy.deepcopy(next(c for c in FIXTURE["cases"] if c["id"] == "current_ability"))
    return case, copy.deepcopy(FIXTURE["catalogs"][case["catalog"]])


def diagnostics(caplog):
    records = [record for record in caplog.records if record.name == "app.chatbot.qa"]
    for record in records:
        assert PRIVATE not in repr(record.__dict__)
        assert record.exc_info is None
        assert record.stack_info is None
        assert record.origin.startswith("app.chatbot.")
        assert "/" not in record.origin
        assert 1 <= len(record.validation_errors) <= 6
        for error in record.validation_errors:
            assert set(error) == {"code", "path"}
            assert error["path"].startswith("$")
    return records


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("invalid", "category"),
    [
        (PRIVATE, "JSON_INVALID"),
        ({"intent": PRIVATE, PRIVATE: "rejected dynamic field"}, "SCHEMA_VALIDATION"),
    ],
)
async def test_schema_retry_logs_only_safe_metadata_and_preserves_metering(
    caplog, invalid, category
):
    case, catalog = scenario()
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog, outputs=[invalid, *case["modelOutputs"]])
    check_expected(case, result)
    records = diagnostics(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.purpose == "CHAT_INTERPRETATION"
    assert record.schema == "QuestionPlan"
    assert record.validation_stage == "RESPONSE_SCHEMA"
    assert record.error_category == category
    assert (record.attempt, record.max_attempts) == (1, 2)
    if category == "JSON_INVALID":
        assert record.validation_errors == [{"code": "json_invalid", "path": "$"}]
    else:
        assert {"code": "literal_error", "path": "$.intent"} in record.validation_errors
    assert case["question"] not in record.getMessage()
    assert len(result["reservations"]) == len(result["usageReports"]) == 3
    assert all(report["outcome"] == "SUCCESS" for report in result["usageReports"])
    assert "이전 출력이 계약 검증에 실패했습니다." in result["modelRequests"][1]["system"]
    assert PRIVATE not in result["modelRequests"][1]["system"]


@pytest.mark.anyio
async def test_contract_retry_is_distinct_from_schema_failure(caplog):
    case, catalog = scenario()
    invalid = copy.deepcopy(case["modelOutputs"][0])
    invalid["subjects"][0]["name"] = PRIVATE
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog, outputs=[invalid, *case["modelOutputs"]])
    check_expected(case, result)
    records = diagnostics(caplog)
    assert len(records) == 1
    assert records[0].validation_stage == "RESPONSE_CONTRACT"
    assert records[0].error_category == "CONTRACT_VALIDATION"
    assert records[0].origin.startswith("app.chatbot.qa.validate_plan:")
    assert records[0].validation_errors == [{"code": "UNOBSERVED_LOOKUP_TERM", "path": "$"}]
    assert len(result["reservations"]) == len(result["usageReports"]) == 3


@pytest.mark.anyio
async def test_nested_schema_failure_logs_safe_field_paths_without_rejected_values(caplog):
    case, catalog = scenario()
    invalid = copy.deepcopy(case["modelOutputs"][0])
    invalid["subjects"][0]["domain"] = PRIVATE
    invalid["subjects"][0][PRIVATE] = PRIVATE
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog, outputs=[invalid, *case["modelOutputs"]])
    check_expected(case, result)
    record = diagnostics(caplog)[0]
    assert {"code": "literal_error", "path": "$.subjects[].domain"} in record.validation_errors
    assert {"code": "extra_forbidden", "path": "$.subjects[]"} in record.validation_errors
    assert "$.subjects[].domain" in record.getMessage()
    assert PRIVATE not in caplog.text


@pytest.mark.anyio
async def test_conversational_plan_with_factual_lookup_gets_specific_safe_feedback(caplog):
    case, catalog = scenario()
    invalid = copy.deepcopy(case["modelOutputs"][0])
    invalid["intent"] = "GREETING"
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog, outputs=[invalid, *case["modelOutputs"]])
    check_expected(case, result)
    record = diagnostics(caplog)[0]
    assert record.validation_errors == [{"code": "PLAN_CONVERSATION_ONLY", "path": "$"}]
    assert "PLAN_CONVERSATION_ONLY" in result["modelRequests"][1]["system"]


@pytest.mark.anyio
async def test_exhausted_contract_retries_keep_failure_and_bounded_attempts(caplog):
    case, catalog = scenario()
    invalid = {"outcome": "ANSWER", "rowRefs": [PRIVATE]}
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(
            case, catalog, outputs=[case["modelOutputs"][0], invalid, invalid]
        )
    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    records = diagnostics(caplog)
    assert [record.attempt for record in records] == [1, 2]
    assert {record.max_attempts for record in records} == {2}
    assert {record.schema for record in records} == {"AnswerSelection"}
    assert {record.purpose for record in records} == {"CHAT_ANSWER"}
    assert {record.error_category for record in records} == {"CONTRACT_VALIDATION"}
    assert len(result["reservations"]) == len(result["usageReports"]) == 3


@pytest.mark.anyio
async def test_execution_call_limit_still_blocks_schema_retry(caplog):
    case, catalog = scenario()
    case["maxCalls"] = 1
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog, outputs=[PRIVATE, *case["modelOutputs"]])
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(diagnostics(caplog)) == 1
    assert len(result["modelRequests"]) == len(result["usageReports"]) == 1


@pytest.mark.anyio
async def test_provider_error_is_not_retried_or_reported_as_validation(caplog, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(ScriptedTextClient, "create_text_response", fail)
    case, catalog = scenario()
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog)
    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    assert diagnostics(caplog) == []
    assert len(result["reservations"]) == len(result["usageReports"]) == 1
    assert result["usageReports"][0]["kind"] == "UNKNOWN"
    assert PRIVATE not in caplog.text
