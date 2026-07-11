from uuid import UUID

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class UploadFile(TimestampMixin, Base):
    __tablename__ = "upload_files"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    file_role: Mapped[str] = mapped_column(String(30))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    storage_url: Mapped[str | None] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(BigInteger)
    detected_episode_start_no: Mapped[int | None] = mapped_column(Integer)
    detected_episode_end_no: Mapped[int | None] = mapped_column(Integer)
    detected_episode_count: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(20))
