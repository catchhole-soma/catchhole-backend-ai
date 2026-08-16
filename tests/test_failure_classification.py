import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.clients.exceptions import (
    AiTokenQuotaExhaustedError,
    SpringWorkerHttpError,
    WorkerLeaseExpiredError,
)
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


def test_spring_worker_http_failure_is_not_classified_as_provider_failure() -> None:
    error = _http_status_error(SpringWorkerHttpError, "INTERNAL_SERVER_ERROR")

    assert analysis_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR


def test_spring_worker_lease_conflict_has_dedicated_failure_code() -> None:
    error = _http_status_error(WorkerLeaseExpiredError, "ANALYSIS_JOB_LEASE_CONFLICT")

    assert analysis_failure_code(error) is AnalysisFailureCode.WORKER_LEASE_EXPIRED
    assert comparison_failure_code(error) is AnalysisFailureCode.WORKER_LEASE_EXPIRED


def test_raw_provider_http_failure_remains_provider_error() -> None:
    error = _http_status_error(httpx.HTTPStatusError, "rate_limit_exceeded")

    assert analysis_failure_code(error) is AnalysisFailureCode.LLM_PROVIDER_ERROR


def _http_status_error(error_type, error_code: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://service.test/request")
    response = httpx.Response(
        500,
        request=request,
        json={"error": {"code": error_code}},
    )
    return error_type("request failed", request=request, response=response)
