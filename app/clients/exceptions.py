import httpx


class AiTokenQuotaExhaustedError(RuntimeError):
    """Spring 원장이 최소 provider 요청 예약을 거절한 비재시도 오류다."""

    error_code = "AI_TOKEN_QUOTA_EXHAUSTED"

    def __init__(
        self,
        *,
        status_code: int | None = None,
        spring_error_code: str | None = None,
        spring_reason_code: str | None = None,
    ) -> None:
        super().__init__("AI token quota is exhausted.")
        self.status_code = status_code
        self.spring_error_code = spring_error_code
        self.spring_reason_code = spring_reason_code


class SpringWorkerHttpError(httpx.HTTPStatusError):
    """Spring Worker API가 반환한 HTTP 실패다."""

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        status_code: int | None = None,
        spring_error_code: str | None = None,
        spring_reason_code: str | None = None,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.status_code = response.status_code if status_code is None else status_code
        self.spring_error_code = spring_error_code
        self.spring_reason_code = spring_reason_code


class SpringWorkerTransportError(RuntimeError):
    """Spring Worker API 요청이 응답 전에 전송 계층에서 실패한 경우다."""


class WorkerLeaseExpiredError(SpringWorkerHttpError):
    """Spring이 Worker lease 만료 또는 불일치를 반환한 경우다."""

    error_code = "ANALYSIS_JOB_LEASE_CONFLICT"
