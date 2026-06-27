from decimal import Decimal
from uuid import UUID

from sqlalchemy import Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import SettingCandidateReviewStatus, SettingEntityType, SettingValueType
from app.models.base import Base
from app.models.mixins import TimestampMixin


class SettingCandidate(TimestampMixin, Base):
    __tablename__ = "setting_candidates"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    episode_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    source_chunk_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    analysis_job_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    entity_type: Mapped[SettingEntityType] = mapped_column(String(30))
    entity_name: Mapped[str] = mapped_column(String(100))
    attribute_name: Mapped[str] = mapped_column(String(100))
    attribute_value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[SettingValueType] = mapped_column(String(30))
    value_json: Mapped[dict | None] = mapped_column(JSONB)
    evidence_spans: Mapped[list[dict] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    review_status: Mapped[SettingCandidateReviewStatus] = mapped_column(String(30))
    raw_ai_result_json: Mapped[dict | None] = mapped_column(JSONB)
