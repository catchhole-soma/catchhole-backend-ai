from typing import TypeVar

import httpx

from app.analysis.exceptions import (
    ComparisonValidationError, LlmExtractionError, OrderedAnalysisIncompleteError, OrderedInputContextError,
)
from app.clients.exceptions import (
    AiTokenQuotaExhaustedError,
    SpringWorkerHttpError,
    SpringWorkerTransportError,
    WorkerLeaseExpiredError,
)
from app.domain.enums import AnalysisFailureCode
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmInputTokenCountError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)

from app.schemas.worker import WorkerFailureDiagnostics, PROVIDER_DIAGNOSTIC_CODES

TException = TypeVar("TException", bound=BaseException)

COMPARISON_CANDIDATE_FAILURE_CODES = frozenset({
    AnalysisFailureCode.LLM_NETWORK_ERROR,
    AnalysisFailureCode.LLM_PROVIDER_ERROR,
    AnalysisFailureCode.LLM_OUTPUT_TRUNCATED,
    AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
    AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
})

_PROVIDER_ACCOUNT_FAILURE_CODES = frozenset({
    "billing_hard_limit_reached", "billing_not_active", "insufficient_quota",
    "quota_exceeded", "usage_limit_reached",
})


def is_global_provider_failure(exc: BaseException) -> bool:
    """Configuration/account failures cannot become dozens of candidate failures.

    Inspect only the provider's fixed code/type metadata, never its message.
    Transient 408/429/5xx errors retain the existing metered retry policy.
    """
    error = _find_exception(exc, httpx.HTTPStatusError)
    if error is None or isinstance(error, SpringWorkerHttpError):
        return False
    status = error.response.status_code
    if 400 <= status < 500 and status not in {408, 429}:
        return True
    if status != 429:
        return False
    try:
        payload = error.response.json()
    except ValueError:
        return False
    detail = payload.get("error") if isinstance(payload, dict) else None
    return isinstance(detail, dict) and any(
        isinstance(detail.get(field), str)
        and detail[field].strip().casefold() in _PROVIDER_ACCOUNT_FAILURE_CODES
        for field in ("code", "type")
    )


def is_candidate_comparison_failure(exc: BaseException) -> bool:
    """Only provider/decision errors are isolatable; API/context/lease remain job-scoped."""
    if is_global_provider_failure(exc) or any(_find_exception(exc, error) is not None for error in (
        OrderedInputContextError, OrderedAnalysisIncompleteError,
        LlmInputTokenCountError,
        SpringWorkerHttpError, SpringWorkerTransportError, AiTokenQuotaExhaustedError,
    )):
        return False
    return comparison_failure_code(exc) in COMPARISON_CANDIDATE_FAILURE_CODES


def analysis_failure_code(exc: BaseException) -> AnalysisFailureCode:
    common_code = _common_failure_code(exc)
    if common_code is not None:
        return common_code
    if _find_exception(exc, ComparisonValidationError) is not None:
        return AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    if _find_exception(exc, LlmExtractionError) is not None:
        return AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    if _find_exception(exc, LlmResponseValidationError) is not None:
        return AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    return AnalysisFailureCode.UNEXPECTED_ERROR


def comparison_failure_code(exc: BaseException) -> AnalysisFailureCode:
    common_code = _common_failure_code(exc)
    if (
        _find_exception(exc, OrderedAnalysisIncompleteError) is not None
        and common_code in COMPARISON_CANDIDATE_FAILURE_CODES
    ):
        return AnalysisFailureCode.UNEXPECTED_ERROR
    spring_error = _find_exception(exc, SpringWorkerHttpError)
    if (
        common_code is AnalysisFailureCode.UNEXPECTED_ERROR
        and _find_exception(exc, OrderedInputContextError) is None
        and _find_exception(exc, OrderedAnalysisIncompleteError) is None
        and _find_exception(exc, SpringWorkerTransportError) is None
        and spring_error is not None
        and spring_error.status_code == 400
        and spring_error.spring_error_code == "WORLD_SETTING_COMPARISON_TARGET_INVALID"
    ):
        # This diagnostic code retains Spring source metadata, which independently
        # prevents the backend from deferring a rejected persistence operation.
        return AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    if common_code is not None:
        return common_code
    if _find_exception(exc, LlmResponseValidationError) is not None:
        return AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    if _find_exception(exc, ComparisonValidationError) is not None:
        return AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    if _find_exception(exc, LlmExtractionError) is not None:
        return AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    return AnalysisFailureCode.UNEXPECTED_ERROR


def is_token_quota_exhausted(exc: BaseException) -> bool:
    return _find_exception(exc, AiTokenQuotaExhaustedError) is not None


def spring_failure_source(exc: BaseException) -> tuple[str | None, str | None]:
    spring_error = _find_exception(exc, SpringWorkerHttpError)
    if spring_error is not None:
        return spring_error.spring_error_code, spring_error.spring_reason_code
    quota_error = _find_exception(exc, AiTokenQuotaExhaustedError)
    if quota_error is not None:
        return quota_error.spring_error_code, quota_error.spring_reason_code
    return None, None


def _common_failure_code(exc: BaseException) -> AnalysisFailureCode | None:
    if is_token_quota_exhausted(exc):
        return AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED
    if _find_exception(exc, WorkerLeaseExpiredError) is not None:
        return AnalysisFailureCode.WORKER_LEASE_EXPIRED
    if _find_exception(exc, LlmInputTokenCountError) is not None:
        return AnalysisFailureCode.UNEXPECTED_ERROR
    if _find_exception(exc, OrderedInputContextError) is not None:
        # The failure endpoint must persist a non-deferrable code too: Python's
        # exception type is no longer available to a later backend claim/finalize.
        return AnalysisFailureCode.UNEXPECTED_ERROR
    if _find_exception(exc, SpringWorkerTransportError) is not None:
        return AnalysisFailureCode.UNEXPECTED_ERROR
    if _find_exception(exc, SpringWorkerHttpError) is not None:
        return AnalysisFailureCode.UNEXPECTED_ERROR
    if is_global_provider_failure(exc):
        # Persist a non-deferrable category too: later Spring finalize/claim
        # cannot inspect this Python exception or the provider HTTP response.
        return AnalysisFailureCode.UNEXPECTED_ERROR
    incomplete = _find_exception(exc, OrderedAnalysisIncompleteError)
    if incomplete is not None:
        return incomplete.failure_code
    if _find_exception(exc, LlmOutputTruncatedError) is not None:
        return AnalysisFailureCode.LLM_OUTPUT_TRUNCATED
    if any(
        _find_exception(exc, error_type) is not None
        for error_type in (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        )
    ):
        return AnalysisFailureCode.LLM_NETWORK_ERROR
    if (
        _find_exception(exc, httpx.HTTPStatusError) is not None
        or _find_exception(exc, LlmIncompleteResponseError) is not None
    ):
        return AnalysisFailureCode.LLM_PROVIDER_ERROR
    return None


def _find_exception(
    exc: BaseException,
    exception_type: type[TException],
) -> TException | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, exception_type):
            return current
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def failure_diagnostics(exc: BaseException, *, stage: str) -> WorkerFailureDiagnostics | None:
    """Return the most specific safe cause metadata without changing retry policy."""
    chain = []
    visited = set()
    current = exc
    while current is not None and id(current) not in visited:
        chain.append(current)
        visited.add(id(current))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    for error in reversed(chain):
        stored = getattr(error, "failure_diagnostics", None)
        if isinstance(stored, WorkerFailureDiagnostics):
            return stored.model_copy(update={
                "stage": stage if stored.stage == "UNKNOWN" else stored.stage,
                "trace_id": stored.trace_id or _current_trace_id(),
            })
    # A typed Spring boundary owns its generic httpx transport cause.
    if any(isinstance(error, SpringWorkerTransportError) for error in chain):
        return WorkerFailureDiagnostics(origin="SPRING", stage=stage, reasonCode="INTERNAL_API_TRANSPORT_ERROR")
    for error in reversed(chain):
        if isinstance(error, SpringWorkerHttpError):
            return WorkerFailureDiagnostics(origin="SPRING", stage=stage, reasonCode="INTERNAL_API_HTTP_ERROR",
                sourceErrorCode=error.spring_error_code, httpStatus=error.status_code,
                requestId=error.response.headers.get("X-Request-ID"))
        if isinstance(error, AiTokenQuotaExhaustedError):
            return WorkerFailureDiagnostics(origin="SPRING", stage=stage,
                reasonCode="INTERNAL_API_HTTP_ERROR" if error.status_code is not None else "UNKNOWN",
                sourceErrorCode=error.spring_error_code or error.error_code, httpStatus=error.status_code)
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            code = "UNKNOWN"
            try:
                detail = response.json().get("error", {})
                if isinstance(detail, dict):
                    code = next((detail[key] for key in ("code", "type")
                                 if isinstance(detail.get(key), str) and detail[key] in PROVIDER_DIAGNOSTIC_CODES), "UNKNOWN")
            except (ValueError, AttributeError):
                pass
            return WorkerFailureDiagnostics(origin="PROVIDER", stage=stage,
                reasonCode="LLM_RATE_LIMIT" if response.status_code == 429 else "LLM_PROVIDER_ERROR",
                sourceErrorCode=code, httpStatus=response.status_code,
                requestId=response.headers.get("x-request-id"), traceId=_current_trace_id())
        if isinstance(error, LlmOutputTruncatedError):
            return WorkerFailureDiagnostics(origin="PROVIDER", stage=stage, reasonCode="LLM_OUTPUT_TRUNCATED", traceId=_current_trace_id())
        if isinstance(error, LlmIncompleteResponseError) and error.response is not None:
            return WorkerFailureDiagnostics(origin="PROVIDER", stage=stage, reasonCode="LLM_PROVIDER_ERROR",
                httpStatus=error.response.status_code, requestId=error.response.headers.get("x-request-id"), traceId=_current_trace_id())
        if isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, LlmIncompleteResponseError)):
            return WorkerFailureDiagnostics(origin="PROVIDER", stage=stage,
                reasonCode="LLM_TIMEOUT" if isinstance(error, httpx.TimeoutException) else "LLM_PROVIDER_ERROR", traceId=_current_trace_id())
        if isinstance(error, (ComparisonValidationError, LlmResponseValidationError, LlmExtractionError)):
            return WorkerFailureDiagnostics(origin="WORKER", stage=stage, reasonCode="COMPARISON_RESULT_INVALID", traceId=_current_trace_id())
    return WorkerFailureDiagnostics(origin="WORKER", stage=stage, reasonCode="UNKNOWN", traceId=_current_trace_id())


def _current_trace_id():
    try:
        from opentelemetry import trace
        context = trace.get_current_span().get_span_context()
        return format(context.trace_id, "032x") if context.is_valid else None
    except Exception:
        return None
