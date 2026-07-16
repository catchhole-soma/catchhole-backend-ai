from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class EpisodeChunk(TimestampMixin, Base):
    __tablename__ = "episode_chunks"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    episode_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    paragraph_start_index: Mapped[int] = mapped_column(Integer)
    paragraph_end_index: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(1536))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_version: Mapped[str | None] = mapped_column(String(50))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
