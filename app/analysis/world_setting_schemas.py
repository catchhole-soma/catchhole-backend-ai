from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.analysis.schemas import ExtractedEvidenceSpan
from app.domain.enums import (
    WorldSettingCategory,
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
        return self
