"""Explicit, immutable input contract for ordered analysis; never inferred from files."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AnalysisReferenceEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, alias="startOffset", ge=0)
    end_offset: int | None = Field(default=None, alias="endOffset", ge=0)


class WorkerAnalysisReference(BaseModel):
    """Unapplied earlier claims; never selectable snapshot/identity targets."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    domain: Literal["characters", "worldSettings"]
    subject_name: str | None = Field(default=None, alias="subjectName")
    source_episode_no: int = Field(alias="sourceEpisodeNo", ge=1)
    reason: str = Field(min_length=1)
    setting_name: str | None = Field(default=None, alias="settingName")
    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    matched_scope_name: str | None = Field(default=None, alias="matchedScopeName", max_length=100)
    matched_property_name: str | None = Field(default=None, alias="matchedPropertyName", max_length=100)
    value: str | None = None
    evidence_spans: list[AnalysisReferenceEvidence] = Field(
        default_factory=list, alias="evidenceSpans"
    )

    def prompt_data(self) -> dict:
        return {
            "domain": self.domain,
            "subject_name": self.subject_name,
            "source_episode_no": self.source_episode_no,
            "reason": self.reason,
            "setting_name": self.setting_name,
            "value": self.value,
            "evidence": [span.quote for span in self.evidence_spans],
            "confirmation_status": "UNRESOLVED",
            "applied_to_current_state": False,
            **{field: value for field in ("scope_name", "matched_scope_name", "matched_property_name")
               if (value := getattr(self, field)) is not None},
        }


class WorkerAnalysisContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    run_id: UUID = Field(alias="runId")
    generation: int = Field(ge=1)
    input_state_hash: str = Field(alias="inputStateHash", pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(alias="sourceHash", pattern=r"^[0-9a-f]{64}$")
    format_version: Literal[1] = Field(alias="formatVersion")
    unresolved_references: list[WorkerAnalysisReference] = Field(
        default_factory=list, alias="unresolvedReferences"
    )


class AnalysisStateProvenance(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    confirmation_status: Literal["CONFIRMED", "PROVISIONAL"] = Field(alias="confirmationStatus")
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1)
    source_candidate_ids: list[UUID] = Field(default_factory=list, alias="sourceCandidateIds")
    review_source: Literal["HUMAN", "AUTOMATIC"] | None = Field(default=None, alias="reviewSource")

    def prompt_data(self) -> dict:
        # Internal candidate identifiers are transport provenance, never model input.
        return {
            "confirmation_status": self.confirmation_status,
            "source_episode_no": self.source_episode_no,
            **({"review_source": self.review_source} if self.review_source is not None else {}),
        }
