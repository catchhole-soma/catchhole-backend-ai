import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.clients.exceptions import (
    AiTokenQuotaExhaustedError,
    SpringWorkerHttpError,
    SpringWorkerTransportError,
    WorkerLeaseExpiredError,
)
from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import (
    analysis_failure_code,
    comparison_failure_code,
)
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)


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
        (
            LlmResponseValidationError("malformed provider payload"),
            AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ComparisonValidationError("invalid decision"),
            AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
        ),
        (
            LlmExtractionError("invalid JSON"),
            AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
        ),
        (RuntimeError("post-processing failed"), AnalysisFailureCode.UNEXPECTED_ERROR),
    ],
)
def test_comparison_failure_code_preserves_distinct_categories(
    error: Exception,
    expected: AnalysisFailureCode,
) -> None:
    assert comparison_failure_code(error) is expected


def test_provider_payload_failure_takes_precedence_over_comparison_wrapper() -> None:
    error = ComparisonValidationError("comparison failed")
    error.__cause__ = LlmResponseValidationError("malformed provider payload")

    assert comparison_failure_code(error) is AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR


def test_spring_worker_http_failure_is_not_classified_as_provider_failure() -> None:
    error = _http_status_error(SpringWorkerHttpError, "INTERNAL_SERVER_ERROR")

    assert analysis_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR


def test_world_setting_contract_validation_400_has_dedicated_comparison_code() -> None:
    error = _http_status_error(
        SpringWorkerHttpError,
        "WORLD_SETTING_COMPARISON_TARGET_INVALID",
        status_code=400,
        reason_code="PROPOSED_PATH_MISMATCH",
    )

    assert analysis_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(error) is AnalysisFailureCode.COMPARISON_VALIDATION_FAILED


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (400, "UNKNOWN_CLIENT_ERROR"),
        (500, "WORLD_SETTING_COMPARISON_TARGET_INVALID"),
        (503, "COMMON_INTERNAL_SERVER_ERROR"),
    ],
)
def test_unknown_spring_failures_remain_unexpected(
    status_code: int,
    error_code: str,
) -> None:
    error = _http_status_error(
        SpringWorkerHttpError,
        error_code,
        status_code=status_code,
    )

    assert comparison_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR


def test_spring_worker_transport_failure_is_not_classified_as_llm_network_failure() -> None:
    request = httpx.Request("PATCH", "https://spring.test/progress")
    error = SpringWorkerTransportError("Spring transport failed")
    error.__cause__ = httpx.ConnectError("Spring unavailable", request=request)

    assert analysis_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR


def test_spring_worker_lease_conflict_has_dedicated_failure_code() -> None:
    error = _http_status_error(WorkerLeaseExpiredError, "ANALYSIS_JOB_LEASE_CONFLICT")

    assert analysis_failure_code(error) is AnalysisFailureCode.WORKER_LEASE_EXPIRED
    assert comparison_failure_code(error) is AnalysisFailureCode.WORKER_LEASE_EXPIRED


def test_raw_provider_http_failure_remains_provider_error() -> None:
    error = _http_status_error(httpx.HTTPStatusError, "rate_limit_exceeded")

    assert analysis_failure_code(error) is AnalysisFailureCode.LLM_PROVIDER_ERROR


def _http_status_error(
    error_type,
    error_code: str,
    status_code: int = 500,
    reason_code: str | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://service.test/request")
    response = httpx.Response(
        status_code,
        request=request,
        json={
            "error": {
                "code": error_code,
                "context": {"reasonCode": reason_code} if reason_code else {},
            }
        },
    )
    if issubclass(error_type, SpringWorkerHttpError):
        return error_type(
            "request failed",
            request=request,
            response=response,
            status_code=status_code,
            spring_error_code=error_code,
            spring_reason_code=reason_code,
        )
    return error_type("request failed", request=request, response=response)
