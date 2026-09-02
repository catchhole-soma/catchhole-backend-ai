from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    WorldSettingCategory,
    WorldSettingComparisonReviewReason,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
    WorldSettingSubjectResolutionType,
)


class RuntimeEvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, alias="startOffset", ge=0)
    end_offset: int | None = Field(default=None, alias="endOffset", ge=0)


class RuntimeSourceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    raw_subject_name: str = Field(alias="rawSubjectName", min_length=1, max_length=100)
    raw_scope_name: str | None = Field(default=None, alias="rawScopeName", max_length=100)
    raw_setting_name: str = Field(alias="rawSettingName", min_length=1, max_length=100)
    extracted_value: str = Field(alias="extractedValue", min_length=1)
    evidence_spans: list[RuntimeEvidenceSpan] = Field(alias="evidenceSpans", min_length=1)


class RuntimeCanonicalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    decision_ref: str = Field(alias="decisionRef", pattern=r"^D[1-9][0-9]*$")
    source_candidates: list[RuntimeSourceCandidate] = Field(
        alias="sourceCandidates",
        min_length=1,
        max_length=20,
    )
    existing_root_property_names_to_move: list[str] = Field(
        default_factory=list,
        alias="existingRootPropertyNamesToMove",
        max_length=20,
    )
    canonical_subject_name: str = Field(
        alias="canonicalSubjectName",
        min_length=1,
        max_length=100,
    )
    target_world_setting_id: UUID | None = Field(
        default=None,
        alias="targetWorldSettingId",
    )
    matched_scope_name: str | None = Field(default=None, alias="matchedScopeName")
    matched_property_name: str | None = Field(default=None, alias="matchedPropertyName")
    consolidation_status: WorldSettingConsolidationStatus = Field(alias="consolidationStatus")
    operation: WorldSettingOperation
    review_reason: WorldSettingComparisonReviewReason | None = Field(
        default=None,
        alias="reviewReason",
    )
    proposed_scope_name: str | None = Field(default=None, alias="proposedScopeName")
    proposed_setting_name: str = Field(
        alias="proposedSettingName",
        min_length=1,
        max_length=100,
    )
    proposed_value: str = Field(alias="proposedValue", min_length=1)
    comparison_reason: str = Field(alias="comparisonReason", min_length=1)


class WorldSettingComparisonRuntimeResult(BaseModel):
    """#139 평가기가 운영 batch 결과를 읽는 정규화 DTO."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["world-setting-comparison-runtime-v1"] = Field(
        default="world-setting-comparison-runtime-v1",
        alias="schemaVersion",
    )
    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    work_id: UUID = Field(alias="workId")
    source_episode_id: UUID = Field(alias="sourceEpisodeId")
    category: WorldSettingCategory
    subject_resolution_type: WorldSettingSubjectResolutionType = Field(
        alias="subjectResolutionType"
    )
    canonical_subject_key: str = Field(alias="canonicalSubjectKey", min_length=1)
    canonical_subject_name: str = Field(
        alias="canonicalSubjectName",
        min_length=1,
        max_length=100,
    )
    canonical_target_world_setting_ids: list[UUID] = Field(
        alias="canonicalTargetWorldSettingIds",
        max_length=3,
    )
    decisions: list[RuntimeCanonicalDecision] = Field(min_length=1, max_length=20)
