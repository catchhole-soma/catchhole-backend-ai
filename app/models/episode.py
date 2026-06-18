from uuid import UUID

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class Episode(TimestampMixin, Base):
    __tablename__ = "episodes"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    source_file_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    episode_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(100))
    content_s3_key: Mapped[str] = mapped_column(String(500))
    content_s3_version: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    char_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    processing_status: Mapped[str] = mapped_column(String(30))
