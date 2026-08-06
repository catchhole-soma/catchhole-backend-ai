from collections import Counter
from enum import StrEnum
from typing import Any
import unicodedata

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import (
    SettingCandidateKind,
    SettingCandidateMatchStatus,
    SettingValueType,
)


class GoldDecision(StrEnum):
    EXTRACT = "EXTRACT"
    DO_NOT_EXTRACT = "DO_NOT_EXTRACT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class Importance(StrEnum):
    MUST = "MUST"
    SHOULD = "SHOULD"
    NICE = "NICE"

    @property
    def weight(self) -> int:
        return {
            Importance.MUST: 3,
            Importance.SHOULD: 2,
            Importance.NICE: 1,
        }[self]


class GoldCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Notion 정답표에는 없는 실행용 식별자이며 dataset 검증 시 행 내용으로 생성한다.
    gold_id: str = Field(default="", exclude=True, repr=False)
    decision: GoldDecision
    importance: Importance | None = None
    entity_name: str = Field(
        validation_alias=AliasChoices("entityName", "entity_name"),
        min_length=1,
    )
    fact_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("factKey", "fact_key"),
    )
    accepted_fact_key_aliases: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "factKeyAliases",
            "acceptedFactKeyAliases",
            "accepted_fact_key_aliases",
        ),
    )
    value_type: SettingValueType | None = Field(
        default=None,
        validation_alias=AliasChoices("valueType", "value_type"),
    )
    attribute_value: str | None = Field(
        default=None,
        validation_alias=AliasChoices("attributeValue", "attribute_value"),
    )
    value_json: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("valueJson", "value_json"),
    )
    evidence_quotes: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidenceQuotes", "evidence_quotes"),
    )
    note: str | None = None

    @model_validator(mode="after")
    def require_scoring_fields_for_extract(self) -> "GoldCandidate":
        if self.decision != GoldDecision.EXTRACT:
            return self
        missing_fields = []
        if self.importance is None:
            missing_fields.append("importance")
        if not self.fact_key or not self.fact_key.strip():
            missing_fields.append("factKey")
        if not self.value_type or not self.value_type.strip():
            missing_fields.append("valueType")
        if self.attribute_value is None or not self.attribute_value.strip():
            missing_fields.append("attributeValue")
        if self.value_json is None:
            missing_fields.append("valueJson")
        if not self.evidence_quotes or any(not quote.strip() for quote in self.evidence_quotes):
            missing_fields.append("evidenceQuotes")
        if missing_fields:
            raise ValueError("EXTRACT gold rows require: " + ", ".join(missing_fields))
        return self

    @property
    def accepted_fact_keys(self) -> tuple[str, ...]:
        # canonical key를 우선하되, 정답지에서 명시한 동치 key도 같은 사실로 허용한다.
        return tuple(
            key
            for key in (self.fact_key, *self.accepted_fact_key_aliases)
            if key is not None and key.strip()
        )

    @property
    def is_scorable_hard_negative(self) -> bool:
        """금지할 key가 명시된 DO_NOT_EXTRACT 행만 자동 오탐 판정에 사용한다."""

        return self.decision == GoldDecision.DO_NOT_EXTRACT and bool(self.accepted_fact_keys)


class GoldEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    episode_no: int = Field(
        validation_alias=AliasChoices("episodeNo", "episode_no"),
        ge=1,
    )
    title: str | None = None
    source_file: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sourceFile", "source_file"),
    )
    source_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sourceText", "source_text"),
    )
    candidates: list[GoldCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_single_source(self) -> "GoldEpisode":
        # 외부 원고와 인라인 fixture를 동시에 주면 어느 쪽이 기준인지 모호해진다.
        if self.source_file is not None and self.source_text is not None:
            raise ValueError("Use either sourceFile or sourceText, not both.")
        return self


class GoldDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    dataset_version: str = Field(
        validation_alias=AliasChoices("datasetVersion", "dataset_version"),
        min_length=1,
    )
    name: str = Field(min_length=1)
    episodes: list[GoldEpisode] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_episode_numbers_and_assign_gold_ids(self) -> "GoldDataset":
        episode_numbers = [episode.episode_no for episode in self.episodes]
        if len(episode_numbers) != len(set(episode_numbers)):
            raise ValueError("Gold dataset episodeNo values must be unique.")

        # 사람이 관리할 컬럼을 늘리지 않도록 회차·캐릭터·key와 중복 순번으로 ID를 만든다.
        for episode in self.episodes:
            occurrence_by_identity: Counter[str] = Counter()
            for candidate in episode.candidates:
                key = candidate.fact_key or candidate.decision.value
                identity = ":".join(
                    (
                        _normalize_id_part(candidate.entity_name),
                        _normalize_id_part(key),
                    )
                )
                occurrence_by_identity[identity] += 1
                candidate.gold_id = (
                    f"episode-{episode.episode_no}:{identity}:{occurrence_by_identity[identity]}"
                )
        return self


def _normalize_id_part(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold().replace(" ", "_")


class PredictionEvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(
        default=None,
        validation_alias=AliasChoices("startOffset", "start_offset"),
        ge=0,
    )
    end_offset: int | None = Field(
        default=None,
        validation_alias=AliasChoices("endOffset", "end_offset"),
        ge=0,
    )


class PredictionCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    entity_name: str = Field(
        validation_alias=AliasChoices("entityName", "entity_name"),
        min_length=1,
    )
    matched_character_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "matchedCharacterId",
            "matched_character_id",
        ),
    )
    match_status: SettingCandidateMatchStatus | None = Field(
        default=None,
        validation_alias=AliasChoices("matchStatus", "match_status"),
    )
    matched_character_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "matchedCharacterName",
            "matched_character_name",
        ),
    )
    attribute_name: str = Field(
        validation_alias=AliasChoices("attributeName", "attribute_name"),
        min_length=1,
    )
    attribute_value: str | None = Field(
        default=None,
        validation_alias=AliasChoices("attributeValue", "attribute_value"),
    )
    value_type: str = Field(
        validation_alias=AliasChoices("valueType", "value_type"),
        min_length=1,
    )
    value_json: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("valueJson", "value_json"),
    )
    evidence_spans: list[PredictionEvidenceSpan] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidenceSpans", "evidence_spans"),
    )
    # 원본 추출 key는 보고서에 보존하고, 스키마 snapshot으로 해소한 canonical key만
    # 평가 내부에서 사용한다. Notion 정답표에 운영 alias를 중복 기입하지 않기 위함이다.
    canonical_attribute_name: str | None = Field(default=None, exclude=True, repr=False)

    @property
    def evaluation_entity_name(self) -> str | None:
        """운영 이름 해소 결과를 반영해 평가할 캐릭터 대표 이름을 반환한다."""

        if self.match_status == SettingCandidateMatchStatus.AMBIGUOUS:
            return None
        if self.match_status == SettingCandidateMatchStatus.MATCHED and self.matched_character_name:
            return self.matched_character_name
        # 신규 캐릭터 후보(UNRESOLVED)와 과거 예측 파일은 추출 이름을 exact 비교한다.
        return self.entity_name

    @property
    def evaluation_fact_key(self) -> str:
        return self.canonical_attribute_name or self.attribute_name


class PredictionEpisode(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    episode_no: int = Field(
        validation_alias=AliasChoices("episodeNo", "episode_no"),
        ge=1,
    )
    candidates: list[PredictionCandidate] = Field(default_factory=list)
    character_discovery_excluded_count: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "characterDiscoveryExcluded",
            "character_discovery_excluded_count",
        ),
        ge=0,
    )

    @model_validator(mode="before")
    @classmethod
    def separate_character_discovery_candidates(cls, value: Any) -> Any:
        """설정 Fact가 없는 캐릭터 발견 후보를 설정 평가 입력에서 분리한다."""

        if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
            return value

        setting_candidates = []
        excluded_count = 0
        for candidate in value["candidates"]:
            if isinstance(candidate, dict):
                candidate_kind = candidate.get(
                    "candidateKind",
                    candidate.get("candidate_kind", SettingCandidateKind.SETTING),
                )
                if candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
                    excluded_count += 1
                    continue
            setting_candidates.append(candidate)

        normalized = dict(value)
        normalized["candidates"] = setting_candidates
        if excluded_count > 0:
            normalized["character_discovery_excluded_count"] = excluded_count
        return normalized


class PredictionBundle(BaseModel):
    episodes: list[PredictionEpisode] = Field(default_factory=list)


class CharacterSettingSchemaSnapshot(BaseModel):
    """평가 시점의 활성 설정 스키마 한 행을 표현한다."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_key: str = Field(
        validation_alias=AliasChoices("schemaKey", "schema_key"),
        min_length=1,
    )
    display_name: str = Field(
        validation_alias=AliasChoices("displayName", "display_name"),
        min_length=1,
    )
    attribute_pattern: str | None = Field(
        default=None,
        validation_alias=AliasChoices("attributePattern", "attribute_pattern"),
    )
    aliases: list[str] = Field(default_factory=list)
    value_type: SettingValueType = Field(
        validation_alias=AliasChoices("valueType", "value_type"),
    )
