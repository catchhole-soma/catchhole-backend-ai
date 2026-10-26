"""GH236 읽기 전용 계약. 회원·작품 ID는 모델 도구 인자가 아니다."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class SettingTool(StrEnum):
    FIND_CHARACTERS = "FIND_CHARACTERS"
    CHARACTER_CURRENT = "CHARACTER_CURRENT"
    CHARACTER_HISTORY = "CHARACTER_HISTORY"
    FIND_WORLD_SETTINGS = "FIND_WORLD_SETTINGS"
    WORLD_CURRENT = "WORLD_CURRENT"
    WORLD_HISTORY = "WORLD_HISTORY"
    CHARACTER_EVIDENCE = "CHARACTER_EVIDENCE"
    WORLD_EVIDENCE = "WORLD_EVIDENCE"
    CHARACTER_FIELDS = "CHARACTER_FIELDS"
    WORLD_FIELDS = "WORLD_FIELDS"
    CHARACTER_CURRENT_COLLECTION = "CHARACTER_CURRENT_COLLECTION"
    WORLD_CURRENT_COLLECTION = "WORLD_CURRENT_COLLECTION"


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )


class SettingToolQuery(ContractModel):
    tool: SettingTool
    target_id: UUID | None = None
    query: str | None = Field(default=None, max_length=100)
    setting_key: str | None = Field(default=None, max_length=150)
    scope_name: str | None = Field(default=None, max_length=100)
    episode_from: int | None = Field(default=None, ge=1, strict=True)
    episode_to: int | None = Field(default=None, ge=1, strict=True)
    page: int = Field(default=0, ge=0, le=10000, strict=True)
    size: int = Field(default=20, ge=1, le=50, strict=True)
    fact_type: (
        Literal["PROFILE", "AGE", "LEVEL", "STAT", "SKILL", "ITEM", "STATUS", "TIME"] | None
    ) = None
    value_equals: str | None = Field(default=None, min_length=1, max_length=200)
    world_category: (
        Literal[
            "RACE",
            "FACTION",
            "LOCATION",
            "MONSTER",
            "POWER_SYSTEM",
            "WORLD_RULE_HISTORY",
            "IMPORTANT_ITEM",
        ]
        | None
    ) = None
    read_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        find = self.tool in {SettingTool.FIND_CHARACTERS, SettingTool.FIND_WORLD_SETTINGS}
        current = self.tool in {SettingTool.CHARACTER_CURRENT, SettingTool.WORLD_CURRENT}
        evidence = self.tool in {SettingTool.CHARACTER_EVIDENCE, SettingTool.WORLD_EVIDENCE}
        world_path = self.tool in {
            SettingTool.WORLD_CURRENT,
            SettingTool.WORLD_HISTORY,
            SettingTool.WORLD_CURRENT_COLLECTION,
        }
        fields = self.tool in {SettingTool.CHARACTER_FIELDS, SettingTool.WORLD_FIELDS}
        work_fields = fields and self.target_id is None
        collection = self.tool in {
            SettingTool.CHARACTER_CURRENT_COLLECTION,
            SettingTool.WORLD_CURRENT_COLLECTION,
        }
        matching = self.tool == SettingTool.FIND_CHARACTERS and self.value_equals is not None
        if (
            (not work_fields and not collection and find != (self.target_id is None))
            or (collection and (self.target_id is not None or self.query is not None))
            or (not find and self.query is not None)
            or ((find and not matching or evidence or fields) and self.setting_key is not None)
            or (self.scope_name is not None and (not world_path or self.setting_key is None))
            or (
                (current or find or work_fields or collection)
                and (self.episode_from is not None or self.episode_to is not None)
            )
            or (
                self.episode_from is not None
                and self.episode_to is not None
                and self.episode_from > self.episode_to
            )
            or (evidence and self.page != 0)
            or (self.setting_key is not None and not self.setting_key.strip())
            or (self.scope_name is not None and not self.scope_name.strip())
            or (self.value_equals is not None and (not matching or not self.value_equals.strip()))
            or (matching and (self.fact_type is None or self.setting_key is None))
            or (
                self.fact_type is not None
                and self.tool
                not in {
                    SettingTool.CHARACTER_CURRENT,
                    SettingTool.CHARACTER_HISTORY,
                    SettingTool.CHARACTER_FIELDS,
                    SettingTool.CHARACTER_CURRENT_COLLECTION,
                }
                and not matching
            )
            or (self.read_token is not None and not collection)
            or (collection and self.page > 0 and self.read_token is None)
            or (
                self.world_category is not None
                and not (
                    self.tool
                    in {SettingTool.FIND_WORLD_SETTINGS, SettingTool.WORLD_CURRENT_COLLECTION}
                    or self.tool == SettingTool.WORLD_FIELDS
                    and self.target_id is None
                )
            )
        ):
            raise ValueError("설정 조회 도구의 인자 조합이 올바르지 않습니다.")
        return self


class SourceReference(ContractModel):
    tool: Literal[SettingTool.CHARACTER_EVIDENCE, SettingTool.WORLD_EVIDENCE]
    target_id: UUID


class SettingSourceProvenance(ContractModel):
    reference: SourceReference
    source_kind: Literal["EPISODE", "SETTING_BOOK", "USER_INPUT", "UNKNOWN"]
    content_edited: bool | None = Field(strict=True)
    application_action: Literal["AUTOMATIC_CONFIRM", "USER_CONFIRM", "UNKNOWN"]
    recorded_at: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?$")

    @model_validator(mode="after")
    def valid_timestamp(self) -> Self:
        if self.recorded_at is not None:
            datetime.fromisoformat(self.recorded_at)
        return self


class SettingProvenance(ContractModel):
    sources: list[SettingSourceProvenance] = Field(max_length=20)
    sources_truncated: bool = Field(strict=True)


class EvidenceQuote(ContractModel):
    text: str = Field(max_length=1000)
    start_offset: int | None = Field(ge=0)
    end_offset: int | None = Field(ge=0)


class DirectEvidence(ContractModel):
    status: Literal[
        "AVAILABLE",
        "LEGACY_UNVERIFIED",
        "NO_SOURCE",
        "SOURCE_MISSING",
        "SOURCE_PURGED",
        "SOURCE_CHANGED",
        "STORAGE_UNAVAILABLE",
        "OUT_OF_RANGE",
        "INVALID_QUOTE",
    ]
    candidate_id: UUID | None
    episode_id: UUID | None
    source_episode_no: int | None
    source_chunk_id: UUID | None
    source_hash: str | None
    source_version: str | None
    matches_current_source: bool | None
    quotes: list[EvidenceQuote] = Field(max_length=5)
    truncated: bool


class SettingToolItem(ContractModel):
    kind: Literal["CHARACTER", "WORLD_SETTING", "CURRENT", "HISTORY", "EVIDENCE", "FIELD"]
    id: UUID | None
    subject_id: UUID
    subject_name: str
    category: str | None
    scope_name: str | None
    setting_key: str | None
    display_name: str | None
    value: str | None = Field(max_length=2000)
    structured_value_json: str | None = Field(max_length=2000)
    value_truncated: bool
    contributes_to_current: bool | None
    operation: str | None
    history_only: bool | None
    setting_version: int | None
    effective_from_episode_no: int | None
    source_episode_id: UUID | None
    source_episode_no: int | None
    origin: Literal["CONFIRMED_ANALYSIS", "USER_INPUT", "UNKNOWN"]
    sources: list[SourceReference] = Field(max_length=20)
    sources_truncated: bool
    evidence: DirectEvidence | None
    provenance: SettingProvenance | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def coherent_provenance(self) -> Self:
        if self.provenance is not None and (
            [source.reference for source in self.provenance.sources] != self.sources
            or self.provenance.sources_truncated != self.sources_truncated
        ):
            raise ValueError("Provenance must match the observed source references")
        return self


class SettingToolPage(ContractModel):
    content: list[SettingToolItem] = Field(max_length=50)
    page: int = Field(ge=0)
    size: int = Field(ge=1, le=50)
    total_elements: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    has_next: bool


class SettingToolResponse(ContractModel):
    schema_version: Literal[1]
    tool: SettingTool
    items: SettingToolPage
    partial: bool
    limitations: list[str]
    collection: "SettingCollection | None" = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SettingCollection(ContractModel):
    population: Literal["ACTIVE_REGISTERED_CHARACTERS", "REGISTERED_WORLD_SETTINGS"]
    subject_count: int = Field(ge=0)
    subjects_with_values: int = Field(ge=0)
    record_count: int = Field(ge=0)
    read_token: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            self.subjects_with_values > self.subject_count
            or self.subjects_with_values > self.record_count
        ):
            raise ValueError("Invalid collection coverage")
        return self


SettingToolResponse.model_rebuild()
