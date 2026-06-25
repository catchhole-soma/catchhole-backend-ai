from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AnalysisJobStatus, AnalysisJobType
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
