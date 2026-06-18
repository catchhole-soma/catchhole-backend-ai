from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AnalysisJobStatus, AnalysisJobType
from app.models.base import Base
from app.models.mixins import TimestampMixin


class AnalysisJob(TimestampMixin, Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    batch_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    episode_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    job_type: Mapped[AnalysisJobType] = mapped_column(String(50))
    status: Mapped[AnalysisJobStatus] = mapped_column(String(30))
    current_step: Mapped[str | None] = mapped_column(String(50))
    model_name: Mapped[str | None] = mapped_column(String(100))
    input_token_count: Mapped[int | None] = mapped_column(Integer)
    output_token_count: Mapped[int | None] = mapped_column(Integer)
    summary_json: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
