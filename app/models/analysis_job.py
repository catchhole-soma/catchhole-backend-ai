from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AnalysisJobStatus, AnalysisJobType, AnalysisReviewMode
from app.models.base import Base
from app.models.mixins import TimestampMixin


class AnalysisJob(TimestampMixin, Base):
    __tablename__ = "analysis_jobs"

    #예시 : id 컬럼, 파이썬 타입: UUID, DB 타입: PostgreSQL UUID, primary key
    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    batch_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    episode_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    job_type: Mapped[AnalysisJobType] = mapped_column(String(40))
    status: Mapped[AnalysisJobStatus] = mapped_column(String(20))
    current_step: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(100))
    input_token_count: Mapped[int | None] = mapped_column(Integer)
    output_token_count: Mapped[int | None] = mapped_column(Integer)
    summary_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    lease_token: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    checkpoint_stage: Mapped[str | None] = mapped_column(String(50))
    analysis_mode: Mapped[str] = mapped_column(String(30), default="CONFIRMED_ONLY")
    review_mode: Mapped[AnalysisReviewMode] = mapped_column(String(20), default=AnalysisReviewMode.MANUAL)
    automatic_input_state: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    automatic_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    analysis_run_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    run_generation: Mapped[int | None] = mapped_column(BigInteger)
    run_sequence: Mapped[int | None] = mapped_column(Integer)
    predecessor_job_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    run_base_state: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    input_state_hash: Mapped[str | None] = mapped_column(String(64))
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    source_episode_no: Mapped[int | None] = mapped_column(Integer)
    source_content_s3_key: Mapped[str | None] = mapped_column(Text)
    source_content_s3_version: Mapped[str | None] = mapped_column(String(255))
    state_journal: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    journal_status: Mapped[str | None] = mapped_column(String(30))
    journal_invalidation_reason: Mapped[str | None] = mapped_column(String(500))
