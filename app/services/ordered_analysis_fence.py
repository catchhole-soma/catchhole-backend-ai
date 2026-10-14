"""Fence shared-DB candidate replacement inside its write transaction.

Both rows stay locked only during candidate replacement, never during model calls.
The clock comes from PostgreSQL in the same timezone as its timestamp columns.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.analysis.exceptions import ComparisonValidationError
from app.domain.enums import (
    AnalysisJobStatus,
    AnalysisMode,
    AnalysisSourceKind,
    SettingBookReferenceTimeKind,
)
from app.models.analysis_job import AnalysisJob
from app.models.episode import Episode
from app.models.setting_book_revision import SettingBookRevision
from app.models.upload_batch import UploadBatch
from app.models.upload_file import UploadFile
from app.models.work import Work
from app.schemas.analysis_context import WorkerAnalysisContext
from app.schemas.analysis_source import validate_reference_time


@dataclass(frozen=True)
class OrderedCandidateWriteContext:
    lease_token: UUID
    context: WorkerAnalysisContext
    episode_id: UUID | None
    content_s3_key: str
    content_s3_version: str | None
    episode_no: int | None
    source_kind: AnalysisSourceKind = AnalysisSourceKind.EPISODE
    setting_book_id: UUID | None = None
    setting_book_revision_id: UUID | None = None
    char_count: int | None = None
    reference_time_kind: SettingBookReferenceTimeKind = SettingBookReferenceTimeKind.UNSPECIFIED
    reference_episode_no: int | None = None

    def __post_init__(self):
        if self.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if (self.episode_id is not None or self.episode_no is not None
                    or self.setting_book_id is None or self.setting_book_revision_id is None
                    or self.char_count is None or self.char_count < 0):
                raise ComparisonValidationError("Ordered setting-book write requires its frozen revision.")
            if self.reference_episode_no is not None and (type(self.reference_episode_no) is not int or self.reference_episode_no < 1):
                raise ComparisonValidationError("Ordered setting-book reference time is invalid.")
            try:
                validate_reference_time(self.reference_time_kind, self.reference_episode_no)
            except ValueError as error:
                raise ComparisonValidationError("Ordered setting-book reference time is invalid.") from error
            if (self.reference_time_kind != (self.context.reference_time_kind or SettingBookReferenceTimeKind.UNSPECIFIED)
                    or self.reference_episode_no != self.context.reference_episode_no):
                raise ComparisonValidationError("Ordered setting-book reference time is stale.")
        elif self.source_kind != AnalysisSourceKind.EPISODE or self.episode_id is None or self.episode_no is None:
            raise ComparisonValidationError("Ordered episode write requires its episode identity.")


def fence_ordered_candidate_write(
    session: Session,
    *,
    analysis_job_id: UUID,
    work_id: UUID,
    write_context: OrderedCandidateWriteContext,
    chunks_only: bool = False,
    read_only: bool = False,
) -> None:
    ctx = write_context.context
    if write_context.source_kind == AnalysisSourceKind.SETTING_BOOK:
        _fence_book_write(session, analysis_job_id, work_id, write_context, chunks_only, read_only)
        return
    statement = (
        select(AnalysisJob.id)
        .join(Episode, Episode.id == AnalysisJob.episode_id)
        .where(
            AnalysisJob.id == analysis_job_id,
            AnalysisJob.work_id == work_id,
            Episode.work_id == work_id,
            Episode.id == write_context.episode_id,
            AnalysisJob.status == AnalysisJobStatus.RUNNING,
            AnalysisJob.lease_token == write_context.lease_token,
            AnalysisJob.lease_expires_at > func.localtimestamp(),
            AnalysisJob.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL,
            AnalysisJob.source_kind == AnalysisSourceKind.EPISODE,
            AnalysisJob.analysis_run_id == ctx.run_id,
            AnalysisJob.run_generation == ctx.generation,
            AnalysisJob.input_state_hash == ctx.input_state_hash,
            AnalysisJob.journal_status == "PENDING",
            (
                AnalysisJob.checkpoint_stage.is_(None)
                if chunks_only
                else or_(
                    AnalysisJob.checkpoint_stage.is_(None),
                    AnalysisJob.checkpoint_stage == "CHUNKS_READY",
                )
            ),
            AnalysisJob.source_content_hash == ctx.source_hash,
            Episode.content_hash == ctx.source_hash,
            AnalysisJob.source_episode_no == write_context.episode_no,
            Episode.episode_no == write_context.episode_no,
            AnalysisJob.source_content_s3_key == write_context.content_s3_key,
            Episode.content_s3_key == write_context.content_s3_key,
            AnalysisJob.source_content_s3_version == write_context.content_s3_version,
            Episode.content_s3_version == write_context.content_s3_version,
        )
        .with_for_update(of=[AnalysisJob, Episode])
    )
    if session.execute(statement).scalar_one_or_none() is None:
        raise ComparisonValidationError("Ordered analysis candidate write is stale.")


def _fence_book_write(session, analysis_job_id, work_id, write_context, chunks_only, read_only=False):
    ctx = write_context.context
    # Backend source edits/archive lock Work first. Use the same lock order.
    work = select(Work.id).where(Work.id == work_id, Work.lifecycle_status == "ACTIVE").with_for_update()
    if session.execute(work).scalar_one_or_none() is None:
        raise ComparisonValidationError("Ordered analysis setting-book write is stale.")
    statement = (
        select(AnalysisJob.id)
        .join(SettingBookRevision, SettingBookRevision.id == AnalysisJob.source_setting_book_revision_id)
        .join(UploadFile, UploadFile.id == AnalysisJob.source_setting_book_id)
        .join(UploadBatch, UploadBatch.id == UploadFile.batch_id)
        .where(
            AnalysisJob.id == analysis_job_id, AnalysisJob.work_id == work_id,
            AnalysisJob.batch_id == UploadFile.batch_id,
            AnalysisJob.job_type == "SETTING_EXTRACTION",
            UploadBatch.work_id == work_id, UploadFile.file_role == "SETTING_BOOK",
            UploadFile.archived_at.is_(None), UploadFile.id == write_context.setting_book_id,
            UploadFile.current_setting_book_revision_id == write_context.setting_book_revision_id,
            SettingBookRevision.id == write_context.setting_book_revision_id,
            SettingBookRevision.setting_book_id == UploadFile.id,
            AnalysisJob.source_kind == AnalysisSourceKind.SETTING_BOOK,
            AnalysisJob.episode_id.is_(None), AnalysisJob.source_episode_no.is_(None),
            AnalysisJob.run_sequence == 0, AnalysisJob.predecessor_job_id.is_(None),
            AnalysisJob.status == AnalysisJobStatus.RUNNING,
            AnalysisJob.lease_token == write_context.lease_token,
            AnalysisJob.lease_expires_at > func.localtimestamp(),
            AnalysisJob.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL,
            AnalysisJob.review_mode == "AUTOMATIC",
            AnalysisJob.analysis_run_id == ctx.run_id, AnalysisJob.run_generation == ctx.generation,
            AnalysisJob.input_state_hash == ctx.input_state_hash, AnalysisJob.journal_status == "PENDING",
            (True if read_only else AnalysisJob.checkpoint_stage.is_(None) if chunks_only else or_(
                AnalysisJob.checkpoint_stage.is_(None), AnalysisJob.checkpoint_stage == "CHUNKS_READY")),
            AnalysisJob.source_content_hash == ctx.source_hash, SettingBookRevision.content_hash == ctx.source_hash,
            AnalysisJob.source_content_s3_key == write_context.content_s3_key,
            SettingBookRevision.content_s3_key == write_context.content_s3_key,
            AnalysisJob.source_content_s3_version == write_context.content_s3_version,
            SettingBookRevision.content_s3_version == write_context.content_s3_version,
            SettingBookRevision.char_count == write_context.char_count,
            SettingBookRevision.reference_time_kind == write_context.reference_time_kind,
            SettingBookRevision.reference_episode_no == write_context.reference_episode_no,
        ).with_for_update(of=[AnalysisJob, UploadFile])
    )
    if session.execute(statement).scalar_one_or_none() is None:
        raise ComparisonValidationError("Ordered analysis setting-book write is stale.")
