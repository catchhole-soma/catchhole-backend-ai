from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UploadBatch(Base):
    __tablename__ = "upload_batches"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    uploaded_by: Mapped[int] = mapped_column(Integer)
    upload_type: Mapped[str] = mapped_column(String(50))
    source_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30))
    file_count: Mapped[int] = mapped_column(Integer)
    episode_start_no: Mapped[int | None] = mapped_column(Integer)
    episode_end_no: Mapped[int | None] = mapped_column(Integer)
    episode_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
