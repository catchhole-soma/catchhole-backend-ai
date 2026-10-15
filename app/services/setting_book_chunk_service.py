import unicodedata
from hashlib import sha256
from uuid import UUID, uuid4

from app.analysis.exceptions import ComparisonValidationError
from app.chunking.chunk_splitter import split_into_chunks
from app.domain.enums import AnalysisSourceKind
from app.models.setting_book_chunk import SettingBookChunk
from app.repositories.setting_book_chunk_repository import SettingBookChunkRepository
from app.services.ordered_analysis_fence import fence_ordered_candidate_write


def setting_book_char_count(text: str) -> int:
    # Java Character.isWhitespace excludes NBSP, figure space and narrow NBSP.
    return sum(not (
        0x09 <= ord(char) <= 0x0D or 0x1C <= ord(char) <= 0x1F
        or (unicodedata.category(char) in {"Zs", "Zl", "Zp"}
            and char not in {"\u00a0", "\u2007", "\u202f"})
    ) for char in text)


class SettingBookChunkService:
    def __init__(self, session_factory, repository_factory=SettingBookChunkRepository):
        self.session_factory = session_factory
        self.repository_factory = repository_factory

    def replace_setting_book_chunks(self, analysis_job_id: UUID, setting_book_revision_id: UUID,
                                    raw_text: str, *, work_id: UUID, ordered_write_context):
        if (ordered_write_context.source_kind != AnalysisSourceKind.SETTING_BOOK
                or ordered_write_context.setting_book_revision_id != setting_book_revision_id):
            raise ComparisonValidationError("Setting-book chunks must match their frozen source.")
        _validate_source_text(raw_text, ordered_write_context)
        chunks = [SettingBookChunk(
            id=uuid4(), analysis_job_id=analysis_job_id, setting_book_revision_id=setting_book_revision_id,
            chunk_index=draft.chunk_index, chunk_text=draft.chunk_text,
            start_offset=draft.start_offset, end_offset=draft.end_offset,
            paragraph_start_index=draft.paragraph_start_index, paragraph_end_index=draft.paragraph_end_index,
        ) for draft in split_into_chunks(raw_text)]
        with self.session_factory() as session:
            try:
                fence_ordered_candidate_write(session, analysis_job_id=analysis_job_id, work_id=work_id,
                                             write_context=ordered_write_context, chunks_only=True)
                repository = self.repository_factory(session)
                repository.delete_by_analysis_job_id(analysis_job_id)
                saved = repository.save_all(chunks)
                session.commit()
            except Exception:
                session.rollback()
                raise
        return saved

    def get_setting_book_chunks(self, analysis_job_id: UUID, setting_book_revision_id: UUID,
                                *, work_id: UUID | None = None, ordered_write_context=None,
                                raw_text: str | None = None):
        with self.session_factory() as session:
            if ordered_write_context is not None:
                if (work_id is None or ordered_write_context.source_kind != AnalysisSourceKind.SETTING_BOOK
                        or ordered_write_context.setting_book_revision_id != setting_book_revision_id):
                    raise ComparisonValidationError("Setting-book chunk reload must match its frozen source.")
                _validate_source_text(raw_text, ordered_write_context)
                fence_ordered_candidate_write(session, analysis_job_id=analysis_job_id, work_id=work_id,
                                             write_context=ordered_write_context, read_only=True)
            chunks = self.repository_factory(session).find_by_source(analysis_job_id, setting_book_revision_id)
            if ordered_write_context is not None:
                _validate_reloaded_chunks(chunks, raw_text)
            return chunks


def _validate_source_text(raw_text, context) -> None:
    if (not isinstance(raw_text, str)
            or sha256(raw_text.encode("utf-8")).hexdigest() != context.context.source_hash
            or setting_book_char_count(raw_text) != context.char_count):
        raise ComparisonValidationError("Setting-book source text does not match its frozen manifest.")


def _validate_reloaded_chunks(chunks, raw_text) -> None:
    # The shared splitter keeps raw slices but omits boundary whitespace between
    # chunks. Recreate its drafts from the immutable source, never from joined chunks.
    drafts = split_into_chunks(raw_text)
    fields = ("chunk_index", "chunk_text", "start_offset", "end_offset",
              "paragraph_start_index", "paragraph_end_index")
    if not chunks or len(chunks) != len(drafts) or any(
        any(getattr(chunk, field) != getattr(draft, field) for field in fields)
        for chunk, draft in zip(chunks, drafts, strict=True)
    ):
        raise ComparisonValidationError("Stored book chunks do not match their frozen source slices.")
