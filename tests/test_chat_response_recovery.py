"""Offline provider-output recovery: metering, bounded calls and private-safe feedback."""

import copy
import json
import logging
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.chatbot.qa import response_schema
from app.chatbot.qa_config import ChatQaSettings
from app.chatbot.qa_validation import ChatResponseContractError, retry_feedback
from app.chatbot.replay import ScriptedTextClient, check_expected, load_fixture, replay_case
from app.llm.exceptions import LlmOutputTruncatedError
from app.schemas.chat_qa import QuestionPlan


FIXTURE = load_fixture(Path(__file__).parent / "fixtures/chat-qa-v1.json")
PRIVATE = "private-response-원문-89e11c19-1d84-436c-b528-e071343b29df"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Response recovery tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def scenario():
    case = copy.deepcopy(next(c for c in FIXTURE["cases"] if c["id"] == "current_ability"))
    return case, copy.deepcopy(FIXTURE["catalogs"][case["catalog"]])


def truncated_provider(monkeypatch, *, first, repeat=False, measured=True):
    original = ScriptedTextClient.create_text_response

    async def respond(self, system_prompt, user_prompt, **kwargs):
        index = len(self.requests)
        if index == first or (repeat and index > first):
            self.requests.append(
                {"system": system_prompt, "input": json.loads(user_prompt), **kwargs}
            )
            raise LlmOutputTruncatedError(
                PRIVATE,
                incomplete_reason=PRIVATE,
                max_output_tokens=kwargs["max_output_tokens"],
                input_token_count=101 if measured else None,
                cached_input_token_count=19 if measured else None,
                output_token_count=kwargs["max_output_tokens"] if measured else None,
            )
        return await original(self, system_prompt, user_prompt, **kwargs)

    monkeypatch.setattr(ScriptedTextClient, "create_text_response", respond)


@pytest.mark.anyio
@pytest.mark.parametrize(("first", "caps"), [(0, [1500, 3000, 3000]), (1, [1500, 3000, 6000])])
async def test_truncation_recovers_once_with_stage_cap_and_preserves_paid_failure(
    monkeypatch, caplog, first, caps
):
    truncated_provider(monkeypatch, first=first)
    case, catalog = scenario()
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = await replay_case(case, catalog)
    check_expected(case, result)
    assert [request["max_output_tokens"] for request in result["modelRequests"]] == caps
    assert len(result["reservations"]) == len(result["usageReports"]) == 3
    failed = result["usageReports"][first]
    assert (failed["kind"], failed["outcome"]) == ("SETTLE", "FAILURE")
    assert (failed["inputTokens"], failed["cachedInputTokens"], failed["outputTokens"]) == (
        101,
        19,
        caps[first],
    )
    retry = result["modelRequests"][first + 1]
    assert "OUTPUT_TRUNCATED" in retry["system"]
    assert PRIVATE not in retry["system"] and PRIVATE not in caplog.text
    assert (
        result["reservations"][first + 1]["reservedTokens"]
        > result["reservations"][first]["reservedTokens"]
    )


@pytest.mark.anyio
async def test_second_truncation_stops_without_publishing_partial_output(monkeypatch):
    truncated_provider(monkeypatch, first=1, repeat=True)
    case, catalog = scenario()
    result = await replay_case(case, catalog, config={"max_attempts": 3})
    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    assert [r["max_output_tokens"] for r in result["modelRequests"]] == [1500, 3000, 6000]
    assert [r["outcome"] for r in result["usageReports"]] == ["SUCCESS", "FAILURE", "FAILURE"]


@pytest.mark.anyio
async def test_unknown_usage_truncation_stays_unknown_while_retry_is_metered(monkeypatch):
    truncated_provider(monkeypatch, first=0, measured=False)
    case, catalog = scenario()
    result = await replay_case(case, catalog)
    check_expected(case, result)
    assert result["usageReports"][0]["kind"] == "UNKNOWN"
    assert result["usageReports"][0]["inputTokens"] is None
    assert len(result["reservations"]) == len(result["usageReports"]) == 3


@pytest.mark.anyio
async def test_execution_limit_blocks_truncation_retry_before_second_provider_call(monkeypatch):
    truncated_provider(monkeypatch, first=0)
    case, catalog = scenario()
    case["maxCalls"] = 1
    result = await replay_case(case, catalog)
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(result["modelRequests"]) == len(result["reservations"]) == 1
    assert result["usageReports"][0]["outcome"] == "FAILURE"


@pytest.mark.anyio
async def test_schema_and_truncation_failures_share_the_same_attempt_limit(monkeypatch):
    truncated_provider(monkeypatch, first=1)
    case, catalog = scenario()
    result = await replay_case(case, catalog, outputs=[PRIVATE, *case["modelOutputs"]])
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert len(result["modelRequests"]) == len(result["reservations"]) == 2
    assert [report["outcome"] for report in result["usageReports"]] == ["SUCCESS", "FAILURE"]


@pytest.mark.anyio
@pytest.mark.parametrize("blocked_by", ["text_calls", "quota"])
async def test_hybrid_retry_obeys_text_call_and_account_limits_and_counts_failed_usage(
    monkeypatch, tmp_path, blocked_by
):
    from tests.test_chat_hybrid import replay, scenario as hybrid_scenario

    truncated_provider(monkeypatch, first=0)

    def setup(spring):
        if blocked_by == "quota":
            spring.reserve_after = 1
            spring.reserve_status = 409
            spring.reserve_code = "AI_TOKEN_QUOTA_EXHAUSTED"

    result = await replay(
        tmp_path,
        hybrid_scenario(),
        hybrid={"max_model_calls": 1 if blocked_by == "text_calls" else 6},
        spring_setup=setup,
    )
    assert result["finish"]["failure"] == (
        "EXECUTION_LIMIT_EXCEEDED" if blocked_by == "text_calls" else "QUOTA_EXHAUSTED"
    )
    assert len(result["raw"].requests) == len(result["spring"].reservations) == 1
    assert result["spring"].reports[0]["outcome"] == "FAILURE"
    registry = result["metrics"].registry
    for kind, value in [("text_input", 101), ("text_cached_input", 19), ("text_output", 1500)]:
        assert registry.get_sample_value("chat_retrieval_tokens_total", {"kind": kind}) == value


@pytest.mark.anyio
async def test_schema_feedback_contains_only_schema_fields_and_fixed_error_kinds():
    case, catalog = scenario()
    invalid = copy.deepcopy(case["modelOutputs"][0])
    invalid["subjects"][0]["domain"] = PRIVATE
    invalid[PRIVATE] = PRIVATE
    result = await replay_case(case, catalog, outputs=[invalid, *case["modelOutputs"]])
    check_expected(case, result)
    retry = result["modelRequests"][1]["system"]
    assert "literal_error" in retry and "$.subjects[].domain" in retry
    assert "extra_forbidden" in retry
    assert PRIVATE not in retry
    assert [r["max_output_tokens"] for r in result["modelRequests"]] == [1500, 1500, 3000]


def test_contract_feedback_rejects_untrusted_messages_and_supports_fixed_rule_codes():
    schema = response_schema(QuestionPlan).schema
    feedback = retry_feedback(ChatResponseContractError("REQUESTED_PARTS_INCOMPLETE"), schema)
    assert feedback[0]["code"] == "REQUESTED_PARTS_INCOMPLETE"
    assert retry_feedback(ValueError(PRIVATE), schema)[0]["code"] == "RESPONSE_CONTRACT"
    assert PRIVATE not in json.dumps(retry_feedback(ChatResponseContractError(PRIVATE), schema))
    assert (
        retry_feedback(ValueError("Unknown field reference"), schema)[0]["code"]
        == "UNKNOWN_REFERENCE"
    )


@pytest.mark.parametrize(
    "code",
    [
        "REQUESTED_PARTS_INCOMPLETE",
        "EVENT_CONTEXT_REQUIRED",
        "GROUNDING_SUPPORT_REQUIRED",
        "GROUNDING_INVALID_PASSAGE",
        "GROUNDING_UNKNOWN_REFERENCE",
        "GROUNDING_QUOTE_NOT_FOUND",
        "GROUNDING_AMBIGUOUS_QUOTE",
        "GROUNDING_UNCOVERED_QUOTE",
    ],
)
def test_shared_contract_rule_codes_have_safe_actionable_feedback(code):
    feedback = retry_feedback(ValueError(code), response_schema(QuestionPlan).schema)
    assert feedback[0]["code"] == code
    assert feedback[0]["rule"]


def test_stage_output_configuration_accepts_env_overrides_and_rejects_lower_retry_cap(monkeypatch):
    monkeypatch.setenv("CHAT_QA_MAX_OUTPUT_TOKENS", "1600")
    monkeypatch.setenv("CHAT_QA_ANSWER_MAX_OUTPUT_TOKENS", "3200")
    monkeypatch.setenv("CHAT_QA_TRUNCATION_RETRY_MAX_OUTPUT_TOKENS", "6400")
    settings = ChatQaSettings()
    assert settings.output_tokens_for("CHAT_INTERPRETATION") == 1600
    assert settings.output_tokens_for("CHAT_ANSWER") == 3200
    assert settings.output_tokens_for("CHAT_ANSWER", truncated_retry=True) == 6400
    with pytest.raises(ValidationError):
        ChatQaSettings(truncation_retry_max_output_tokens=2000)
