import asyncio
import logging
import traceback

import pytest
from pydantic import BaseModel

from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.analysis.json_response import request_validated_model
from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import comparison_failure_code
from app.llm.exceptions import LlmIncompleteResponseError, LlmResponseValidationError
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
