from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    CharacterFactComparisonOperation,
    CharacterFactComparisonStatus,
    CharacterFactTemporalScope,
    SettingCandidateKind,
    SettingCandidateMatchStatus,
    SettingCandidateReviewStatus,
    SettingEntityType,
    SettingValueType,
)
from app.models.base import Base
from app.models.mixins import TimestampMixin


class SettingCandidate(TimestampMixin, Base):
    __tablename__ = "setting_candidates"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True))
    episode_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    source_chunk_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    source_content_s3_key: Mapped[str | None] = mapped_column(String(512))
    analysis_job_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    candidate_kind: Mapped[SettingCandidateKind] = mapped_column(
        String(30),
        default=SettingCandidateKind.SETTING,
    )
    entity_type: Mapped[SettingEntityType] = mapped_column(String(30))
    entity_name: Mapped[str] = mapped_column(String(100))
    raw_entity_mention: Mapped[str | None] = mapped_column(String(100))
    matched_character_id: Mapped[UUID | None] = mapped_column(PostgresUUID(as_uuid=True))
    match_status: Mapped[SettingCandidateMatchStatus] = mapped_column(String(30))
    attribute_name: Mapped[str | None] = mapped_column(String(100))
    attribute_value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[SettingValueType | None] = mapped_column(String(30))
    value_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    evidence_spans: Mapped[list[dict] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    review_status: Mapped[SettingCandidateReviewStatus] = mapped_column(String(30))
    raw_ai_result_json: Mapped[dict | None] = mapped_column(JSONB)
    # Python은 최초 상태만 기록하고, claim 이후 비교 생명주기와 결과는 Spring이 소유한다.
    comparison_status: Mapped[CharacterFactComparisonStatus] = mapped_column(
        String(40),
        default=CharacterFactComparisonStatus.NOT_REQUIRED,
    )
    suggested_operation: Mapped[CharacterFactComparisonOperation | None] = mapped_column(
        String(30)
    )
    temporal_scope: Mapped[CharacterFactTemporalScope | None] = mapped_column(String(30))
    comparison_target_fact_type: Mapped[str | None] = mapped_column(String(30))
    comparison_target_fact_key: Mapped[str | None] = mapped_column(String(150))
    proposed_fact_value: Mapped[str | None] = mapped_column(Text)
    proposed_value_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    removed_snapshot_entries_json: Mapped[list[dict] | None] = mapped_column(JSONB)
    comparison_reason: Mapped[str | None] = mapped_column(Text)
    comparison_base_snapshot_version: Mapped[int | None] = mapped_column(BigInteger)
    comparison_context_hash: Mapped[str | None] = mapped_column(String(64))
    raw_comparison_json: Mapped[dict | None] = mapped_column(JSONB)
    compared_at: Mapped[datetime | None] = mapped_column(DateTime)
    comparison_error_message: Mapped[str | None] = mapped_column(Text)
