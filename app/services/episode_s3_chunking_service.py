from uuid import UUID

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.services.episode_chunk_service import EpisodeChunkService
from app.storage.s3 import S3TextObjectStorage


class EpisodeS3ChunkingService:
    def __init__(
        self,
        storage: S3TextObjectStorage,
        chunk_service: EpisodeChunkService,
    ) -> None:
        self.storage = storage
        self.chunk_service = chunk_service

    def replace_chunks_from_s3_content(self, episode_id: UUID, content_s3_key: str):
        # 1. Spring claim payload에 포함된 content_s3_key를 사용한다.
        # Worker가 같은 episode를 다시 DB에서 조회하면 claim 시점 payload와 다른 값을 볼 수 있다.
        if not content_s3_key:
            raise AppException(
                ErrorCode.INVALID_REQUEST,
                detail={"episode_id": str(episode_id), "reason": "content_s3_key is missing"},
            )
            
        # 2. S3에서 원문 텍스트를 읽어옴
        raw_text = self.storage.get_text(content_s3_key)
        # 3. 읽어온 원문을 EpisodeChunkService에 넘겨 chunk 삭제 + 새 chunk 생성 + 저장을 수행
        return self.chunk_service.replace_episode_chunks(
            episode_id=episode_id,
            raw_text=raw_text,
        )
