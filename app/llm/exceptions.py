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


class LlmOutputTruncatedError(LlmResponseValidationError):
    """Responses API가 출력 상한 때문에 완전한 결과를 만들지 못한 경우다."""

    def __init__(
        self,
        message: str,
        *,
        incomplete_reason: str | None,
        max_output_tokens: int,
        input_token_count: int | None = None,
        cached_input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        super().__init__(
            message,
            input_token_count=input_token_count,
            cached_input_token_count=cached_input_token_count,
            output_token_count=output_token_count,
        )
        self.incomplete_reason = incomplete_reason
        self.max_output_tokens = max_output_tokens


class LlmIncompleteResponseError(LlmResponseValidationError):
    """HTTP 200이지만 Responses API 상태가 completed가 아닌 provider 실패다."""
