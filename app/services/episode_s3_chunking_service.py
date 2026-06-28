from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.repositories.episode_repository import EpisodeRepository
from app.services.episode_chunk_service import EpisodeChunkService
from app.storage.s3 import S3TextObjectStorage


class EpisodeS3ChunkingService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage: S3TextObjectStorage,
        chunk_service: EpisodeChunkService,
        episode_repository_factory: Callable[[Session], EpisodeRepository] = EpisodeRepository,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.chunk_service = chunk_service
        self.episode_repository_factory = episode_repository_factory

    def replace_chunks_from_s3_content(self, episode_id: UUID):
        # 1. DB 세션을 열고 episode_id로 Episode를 조회
        with self.session_factory() as session:
            repository = self.episode_repository_factory(session)
            episode = repository.get_by_id_or_throw(episode_id)
            # 2. Episode에 저장된 S3 key를 꺼냄
            content_s3_key = episode.content_s3_key
        
        # 3. S3 key가 없으면 잘못된 요청으로 보고 예외
        if not content_s3_key:
            raise AppException(
                ErrorCode.INVALID_REQUEST,
                detail={"episode_id": str(episode_id), "reason": "content_s3_key is missing"},
            )
            
        # 4. S3에서 원문 텍스트를 읽어옴
        raw_text = self.storage.get_text(content_s3_key)
        # 5. 읽어온 원문을 EpisodeChunkService에 넘겨 chunk 삭제 + 새 chunk 생성 + 저장을 수행
        return self.chunk_service.replace_episode_chunks(
            episode_id=episode_id,
            raw_text=raw_text,
        )
