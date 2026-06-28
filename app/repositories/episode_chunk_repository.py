from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.episode_chunk import EpisodeChunk


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

    def delete_by_episode_id(self, episode_id: UUID) -> None:
        # 재분석 또는 재청킹 시 같은 회차의 이전 청킹 결과를 정리한다.
        statement = delete(EpisodeChunk).where(EpisodeChunk.episode_id == episode_id)
        self.session.execute(statement)
