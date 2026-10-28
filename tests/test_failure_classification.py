import httpx
import pytest

from app.analysis.exceptions import (
    ComparisonValidationError,
    LlmExtractionError,
    OrderedAnalysisIncompleteError,
    OrderedInputContextError,
)
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
    is_candidate_comparison_failure,
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


@pytest.mark.parametrize(("status", "code"), [
    (400, "invalid_request_error"), (401, "invalid_api_key"), (403, "permission_denied"),
    (429, "insufficient_quota"), (429, "billing_hard_limit_reached"),
    (429, "billing_not_active"), (429, "quota_exceeded"), (429, "usage_limit_reached"),
])
def test_global_provider_failures_cannot_be_saved_as_deferrable_candidate_errors(status, code):
    failure = _http_status_error(httpx.HTTPStatusError, code, status_code=status)
    wrapped = ComparisonValidationError("wrapped response error")
    wrapped.__cause__ = failure
    for error in (failure, wrapped):
        assert is_candidate_comparison_failure(error) is False
        assert comparison_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR
        assert analysis_failure_code(error) is AnalysisFailureCode.UNEXPECTED_ERROR


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_provider_failures_keep_existing_candidate_isolation_after_retries(status):
    failure = _http_status_error(httpx.HTTPStatusError, "rate_limit_exceeded", status_code=status)
    assert is_candidate_comparison_failure(failure) is True
    assert comparison_failure_code(failure) is AnalysisFailureCode.LLM_PROVIDER_ERROR


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


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("cause_type", [ComparisonValidationError, LlmResponseValidationError])
def test_input_context_failure_persists_non_deferrable_code_even_with_provider_cause(
    wrapped, cause_type,
):
    failure = OrderedInputContextError("Frozen input is invalid.")
    failure.__cause__ = cause_type("Earlier response failed.")
    if wrapped:
        wrapper = ComparisonValidationError("Comparison failed.")
        wrapper.__cause__ = failure
        failure = wrapper
    assert analysis_failure_code(failure) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(failure) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert is_candidate_comparison_failure(failure) is False


@pytest.mark.parametrize("outer_type", [OrderedInputContextError, OrderedAnalysisIncompleteError])
@pytest.mark.parametrize("quota", [True, False])
def test_execution_failure_takes_precedence_over_outer_comparison_failure(outer_type, quota):
    failure = outer_type(AnalysisFailureCode.COMPARISON_VALIDATION_FAILED)
    failure.__cause__ = AiTokenQuotaExhaustedError() if quota else _http_status_error(
        WorkerLeaseExpiredError, "ANALYSIS_JOB_LEASE_CONFLICT", status_code=409,
    )
    expected = (AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED if quota
                else AnalysisFailureCode.WORKER_LEASE_EXPIRED)
    assert analysis_failure_code(failure) is expected
    assert comparison_failure_code(failure) is expected
    assert is_candidate_comparison_failure(failure) is False


def test_input_context_failure_overrides_wrapped_world_backend_validation_code():
    failure = OrderedInputContextError("Input and response do not belong together.")
    failure.__cause__ = _http_status_error(
        SpringWorkerHttpError, "WORLD_SETTING_COMPARISON_TARGET_INVALID", status_code=400,
    )
    assert analysis_failure_code(failure) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(failure) is AnalysisFailureCode.UNEXPECTED_ERROR


def test_incomplete_job_keeps_diagnostic_code_but_cannot_be_persisted_as_deferrable_batch():
    failure = OrderedAnalysisIncompleteError(AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR)
    assert analysis_failure_code(failure) is AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    assert comparison_failure_code(failure) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert is_candidate_comparison_failure(failure) is False


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


def test_safe_provider_diagnostics_do_not_change_coarse_failure_or_retry_scope():
    from app.exceptions.failure_classification import failure_diagnostics
    response = httpx.Response(429, request=httpx.Request("POST", "https://provider.example/responses"),
                              headers={"x-request-id": "request_123"}, json={"error": {
                                  "code": "rate_limit_exceeded", "message": "PRIVATE_MANUSCRIPT"}})
    error = httpx.HTTPStatusError("PRIVATE_MESSAGE", request=response.request, response=response)
    diagnostic = failure_diagnostics(error, stage="WORLD_COMPARE")
    assert diagnostic.origin == "PROVIDER" and diagnostic.reason_code == "LLM_RATE_LIMIT"
    assert diagnostic.http_status == 429 and diagnostic.request_id == "request_123"
    assert diagnostic.source_error_code == "rate_limit_exceeded"
    assert "PRIVATE" not in diagnostic.model_dump_json()
    assert comparison_failure_code(error) is AnalysisFailureCode.LLM_PROVIDER_ERROR
    assert is_candidate_comparison_failure(error)


def test_safe_diagnostics_ignore_untyped_exception_metadata_and_cyclic_causes():
    from app.exceptions.failure_classification import failure_diagnostics
    error = RuntimeError("PRIVATE_MESSAGE")
    error.failure_diagnostics = {"reasonCode": "PRIVATE_REASON"}
    error.__cause__ = error
    diagnostic = failure_diagnostics(error, stage="FINALIZE")
    assert diagnostic.reason_code == "UNKNOWN"
    assert diagnostic.stage == "FINALIZE"
    assert "PRIVATE" not in diagnostic.model_dump_json()


def test_quota_diagnostic_preserves_spring_origin_without_making_quota_retryable():
    from app.exceptions.failure_classification import failure_diagnostics
    error = AiTokenQuotaExhaustedError(status_code=409, spring_error_code="AI_TOKEN_QUOTA_EXHAUSTED")
    diagnostic = failure_diagnostics(error, stage="CHARACTER_COMPARE")
    assert diagnostic.origin == "SPRING" and diagnostic.http_status == 409
    assert diagnostic.source_error_code == "AI_TOKEN_QUOTA_EXHAUSTED"
    assert analysis_failure_code(error) is AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED
    assert not is_candidate_comparison_failure(error)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_typed_spring_transport_boundary_owns_generic_httpx_cause(error_type):
    from app.exceptions.failure_classification import failure_diagnostics
    request = httpx.Request("POST", "http://spring.private/internal?secret=PRIVATE_QUERY")
    cause = error_type("PRIVATE_TRANSPORT_MESSAGE", request=request)
    transport = SpringWorkerTransportError("Spring request failed before response.")
    transport.__cause__ = cause
    wrapper = RuntimeError("Job failed")
    wrapper.__cause__ = transport

    diagnostic = failure_diagnostics(wrapper, stage="WORLD_CONTEXT")
    assert diagnostic.origin == "SPRING"
    assert diagnostic.reason_code == "INTERNAL_API_TRANSPORT_ERROR"
    assert diagnostic.stage == "WORLD_CONTEXT"
    assert diagnostic.http_status is None and diagnostic.request_id is None
    assert "PRIVATE" not in diagnostic.model_dump_json()
    assert "spring.private" not in diagnostic.model_dump_json()
    assert analysis_failure_code(wrapper) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(wrapper) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert not is_candidate_comparison_failure(wrapper)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_genuine_provider_transport_causes_keep_provider_diagnostics(error_type):
    from app.exceptions.failure_classification import failure_diagnostics
    provider = error_type("PRIVATE", request=httpx.Request("POST", "https://provider.example"))
    wrapper = RuntimeError("comparison failed")
    wrapper.__cause__ = provider
    diagnostic = failure_diagnostics(wrapper, stage="CHARACTER_COMPARE")
    assert diagnostic.origin == "PROVIDER"
    assert diagnostic.reason_code == ("LLM_TIMEOUT" if error_type is httpx.ReadTimeout else "LLM_PROVIDER_ERROR")
    assert comparison_failure_code(wrapper) is AnalysisFailureCode.LLM_NETWORK_ERROR
    assert is_candidate_comparison_failure(wrapper)


def test_specific_stored_diagnostic_survives_spring_transport_boundary():
    from app.exceptions.failure_classification import failure_diagnostics
    from app.schemas.worker import WorkerFailureDiagnostics
    specific = WorkerFailureDiagnostics(origin="SPRING", stage="CHARACTER_CONTEXT",
        reasonCode="CURRENT_EVIDENCE_SOURCE_READ_FAILED", httpStatus=503,
        requestId="11111111-1111-4111-8111-111111111111")
    transport = SpringWorkerTransportError("Spring request failed.")
    transport.failure_diagnostics = specific
    transport.__cause__ = httpx.ReadTimeout("PRIVATE", request=httpx.Request("POST", "http://spring.private"))
    assert failure_diagnostics(transport, stage="FINALIZE") == specific
