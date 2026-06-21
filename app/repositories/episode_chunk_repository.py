from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.episode_chunk import EpisodeChunk


class EpisodeChunkRepository:
    # EpisodeChunk DB 접근만 담당한다. 트랜잭션 commit은 Service/Worker 계층에서 처리한다.
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_episode_id(self, episode_id: UUID) -> list[EpisodeChunk]:
        # 회차 원문을 다시 구성하거나 분석할 때 chunk_index 순서로 조회한다.
        statement = (
            select(EpisodeChunk)
            .where(EpisodeChunk.episode_id == episode_id)
            .order_by(EpisodeChunk.chunk_index)
        )
        return list(self.session.scalars(statement).all())

    def save_all(self, chunks: list[EpisodeChunk]) -> list[EpisodeChunk]:
        # 청킹 결과를 한 번에 session에 등록한다.
        self.session.add_all(chunks)
        return chunks

    def delete_by_episode_id(self, episode_id: UUID) -> None:
        # force 재분석이나 재청킹 시 기존 청크를 회차 기준으로 정리한다.
        statement = delete(EpisodeChunk).where(EpisodeChunk.episode_id == episode_id)
        self.session.execute(statement)
