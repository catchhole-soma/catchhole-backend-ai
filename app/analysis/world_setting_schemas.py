from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.analysis.schemas import ExtractedEvidenceSpan
from app.domain.enums import (
    WorldSettingCategory,
    WorldSettingComparisonReviewReason,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
)

TrimmedName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
TrimmedValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ExtractedWorldSettingCandidate(BaseModel):
    category: WorldSettingCategory
    subject_name: TrimmedName
    scope_name: TrimmedName | None = None
    setting_name: TrimmedName
    extracted_value: TrimmedValue
    evidence_spans: list[ExtractedEvidenceSpan] = Field(min_length=1)
    confidence: Literal[0.65, 0.8, 0.95]


class WorldSettingExtractionResult(BaseModel):
    candidates: list[ExtractedWorldSettingCandidate] = Field(default_factory=list)


class WorldSettingSubjectSelection(BaseModel):
    selected_subject_refs: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_unique_refs(self) -> "WorldSettingSubjectSelection":
        if len(set(self.selected_subject_refs)) != len(self.selected_subject_refs):
            raise ValueError("selected_subject_refs must not contain duplicates.")
        return self


class WorldSettingComparisonDecision(BaseModel):
    consolidation_status: WorldSettingConsolidationStatus
    operation: WorldSettingOperation
    review_reason: WorldSettingComparisonReviewReason | None = None
    target_ref: str | None = None
    matched_scope_name: TrimmedName | None = None
    matched_property_name: TrimmedName | None = None
    proposed_scope_name: TrimmedName | None = None
    proposed_setting_name: TrimmedName
    proposed_value: TrimmedValue
    comparison_reason: TrimmedValue

    @model_validator(mode="after")
    def validate_operation_payload(self) -> "WorldSettingComparisonDecision":
        if self.matched_scope_name is not None and self.matched_property_name is None:
            raise ValueError("matched_scope_name requires matched_property_name.")
        if self.matched_property_name is not None and self.target_ref is None:
            raise ValueError("matched_property_name requires target_ref.")
        if (
            self.operation in {WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}
            and (self.target_ref is None or self.matched_property_name is None)
        ):
            raise ValueError("UPDATE and MERGE require target_ref and matched_property_name.")
        if self.operation == WorldSettingOperation.ADD and self.matched_property_name is not None:
            raise ValueError("ADD must not include matched_property_name.")
        if self.operation == WorldSettingOperation.REVIEW_REQUIRED:
            if self.review_reason == WorldSettingComparisonReviewReason.SCOPE_UNRESOLVED and (
                self.target_ref is None
                or self.matched_scope_name is None
                or self.matched_property_name is None
            ):
                raise ValueError(
                    "Scope REVIEW_REQUIRED requires SCOPE_UNRESOLVED and a scoped matched property."
                )
            if self.review_reason == WorldSettingComparisonReviewReason.BATCH_LIMIT_EXCEEDED and (
                self.matched_scope_name is not None or self.matched_property_name is not None
            ):
                raise ValueError("Batch-limit REVIEW_REQUIRED must not include a matched path.")
            if self.review_reason not in {
                WorldSettingComparisonReviewReason.SCOPE_UNRESOLVED,
                WorldSettingComparisonReviewReason.BATCH_LIMIT_EXCEEDED,
            }:
                raise ValueError("REVIEW_REQUIRED requires a supported review_reason.")
        elif self.review_reason is not None:
            raise ValueError("Only REVIEW_REQUIRED may include review_reason.")
        return self


class WorldSettingComparisonBatchDecision(WorldSettingComparisonDecision):
    source_candidate_refs: list[str] = Field(min_length=1, max_length=20)
    existing_root_property_names_to_move: list[TrimmedName] = Field(
        default_factory=list,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_unique_source_refs(self) -> "WorldSettingComparisonBatchDecision":
        if len(set(self.source_candidate_refs)) != len(self.source_candidate_refs):
            raise ValueError("source_candidate_refs must not contain duplicates.")
        if len(set(self.existing_root_property_names_to_move)) != len(
            self.existing_root_property_names_to_move
        ):
            raise ValueError("existing_root_property_names_to_move must not contain duplicates.")
        return self


class WorldSettingComparisonBatchResult(BaseModel):
    decisions: list[WorldSettingComparisonBatchDecision] = Field(
        min_length=1,
        max_length=20,
    )
