from uuid import UUID

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class SettingBookRevision(TimestampMixin, Base):
    __tablename__ = "setting_book_revisions"
    __table_args__ = (UniqueConstraint("setting_book_id", "revision_no"),)

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    setting_book_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    revision_no: Mapped[int] = mapped_column(Integer)
    content_s3_key: Mapped[str] = mapped_column(String(512))
    content_s3_version: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    char_count: Mapped[int] = mapped_column(Integer)
    reference_time_kind: Mapped[str] = mapped_column(String(20), default="UNSPECIFIED", server_default="UNSPECIFIED")
    reference_episode_no: Mapped[int | None] = mapped_column(Integer)
