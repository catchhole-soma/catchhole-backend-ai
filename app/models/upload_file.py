from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UploadFile(Base):
    __tablename__ = "upload_files"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    s3_key: Mapped[str] = mapped_column(String(500))
    file_size: Mapped[int] = mapped_column(Integer)
    detected_episode_start_no: Mapped[int | None] = mapped_column(Integer)
    detected_episode_end_no: Mapped[int | None] = mapped_column(Integer)
    detected_episode_count: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
