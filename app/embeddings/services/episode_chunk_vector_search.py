from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.embeddings.client import OpenAIEmbeddingsClient
from app.repositories.episode_chunk_repository import (
    EpisodeChunkRepository,
    EpisodeChunkSearchResult,
)


class QueryEmbeddingClientApi(Protocol):
    """검색 Service가 임베딩 클라이언트에 요구하는 최소 규격"""

    model: str
    version: str

    def create_embedding(self, text: str) -> list[float]:
        pass


class EpisodeChunkVectorSearchService:
    """검색 문장을 임베딩하고 관련 EpisodeChunk Top-K를 조회한다."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        embedding_client: QueryEmbeddingClientApi | None = None,
        repository_factory: Callable[[Session], EpisodeChunkRepository] = EpisodeChunkRepository,
    ) -> None:
        self.session_factory = session_factory
        self.embedding_client = embedding_client or OpenAIEmbeddingsClient.from_settings()
        self.repository_factory = repository_factory

    def search_similar_chunks(
        self,
        query_text: str,
        work_id: UUID,
        top_k: int,
        episode_no_from: int | None = None,
        episode_no_to: int | None = None,
        excluded_chunk_ids: list[UUID] | None = None,
    ) -> list[EpisodeChunkSearchResult]:
        """검색 문장과 범용 범위를 받아 의미적으로 가까운 청크를 반환한다.

        설정 후보의 유형에 따른 검색어 생성이나 결과 재정렬은 수행하지 않는다.
        이 Service는 NVM-143이 사용할 query embedding과 pgvector 조회 연결만 담당한다.
        """

        # 잘못된 조건으로 OpenAI 호출 비용이 발생하지 않도록 임베딩 생성 전에 검증한다.
        self._validate_search_conditions(
            query_text=query_text,
            top_k=top_k,
            episode_no_from=episode_no_from,
            episode_no_to=episode_no_to,
        )

        # 검색 문장도 저장된 청크와 같은 client 설정으로 임베딩해야 비교할 수 있다.
        # 외부 API 응답을 기다리는 동안 DB 세션을 점유하지 않도록 세션보다 먼저 호출한다.
        query_embedding = self.embedding_client.create_embedding(query_text)

        # 조회 전용 세션이므로 commit은 필요 없으며 with 종료 시 연결을 반환한다.
        with self.session_factory() as session:
            repository = self.repository_factory(session)
            return repository.search_similar_chunks(
                query_embedding=query_embedding,
                work_id=work_id,
                embedding_model=self.embedding_client.model,
                embedding_version=self.embedding_client.version,
                top_k=top_k,
                episode_no_from=episode_no_from,
                episode_no_to=episode_no_to,
                excluded_chunk_ids=excluded_chunk_ids,
            )

    @staticmethod
    def _validate_search_conditions(
        query_text: str,
        top_k: int,
        episode_no_from: int | None,
        episode_no_to: int | None,
    ) -> None:
        """외부 API와 DB를 호출하기 전에 검색 조건의 기본 형식을 검증한다."""

        if not query_text.strip():
            raise ValueError("Query text must not be blank.")
        if top_k <= 0:
            raise ValueError("Top-K must be greater than zero.")
        if (
            episode_no_from is not None
            and episode_no_to is not None
            and episode_no_from > episode_no_to
        ):
            raise ValueError("Episode number range is invalid.")
