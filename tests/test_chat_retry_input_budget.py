"""Repair annotations fit the same input budget without dropping question/evidence."""

import copy
import logging
import socket

import pytest

from app.chatbot.client import ChatTransportError
from app.chatbot.qa import _Session, encode, response_schema
from app.chatbot.qa_render import message
from app.chatbot.qa_validation import ChatResponseContractError, ResponseValidationFailure
from app.chatbot.replay import replay_case
from app.chatbot.runtime import ProviderOutputTruncated
from app.schemas.setting_tools import ContractModel
from app.usage.metering import _estimate_text_token_upper_bound
from tests.test_chat_response_recovery import PRIVATE, scenario, truncated_provider


class RepairAnswer(ContractModel):
    value: str


PROMPT = "Answer from the supplied evidence. Preserve every requested part and return the schema."
SCHEMA = response_schema(RepairAnswer)
MODEL = "gpt-6-luna"
INPUT_LIMIT = 32000


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Retry budget tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def estimated(prompt, payload, schema=SCHEMA):
    return _estimate_text_token_upper_bound(prompt, encode(payload), MODEL, 0, schema)


def near_limit_payload(headroom):
    payload = {
        "question": "Keep this synthetic question unchanged.",
        "requestedParts": [{"ref": "P1", "description": "Preserve the original causal request."}],
        "passages": [{"reference": "E1", "text": ""}],
    }
    # Use the real production estimator. No rejected answer or private corpus is
    # included, and the configured 32k limit itself is never enlarged.
    low, high = 0, 64000
    while low < high:
        middle = (low + high + 1) // 2
        payload["passages"][0]["text"] = "x" * middle
        if estimated(PROMPT, payload) <= INPUT_LIMIT - headroom:
            low = middle
        else:
            high = middle - 1
    payload["passages"][0]["text"] = "x" * low
    assert estimated(PROMPT, payload) <= INPUT_LIMIT - headroom
    assert INPUT_LIMIT - headroom - estimated(PROMPT, payload) < 4
    return payload


async def model_replay(monkeypatch, payload, *, outputs=None, max_calls=8):
    errors = []

    async def run(self):
        def validate(value):
            if value.value == PRIVATE:
                raise ChatResponseContractError("UNOBSERVED_SUBJECT_NAME")

        try:
            value = await self.model(
                RepairAnswer, PROMPT, payload, "CHAT_ANSWER", "synthetic-repair", validate
            )
        except Exception as exc:
            errors.append(exc)
            raise
        return message("ANSWER", value.value, [])

    monkeypatch.setattr(_Session, "run", run)
    case, catalog = scenario()
    case["maxCalls"] = max_calls
    result = await replay_case(
        case,
        catalog,
        outputs=outputs if outputs is not None else [{"value": PRIVATE}, {"value": "confirmed"}],
        config={"max_context_characters": 64000, "max_input_tokens": INPUT_LIMIT},
    )
    return result, errors


@pytest.mark.anyio
@pytest.mark.parametrize("headroom,form", [(1000, "detailed"), (150, "path"), (80, "code")])
async def test_validation_retry_chooses_a_fitting_annotation_without_changing_payload(
    monkeypatch, caplog, headroom, form
):
    payload = near_limit_payload(headroom)
    original = copy.deepcopy(payload)
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result, errors = await model_replay(monkeypatch, payload)
    assert not errors
    assert result["finish"]["result"]["text"] == "confirmed"
    assert payload == original
    requests = result["modelRequests"]
    assert len(requests) == len(result["reservations"]) == len(result["usageReports"]) == 2
    assert all(request["input"] == original for request in requests)
    assert all(request["system"].startswith(PROMPT) for request in requests)
    assert all(
        estimated(request["system"], request["input"]) <= INPUT_LIMIT for request in requests
    )
    assert [request["max_output_tokens"] for request in requests] == [3000, 3000]
    retry = requests[1]["system"]
    assert "UNOBSERVED_SUBJECT_NAME" in retry
    assert ("안전한 검증 피드백" in retry) == (form == "detailed")
    assert ("$.subjects[].name" in retry) == (form != "code")
    assert "schema" in retry and ("근거" in retry or "evidence" in retry)
    assert PRIVATE not in retry + caplog.text
    assert all(report["outcome"] == "SUCCESS" for report in result["usageReports"])


@pytest.mark.anyio
async def test_no_repair_room_preserves_validation_failure_without_another_reservation(monkeypatch):
    result, errors = await model_replay(monkeypatch, near_limit_payload(0))
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert len(errors) == 1 and type(errors[0]) is ResponseValidationFailure
    assert (
        len(result["modelRequests"])
        == len(result["reservations"])
        == len(result["usageReports"])
        == 1
    )
    assert errors[0].usage is None
    assert PRIVATE not in repr(errors[0])


@pytest.mark.anyio
@pytest.mark.parametrize("headroom,recovers", [(80, True), (0, False)])
async def test_truncation_keeps_its_type_and_paid_usage_when_repair_input_is_bounded(
    monkeypatch, headroom, recovers
):
    truncated_provider(monkeypatch, first=0)
    payload = near_limit_payload(headroom)
    result, errors = await model_replay(monkeypatch, payload, outputs=[{"value": "confirmed"}])
    expected_calls = 2 if recovers else 1
    assert (
        len(result["modelRequests"])
        == len(result["reservations"])
        == len(result["usageReports"])
        == expected_calls
    )
    assert result["usageReports"][0]["outcome"] == "FAILURE"
    assert result["usageReports"][0]["inputTokens"] == 101
    assert result["usageReports"][0]["outputTokens"] == 3000
    if recovers:
        assert not errors
        assert result["finish"]["result"]["text"] == "confirmed"
        assert result["modelRequests"][1]["max_output_tokens"] == 6000
        assert "OUTPUT_TRUNCATED" in result["modelRequests"][1]["system"]
        assert result["modelRequests"][1]["input"] == payload
        assert estimated(result["modelRequests"][1]["system"], payload) <= INPUT_LIMIT
    else:
        assert result["finish"]["failure"] == "PROVIDER_FAILURE"
        assert len(errors) == 1 and type(errors[0]) is ProviderOutputTruncated
        # Usage was already settled once by Execution.call; do not replay it.
        assert errors[0].usage is None


@pytest.mark.anyio
async def test_initial_input_overflow_remains_execution_limit_without_provider_or_reservation(
    monkeypatch,
):
    payload = near_limit_payload(0)
    payload["passages"][0]["text"] += "x" * 100
    result, errors = await model_replay(monkeypatch, payload)
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(errors) == 1 and type(errors[0]) is ChatTransportError
    assert not result["modelRequests"] and not result["reservations"] and not result["usageReports"]


@pytest.mark.anyio
async def test_fitting_compact_retry_still_obeys_execution_call_limit(monkeypatch):
    result, errors = await model_replay(monkeypatch, near_limit_payload(80), max_calls=1)
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(errors) == 1 and type(errors[0]) is ChatTransportError
    assert (
        len(result["modelRequests"])
        == len(result["reservations"])
        == len(result["usageReports"])
        == 1
    )


@pytest.mark.anyio
async def test_hybrid_validation_without_retry_room_continues_search(monkeypatch, tmp_path):
    from tests.test_chat_hybrid import replay
    from tests.test_chat_hybrid_items import passage
    from tests.test_chat_requested_parts import (
        answer,
        assert_answer,
        claim,
        parts_case,
        search_with,
    )

    original = _Session.model

    async def bounded_answer(self, cls, prompt, payload, *args, **kwargs):
        previous = self.cfg
        if cls.__name__ == "GroundedAnswer":
            cap = estimated(prompt, payload, response_schema(cls))
            assert 1000 <= cap <= INPUT_LIMIT
            self.cfg = previous.model_copy(update={"max_input_tokens": cap})
        try:
            return await original(self, cls, prompt, payload, *args, **kwargs)
        finally:
            self.cfg = previous

    monkeypatch.setattr(_Session, "model", bounded_answer)
    case = parts_case(event_context=None)
    text = "미라는 봉화를 켜고 기뻐했으며 이후 경비를 자원했다."
    case["outputs"] += [PRIVATE, answer(claim(text, text, ref="E2", event_match=None))]
    result = await replay(
        tmp_path,
        case,
        search_setup=search_with(
            keyword=[passage("합성 장면에서 날씨를 이야기했다.", 1)],
            vector=[passage(text, 2, episode=24, method="VECTOR")],
        ),
    )
    assert text in assert_answer(result)["text"]
    assert len(result["raw"].requests) == 3
    assert len(result["embedding"].inputs) == 1
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 4
    assert (
        result["raw"].requests[-1]["input"]["plan"]["requestedParts"]
        == case["outputs"][0]["requestedParts"]
    )
