from uuid import UUID

from sqlalchemy import Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class SettingBookChunk(TimestampMixin, Base):
    __tablename__ = "setting_book_chunks"
    __table_args__ = (UniqueConstraint("analysis_job_id", "chunk_index"),)

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    analysis_job_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    setting_book_revision_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    paragraph_start_index: Mapped[int] = mapped_column(Integer)
    paragraph_end_index: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
