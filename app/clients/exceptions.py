class AiTokenQuotaExhaustedError(RuntimeError):
    """Spring 원장이 최소 provider 요청 예약을 거절한 비재시도 오류다."""

    error_code = "AI_TOKEN_QUOTA_EXHAUSTED"

    def __init__(self) -> None:
        super().__init__("AI token quota is exhausted.")
