import httpx

from app.core.config import Settings, get_settings
from app.embeddings.exceptions import RecoverableEmbeddingProviderError
from app.embeddings.responses import EmbeddingBatchResponse


class OpenAIEmbeddingsClient:
    """OpenAI Embeddings API 호출과 응답 검증을 담당한다."""

    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int,
        version: str,
        embeddings_api_url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        """임베딩 생성에 사용할 API 설정과 HTTP 클라이언트를 초기화한다.

        테스트에서는 ``http_client``에 MockTransport가 적용된 클라이언트를
        주입할 수 있으며, 운영 코드에서는 기본 60초 timeout을 사용한다.
        """

        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive.")

        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.version = version
        self.embeddings_api_url = embeddings_api_url
        self.http_client = http_client or httpx.Client(timeout=60)

    @classmethod #첫 번째 인자로 클래스 자체를 자동으로 전달
    def from_settings(cls, settings: Settings | None = None) -> "OpenAIEmbeddingsClient":
        """애플리케이션 설정값으로 임베딩 클라이언트를 생성한다.

        설정을 직접 전달하지 않으면 캐시된 전역 설정을 사용한다.
        """

        settings = settings or get_settings()
        return cls(
            api_key=settings.llm_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            version=settings.embedding_version,
            embeddings_api_url=settings.openai_embeddings_api_url,
        )

    def create_embedding(self, text: str) -> list[float]:
        """단일 문자열을 임베딩하고 생성된 벡터 하나를 반환한다.
        검색 query 하나를 임베딩 해서 단건 조회하는 경우를 위함
        예) query_vector = client.create_embedding("주인공의 나이가 달라짐")
        """

        return self.create_embeddings([text]).embeddings[0]

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        """여러 문자열(청크들)을 한 요청으로 임베딩하고 검증된 배치 결과를 반환한다.

        일시적인 네트워크·provider 오류는 ``RecoverableEmbeddingProviderError``로
        변환한다. 복구 불가능한 HTTP 오류와 입력·응답 계약 오류는 그대로 전달한다.
        """

        self._validate_inputs(inputs)

        try:
            response = self.http_client.post(
                self.embeddings_api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": inputs,
                    "dimensions": self.dimensions,
                    "encoding_format": "float",
                },
            )
            response.raise_for_status()
        except httpx.TransportError as exc:
            # timeout·연결 실패는 청크를 NULL로 남긴 뒤 backfill할 수 있는 일시적 장애다.
            raise RecoverableEmbeddingProviderError(
                "Embedding provider connection failed temporarily."
            ) from exc
        except httpx.HTTPStatusError as exc:
            if self._is_recoverable_http_status(exc.response.status_code):
                # 요청 제한과 provider 서버 장애는 현재 분석의 설정 추출과 분리한다.
                raise RecoverableEmbeddingProviderError(
                    f"Embedding provider failed temporarily: status={exc.response.status_code}"
                ) from exc
            # 400·401·403처럼 재처리로 해결되지 않는 요청·인증 오류는 분석을 실패시킨다.
            raise

        payload = response.json()
        embeddings = self._extract_embeddings(payload, len(inputs))
        usage = payload.get("usage") or {}
        return EmbeddingBatchResponse(
            embeddings=embeddings,
            model=payload.get("model") or self.model,
            input_token_count=usage.get("prompt_tokens"),
            raw_response=payload,
        )

    def _validate_inputs(self, inputs: list[str]) -> None:
        """API 키가 있고 임베딩할 문자열 목록이 비어 있지 않은지 검사한다."""

        if not self.api_key:
            raise ValueError("LLM_API_KEY is required.")
        if not inputs:
            raise ValueError("Embedding inputs must not be empty.")
        if any(not text.strip() for text in inputs):
            raise ValueError("Embedding input text must not be blank.")

    @staticmethod
    def _is_recoverable_http_status(status_code: int) -> bool:
        """잠시 후 재처리할 가치가 있는 HTTP 상태인지 판단한다."""

        return status_code in {408, 409, 429} or status_code >= 500

    def _extract_embeddings(self, payload: dict, expected_count: int) -> list[list[float]]:
        """API 응답에서 벡터를 꺼내 입력 순서로 정렬하고 개수와 차원을 검증한다.

        공식 API 응답도 외부 입력이므로 저장 전에 필수 필드와 형식을 검증한다.
        """

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise ValueError("Embedding response count does not match the input count.")

        if any(not isinstance(item, dict) for item in data):
            raise ValueError("Embedding response items are invalid.")

        # index는 요청 input의 위치를 나타내므로 누락되거나 정수가 아니면 순서를 복원할 수 없다.
        if any(type(item.get("index")) is not int for item in data):
            raise ValueError("Embedding response indices are invalid.")

        # 응답 배열 자체의 순서에 의존하지 않고 index를 기준으로 입력 순서를 복원한다.
        ordered_data = sorted(data, key=lambda item: item["index"])
        if [item["index"] for item in ordered_data] != list(range(expected_count)):
            raise ValueError("Embedding response indices are invalid.")

        embeddings: list[list[float]] = []
        for item in ordered_data:
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or len(embedding) != self.dimensions:
                raise ValueError("Embedding response dimensions do not match the configured dimensions.")
            embeddings.append([float(value) for value in embedding])
        return embeddings
