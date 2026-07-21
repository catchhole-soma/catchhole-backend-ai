class EmbeddingError(Exception):
    """임베딩 생성·저장 흐름에서 사용하는 기본 예외다."""


class RecoverableEmbeddingProviderError(EmbeddingError):
    """분석을 계속하고 나중에 임베딩을 재처리할 수 있는 외부 제공자 장애다."""


class EmbeddingDataIntegrityError(EmbeddingError):
    """청크와 임베딩 저장 대상이 일치하지 않는 데이터 정합성 오류다."""
