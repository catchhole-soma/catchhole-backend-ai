import httpx


class AiTokenQuotaExhaustedError(RuntimeError):
    """Spring 원장이 최소 provider 요청 예약을 거절한 비재시도 오류다."""

    error_code = "AI_TOKEN_QUOTA_EXHAUSTED"

    def __init__(self) -> None:
        super().__init__("AI token quota is exhausted.")


class SpringWorkerHttpError(httpx.HTTPStatusError):
    """Spring Worker API가 반환한 HTTP 실패다."""


class WorkerLeaseExpiredError(SpringWorkerHttpError):
    """Spring이 Worker lease 만료 또는 불일치를 반환한 경우다."""

    error_code = "ANALYSIS_JOB_LEASE_CONFLICT"
