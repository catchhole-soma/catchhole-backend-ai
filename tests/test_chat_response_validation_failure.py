"""Keep invalid complete responses distinct without weakening failure or metering."""

import copy

import pytest

from app.chatbot.client import ChatTransportError
from app.chatbot.qa import _Session
from app.chatbot.qa_validation import ResponseValidationFailure
from app.chatbot.replay import ScriptedTextClient, replay_case
from app.chatbot.runtime import ProviderFailure
from tests.test_chat_response_recovery import PRIVATE, scenario, truncated_provider


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    import socket

    def reject(*args, **kwargs):
        raise AssertionError("Validation-failure replays cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


@pytest.fixture
def model_errors(monkeypatch):
    errors = []
    original = _Session.model

    async def capture(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(_Session, "model", capture)
    return errors


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_kind", ["json", "schema", "subject", "row"])
async def test_exhausted_validation_is_typed_and_keeps_legacy_finish(model_errors, invalid_kind):
    case, catalog = scenario()
    plan = copy.deepcopy(case["modelOutputs"][0])
    if invalid_kind == "json":
        outputs = [PRIVATE, PRIVATE]
    elif invalid_kind == "schema":
        invalid = {**plan, "intent": PRIVATE}
        outputs = [invalid, invalid]
    elif invalid_kind == "subject":
        plan["subjects"][0]["name"] = PRIVATE
        outputs = [plan, plan]
    else:
        invalid = {"outcome": "ANSWER", "rowRefs": [PRIVATE]}
        outputs = [plan, invalid, invalid]

    result = await replay_case(case, catalog, outputs=outputs)

    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    assert len(model_errors) == 1
    error = model_errors[0]
    assert type(error) is ResponseValidationFailure
    assert isinstance(error, ProviderFailure)
    assert error.usage is None
    assert error.__context__ is None and error.__cause__ is None
    assert PRIVATE not in repr(error) + repr(error.__dict__)
    assert len(result["reservations"]) == len(result["usageReports"]) == len(outputs)
    assert {report["kind"] for report in result["usageReports"]} == {"SETTLE"}
    assert {report["outcome"] for report in result["usageReports"]} == {"SUCCESS"}
    if invalid_kind != "row":
        assert result["queries"] == []


@pytest.mark.anyio
async def test_transport_failure_is_not_an_invalid_response(monkeypatch, model_errors):
    async def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(ScriptedTextClient, "create_text_response", fail)
    case, catalog = scenario()
    result = await replay_case(case, catalog)

    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert len(model_errors) == 1
    assert type(model_errors[0]) is ProviderFailure
    assert len(result["reservations"]) == len(result["usageReports"]) == 1
    assert result["usageReports"][0]["kind"] == "UNKNOWN"


@pytest.mark.anyio
@pytest.mark.parametrize("schema_first", [False, True])
async def test_final_truncation_keeps_provider_failure_and_paid_usage(
    monkeypatch, model_errors, schema_first
):
    truncated_provider(monkeypatch, first=1 if schema_first else 0, repeat=True)
    case, catalog = scenario()
    outputs = [PRIVATE] if schema_first else []
    result = await replay_case(case, catalog, outputs=outputs)

    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert len(model_errors) == 1
    assert type(model_errors[0]) is ProviderFailure
    assert len(result["reservations"]) == len(result["usageReports"]) == 2
    assert result["usageReports"][-1]["outcome"] == "FAILURE"
    assert result["usageReports"][-1]["inputTokens"] == 101


@pytest.mark.anyio
async def test_complete_invalid_response_after_truncation_is_validation_failure(
    monkeypatch, model_errors
):
    truncated_provider(monkeypatch, first=0)
    case, catalog = scenario()
    result = await replay_case(case, catalog, outputs=[PRIVATE])

    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert len(model_errors) == 1
    assert type(model_errors[0]) is ResponseValidationFailure
    assert len(result["reservations"]) == len(result["usageReports"]) == 2
    assert [report["outcome"] for report in result["usageReports"]] == ["FAILURE", "SUCCESS"]


@pytest.mark.anyio
async def test_call_limit_stops_recovery_without_becoming_validation_failure(model_errors):
    case, catalog = scenario()
    case["maxCalls"] = 1
    result = await replay_case(case, catalog, outputs=[PRIVATE, *case["modelOutputs"]])

    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(model_errors) == 1
    assert type(model_errors[0]) is ChatTransportError
    assert len(result["reservations"]) == len(result["usageReports"]) == 1
