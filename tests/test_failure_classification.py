import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import (
    analysis_failure_code,
    comparison_failure_code,
)
from app.llm.exceptions import LlmIncompleteResponseError, LlmOutputTruncatedError


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AiTokenQuotaExhaustedError(), AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED),
        (
            LlmOutputTruncatedError(
                "truncated",
                incomplete_reason="max_output_tokens",
                max_output_tokens=4000,
            ),
            AnalysisFailureCode.LLM_OUTPUT_TRUNCATED,
        ),
        (
            httpx.ConnectError(
                "network unavailable",
                request=httpx.Request("POST", "https://api.openai.test/v1/responses"),
            ),
            AnalysisFailureCode.LLM_NETWORK_ERROR,
        ),
        (
            LlmIncompleteResponseError("provider incomplete"),
            AnalysisFailureCode.LLM_PROVIDER_ERROR,
        ),
        (LlmExtractionError("invalid JSON"), AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR),
        (RuntimeError("unknown"), AnalysisFailureCode.UNEXPECTED_ERROR),
    ],
)
def test_analysis_failure_code_classifies_operational_failures(
    error: Exception,
    expected: AnalysisFailureCode,
) -> None:
    assert analysis_failure_code(error) is expected


def test_failure_classification_follows_exception_cause_chain() -> None:
    wrapped = RuntimeError("analysis failed")
    wrapped.__cause__ = AiTokenQuotaExhaustedError()

    assert analysis_failure_code(wrapped) is AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED


def test_comparison_parse_failure_uses_comparison_specific_code() -> None:
    assert comparison_failure_code(ComparisonValidationError("invalid decision")) is (
        AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    )
