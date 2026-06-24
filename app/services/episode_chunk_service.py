from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.chunking.chunk_splitter import split_into_chunks
from app.chunking.text_normalizer import normalize_text
from app.mappers.episode_chunk_mapper import EpisodeChunkMapper
from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import EpisodeChunkRepository


class EpisodeChunkService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
         # repository_factory(session)을 호출하면 EpisodeChunkRepository(session)이 생성
        repository_factory: Callable[[Session], EpisodeChunkRepository] = EpisodeChunkRepository,
    ) -> None:
        self.session_factory = session_factory
        self.repository_factory = repository_factory

    def replace_episode_chunks(
        self,
        episode_id: UUID,
        raw_text: str,
        metadata_json: dict | None = None,
    ) -> list[EpisodeChunk]:
        # 1. 원문 텍스트의 줄바꿈, 특수 공백, 탭 등을 정리
        normalized_text = normalize_text(raw_text)
        # 2. 정규화된 원문을 LLM/RAG에 넣기 좋은 크기의 chunk draft들로 나누기
        drafts = split_into_chunks(normalized_text)
        # 3. EpisodeChunkDraft를 실제 DB에 저장할 EpisodeChunk 엔티티로 변환
        chunks = [
            EpisodeChunkMapper.to_entity(
                episode_id=episode_id,
                draft=draft,
                metadata_json=metadata_json,
            )
            for draft in drafts
        ]
        
        # 4. DB 세션을 열고, with 블록이 끝나면 session.close()가 자동으로 호출
        with self.session_factory() as session:
            repository = self.repository_factory(session)
            try:
                # 5. 해당 회차의 기존 chunk를 전부 삭제(다시 분석하는 상황 고려)
                repository.delete_by_episode_id(episode_id)
                # 6. 새로 생성한 chunk들을 저장 대상으로 세션에 등록
                saved_chunks = repository.save_all(chunks)
                # 7. 삭제 + 저장 작업을 하나의 트랜잭션으로 확정
                session.commit()
            except Exception:
                # 9. 중간에 에러가 나면 삭제나 저장 작업을 모두 되돌림
                session.rollback()
                raise # 10. 예외를 밖으로 던진다.

        return saved_chunks
