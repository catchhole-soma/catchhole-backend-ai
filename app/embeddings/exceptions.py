class EmbeddingError(Exception):
    """임베딩 생성·저장 흐름에서 사용하는 기본 예외다."""


class RecoverableEmbeddingProviderError(EmbeddingError):
    """분석을 계속하고 나중에 임베딩을 재처리할 수 있는 외부 제공자 장애다."""


class EmbeddingResponseValidationError(EmbeddingError, ValueError):
    """사용량이 포함된 성공 응답의 임베딩 데이터가 계약을 위반한 경우다."""

    def __init__(self, message: str, input_token_count: int | None = None) -> None:
        super().__init__(message)
        self.input_token_count = input_token_count


class EmbeddingDataIntegrityError(EmbeddingError):
    """청크와 임베딩 저장 대상이 일치하지 않는 데이터 정합성 오류다."""
