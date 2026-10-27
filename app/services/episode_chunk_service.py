from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.chunking.chunk_splitter import split_episode_into_chunks
from app.mappers.episode_chunk_mapper import EpisodeChunkMapper
from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import EpisodeChunkRepository
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext, fence_ordered_candidate_write


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
        ordered_write_context: OrderedCandidateWriteContext | None = None,
        analysis_job_id: UUID | None = None, work_id: UUID | None = None,
    ) -> list[EpisodeChunk]:
        # Episode.content_s3_key로 읽은 회차 원문을 변경하지 않고 나눠야
        # chunk/evidence offset을 같은 원문에 그대로 적용할 수 있다.
        drafts = split_episode_into_chunks(raw_text)
        # EpisodeChunkDraft를 실제 DB에 저장할 EpisodeChunk 엔티티로 변환
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
                if ordered_write_context is not None:
                    if analysis_job_id is None or work_id is None:
                        raise ValueError("Ordered chunk storage requires the claimed job and work.")
                    fence_ordered_candidate_write(
                        session, analysis_job_id=analysis_job_id, work_id=work_id,
                        write_context=ordered_write_context, chunks_only=True,
                    )
                existing = repository.find_by_episode_id(episode_id)
                # Confirmed narrative evidence retains these IDs. Reanalysis of the same
                # source and boundaries must not invalidate those references or embeddings.
                if len(existing) == len(chunks) and all(
                    _same_source_chunk(previous, current)
                    for previous, current in zip(existing, chunks, strict=True)
                ):
                    for chunk in existing:
                        chunk.metadata_json = metadata_json
                    saved_chunks = existing
                else:
                    repository.delete_by_episode_id(episode_id)
                    saved_chunks = repository.save_all(chunks)
                # 삭제 + 저장 작업을 하나의 트랜잭션으로 확정
                session.commit()
            except Exception:
                # 중간에 에러가 나면 삭제나 저장 작업을 모두 되돌림
                session.rollback()
                raise

        return saved_chunks

    def get_episode_chunks(self, episode_id: UUID) -> list[EpisodeChunk]:
        with self.session_factory() as session:
            return self.repository_factory(session).find_by_episode_id(episode_id)


def _same_source_chunk(previous: EpisodeChunk, current: EpisodeChunk) -> bool:
    return all(
        getattr(previous, field) == getattr(current, field)
        for field in (
            "episode_id", "chunk_index", "chunk_text", "start_offset", "end_offset",
            "paragraph_start_index", "paragraph_end_index",
        )
    )
