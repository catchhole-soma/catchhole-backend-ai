"""SQLAlchemy mappings for Flyway V76; production never creates this schema."""

from datetime import datetime
from uuid import UUID
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class RetrievalSource(Base):
    __tablename__ = "retrieval_sources"
    episode_id: Mapped[UUID] = mapped_column(PgUUID, primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PgUUID)
    source_key: Mapped[str] = mapped_column(String(512))
    source_version: Mapped[str | None] = mapped_column(String(100))
    source_hash: Mapped[str] = mapped_column(String(64))
    chunk_policy: Mapped[str] = mapped_column(String(100))
    chunk_count: Mapped[int] = mapped_column(Integer)
    prepared_at: Mapped[datetime] = mapped_column(DateTime)


class RetrievalChunk(Base):
    __tablename__ = "retrieval_chunks"
    id: Mapped[UUID] = mapped_column(PgUUID, primary_key=True)
    episode_id: Mapped[UUID] = mapped_column(PgUUID)
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    chunk_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(1536))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_version: Mapped[str | None] = mapped_column(String(50))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="MISSING")
    failure_code: Mapped[str | None] = mapped_column(String(40))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_operation_id: Mapped[UUID | None] = mapped_column(PgUUID)
