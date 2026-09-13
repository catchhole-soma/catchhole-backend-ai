"""Fence shared-DB candidate replacement inside its write transaction.

Both rows stay locked only during candidate replacement, never during model calls.
The clock comes from PostgreSQL in the same timezone as its timestamp columns.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.analysis.exceptions import ComparisonValidationError
from app.domain.enums import AnalysisJobStatus, AnalysisMode
from app.models.analysis_job import AnalysisJob
from app.models.episode import Episode
from app.schemas.analysis_context import WorkerAnalysisContext


@dataclass(frozen=True)
class OrderedCandidateWriteContext:
    lease_token: UUID
    context: WorkerAnalysisContext
    episode_id: UUID
    content_s3_key: str
    content_s3_version: str | None
    episode_no: int


def fence_ordered_candidate_write(
    session: Session,
    *,
    analysis_job_id: UUID,
    work_id: UUID,
    write_context: OrderedCandidateWriteContext,
    chunks_only: bool = False,
) -> None:
    ctx = write_context.context
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
