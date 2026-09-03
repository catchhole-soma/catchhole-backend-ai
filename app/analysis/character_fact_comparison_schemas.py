from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.domain.enums import (
    CharacterFactComparisonOperation,
    CharacterFactTemporalScope,
)

TrimmedReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TrimmedFactValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CharacterFactComparisonDecision(BaseModel):
    operation: CharacterFactComparisonOperation
    target_ref: str | None = None
    removed_snapshot_refs: list[str] = Field(default_factory=list, max_length=30)
    proposed_fact_value: TrimmedFactValue | None = None
    proposed_value_json: dict[str, Any] | None = None
    temporal_scope: CharacterFactTemporalScope
    comparison_reason: TrimmedReason

    @model_validator(mode="before")
    @classmethod
    def discard_irrelevant_proposals(cls, payload: Any) -> Any:
        """snapshot을 바꾸지 않는 판단에 딸려 온 제안값은 결정적으로 버린다.

        provider가 EXCLUDE 등의 operation을 올바르게 골랐지만 이전 출력 습관 때문에
        proposed 값을 함께 채우는 경우가 있다. 이 값들은 저장에도 사용되지 않으므로
        같은 요청을 반복 호출하지 않고 계약상 null로 정규화한다. target/removal처럼
        실제 의미를 바꾸는 잘못된 필드는 아래 엄격한 검증에 그대로 맡긴다.
        """

        if not isinstance(payload, dict):
            return payload
        if payload.get("operation") not in {
            CharacterFactComparisonOperation.HISTORY_ONLY,
            CharacterFactComparisonOperation.EXCLUDE,
            CharacterFactComparisonOperation.REVIEW_REQUIRED,
            "HISTORY_ONLY",
            "EXCLUDE",
            "REVIEW_REQUIRED",
        }:
            return payload
        normalized = dict(payload)
        normalized["proposed_fact_value"] = None
        normalized["proposed_value_json"] = None
        return normalized

    @model_validator(mode="after")
    def validate_operation_payload(self) -> "CharacterFactComparisonDecision":
        if len(set(self.removed_snapshot_refs)) != len(self.removed_snapshot_refs):
            raise ValueError("removed_snapshot_refs must not contain duplicates.")

        target_required = self.operation in {
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
        }
        if target_required and self.target_ref is None:
            raise ValueError("UPDATE and MERGE require target_ref.")
        if not target_required and self.target_ref is not None:
            raise ValueError("Only UPDATE and MERGE may include target_ref.")

        if (
            self.operation == CharacterFactComparisonOperation.REMOVE
            and not self.removed_snapshot_refs
        ):
            raise ValueError("REMOVE requires at least one removed_snapshot_ref.")

        changes_snapshot = self.operation in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
        }
        if changes_snapshot and (
            self.proposed_fact_value is None or self.proposed_value_json is None
        ):
            raise ValueError(
                "ADD, UPDATE, and MERGE require proposed_fact_value and proposed_value_json."
            )

        if self.temporal_scope in {
            CharacterFactTemporalScope.PAST,
            CharacterFactTemporalScope.HYPOTHETICAL,
        } and self.operation not in {
            CharacterFactComparisonOperation.HISTORY_ONLY,
            CharacterFactComparisonOperation.REVIEW_REQUIRED,
        }:
            raise ValueError(
                "PAST and HYPOTHETICAL candidates require HISTORY_ONLY or REVIEW_REQUIRED."
            )
        if (
            self.temporal_scope == CharacterFactTemporalScope.UNKNOWN
            and self.operation != CharacterFactComparisonOperation.REVIEW_REQUIRED
        ):
            raise ValueError("UNKNOWN temporal scope requires REVIEW_REQUIRED.")
        if self.temporal_scope != CharacterFactTemporalScope.PRESENT and self.removed_snapshot_refs:
            raise ValueError("Only PRESENT candidates may remove snapshot entries.")
        if self.operation in {
            CharacterFactComparisonOperation.HISTORY_ONLY,
            CharacterFactComparisonOperation.EXCLUDE,
            CharacterFactComparisonOperation.REVIEW_REQUIRED,
        }:
            if self.removed_snapshot_refs:
                raise ValueError(f"{self.operation} must not remove snapshot entries.")
        if self.operation in {
            CharacterFactComparisonOperation.HISTORY_ONLY,
            CharacterFactComparisonOperation.EXCLUDE,
            CharacterFactComparisonOperation.REVIEW_REQUIRED,
            CharacterFactComparisonOperation.REMOVE,
        }:
            if self.proposed_value_json is not None:
                raise ValueError(f"{self.operation} must not include proposed_value_json.")
            if self.proposed_fact_value is not None:
                raise ValueError(f"{self.operation} must not include proposed_fact_value.")
        return self


class CharacterFactComparisonBatchDecision(CharacterFactComparisonDecision):
    """Provider decision for exactly one source candidate in a character batch."""

    candidate_ref: str = Field(pattern=r"^C[1-9][0-9]*$")
    resolved_canonical_fact_key: str = Field(
        min_length=1,
        max_length=150,
    )


class CharacterFactComparisonBatchResult(BaseModel):
    decisions: list[CharacterFactComparisonBatchDecision] = Field(
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_unique_candidate_refs(self) -> "CharacterFactComparisonBatchResult":
        candidate_refs = [decision.candidate_ref for decision in self.decisions]
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("Each candidate_ref must appear exactly once in decisions.")
        return self
