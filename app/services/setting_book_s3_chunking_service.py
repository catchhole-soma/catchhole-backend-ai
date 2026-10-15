from app.analysis.exceptions import ComparisonValidationError
from app.domain.enums import AnalysisSourceKind
from app.services.setting_book_chunk_service import _validate_source_text


class SettingBookS3ChunkingService:
    def __init__(self, storage, chunk_service):
        self.storage = storage
        self.chunk_service = chunk_service

    def replace_chunks_from_s3_content(self, *, analysis_job_id, work_id,
                                      setting_book_revision_id, content_s3_key, ordered_write_context):
        raw_text = self._read_frozen_source(setting_book_revision_id, content_s3_key, ordered_write_context)
        return self.chunk_service.replace_setting_book_chunks(
            analysis_job_id, setting_book_revision_id, raw_text,
            work_id=work_id, ordered_write_context=ordered_write_context,
        )

    def get_chunks_from_s3_content(self, *, analysis_job_id, work_id,
                                  setting_book_revision_id, content_s3_key, ordered_write_context):
        raw_text = self._read_frozen_source(setting_book_revision_id, content_s3_key, ordered_write_context)
        return self.chunk_service.get_setting_book_chunks(
            analysis_job_id, setting_book_revision_id, raw_text=raw_text,
            work_id=work_id, ordered_write_context=ordered_write_context,
        )

    def read_frozen_source(self, *, setting_book_revision_id, content_s3_key, ordered_write_context):
        raw_text = self._read_frozen_source(setting_book_revision_id, content_s3_key, ordered_write_context)
        _validate_source_text(raw_text, ordered_write_context)
        return raw_text

    def _read_frozen_source(self, setting_book_revision_id, content_s3_key, ordered_write_context):
        if (ordered_write_context.source_kind != AnalysisSourceKind.SETTING_BOOK
                or setting_book_revision_id != ordered_write_context.setting_book_revision_id
                or content_s3_key != ordered_write_context.content_s3_key):
            raise ComparisonValidationError("Setting-book S3 input is outside its frozen source.")
        return self.storage.get_text(content_s3_key, version_id=ordered_write_context.content_s3_version)
