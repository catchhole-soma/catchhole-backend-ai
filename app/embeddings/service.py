from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.embeddings.client import OpenAIEmbeddingsClient
from app.embeddings.responses import EmbeddingBatchResponse
from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import (
    EpisodeChunkEmbeddingUpdate,
    EpisodeChunkRepository,
)


@dataclass(frozen=True)
class ChunkEmbeddingResult:
    """청크 임베딩 한 묶음의 처리 결과다."""

    embedded_chunk_count: int
    input_token_count: int | None = None


class EmbeddingClientApi(Protocol):
    """ChunkEmbeddingService가 임베딩 클라이언트에 요구하는 최소 규격이다."""

    version: str

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        pass


class ChunkEmbeddingService:
    """저장된 청크 텍스트를 임베딩하고 벡터와 생성 정보를 DB에 반영한다."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        embedding_client: EmbeddingClientApi | None = None,
        repository_factory: Callable[[Session], EpisodeChunkRepository] = EpisodeChunkRepository,
        now_factory: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.session_factory = session_factory
        self.embedding_client = embedding_client or OpenAIEmbeddingsClient.from_settings()
        self.repository_factory = repository_factory
        self.now_factory = now_factory

    def embed_chunks(self, chunks: list[EpisodeChunk]) -> ChunkEmbeddingResult:
        """청크 순서대로 벡터를 생성하고 임베딩 필드만 한 트랜잭션으로 갱신한다."""

        if not chunks:
            return ChunkEmbeddingResult(embedded_chunk_count=0)

        # 외부 API를 기다리는 동안 DB 트랜잭션을 점유하지 않도록 먼저 벡터를 생성한다.
        response = self.embedding_client.create_embeddings(
            [chunk.chunk_text for chunk in chunks]
        )
        embedded_at = self.now_factory()
        embedding_updates = [
            EpisodeChunkEmbeddingUpdate(
                chunk_id=chunk.id,
                embedding=embedding,
                embedding_model=response.model,
                embedding_version=self.embedding_client.version,
                embedded_at=embedded_at,
            )
            for chunk, embedding in zip(chunks, response.embeddings, strict=True) # 청크와 벡터를 입력 순서대로 1:1로 묶고, 개수가 다르면 예외를 발생시킨다.
        ]

        with self.session_factory() as session:
            repository = self.repository_factory(session)
            try:
                updated_chunks = repository.update_embeddings(embedding_updates)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return ChunkEmbeddingResult(
            embedded_chunk_count=len(updated_chunks),
            input_token_count=response.input_token_count,
        )
