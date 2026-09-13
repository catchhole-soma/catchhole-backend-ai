import asyncio
import logging
import traceback

import httpx
import pytest
from pydantic import BaseModel

from app.analysis.exceptions import (
    ComparisonValidationError,
    LlmExtractionError,
    OrderedInputContextError,
)
from app.analysis.json_response import (
    parse_json_object,
    request_validated_model,
    safe_validation_error_summary,
)
from app.analysis.ordered_context import BoundedOrderedClient
from app.clients.exceptions import AiTokenQuotaExhaustedError, WorkerLeaseExpiredError
from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import comparison_failure_code
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)
from app.llm.protocols import LlmResponseSchema
from app.llm.responses import LlmTextResponse


class ResponseModel(BaseModel):
    value: str


class IncompleteResponseClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(self, **kwargs):
        self.call_count += 1
        raise LlmIncompleteResponseError("provider incomplete")


class InvalidSchemaResponseClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(self, **kwargs):
        self.call_count += 1
        return LlmTextResponse(text='{"value":{"secret":"SECRET_PROVIDER_VALUE"}}')


class ProviderValidationErrorClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(self, **kwargs):
        self.call_count += 1
        raise LlmResponseValidationError("SECRET_PROVIDER_VALUE")


def test_validation_summary_preserves_project_origin_without_values_or_absolute_path() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_json_object("SECRET_PROVIDER_VALUE")

    summary = safe_validation_error_summary(exc_info.value)

    assert summary.startswith("JSONDecodeError(origin=app.analysis.json_response.parse_json_object:")
    assert summary.removesuffix(")").rsplit(":", 1)[1].isdigit()
    assert "SECRET_PROVIDER_VALUE" not in summary
    assert "/" not in summary


def test_validation_summary_without_project_traceback_keeps_only_exception_type() -> None:
    with pytest.raises(ValueError) as exc_info:
        raise ValueError("SECRET_PROVIDER_VALUE")

    assert safe_validation_error_summary(exc_info.value) == "ValueError"
    assert safe_validation_error_summary(ValueError("SECRET_PROVIDER_VALUE")) == "ValueError"
    assert safe_validation_error_summary(None) == "unknown error"


def test_non_truncation_incomplete_response_is_not_retried() -> None:
    client = IncompleteResponseClient()

    with pytest.raises(LlmIncompleteResponseError):
        asyncio.run(
            request_validated_model(
                client=client,
                response_model=ResponseModel,
                system_prompt="Return JSON.",
                user_prompt="input",
                model="test-model",
                max_output_tokens=100,
                max_attempts=3,
                prompt_cache_key="test-key",
                operation_name="test extraction",
                logger=logging.getLogger(__name__),
            )
        )

    assert client.call_count == 1


def test_validation_logs_and_final_traceback_omit_provider_values(caplog) -> None:
    client = InvalidSchemaResponseClient()

    with (
        caplog.at_level(logging.WARNING, logger=__name__),
        pytest.raises(LlmExtractionError) as exc_info,
    ):
        asyncio.run(
            request_validated_model(
                client=client,
                response_model=ResponseModel,
                system_prompt="SECRET_SYSTEM_PROMPT",
                user_prompt="SECRET_NOVEL_BODY",
                model="test-model",
                max_output_tokens=100,
                max_attempts=2,
                prompt_cache_key="test-key",
                operation_name="test extraction",
                logger=logging.getLogger(__name__),
            )
        )

    formatted_exception = "".join(traceback.format_exception(exc_info.value))
    assert client.call_count == 2
    assert "ValidationError" in caplog.text
    assert "string_type" in caplog.text
    assert "SECRET_PROVIDER_VALUE" not in caplog.text
    assert "SECRET_SYSTEM_PROMPT" not in caplog.text
    assert "SECRET_NOVEL_BODY" not in caplog.text
    assert "SECRET_PROVIDER_VALUE" not in str(exc_info.value)
    assert "SECRET_PROVIDER_VALUE" not in formatted_exception


def test_provider_validation_failure_preserves_sanitized_parse_cause() -> None:
    client = ProviderValidationErrorClient()

    with pytest.raises(ComparisonValidationError) as exc_info:
        asyncio.run(
            request_validated_model(
                client=client,
                response_model=ResponseModel,
                system_prompt="SECRET_SYSTEM_PROMPT",
                user_prompt="SECRET_NOVEL_BODY",
                model="test-model",
                max_output_tokens=100,
                max_attempts=2,
                prompt_cache_key="test-key",
                operation_name="Character-fact comparison",
                logger=logging.getLogger(__name__),
            )
        )

    error = exc_info.value
    formatted_exception = "".join(traceback.format_exception(error))
    assert client.call_count == 2
    assert isinstance(error.__cause__, LlmResponseValidationError)
    assert comparison_failure_code(error) is AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    assert "SECRET_PROVIDER_VALUE" not in str(error)
    assert "SECRET_PROVIDER_VALUE" not in formatted_exception
    assert "SECRET_SYSTEM_PROMPT" not in formatted_exception
    assert "SECRET_NOVEL_BODY" not in formatted_exception


@pytest.mark.parametrize("structured", [False, True])
def test_requested_schema_survives_validation_retry_without_changing_legacy_calls(structured):
    requests = []

    class RecordingClient:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            return LlmTextResponse(text='{}' if len(requests) == 1 else '{"value":"ok"}')

    schema = LlmResponseSchema(
        name="test_response",
        schema={"type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False},
    )
    result = asyncio.run(request_validated_model(
        client=RecordingClient(), response_model=ResponseModel,
        system_prompt="Return the requested object.", user_prompt="original input",
        model="test-model", max_output_tokens=100, max_attempts=2,
        prompt_cache_key="test-key", operation_name="test extraction",
        logger=logging.getLogger(__name__),
        retry_user_prompt_builder=lambda original, error: original + "\nInclude value.",
        response_schema=schema if structured else None,
    ))

    assert result.value == "ok"
    assert len(requests) == 2
    assert requests[0]["user_prompt"] == "original input"
    assert requests[1]["user_prompt"] == "original input\nInclude value."
    for request in requests:
        if structured:
            assert request["response_schema"] is schema
        else:
            assert "response_schema" not in request


def _request(client, **kwargs):
    return asyncio.run(request_validated_model(
        client=client,
        response_model=ResponseModel,
        system_prompt="Return JSON.",
        user_prompt="original input",
        model="test-model",
        max_output_tokens=100,
        max_attempts=3,
        prompt_cache_key="test-key",
        operation_name=kwargs.pop("operation_name", "Ordered character subject resolution"),
        logger=logging.getLogger(__name__),
        **kwargs,
    ))


class ReferenceResponseClient:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text=ResponseModel(value=next(self.values)).model_dump_json())


def _validate_reference(result):
    if result.value != "T1":
        raise ComparisonValidationError(f"Unknown target reference: {result.value}")


def test_domain_reference_retry_uses_original_prompt_and_returns_valid_target(caplog):
    client = ReferenceResponseClient(["SECRET_UNKNOWN_TARGET", "SECRET_UNKNOWN_TARGET", "T1"])
    feedback_errors = []

    def retry_prompt(original, error):
        feedback_errors.append(error)
        return original + "\nInvalid target reference. Use a provided target."

    with caplog.at_level(logging.WARNING, logger=__name__):
        result = _request(
            client,
            validate_model=_validate_reference,
            retry_user_prompt_builder=retry_prompt,
        )

    assert result.value == "T1"
    assert len(client.requests) == 3
    assert len(feedback_errors) == 2
    assert all(isinstance(error, ComparisonValidationError) for error in feedback_errors)
    assert client.requests[0]["user_prompt"] == "original input"
    assert client.requests[1]["user_prompt"] == client.requests[2]["user_prompt"]
    assert "SECRET_UNKNOWN_TARGET" not in caplog.text
    assert all("SECRET_UNKNOWN_TARGET" not in request["user_prompt"] for request in client.requests)


@pytest.mark.parametrize("operation_name", [
    "Ordered character subject resolution",
    "Ordered world subject resolution",
    "Character-fact comparison",
])
def test_domain_reference_exhaustion_remains_a_sanitized_validation_failure(operation_name, caplog):
    client = ReferenceResponseClient(["SECRET_UNKNOWN_TARGET"] * 3)

    with (
        caplog.at_level(logging.WARNING, logger=__name__),
        pytest.raises(ComparisonValidationError) as exc_info,
    ):
        _request(client, operation_name=operation_name, validate_model=_validate_reference)

    assert len(client.requests) == 3
    assert "failed after 3 attempts" in str(exc_info.value)
    assert comparison_failure_code(exc_info.value) is AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert "SECRET_UNKNOWN_TARGET" not in caplog.text
    assert "SECRET_UNKNOWN_TARGET" not in "".join(traceback.format_exception(exc_info.value))


@pytest.mark.parametrize("failure", [
    OrderedInputContextError("Frozen input changed."),
    LlmIncompleteResponseError("Provider incomplete."),
    LlmOutputTruncatedError(
        "Provider truncated.", incomplete_reason="max_output_tokens", max_output_tokens=100,
    ),
    AiTokenQuotaExhaustedError(),
    WorkerLeaseExpiredError(
        "Lease expired.",
        request=httpx.Request("POST", "https://worker.invalid/complete"),
        response=httpx.Response(409),
    ),
])
def test_fixed_input_and_execution_failures_in_validator_are_not_retried(failure):
    client = ReferenceResponseClient(["T1"])
    feedback_errors = []

    def validate(result):
        raise failure

    with pytest.raises(type(failure)) as exc_info:
        _request(
            client,
            validate_model=validate,
            retry_user_prompt_builder=lambda original, error: feedback_errors.append(error),
        )

    assert exc_info.value is failure
    assert len(client.requests) == 1
    assert feedback_errors == []


@pytest.mark.parametrize("failure", [
    ValueError("Client input invalid."),
    TypeError("Client input invalid."),
    ComparisonValidationError("ordered_required_context_exceeds_input_limit"),
    OrderedInputContextError("Frozen input changed."),
    AiTokenQuotaExhaustedError(),
    httpx.ReadTimeout("Provider timeout."),
    httpx.HTTPStatusError(
        "Provider unavailable.",
        request=httpx.Request("POST", "https://provider.invalid/responses"),
        response=httpx.Response(503),
    ),
    WorkerLeaseExpiredError(
        "Lease expired.",
        request=httpx.Request("POST", "https://worker.invalid/complete"),
        response=httpx.Response(409),
    ),
])
def test_client_failures_are_not_treated_as_response_validation(failure):
    requests = []

    class FailingClient:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            raise failure

    with pytest.raises(type(failure)) as exc_info:
        _request(FailingClient())

    assert exc_info.value is failure
    assert len(requests) == 1


def test_ordered_context_overflow_on_retry_does_not_call_provider_again(monkeypatch):
    client = ReferenceResponseClient(["SECRET_UNKNOWN_TARGET"])
    bound_checks = []
    failure = ComparisonValidationError("ordered_required_context_exceeds_input_limit")

    def ensure_fits(system_prompt, user_prompt, max_tokens, response_schema=None):
        bound_checks.append(user_prompt)
        if user_prompt != "original input":
            raise failure

    monkeypatch.setattr("app.analysis.ordered_context.ensure_ordered_prompt_fits", ensure_fits)

    with pytest.raises(ComparisonValidationError) as exc_info:
        _request(
            BoundedOrderedClient(client),
            validate_model=_validate_reference,
            retry_user_prompt_builder=lambda original, error: original + "\nRetry feedback.",
        )

    assert exc_info.value is failure
    assert len(client.requests) == 1
    assert len(bound_checks) == 2


def test_validation_diagnostics_hook_includes_final_failed_attempt():
    observed = []
    client = InvalidSchemaResponseClient()

    with pytest.raises(ComparisonValidationError):
        _request(client, operation_name="World-setting comparison",
                 validation_failure_callback=lambda attempt, error, parsed: observed.append(
                     (attempt, type(error).__name__, parsed is not None),
                 ))

    assert observed == [(1, "ValidationError", True), (2, "ValidationError", True),
                        (3, "ValidationError", True)]


def test_validation_diagnostics_failure_does_not_hide_real_error_or_log_hook_values(caplog):
    client = InvalidSchemaResponseClient()

    def broken_diagnostics(*args):
        raise RuntimeError("SECRET_DIAGNOSTIC_VALUE")

    with caplog.at_level(logging.WARNING), pytest.raises(ComparisonValidationError) as caught:
        _request(client, operation_name="World-setting comparison",
                 validation_failure_callback=broken_diagnostics)

    assert client.call_count == 3
    assert "ValidationError" in str(caught.value)
    assert "SECRET_" not in caplog.text + str(caught.value)


def test_execution_failure_never_calls_validation_diagnostics():
    calls = []
    client = IncompleteResponseClient()
    with pytest.raises(LlmIncompleteResponseError):
        _request(client, validation_failure_callback=lambda *args: calls.append(args))
    assert calls == []
