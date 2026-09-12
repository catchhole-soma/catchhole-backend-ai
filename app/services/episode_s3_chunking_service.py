from uuid import UUID
from hashlib import sha256

from app.analysis.exceptions import ComparisonValidationError
from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.services.episode_chunk_service import EpisodeChunkService
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext
from app.storage.s3 import S3TextObjectStorage


class EpisodeS3ChunkingService:
    def __init__(
        self,
        storage: S3TextObjectStorage,
        chunk_service: EpisodeChunkService,
    ) -> None:
        self.storage = storage
        self.chunk_service = chunk_service

    def replace_chunks_from_s3_content(
        self, episode_id: UUID, content_s3_key: str,
        ordered_write_context: OrderedCandidateWriteContext | None = None,
        analysis_job_id: UUID | None = None, work_id: UUID | None = None,
    ):
        # 1. Spring claim payload에 포함된 content_s3_key를 사용한다.
        # Worker가 같은 episode를 다시 DB에서 조회하면 claim 시점 payload와 다른 값을 볼 수 있다.
        if not content_s3_key:
            raise AppException(
                ErrorCode.INVALID_REQUEST,
                detail={"episode_id": str(episode_id), "reason": "content_s3_key is missing"},
            )
            
        # 2. S3에서 원문 텍스트를 읽어옴
        raw_text = self.storage.get_text(content_s3_key,
            **({"version_id": ordered_write_context.content_s3_version}
               if ordered_write_context is not None else {}))
        if ordered_write_context is not None:
            if analysis_job_id is None or work_id is None:
                raise ValueError("Ordered chunk storage requires the claimed job and work.")
            if sha256(raw_text.encode("utf-8")).hexdigest() != ordered_write_context.context.source_hash:
                raise ComparisonValidationError("Ordered source content hash changed.")
        # 3. 읽어온 원문을 EpisodeChunkService에 넘겨 chunk 삭제 + 새 chunk 생성 + 저장을 수행
        return self.chunk_service.replace_episode_chunks(
            episode_id=episode_id,
            raw_text=raw_text,
            **({"ordered_write_context": ordered_write_context,
                "analysis_job_id": analysis_job_id, "work_id": work_id}
               if ordered_write_context is not None else {}),
        )
