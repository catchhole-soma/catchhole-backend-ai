from typing import TypeVar

import httpx

from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.clients.exceptions import (
    AiTokenQuotaExhaustedError,
    SpringWorkerHttpError,
    SpringWorkerTransportError,
    WorkerLeaseExpiredError,
)
from app.domain.enums import AnalysisFailureCode
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)

TException = TypeVar("TException", bound=BaseException)


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


def _common_failure_code(exc: BaseException) -> AnalysisFailureCode | None:
    if is_token_quota_exhausted(exc):
        return AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED
    if _find_exception(exc, WorkerLeaseExpiredError) is not None:
        return AnalysisFailureCode.WORKER_LEASE_EXPIRED
    if _find_exception(exc, SpringWorkerTransportError) is not None:
        return AnalysisFailureCode.UNEXPECTED_ERROR
    if _find_exception(exc, SpringWorkerHttpError) is not None:
        return AnalysisFailureCode.UNEXPECTED_ERROR
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
