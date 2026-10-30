"""Explicit, immutable input contract for ordered analysis; never inferred from files."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis.evidence_projection import located_evidence_quotes

from app.domain.enums import (
    AnalysisSourceKind,
    CharacterFactTemporalScope,
    SettingBookReferenceTimeKind,
)
from app.schemas.analysis_source import WorkerAnalysisSource, validate_reference_time


class AnalysisReferenceEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, alias="startOffset", ge=0)
    end_offset: int | None = Field(default=None, alias="endOffset", ge=0)


class AnalysisReferenceSelection(BaseModel):
    """Frozen routing hints; never model-visible identity or mutation targets."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    actual_subject_ids: tuple[UUID, ...] = Field(default=(), alias="actualSubjectIds")
    provisional_subject_keys: tuple[str, ...] = Field(default=(), alias="provisionalSubjectKeys")
    raw_subject_mention: str | None = Field(default=None, alias="rawSubjectMention")
    candidate_kind: str | None = Field(default=None, alias="candidateKind")
    fact_type: str | None = Field(default=None, alias="factType")
    canonical_fact_key: str | None = Field(default=None, alias="canonicalFactKey")
    value_json: dict | None = Field(default=None, alias="valueJson")
    world_category: str | None = Field(default=None, alias="worldCategory")


class WorkerAnalysisReference(BaseModel):
    """Unapplied earlier claims; never selectable snapshot/identity targets."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    selection: AnalysisReferenceSelection | None = None
    domain: Literal["characters", "worldSettings"]
    subject_name: str | None = Field(default=None, alias="subjectName")
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1)
    source: WorkerAnalysisSource | None = None
    reason: str = Field(min_length=1)
    setting_name: str | None = Field(default=None, alias="settingName")
    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    matched_scope_name: str | None = Field(default=None, alias="matchedScopeName", max_length=100)
    matched_property_name: str | None = Field(default=None, alias="matchedPropertyName", max_length=100)
    value: str | None = None
    evidence_spans: list[AnalysisReferenceEvidence] = Field(
        default_factory=list, alias="evidenceSpans"
    )

    @model_validator(mode="after")
    def validate_source(self):
        if self.source is not None and self.source.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if self.source_episode_no is not None:
                raise ValueError("Setting-book references cannot have an episode number.")
        elif self.source_episode_no is None:
            raise ValueError("Episode references require a source episode number.")
        return self

    def prompt_data(self) -> dict:
        return {
            "domain": self.domain,
            "subject_name": self.subject_name,
            "source_episode_no": self.source_episode_no,
            "reason": self.reason,
            "setting_name": self.setting_name,
            "value": self.value,
            "evidence": (located_evidence_quotes(self.evidence_spans) if self.domain == "characters"
                         else [span.quote for span in self.evidence_spans]),
            "confirmation_status": "UNRESOLVED",
            "applied_to_current_state": False,
            **({"source": self.source.prompt_data()} if self.source is not None else {}),
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
    reference_time_kind: SettingBookReferenceTimeKind | None = Field(default=None, alias="referenceTimeKind")
    reference_episode_no: int | None = Field(default=None, alias="referenceEpisodeNo", ge=1, strict=True)

    @model_validator(mode="after")
    def validate_reference(self):
        validate_reference_time(self.reference_time_kind, self.reference_episode_no)
        return self


class AnalysisCurrentSourceTime(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    source_kind: AnalysisSourceKind | None = Field(default=None, alias="sourceKind")
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1, strict=True)
    reference_time_kind: SettingBookReferenceTimeKind | None = Field(default=None, alias="referenceTimeKind")
    reference_episode_no: int | None = Field(default=None, alias="referenceEpisodeNo", ge=1, strict=True)
    effective_from_episode_no: int | None = Field(default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True)
    temporal_scope: CharacterFactTemporalScope = Field(alias="temporalScope")

    @model_validator(mode="after")
    def validate_source(self):
        validate_reference_time(self.reference_time_kind, self.reference_episode_no)
        if self.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if self.source_episode_no is not None or self.reference_time_kind is None:
                raise ValueError("Book current time requires its frozen reference and no source episode.")
        elif self.source_kind == AnalysisSourceKind.EPISODE:
            if self.source_episode_no is None or self.reference_time_kind is not None:
                raise ValueError("Episode current time requires its source episode and no book reference.")
        elif self.temporal_scope != CharacterFactTemporalScope.UNKNOWN:
            raise ValueError("Unknown current origin must retain unknown temporal scope.")
        return self

    def prompt_data(self):
        return self.model_dump(mode="json")


class AnalysisStateProvenance(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    confirmation_status: Literal["CONFIRMED", "PROVISIONAL"] = Field(alias="confirmationStatus")
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1)
    source_candidate_ids: list[UUID] = Field(default_factory=list, alias="sourceCandidateIds")
    review_source: Literal["HUMAN", "AUTOMATIC"] | None = Field(default=None, alias="reviewSource")
    source_kind: AnalysisSourceKind | None = Field(default=None, alias="sourceKind")
    source_setting_book_id: UUID | None = Field(default=None, alias="sourceSettingBookId")
    source_setting_book_name: str | None = Field(default=None, alias="sourceSettingBookName")
    source_setting_book_revision_id: UUID | None = Field(default=None, alias="sourceSettingBookRevisionId")
    source_setting_book_revision_no: int | None = Field(default=None, alias="sourceSettingBookRevisionNo", ge=1)
    source_hash: str | None = Field(default=None, alias="sourceHash", pattern=r"^[0-9a-f]{64}$")
    source_reference_time_kind: SettingBookReferenceTimeKind | None = Field(default=None, alias="sourceReferenceTimeKind")
    source_reference_episode_no: int | None = Field(default=None, alias="sourceReferenceEpisodeNo", ge=1, strict=True)
    current_value_protected: bool | None = Field(default=None, alias="currentValueProtected", strict=True)
    effective_from_episode_no: int | None = Field(default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True)
    current_source_times: list[AnalysisCurrentSourceTime] = Field(default_factory=list, alias="currentSourceTimes")

    @model_validator(mode="after")
    def validate_source(self):
        validate_reference_time(self.source_reference_time_kind, self.source_reference_episode_no)
        if self.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if self.source_episode_no is not None:
                raise ValueError("Setting-book provenance cannot have an episode number.")
            if (self.source_setting_book_id is None or self.source_setting_book_revision_id is None
                    or self.source_setting_book_revision_no is None):
                raise ValueError("Setting-book provenance requires immutable revision identity.")
        return self

    def prompt_data(self) -> dict:
        # Internal candidate identifiers are transport provenance, never model input.
        return {
            "confirmation_status": self.confirmation_status,
            "source_episode_no": self.source_episode_no,
            **({"source_kind": self.source_kind} if self.source_kind is not None else {}),
            **({"source_setting_book_name": self.source_setting_book_name,
                "source_setting_book_revision_no": self.source_setting_book_revision_no}
               if self.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
            **({"source_reference_time_kind": self.source_reference_time_kind or SettingBookReferenceTimeKind.UNSPECIFIED,
                "source_reference_episode_no": self.source_reference_episode_no}
               if self.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
            **({"current_value_protected": self.current_value_protected} if self.current_value_protected is not None else {}),
            **({"effective_from_episode_no": self.effective_from_episode_no} if self.effective_from_episode_no is not None else {}),
            **({"current_source_times": [item.prompt_data() for item in self.current_source_times]} if self.current_source_times else {}),
            **({"review_source": self.review_source} if self.review_source is not None else {}),
        }

    @classmethod
    def from_source(cls, *, confirmation_status, source_episode_no=None, source=None, effective_from_episode_no=None):
        return cls(
            confirmation_status=confirmation_status, source_episode_no=source_episode_no,
            effective_from_episode_no=effective_from_episode_no,
            **({"source_kind": source.source_kind,
                "source_setting_book_id": source.setting_book_id,
                "source_setting_book_name": source.setting_book_name,
                "source_setting_book_revision_id": source.revision_id,
                "source_setting_book_revision_no": source.revision_no,
                "source_reference_time_kind": (source.reference_time_kind if source.source_kind == AnalysisSourceKind.SETTING_BOOK else None),
                "source_reference_episode_no": source.reference_episode_no,
                "source_hash": source.content_hash} if source is not None else {}),
        )

    def with_merged_sources(self, previous):
        """MERGE retains all contributing CURRENT origins; UPDATE replaces them."""
        return self.model_copy(update={"current_source_times": [
            *_represented_source_times(previous), *_represented_source_times(self),
        ]})


def _represented_source_times(provenance):
    if provenance is not None and provenance.current_source_times:
        return list(provenance.current_source_times)
    kind = provenance.source_kind if provenance is not None else None
    if kind is None and provenance is not None and provenance.source_episode_no is not None:
        kind = AnalysisSourceKind.EPISODE
    return [AnalysisCurrentSourceTime(
        source_kind=kind,
        source_episode_no=(provenance.source_episode_no if kind == AnalysisSourceKind.EPISODE else None),
        reference_time_kind=((provenance.source_reference_time_kind or SettingBookReferenceTimeKind.UNSPECIFIED)
                             if kind == AnalysisSourceKind.SETTING_BOOK else None),
        reference_episode_no=(provenance.source_reference_episode_no if kind == AnalysisSourceKind.SETTING_BOOK else None),
        effective_from_episode_no=(provenance.effective_from_episode_no if provenance is not None else None),
        temporal_scope=(CharacterFactTemporalScope.PRESENT if kind is not None else CharacterFactTemporalScope.UNKNOWN),
    )]
