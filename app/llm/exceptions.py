class LlmResponseValidationError(ValueError):
    """사용량이 포함된 성공 응답의 텍스트 구조가 계약을 위반한 경우다."""

    def __init__(
        self,
        message: str,
        input_token_count: int | None = None,
        cached_input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.input_token_count = input_token_count
        self.cached_input_token_count = cached_input_token_count
        self.output_token_count = output_token_count
