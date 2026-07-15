from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.episode_chunk import EpisodeChunk


@dataclass(frozen=True)
class EpisodeChunkEmbeddingUpdate:
    """청크에 반영할 임베딩 필드만 묶은 저장 요청이다."""

    chunk_id: UUID
    embedding: list[float]
    embedding_model: str
    embedding_version: str
    embedded_at: datetime


class EpisodeChunkRepository:
    # 청킹 결과의 DB 접근만 담당한다. commit/rollback은 Service 또는 Worker 계층에서 처리한다.
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_episode_id(self, episode_id: UUID) -> list[EpisodeChunk]:
        # LLM 입력이나 근거 조회에서 사용할 수 있도록 청크 순서를 보장해 가져온다.
        statement = (
            select(EpisodeChunk)
            .where(EpisodeChunk.episode_id == episode_id)
            .order_by(EpisodeChunk.chunk_index)
        )
        return list(self.session.scalars(statement).all())

    def save_all(self, chunks: list[EpisodeChunk]) -> list[EpisodeChunk]:
        # split 결과로 만들어진 여러 청크를 한 번에 저장 대기 상태로 올린다.
        self.session.add_all(chunks)
        return chunks

    def update_embeddings(
        self,
        embedding_updates: list[EpisodeChunkEmbeddingUpdate],
    ) -> list[EpisodeChunk]:
        """저장된 청크를 조회해 임베딩 관련 필드만 갱신한다.

        재청킹 등으로 대상 청크가 사라졌다면 일부만 반영하지 않도록 예외를
        발생시킨다. 트랜잭션 확정과 rollback은 호출한 Service가 담당한다.
        """

        if not embedding_updates:
            return []

        target_chunk_ids = [
            embedding_update.chunk_id
            for embedding_update in embedding_updates
        ]

        # 같은 청크를 한 요청에서 두 번 갱신하는 잘못된 입력을 미리 차단한다.
        if len(set(target_chunk_ids)) != len(target_chunk_ids):
            raise ValueError("Duplicate chunk IDs exist in embedding updates.")

        # 갱신 대상을 한 번에 조회하고, 청크 ID로 바로 찾을 수 있게 구성한다.
        statement = select(EpisodeChunk).where(EpisodeChunk.id.in_(target_chunk_ids))
        chunks_by_id = {
            chunk.id: chunk
            for chunk in self.session.scalars(statement).all()
        }

        # 모든 대상이 존재하는지 먼저 확인해 일부 청크만 변경되는 것을 막는다.
        missing_chunk_ids = [
            chunk_id
            for chunk_id in target_chunk_ids
            if chunk_id not in chunks_by_id
        ]
        if missing_chunk_ids:
            # 누락된 청크를 로그와 오류 메시지에서 바로 확인할 수 있도록 ID 목록을 남긴다.
            missing_ids = ", ".join(str(chunk_id) for chunk_id in missing_chunk_ids)
            raise ValueError(f"Embedding update targets do not exist: {missing_ids}")

        # 검증을 마친 요청 순서대로 ORM 객체의 임베딩 필드만 변경한다.
        # Session이 변경을 추적하므로 호출한 Service가 commit할 때 UPDATE가 실행된다.
        updated_chunks: list[EpisodeChunk] = []
        for embedding_update in embedding_updates:
            chunk = chunks_by_id[embedding_update.chunk_id]
            chunk.embedding = embedding_update.embedding
            chunk.embedding_model = embedding_update.embedding_model
            chunk.embedding_version = embedding_update.embedding_version
            chunk.embedded_at = embedding_update.embedded_at
            updated_chunks.append(chunk)

        return updated_chunks

    def delete_by_episode_id(self, episode_id: UUID) -> None:
        # 재분석 또는 재청킹 시 같은 회차의 이전 청킹 결과를 정리한다.
        statement = delete(EpisodeChunk).where(EpisodeChunk.episode_id == episode_id)
        self.session.execute(statement)
