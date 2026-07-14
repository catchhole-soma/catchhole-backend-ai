from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EmbeddingBatchResponse:
    """한 번의 임베딩 API 호출 결과를 애플리케이션 내부 형식으로 보관한다.

    입력 순서대로 정렬한 벡터와 실제 응답 모델, 사용 토큰 수를 제공하며,
    추후 로깅이나 문제 분석이 필요할 때를 위해 원본 응답도 함께 보존한다.
    """

    embeddings: list[list[float]]
    model: str
    input_token_count: int | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
