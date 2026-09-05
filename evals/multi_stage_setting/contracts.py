from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from app.domain.enums import (
    CharacterFactComparisonOperation,
    CharacterFactTemporalScope,
    SettingValueType,
    WorldSettingCategory,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
)
from app.mappers.world_setting_candidate_mapper import (
    normalize_world_setting_name,
    world_setting_path_key as production_world_setting_path_key,
)
from evals.multi_stage_setting import SCHEMA_VERSION
from evals.setting_extraction.models import GoldDecision, Importance
from evals.setting_extraction.normalization import normalize_fact_key, normalize_text


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: _to_camel(name),
        populate_by_name=True,
        extra="forbid",
    )


class EvaluationDomain(StrEnum):
    CHARACTER = "CHARACTER"
    WORLD = "WORLD"


class CharacterFactType(StrEnum):
    """Character snapshot types accepted by the Spring persistence contract."""

    PROFILE = "PROFILE"
    AGE = "AGE"
    LEVEL = "LEVEL"
    STAT = "STAT"
    SKILL = "SKILL"
    ITEM = "ITEM"
    STATUS = "STATUS"
    TIME = "TIME"


class CandidateKind(StrEnum):
    SETTING = "SETTING"
    CHARACTER_DISCOVERY = "CHARACTER_DISCOVERY"
    WORLD_SETTING = "WORLD_SETTING"


class ReviewStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    FINAL = "FINAL"


class StartStateMode(StrEnum):
    EMPTY = "EMPTY"
    PREVIOUS_GOLD = "PREVIOUS_GOLD"
    SEED = "SEED"


class StateGenerationStatus(StrEnum):
    PENDING = "PENDING"
    GENERATED = "GENERATED"
    VERIFIED = "VERIFIED"


class ValueJsonProvenance(StrEnum):
    ANNOTATED = "ANNOTATED"
    GENERATED_SCALAR = "GENERATED_SCALAR"
    UNAVAILABLE = "UNAVAILABLE"


class EvaluationMode(StrEnum):
    ORACLE = "ORACLE"
    FIXED = "FIXED"
    ROLLING = "ROLLING"


class StateApplicationPolicy(StrEnum):
    """How predictions are allowed to affect later scenario inputs."""

    SCENARIO_LOCAL = "SCENARIO_LOCAL"
    ACCEPT_ALL_PREDICTIONS = "ACCEPT_ALL_PREDICTIONS"


class ScenarioPipelineStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PIPELINE_FAILED = "PIPELINE_FAILED"


class UpstreamOutcome(StrEnum):
    REACHED = "REACHED"
    UPSTREAM_MISSING = "UPSTREAM_MISSING"
    UPSTREAM_PARTIAL = "UPSTREAM_PARTIAL"
    UPSTREAM_VALUE_ERROR = "UPSTREAM_VALUE_ERROR"
    UPSTREAM_EXTRA = "UPSTREAM_EXTRA"
    UPSTREAM_BLOCKED_SUBJECT = "UPSTREAM_BLOCKED_SUBJECT"


class FailureCause(StrEnum):
    EXTRACTION_MISS = "EXTRACTION_MISS"
    RETRIEVAL_MISS = "RETRIEVAL_MISS"
    COMPARISON_ERROR = "COMPARISON_ERROR"
    STATE_APPLICATION_ERROR = "STATE_APPLICATION_ERROR"
    UPSTREAM_FALSE_POSITIVE = "UPSTREAM_FALSE_POSITIVE"


class KnownCharacter(StrictModel):
    entity_ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Spring claim은 ACTIVE 캐릭터를 createdAt DESC로 제공한다. fixture에는 wall-clock
    # 시각 대신 결정적인 생성 순서를 보존하고, legacy/외부 SEED는 None을 허용한다.
    creation_order: int | None = Field(default=None, ge=0)
    active: bool = True


class CharacterStateEntry(StrictModel):
    ref: str = Field(min_length=1)
    entity_ref: str = Field(min_length=1)
    entity_name: str = Field(min_length=1)
    fact_type: CharacterFactType
    fact_key: str = Field(min_length=1)
    value_type: SettingValueType | None = None
    value: str | None = None
    value_json: dict[str, Any] | None = None
    # STATUS 비교 문맥에서 최신 source Fact 순서를 재현하기 위한 평가 provenance다.
    source_episode_no: int | None = Field(default=None, ge=1)
    source_sort_order: int | None = Field(default=None, ge=0)


class CharacterHistoryEntry(StrictModel):
    scenario_id: str = Field(min_length=1)
    source_gold_id: str = Field(min_length=1)
    entity_ref: str = Field(min_length=1)
    entity_name: str = Field(min_length=1)
    fact_type: CharacterFactType
    fact_key: str = Field(min_length=1)
    value: str | None = None
    value_json: dict[str, Any] | None = None
    operation: CharacterFactComparisonOperation
    temporal_scope: CharacterFactTemporalScope


class WorldStateEntry(StrictModel):
    ref: str = Field(min_length=1)
    # Stable Backend subject identity. Legacy/generated fixtures may omit it and use
    # the deterministic category+display-name subject ref instead.
    # Omitting a missing value during serialization preserves legacy SEED state hashes.
    subject_ref: str | None = Field(
        default=None,
        min_length=1,
        exclude_if=lambda value: value is None,
    )
    category: WorldSettingCategory
    subject_name: str = Field(min_length=1)
    scope_name: str | None = None
    setting_name: str = Field(min_length=1)
    value: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_subject_ref(self) -> WorldStateEntry:
        if self.subject_ref is not None and self.subject_ref != self.subject_ref.strip():
            raise ValueError("subjectRef must not contain surrounding whitespace.")
        return self


class HeldWorldConflict(StrictModel):
    scenario_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    subject_ref: str | None = Field(
        default=None,
        min_length=1,
        exclude_if=lambda value: value is None,
    )
    category: WorldSettingCategory
    subject_name: str = Field(min_length=1)
    scope_name: str | None = None
    setting_name: str = Field(min_length=1)
    source_values: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_source_values(self) -> HeldWorldConflict:
        validate_world_source_values(self.source_values)
        return self


class EvaluationState(StrictModel):
    known_characters: list[KnownCharacter] = Field(default_factory=list)
    character_facts: list[CharacterStateEntry] = Field(default_factory=list)
    character_history: list[CharacterHistoryEntry] = Field(default_factory=list)
    world_facts: list[WorldStateEntry] = Field(default_factory=list)
    held_world_conflicts: list[HeldWorldConflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_state_keys(self) -> EvaluationState:
        _require_unique([item.entity_ref for item in self.known_characters], "character refs")
        _require_unique([item.ref for item in self.character_facts], "character state refs")
        _require_unique([item.ref for item in self.world_facts], "world state refs")
        _require_unique(
            [
                (
                    normalize_text(item.entity_ref),
                    normalize_text(item.fact_type),
                    normalize_fact_key(item.fact_key),
                )
                for item in self.character_facts
            ],
            "canonical character slots",
        )
        _require_unique(
            [
                (
                    world_entry_subject_ref(item),
                    normalize_world_setting_name(item.scope_name or ""),
                    normalize_world_setting_name(item.setting_name),
                )
                for item in self.world_facts
            ],
            "canonical world paths",
        )
        stable_subject_identities: dict[str, tuple[str, str]] = {}
        for item in self.world_facts:
            if item.subject_ref is None:
                continue
            identity = (
                str(item.category),
                normalize_world_setting_name(item.subject_name),
            )
            previous = stable_subject_identities.setdefault(item.subject_ref, identity)
            if previous != identity:
                raise ValueError(
                    "One world subjectRef must identify exactly one category and "
                    "canonical subject display name."
                )
        validate_world_state_properties(self.world_facts)
        for item in self.character_facts:
            validate_character_fact_slot(item.fact_type, item.fact_key)
            _validate_typed_scalar_json(item.value_type, item.value_json)
            expected = character_state_ref(item.entity_ref, item.fact_type, item.fact_key)
            if item.ref != expected:
                raise ValueError("Character state ref must equal its canonical slot ref.")
        for item in self.world_facts:
            expected = world_state_ref(
                item.category,
                item.subject_name,
                item.scope_name,
                item.setting_name,
                subject_ref=item.subject_ref,
            )
            if item.ref != expected:
                raise ValueError("World state ref must equal its canonical path ref.")
        for item in self.character_history:
            validate_character_fact_slot(item.fact_type, item.fact_key)
        _require_unique(
            [(item.scenario_id, item.decision_id) for item in self.held_world_conflicts],
            "held world conflict decisions",
        )
        return self

    def canonical(self) -> EvaluationState:
        return self.model_copy(
            update={
                "known_characters": sorted(
                    self.known_characters,
                    key=lambda item: (normalize_text(item.entity_ref), normalize_text(item.name)),
                ),
                "character_facts": sorted(
                    self.character_facts,
                    key=lambda item: (
                        normalize_text(item.entity_ref),
                        normalize_text(item.fact_type),
                        normalize_fact_key(item.fact_key),
                    ),
                ),
                "character_history": sorted(
                    self.character_history,
                    key=lambda item: (
                        item.scenario_id,
                        item.source_gold_id,
                        normalize_text(item.entity_ref),
                        normalize_fact_key(item.fact_key),
                    ),
                ),
                "world_facts": sorted(
                    self.world_facts,
                    key=lambda item: (
                        normalize_text(world_entry_subject_ref(item)),
                        world_entry_subject_ref(item),
                        normalize_world_setting_name(item.scope_name or ""),
                        item.scope_name or "",
                        normalize_world_setting_name(item.setting_name),
                        item.setting_name,
                        item.ref,
                    ),
                ),
                "held_world_conflicts": sorted(
                    self.held_world_conflicts,
                    key=lambda item: (item.scenario_id, item.decision_id),
                ),
            }
        )

    def content_hash(self) -> str:
        payload = self.canonical().model_dump(mode="json", by_alias=True)
        return _json_hash(payload)


class ScenarioGold(StrictModel):
    scenario_id: str = Field(min_length=1)
    episode_no: int = Field(ge=1)
    episode_title: str | None = None
    source_identifier: str = Field(min_length=1)
    source_hash: str | None = None
    source_text: str | None = Field(default=None, exclude=True, repr=False)
    target_domains: set[EvaluationDomain] = Field(min_length=1)
    gold_version: str = Field(min_length=1)
    # 데이터셋 출처의 업로드 묶음을 추적하는 메타데이터다. #152 운영 comparator
    # batch는 단일 회차(AnalysisJob) 내에서만 구성되므로 평가 runtime이 이 값으로
    # 서로 다른 scenario의 후보를 하나의 provider batch로 합치지 않는다.
    evaluation_batch_id: str | None = None
    candidate_free: bool = False
    start_state_mode: StartStateMode
    previous_scenario_id: str | None = None
    cumulative_through_episode: int = Field(ge=0)
    provided_context: str = ""
    known_character_names: list[str] = Field(default_factory=list)
    state_generation_status: StateGenerationStatus = StateGenerationStatus.PENDING
    before_state_uri: str | None = None
    before_state_hash: str | None = None
    after_state_uri: str | None = None
    after_state_hash: str | None = None
    seed_state: EvaluationState | None = None
    review_status: ReviewStatus
    review_note: str | None = None

    @field_serializer("target_domains")
    def serialize_target_domains(
        self,
        value: set[EvaluationDomain],
    ) -> list[str]:
        # set iteration order depends on PYTHONHASHSEED. Gold is hashed in one CLI
        # process and loaded in another, so its JSON representation must be stable.
        return sorted(domain.value for domain in value)

    @model_validator(mode="after")
    def validate_state_chain(self) -> ScenarioGold:
        if self.start_state_mode == StartStateMode.EMPTY:
            if self.previous_scenario_id is not None or self.seed_state is not None:
                raise ValueError("EMPTY scenario must not reference a previous or seed state.")
            if self.cumulative_through_episode != 0:
                raise ValueError("EMPTY scenario cumulativeThroughEpisode must be 0.")
        elif self.start_state_mode == StartStateMode.PREVIOUS_GOLD:
            if self.previous_scenario_id is None:
                raise ValueError("PREVIOUS_GOLD scenario requires previousScenarioId.")
            if self.seed_state is not None:
                raise ValueError("PREVIOUS_GOLD scenario must not embed seedState.")
        elif self.seed_state is None and self.before_state_uri is None:
            raise ValueError("SEED scenario requires seedState or beforeStateUri.")
        if any(not name.strip() for name in self.known_character_names):
            raise ValueError("knownCharacterNames must not contain blank values.")
        return self


class Stage1Common(StrictModel):
    gold_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    episode_no: int = Field(ge=1)
    sort_order: int = Field(ge=0)
    decision: GoldDecision
    importance: Importance | None = None
    context_tags: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)
    location_hint: str | None = None
    same_fact_group: str | None = None
    source_page_url: str | None = None
    current_schema_representable: bool = True
    review_status: ReviewStatus
    review_note: str | None = None

    def validate_extract_fields(self) -> None:
        if self.decision != GoldDecision.EXTRACT:
            return
        if self.importance is None:
            raise ValueError("EXTRACT Stage1 row requires importance.")
        if not self.evidence_quotes or any(not quote.strip() for quote in self.evidence_quotes):
            raise ValueError("EXTRACT Stage1 row requires evidenceQuotes.")


class CharacterStage1Gold(Stage1Common):
    domain: Literal[EvaluationDomain.CHARACTER]
    candidate_kind: Literal[CandidateKind.SETTING, CandidateKind.CHARACTER_DISCOVERY]
    entity_ref: str = Field(min_length=1)
    entity_name: str = Field(min_length=1)
    raw_entity_mention: str | None = None
    fact_type: CharacterFactType | None = None
    fact_key: str | None = None
    # ORACLE comparator input may differ from the reviewed final canonical key.
    # For legacy fixtures the canonical key remains the input key as well.
    input_fact_key: str | None = Field(default=None, max_length=150)
    accepted_fact_key_aliases: list[str] = Field(default_factory=list)
    value_type: SettingValueType | None = None
    display_value: str | None = None
    value_json: dict[str, Any] | None = None
    value_json_provenance: ValueJsonProvenance = ValueJsonProvenance.UNAVAILABLE
    structured_scorable: bool = False

    @model_validator(mode="after")
    def validate_character_candidate(self) -> CharacterStage1Gold:
        self.validate_extract_fields()
        setting_fields = (
            self.fact_type,
            self.fact_key,
            self.value_type,
            self.display_value,
        )
        if self.candidate_kind == CandidateKind.CHARACTER_DISCOVERY:
            if any(value is not None for value in setting_fields) or self.value_json is not None:
                raise ValueError("CHARACTER_DISCOVERY must not include setting value fields.")
            if self.input_fact_key is not None:
                raise ValueError("CHARACTER_DISCOVERY must not include inputFactKey.")
            if self.accepted_fact_key_aliases:
                raise ValueError("CHARACTER_DISCOVERY must not include fact key aliases.")
            return self
        if any(
            value is None or (isinstance(value, str) and not value.strip())
            for value in setting_fields
        ):
            raise ValueError(
                "Character SETTING requires factType, factKey, valueType, displayValue."
            )
        assert self.fact_type is not None and self.fact_key is not None
        validate_character_fact_slot(self.fact_type, self.fact_key)
        if self.input_fact_key is not None:
            validate_character_fact_slot(self.fact_type, self.input_fact_key)
        _validate_typed_scalar_json(self.value_type, self.value_json)
        if self.structured_scorable and self.value_json is None:
            raise ValueError("structuredScorable=true requires valueJson.")
        return self

    @property
    def accepted_fact_keys(self) -> tuple[str, ...]:
        return tuple(
            item
            for item in (self.fact_key, *self.accepted_fact_key_aliases)
            if item is not None and item.strip()
        )


class WorldStage1Gold(Stage1Common):
    domain: Literal[EvaluationDomain.WORLD]
    candidate_kind: Literal[CandidateKind.WORLD_SETTING]
    category: WorldSettingCategory
    subject_name: str = Field(min_length=1)
    scope_name: str | None = None
    setting_name: str = Field(min_length=1)
    accepted_setting_name_aliases: list[str] = Field(default_factory=list)
    source_values: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_world_candidate(self) -> WorldStage1Gold:
        self.validate_extract_fields()
        validate_world_source_values(self.source_values)
        return self

    @property
    def display_value(self) -> str:
        return "\n".join(self.source_values)

    @property
    def accepted_setting_names(self) -> tuple[str, ...]:
        """Return the canonical world setting name and reviewed Stage1 aliases."""

        return tuple(
            item
            for item in (self.setting_name, *self.accepted_setting_name_aliases)
            if item.strip()
        )


Stage1Gold = Annotated[
    CharacterStage1Gold | WorldStage1Gold,
    Field(discriminator="domain"),
]


class Stage2Common(StrictModel):
    decision_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    episode_no: int = Field(ge=1)
    sort_order: int = Field(ge=0)
    source_gold_ids: list[str] = Field(min_length=1)
    target_ref: str | None = None
    before_value: str | None = None
    before_value_json: dict[str, Any] | None = None
    proposed_value: str | None = None
    proposed_value_json: dict[str, Any] | None = None
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    comparison_reason: str | None = None
    review_status: ReviewStatus
    review_note: str | None = None

    @model_validator(mode="after")
    def validate_semantic_facts(self) -> Stage2Common:
        for label, values in (
            ("requiredFacts", self.required_facts),
            ("forbiddenFacts", self.forbidden_facts),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"{label} must not contain blank facts.")
            normalized = [normalize_text(value) for value in values]
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"{label} must not contain duplicate facts.")
        required = {normalize_text(value) for value in self.required_facts}
        forbidden = {normalize_text(value) for value in self.forbidden_facts}
        if required & forbidden:
            raise ValueError("The same fact must not be both required and forbidden.")
        return self


def _validate_character_stage2_operation(
    *,
    operation: CharacterFactComparisonOperation,
    temporal_scope: CharacterFactTemporalScope,
    target_ref: str | None,
    removed_snapshot_refs: list[str],
    proposed_value: str | None,
    proposed_value_json: dict[str, Any] | None,
) -> None:
    if len(set(removed_snapshot_refs)) != len(removed_snapshot_refs):
        raise ValueError("removedSnapshotRefs must not contain duplicates.")
    target_required = operation in {
        CharacterFactComparisonOperation.UPDATE,
        CharacterFactComparisonOperation.MERGE,
    }
    if target_required != (target_ref is not None):
        raise ValueError("UPDATE and MERGE require targetRef; other operations forbid it.")
    if operation == CharacterFactComparisonOperation.REMOVE and not removed_snapshot_refs:
        raise ValueError("REMOVE requires at least one removedSnapshotRef.")
    changes_snapshot = operation in {
        CharacterFactComparisonOperation.ADD,
        CharacterFactComparisonOperation.UPDATE,
        CharacterFactComparisonOperation.MERGE,
    }
    if changes_snapshot and (not proposed_value or not proposed_value_json):
        raise ValueError("ADD, UPDATE, MERGE require proposedValue/proposedValueJson.")
    if (
        changes_snapshot
        and proposed_value_json is not None
        and proposed_value_json.get("active") is False
    ):
        raise ValueError("active=false facts must not use ADD, UPDATE, or MERGE.")
    if not changes_snapshot and (proposed_value is not None or proposed_value_json is not None):
        raise ValueError(f"{operation} must not include proposed values.")
    if temporal_scope in {
        CharacterFactTemporalScope.PAST,
        CharacterFactTemporalScope.HYPOTHETICAL,
    } and operation not in {
        CharacterFactComparisonOperation.HISTORY_ONLY,
        CharacterFactComparisonOperation.REVIEW_REQUIRED,
    }:
        raise ValueError("PAST/HYPOTHETICAL require HISTORY_ONLY or REVIEW_REQUIRED.")
    if (
        temporal_scope == CharacterFactTemporalScope.UNKNOWN
        and operation != CharacterFactComparisonOperation.REVIEW_REQUIRED
    ):
        raise ValueError("UNKNOWN temporalScope requires REVIEW_REQUIRED.")
    if removed_snapshot_refs and (
        temporal_scope != CharacterFactTemporalScope.PRESENT
        or operation
        not in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
            CharacterFactComparisonOperation.REMOVE,
        }
    ):
        raise ValueError("removedSnapshotRefs require a PRESENT STATUS transition.")
    if target_ref is not None and target_ref in removed_snapshot_refs:
        raise ValueError("targetRef must not also appear in removedSnapshotRefs.")


class CharacterStage2Gold(Stage2Common):
    domain: Literal[EvaluationDomain.CHARACTER]
    operation: CharacterFactComparisonOperation
    temporal_scope: CharacterFactTemporalScope
    removed_snapshot_refs: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_character_operation(self) -> CharacterStage2Gold:
        if len(self.source_gold_ids) != 1:
            raise ValueError("Character Stage2 decision requires exactly one source Gold row.")
        _validate_character_stage2_operation(
            operation=self.operation,
            temporal_scope=self.temporal_scope,
            target_ref=self.target_ref,
            removed_snapshot_refs=self.removed_snapshot_refs,
            proposed_value=self.proposed_value,
            proposed_value_json=self.proposed_value_json,
        )
        return self


class WorldStage2Gold(Stage2Common):
    domain: Literal[EvaluationDomain.WORLD]
    operation: WorldSettingOperation
    consolidation_status: WorldSettingConsolidationStatus
    matched_scope_name: str | None = None
    matched_property_name: str | None = None
    proposed_scope_name: str | None = None
    proposed_setting_name: str | None = None
    existing_root_property_names_to_move: list[str] = Field(
        default_factory=list,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_world_operation(self) -> WorldStage2Gold:
        if self.before_value_json is not None or self.proposed_value_json is not None:
            raise ValueError("World Stage2 uses string values and must not include valueJson.")
        if self.matched_property_name is not None and self.target_ref is None:
            raise ValueError("matchedPropertyName requires targetRef.")
        if self.operation in {WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}:
            if self.target_ref is None or self.matched_property_name is None:
                raise ValueError("World UPDATE/MERGE require targetRef and matchedPropertyName.")
            if (
                self.proposed_scope_name != self.matched_scope_name
                or self.proposed_setting_name != self.matched_property_name
            ):
                raise ValueError("World UPDATE/MERGE must preserve the matched property path.")
        elif self.operation == WorldSettingOperation.ADD:
            if self.matched_property_name is not None:
                raise ValueError("World ADD must not target an existing property.")
        if any(not name.strip() for name in self.existing_root_property_names_to_move):
            raise ValueError("existingRootPropertyNamesToMove must not contain blanks.")
        if len(
            {
                normalize_world_setting_name(name)
                for name in self.existing_root_property_names_to_move
            }
        ) != len(self.existing_root_property_names_to_move):
            raise ValueError("existingRootPropertyNamesToMove must not contain duplicate names.")
        if self.existing_root_property_names_to_move and (
            self.operation != WorldSettingOperation.ADD
            or self.target_ref is None
            or self.proposed_scope_name is None
        ):
            raise ValueError(
                "Existing root properties may move only with a scoped World ADD target."
            )
        if (
            self.proposed_scope_name is not None
            and self.proposed_setting_name is not None
            and normalize_world_setting_name(self.proposed_scope_name)
            == normalize_world_setting_name(self.proposed_setting_name)
        ):
            raise ValueError("A world scope name must differ from its setting name.")
        if self.matched_scope_name is not None and self.matched_property_name is None:
            raise ValueError("matchedScopeName requires matchedPropertyName.")
        # 운영 comparator DTO는 EXCLUDE/CONFLICT에서도 검토 화면용 원문 값을 보존한다.
        if not self.proposed_setting_name or not self.proposed_value:
            raise ValueError("World Stage2 requires proposedSettingName/proposedValue.")
        return self


Stage2Gold = Annotated[
    CharacterStage2Gold | WorldStage2Gold,
    Field(discriminator="domain"),
]


class GoldSnapshotV3(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    dataset_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scorable: bool = True
    fixture_hash: str | None = None
    evaluation_scenario_ids: list[str] = Field(default_factory=list)
    scenarios: list[ScenarioGold] = Field(min_length=1)
    stage1: list[Stage1Gold] = Field(default_factory=list)
    stage2: list[Stage2Gold] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_relations(self) -> GoldSnapshotV3:
        _require_unique([item.scenario_id for item in self.scenarios], "scenario IDs")
        _require_unique([item.episode_no for item in self.scenarios], "scenario episode numbers")
        _require_unique([item.gold_id for item in self.stage1], "Stage1 Gold IDs")
        _require_unique([item.decision_id for item in self.stage2], "Stage2 decision IDs")
        _require_unique(
            [(item.scenario_id, item.domain, item.sort_order) for item in self.stage1],
            "Stage1 sort orders per scenario/domain",
        )
        _require_unique(
            [(item.scenario_id, item.domain, item.sort_order) for item in self.stage2],
            "Stage2 sort orders per scenario/domain",
        )
        scenario_by_id = {item.scenario_id: item for item in self.scenarios}
        selected_ids = self.evaluation_scenario_ids or list(scenario_by_id)
        _require_unique(selected_ids, "evaluation scenario IDs")
        unknown_selected = sorted(set(selected_ids) - scenario_by_id.keys())
        if unknown_selected:
            raise ValueError(f"Unknown evaluationScenarioIds: {unknown_selected}")
        if not self.evaluation_scenario_ids:
            self.evaluation_scenario_ids = selected_ids
        stage1_by_id = {item.gold_id: item for item in self.stage1}
        for scenario in self.scenarios:
            if scenario.previous_scenario_id is not None:
                previous = scenario_by_id.get(scenario.previous_scenario_id)
                if previous is None:
                    raise ValueError(
                        f"Scenario {scenario.scenario_id} references unknown previous scenario."
                    )
                if previous.episode_no >= scenario.episode_no:
                    raise ValueError("previousScenarioId must reference an earlier episode.")
                if scenario.cumulative_through_episode != previous.episode_no:
                    raise ValueError(
                        "cumulativeThroughEpisode must equal the previous scenario episode."
                    )
        for row in self.stage1:
            scenario = scenario_by_id.get(row.scenario_id)
            if scenario is None or scenario.episode_no != row.episode_no:
                raise ValueError(f"Stage1 row {row.gold_id} has an invalid scenario relation.")
            if row.domain not in scenario.target_domains:
                raise ValueError(f"Stage1 row {row.gold_id} uses a disabled scenario domain.")
        source_use_count: dict[str, int] = {}
        decision_by_source: dict[str, Stage2Gold] = {}
        for decision in self.stage2:
            scenario = scenario_by_id.get(decision.scenario_id)
            if scenario is None or scenario.episode_no != decision.episode_no:
                raise ValueError(
                    f"Stage2 row {decision.decision_id} has an invalid scenario relation."
                )
            for gold_id in decision.source_gold_ids:
                source = stage1_by_id.get(gold_id)
                if source is None:
                    raise ValueError(
                        f"Stage2 row {decision.decision_id} references unknown Stage1 {gold_id}."
                    )
                if source.scenario_id != decision.scenario_id or source.domain != decision.domain:
                    raise ValueError(
                        f"Stage2 row {decision.decision_id} source relation has wrong scenario/domain."
                    )
                if source.decision != GoldDecision.EXTRACT:
                    raise ValueError("Only EXTRACT Stage1 rows may feed Stage2 decisions.")
                if (
                    isinstance(source, CharacterStage1Gold)
                    and source.candidate_kind == CandidateKind.CHARACTER_DISCOVERY
                ):
                    raise ValueError("CHARACTER_DISCOVERY does not feed setting comparison.")
                source_use_count[gold_id] = source_use_count.get(gold_id, 0) + 1
                decision_by_source[gold_id] = decision
            sources = [stage1_by_id[gold_id] for gold_id in decision.source_gold_ids]
            if isinstance(decision, CharacterStage2Gold):
                source = sources[0]
                assert isinstance(source, CharacterStage1Gold)
                _validate_typed_scalar_json(
                    source.value_type,
                    decision.proposed_value_json,
                )
                _validate_typed_scalar_json(
                    source.value_type,
                    decision.before_value_json,
                )
                if decision.removed_snapshot_refs and source.fact_type != "STATUS":
                    raise ValueError(
                        "Character removedSnapshotRefs require a STATUS source candidate."
                    )
                if (
                    decision.operation != CharacterFactComparisonOperation.REMOVE
                    and isinstance(source, CharacterStage1Gold)
                    and source.fact_type is not None
                    and source.fact_key is not None
                    and character_state_ref(
                        source.entity_ref,
                        source.fact_type,
                        source.fact_key,
                    )
                    in decision.removed_snapshot_refs
                ):
                    raise ValueError(
                        "removedSnapshotRefs must not remove the candidate's exact slot."
                    )
            elif (
                len(
                    {
                        (
                            str(source.category),
                            normalize_world_setting_name(source.subject_name),
                            normalize_world_setting_name(source.scope_name or ""),
                        )
                        for source in sources
                        if isinstance(source, WorldStage1Gold)
                    }
                )
                != 1
            ):
                raise ValueError(
                    f"World Stage2 row {decision.decision_id} sources must share "
                    "one category, subject, and raw scope."
                )
            elif isinstance(decision, WorldStage2Gold):
                primary = sources[0]
                assert isinstance(primary, WorldStage1Gold)
                compares_existing_property = decision.operation in {
                    WorldSettingOperation.UPDATE,
                    WorldSettingOperation.MERGE,
                } or (
                    decision.operation == WorldSettingOperation.EXCLUDE
                    and decision.matched_property_name is not None
                )
                if compares_existing_property and normalize_world_setting_name(
                    primary.scope_name or ""
                ) != normalize_world_setting_name(decision.matched_scope_name or ""):
                    raise ValueError(
                        "World UPDATE/MERGE or matched EXCLUDE must use the "
                        "extracted scope as matchedScopeName."
                    )
                if decision.operation == WorldSettingOperation.EXCLUDE and (
                    normalize_world_setting_name(decision.proposed_scope_name or "")
                    != normalize_world_setting_name(primary.scope_name or "")
                    or normalize_world_setting_name(decision.proposed_setting_name or "")
                    != normalize_world_setting_name(primary.setting_name)
                ):
                    raise ValueError("World EXCLUDE must preserve the extracted property path.")
                source_values = {
                    normalize_world_setting_name(value)
                    for source in sources
                    if isinstance(source, WorldStage1Gold)
                    for value in source.source_values
                }
                if (
                    decision.operation in {WorldSettingOperation.ADD, WorldSettingOperation.EXCLUDE}
                    and len(source_values) == 1
                    and normalize_world_setting_name(decision.proposed_value)
                    != next(iter(source_values))
                ):
                    raise ValueError(
                        "Single-value World ADD/EXCLUDE must preserve the extracted value."
                    )
        duplicates = sorted(key for key, count in source_use_count.items() if count > 1)
        if duplicates:
            raise ValueError(f"Stage1 rows may feed only one Stage2 decision: {duplicates}")
        world_groups: dict[tuple[str, str, str, str, str], list[str]] = {}
        for row in self.stage1:
            if isinstance(row, WorldStage1Gold) and row.decision == GoldDecision.EXTRACT:
                world_groups.setdefault(
                    (
                        row.scenario_id,
                        *world_path_key(
                            row.category,
                            row.subject_name,
                            row.scope_name,
                            row.setting_name,
                        ),
                    ),
                    [],
                ).append(row.gold_id)
        for source_ids in world_groups.values():
            if len(source_ids) < 2:
                continue
            decisions = {
                decision_by_source[source_id].decision_id
                for source_id in source_ids
                if source_id in decision_by_source
            }
            if len(decisions) != 1:
                raise ValueError(
                    "World Stage1 rows on one canonical path must feed one Stage2 decision."
                )
        expected_stage2_sources = {
            row.gold_id
            for row in self.stage1
            if row.decision == GoldDecision.EXTRACT
            and not (
                isinstance(row, CharacterStage1Gold)
                and row.candidate_kind == CandidateKind.CHARACTER_DISCOVERY
            )
        }
        missing_stage2 = sorted(expected_stage2_sources - source_use_count.keys())
        if missing_stage2:
            raise ValueError(f"EXTRACT setting rows require one Stage2 decision: {missing_stage2}")
        for scenario in self.scenarios:
            positive_rows = [
                row
                for row in self.stage1
                if row.scenario_id == scenario.scenario_id and row.decision == GoldDecision.EXTRACT
            ]
            if scenario.candidate_free and positive_rows:
                raise ValueError(
                    f"Candidate-free scenario {scenario.scenario_id} has EXTRACT rows."
                )
            if not scenario.candidate_free and not positive_rows:
                raise ValueError(
                    f"Scenario {scenario.scenario_id} has no EXTRACT rows; "
                    "mark it candidateFree=true."
                )
        return self

    def computed_fixture_hash(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True, exclude={"fixture_hash"})
        return _json_hash(payload)

    def with_fixture_hash(self) -> GoldSnapshotV3:
        return self.model_copy(update={"fixture_hash": self.computed_fixture_hash()})


class PredictionEvidence(StrictModel):
    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


class CharacterStage1Prediction(StrictModel):
    candidate_id: str = Field(min_length=1)
    sort_order: int = Field(default=0, ge=0)
    domain: Literal[EvaluationDomain.CHARACTER]
    candidate_kind: Literal[CandidateKind.SETTING, CandidateKind.CHARACTER_DISCOVERY]
    # 운영 UUID가 아니라 평가 state 안에서만 안정적인 캐릭터 selector다. 기존 캐릭터는
    # beforeState의 entityRef를 사용하고, 신규 발견은 prediction namespace를 사용한다.
    entity_ref: str | None = None
    entity_name: str = Field(min_length=1)
    matched_character_name: str | None = None
    match_status: str | None = None
    raw_entity_mention: str | None = None
    fact_type: CharacterFactType | None = None
    fact_key: str | None = None
    value_type: SettingValueType | None = None
    display_value: str | None = None
    value_json: dict[str, Any] | None = None
    evidence_spans: list[PredictionEvidence] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_character_fact_slot(self) -> CharacterStage1Prediction:
        setting_fields = (
            self.fact_type,
            self.fact_key,
            self.value_type,
            self.display_value,
        )
        if self.candidate_kind == CandidateKind.CHARACTER_DISCOVERY:
            if any(value is not None for value in setting_fields) or self.value_json is not None:
                raise ValueError(
                    "CHARACTER_DISCOVERY prediction must not include setting value fields."
                )
            return self
        if self.fact_type is not None and self.fact_key is not None:
            validate_character_fact_slot(self.fact_type, self.fact_key)
        _validate_typed_scalar_json(self.value_type, self.value_json)
        return self


class WorldStage1Prediction(StrictModel):
    candidate_id: str = Field(min_length=1)
    sort_order: int = Field(default=0, ge=0)
    domain: Literal[EvaluationDomain.WORLD]
    candidate_kind: Literal[CandidateKind.WORLD_SETTING] = CandidateKind.WORLD_SETTING
    category: WorldSettingCategory
    subject_name: str = Field(min_length=1)
    scope_name: str | None = None
    setting_name: str = Field(min_length=1)
    source_values: list[str] = Field(min_length=1)
    evidence_spans: list[PredictionEvidence] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_source_values(self) -> WorldStage1Prediction:
        validate_world_source_values(self.source_values)
        return self

    @property
    def display_value(self) -> str:
        return "\n".join(self.source_values)


Stage1Prediction = Annotated[
    CharacterStage1Prediction | WorldStage1Prediction,
    Field(discriminator="domain"),
]


class CharacterStage2Prediction(StrictModel):
    source_candidate_id: str = Field(min_length=1)
    domain: Literal[EvaluationDomain.CHARACTER]
    operation: CharacterFactComparisonOperation
    resolved_canonical_fact_key: str = Field(min_length=1, max_length=150)
    target_ref: str | None = None
    removed_snapshot_refs: list[str] = Field(default_factory=list, max_length=30)
    proposed_value: str | None = None
    proposed_value_json: dict[str, Any] | None = None
    temporal_scope: CharacterFactTemporalScope
    comparison_reason: str | None = None

    @model_validator(mode="after")
    def validate_character_operation(self) -> CharacterStage2Prediction:
        _validate_character_stage2_operation(
            operation=self.operation,
            temporal_scope=self.temporal_scope,
            target_ref=self.target_ref,
            removed_snapshot_refs=self.removed_snapshot_refs,
            proposed_value=self.proposed_value,
            proposed_value_json=self.proposed_value_json,
        )
        return self


class WorldStage2Prediction(StrictModel):
    source_candidate_id: str = Field(min_length=1)
    domain: Literal[EvaluationDomain.WORLD]
    consolidation_status: WorldSettingConsolidationStatus
    operation: WorldSettingOperation
    target_ref: str | None = None
    matched_scope_name: str | None = None
    matched_property_name: str | None = None
    proposed_scope_name: str | None = None
    proposed_setting_name: str = Field(min_length=1)
    proposed_value: str = Field(min_length=1)
    existing_root_property_names_to_move: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    comparison_reason: str | None = None

    @model_validator(mode="after")
    def validate_world_operation(self) -> WorldStage2Prediction:
        if any(not name.strip() for name in self.existing_root_property_names_to_move):
            raise ValueError("existingRootPropertyNamesToMove must not contain blanks.")
        if len(
            {
                normalize_world_setting_name(name)
                for name in self.existing_root_property_names_to_move
            }
        ) != len(self.existing_root_property_names_to_move):
            raise ValueError("existingRootPropertyNamesToMove must not contain duplicate names.")
        if self.existing_root_property_names_to_move and (
            self.operation != WorldSettingOperation.ADD
            or self.target_ref is None
            or self.proposed_scope_name is None
        ):
            raise ValueError(
                "Existing root properties may move only with a scoped World ADD target."
            )
        if self.proposed_scope_name is not None and normalize_world_setting_name(
            self.proposed_scope_name
        ) == normalize_world_setting_name(self.proposed_setting_name):
            raise ValueError("A world scope name must differ from its setting name.")
        return self


Stage2Prediction = Annotated[
    CharacterStage2Prediction | WorldStage2Prediction,
    Field(discriminator="domain"),
]


class RuntimeFailure(StrictModel):
    stage: Literal["CHARACTER_STAGE1", "WORLD_STAGE1", "CHARACTER_STAGE2", "WORLD_STAGE2"]
    source_id: str | None = None
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)


class ScenarioPrediction(StrictModel):
    scenario_id: str = Field(min_length=1)
    pipeline_status: ScenarioPipelineStatus = ScenarioPipelineStatus.COMPLETED
    failed_stage: Literal["CHARACTER_STAGE1", "WORLD_STAGE1"] | None = None
    raw_stage1: list[Stage1Prediction] = Field(default_factory=list)
    stage1: list[Stage1Prediction] = Field(default_factory=list)
    stage2: list[Stage2Prediction] = Field(default_factory=list)
    failures: list[RuntimeFailure] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_pipeline_status(self) -> ScenarioPrediction:
        for item in self.stage1:
            if not isinstance(item, CharacterStage1Prediction):
                continue
            if item.candidate_kind != CandidateKind.SETTING:
                continue
            setting_fields = (
                item.fact_type,
                item.fact_key,
                item.value_type,
                item.display_value,
            )
            if any(
                value is None or (isinstance(value, str) and not value.strip())
                for value in setting_fields
            ):
                raise ValueError(
                    "Character SETTING handoff requires factType, factKey, "
                    "valueType, and displayValue."
                )
        if self.pipeline_status == ScenarioPipelineStatus.PIPELINE_FAILED:
            if self.failed_stage is None:
                raise ValueError("PIPELINE_FAILED scenario requires failedStage.")
            if self.failed_stage == "CHARACTER_STAGE1" and (self.stage1 or self.stage2):
                raise ValueError(
                    "CHARACTER_STAGE1 failure must not include handoff or Stage2 outputs."
                )
            if self.failed_stage == "WORLD_STAGE1" and (
                any(item.domain == EvaluationDomain.WORLD for item in self.stage1)
                or any(item.domain == EvaluationDomain.WORLD for item in self.stage2)
            ):
                raise ValueError(
                    "WORLD_STAGE1 failure must not include World handoff or Stage2 outputs."
                )
        elif self.failed_stage is not None:
            raise ValueError("COMPLETED scenario must not declare failedStage.")
        return self


class PredictionBundleV3(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    fixture_hash: str = Field(min_length=1)
    mode: EvaluationMode
    state_application_policy: StateApplicationPolicy | None = None
    evaluation_domains: set[EvaluationDomain] = Field(
        default_factory=lambda: {EvaluationDomain.CHARACTER, EvaluationDomain.WORLD},
        min_length=1,
    )
    evaluation_scenario_ids: list[str] = Field(default_factory=list)
    analysis_model: str | None = None
    subject_resolution_model: str | None = None
    comparison_model: str | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    character_schema_hash: str | None = None
    max_chunks: int | None = Field(default=None, ge=1)
    scenarios: list[ScenarioPrediction] = Field(default_factory=list)

    @field_serializer("evaluation_domains")
    def serialize_evaluation_domains(
        self,
        value: set[EvaluationDomain],
    ) -> list[str]:
        return sorted(domain.value for domain in value)

    @model_validator(mode="after")
    def require_unique_scenarios(self) -> PredictionBundleV3:
        expected_policy = (
            StateApplicationPolicy.ACCEPT_ALL_PREDICTIONS
            if self.mode == EvaluationMode.ROLLING
            else StateApplicationPolicy.SCENARIO_LOCAL
        )
        if self.state_application_policy is None:
            self.state_application_policy = expected_policy
        elif self.state_application_policy != expected_policy:
            raise ValueError(
                f"stateApplicationPolicy {self.state_application_policy} is inconsistent "
                f"with evaluation mode {self.mode}."
            )
        _require_unique([item.scenario_id for item in self.scenarios], "prediction scenario IDs")
        _require_unique(self.evaluation_scenario_ids, "prediction evaluation scenario IDs")
        for scenario in self.scenarios:
            stage1_by_id = {item.candidate_id: item for item in scenario.stage1}
            _require_unique(
                [item.candidate_id for item in scenario.stage1],
                f"prediction candidate IDs in {scenario.scenario_id}",
            )
            _require_unique(
                [item.source_candidate_id for item in scenario.stage2],
                f"Stage2 source candidate IDs in {scenario.scenario_id}",
            )
            if self.mode == EvaluationMode.ORACLE:
                # ORACLE sourceCandidateId points to a Gold Stage1 row, which is not
                # available at this model boundary. The evaluator validates that
                # cross-fixture relation once Gold is present.
                continue
            for decision in scenario.stage2:
                source = stage1_by_id.get(decision.source_candidate_id)
                if source is None:
                    raise ValueError(
                        f"Stage2 prediction in {scenario.scenario_id} references unknown "
                        f"Stage1 candidate {decision.source_candidate_id}."
                    )
                if source.domain != decision.domain:
                    raise ValueError(
                        f"Stage2 prediction in {scenario.scenario_id} has a different "
                        f"domain from Stage1 candidate {decision.source_candidate_id}."
                    )
                if (
                    isinstance(decision, CharacterStage2Prediction)
                    and decision.removed_snapshot_refs
                    and isinstance(source, CharacterStage1Prediction)
                    and source.fact_type != CharacterFactType.STATUS
                ):
                    raise ValueError(
                        "Character removedSnapshotRefs require a STATUS source candidate."
                    )
                if isinstance(decision, CharacterStage2Prediction) and isinstance(
                    source, CharacterStage1Prediction
                ):
                    _validate_typed_scalar_json(
                        source.value_type,
                        decision.proposed_value_json,
                    )
        return self


def known_characters_for_runtime(state: EvaluationState) -> list[KnownCharacter]:
    """Return the Spring claim-equivalent ACTIVE character order.

    Newly generated fixtures carry ``creationOrder`` and therefore reproduce
    ``createdAt DESC``. Legacy/external states without that metadata remain
    deterministic by falling back to the stable entity ref after ordered rows.
    Duplicate names are intentionally preserved because they must produce an
    ambiguous name match instead of being collapsed into one character.
    """

    return sorted(
        (item for item in state.known_characters if item.active),
        key=lambda item: (
            item.creation_order is None,
            -(item.creation_order or 0),
            normalize_text(item.entity_ref),
        ),
    )


def character_state_ref(entity_ref: str, fact_type: str, fact_key: str) -> str:
    return ":".join(
        (
            "gold",
            "character",
            _stable_ref_segment(entity_ref),
            _stable_ref_segment(fact_type.upper()),
            _stable_ref_segment(fact_key),
        )
    )


def world_state_ref(
    category: WorldSettingCategory | str,
    subject_name: str,
    scope_name: str | None,
    setting_name: str,
    *,
    subject_ref: str | None = None,
) -> str:
    if subject_ref is None:
        parts = [
            "gold",
            "world",
            _stable_ref_segment(str(category)),
            _stable_ref_segment(subject_name),
        ]
    else:
        # A separate top-level namespace prevents an arbitrary legacy subject display
        # name from colliding with an explicit Backend subject identity.
        parts = [
            "gold",
            "world-by-subject-ref",
            _stable_ref_segment(str(category)),
            _stable_ref_segment(subject_ref),
        ]
    if scope_name is not None and scope_name.strip():
        parts.append(_stable_ref_segment(scope_name))
    parts.append(_stable_ref_segment(setting_name))
    return ":".join(parts)


def world_subject_ref(
    category: WorldSettingCategory | str,
    subject_name: str,
    *,
    subject_ref: str | None = None,
) -> str:
    if subject_ref is not None:
        return ":".join(
            (
                "gold",
                "world-subject-ref",
                _stable_ref_segment(subject_ref),
            )
        )
    return ":".join(
        (
            "gold",
            "world-subject",
            _stable_ref_segment(str(category)),
            _stable_ref_segment(subject_name),
        )
    )


def world_entry_subject_ref(entry: WorldStateEntry) -> str:
    return world_subject_ref(
        entry.category,
        entry.subject_name,
        subject_ref=entry.subject_ref,
    )


def world_path_key(
    category: WorldSettingCategory | str,
    subject_name: str,
    scope_name: str | None,
    setting_name: str,
) -> tuple[str, str, str, str]:
    return production_world_setting_path_key(
        category,
        subject_name,
        scope_name,
        setting_name,
    )


def _stable_ref_segment(value: str) -> str:
    """Escape the ref delimiter without changing ordinary human-readable segments."""

    return value.strip().replace("%", "%25").replace(":", "%3A")


def infer_character_fact_type(fact_key: str) -> CharacterFactType | None:
    """운영 handoff와 평가 reducer가 공유하는 canonical Fact prefix 매핑."""

    prefix = fact_key.split(".", 1)[0].strip().casefold()
    return {
        "age": CharacterFactType.AGE,
        "level": CharacterFactType.LEVEL,
        "profile": CharacterFactType.PROFILE,
        "stat": CharacterFactType.STAT,
        "stats": CharacterFactType.STAT,
        "skill": CharacterFactType.SKILL,
        "skills": CharacterFactType.SKILL,
        "item": CharacterFactType.ITEM,
        "items": CharacterFactType.ITEM,
        "status": CharacterFactType.STATUS,
        "statuses": CharacterFactType.STATUS,
        "time": CharacterFactType.TIME,
    }.get(prefix)


def validate_character_fact_slot(
    fact_type: CharacterFactType | str,
    fact_key: str,
) -> None:
    """Reject built-in key/type combinations Spring could never persist.

    Work-specific schemas may use an arbitrary schemaKey, so an unknown prefix remains
    valid. Dynamic built-in keys such as ``skill.<name>`` retain their suffix while the
    recognized namespace fixes the persisted CharacterFactType.
    """

    resolved_type = CharacterFactType(fact_type)
    inferred_type = infer_character_fact_type(fact_key)
    if inferred_type is not None and inferred_type != resolved_type:
        raise ValueError(f"factType {resolved_type} does not match built-in factKey {fact_key}.")


def _validate_typed_scalar_json(
    value_type: SettingValueType | str | None,
    value_json: dict[str, Any] | None,
) -> None:
    """Reject scalar payloads that production cannot interpret without coercion."""

    if value_json is None or value_type is None:
        return
    resolved_type = SettingValueType(value_type)
    if resolved_type not in {
        SettingValueType.STRING,
        SettingValueType.NUMBER,
        SettingValueType.BOOLEAN,
    }:
        return
    if "value" not in value_json:
        raise ValueError(f"{resolved_type} valueJson must contain a typed value field.")
    value = value_json["value"]
    if resolved_type == SettingValueType.STRING:
        valid = isinstance(value, str)
    elif resolved_type == SettingValueType.NUMBER:
        valid = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    else:
        valid = isinstance(value, bool)
    if not valid:
        raise ValueError(
            f"{resolved_type} valueJson.value must preserve its native JSON scalar type."
        )


def validate_world_state_properties(entries: list[WorldStateEntry]) -> None:
    """Validate the flat evaluation view against Spring's property-tree shape."""

    seen_paths: set[tuple[str, str, str, str]] = set()
    top_level_shapes: dict[tuple[str, str, str], str] = {}
    for entry in entries:
        path = (
            str(entry.category),
            world_entry_subject_ref(entry),
            normalize_world_setting_name(entry.scope_name or ""),
            normalize_world_setting_name(entry.setting_name),
        )
        if path in seen_paths:
            raise ValueError("World state contains duplicate canonical property paths.")
        seen_paths.add(path)
        category, subject, scope, setting = path
        top_level_name = setting if not scope else scope
        shape = "PROPERTY" if not scope else "SCOPE"
        shape_key = (category, subject, top_level_name)
        existing_shape = top_level_shapes.setdefault(shape_key, shape)
        if existing_shape != shape:
            raise ValueError(
                "World state cannot use one top-level name as both a property and a scope."
            )


def validate_world_source_values(values: list[str]) -> None:
    """Apply the same value identity used by production candidate consolidation."""

    if any(not value.strip() for value in values):
        raise ValueError("sourceValues must not contain blank values.")
    normalized = [normalize_world_setting_name(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("sourceValues must be unique after normalization.")


def _require_unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label} are not allowed.")


def _json_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
