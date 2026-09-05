from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid5

import httpx

from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.analysis.character_fact_comparison_pipeline import (
    CharacterFactBatchComparator,
    execute_character_fact_comparison_batch,
)
from app.analysis.character_fact_projection import is_explicit_inactive_status
from app.analysis.character_fact_comparator import (
    CHARACTER_FACT_COMPARISON_BATCH_CACHE_KEY,
    CharacterFactComparator,
)
from app.analysis.character_name_resolver import (
    ActiveCharacterStatus,
    KnownCharacter as RuntimeKnownCharacter,
)
from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionChunkContext,
)
from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.schemas import ExtractedSettingCandidate
from app.analysis.setting_extractor import (
    SETTING_EXTRACTION_CACHE_KEY_VERSION,
    CharacterSettingExtractor,
    CharacterSettingSchemaHint,
)
from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.chunking.chunk_splitter import EpisodeChunkDraft, split_into_chunks
from app.domain.enums import (
    CharacterFactComparisonOperation,
    SettingCandidateKind,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
)
from app.domain.setting_values import normalize_setting_display_value
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.exceptions import LlmIncompleteResponseError, LlmResponseValidationError
from app.llm.protocols import LlmResponseSchema, TextGenerationClient
from app.llm.responses import LlmTextResponse
from app.mappers.world_setting_candidate_mapper import (
    WorldSettingCandidateMapper,
    normalize_world_setting_name,
)
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import (
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchDecision,
    WorkerCharacterFactComparisonBatchSnapshotEntry,
    WorkerEvidenceSpan,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
    WorkerWorldSettingSubject,
)
from app.services.setting_candidate_service import prepare_setting_candidates
from evals.multi_stage_setting.contracts import (
    CandidateKind,
    CharacterFactType,
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Gold,
    CharacterStage2Prediction,
    EvaluationDomain,
    EvaluationMode,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    PredictionBundleV3,
    PredictionEvidence,
    RuntimeFailure,
    ScenarioGold,
    ScenarioPipelineStatus,
    ScenarioPrediction,
    StateApplicationPolicy,
    Stage1Prediction,
    Stage2Prediction,
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStage2Prediction,
    character_state_ref,
    infer_character_fact_type,
    known_characters_for_runtime,
    world_path_key,
    world_subject_ref,
)
from evals.multi_stage_setting.state_effects import (
    StateApplicationError,
    apply_prediction_decision,
    build_gold_state_chain,
)


RUNTIME_UUID_NAMESPACE = UUID("1754f2f4-2b5d-5ef3-bf5c-2e245eea7a35")
MAX_CHARACTER_CONTEXT_ENTRIES = 30


class WorldComparatorApi(Protocol):
    async def compare_batch(
        self,
        category: str,
        candidates: list[WorkerWorldSettingComparisonBatchCandidate],
        targets: list[WorkerWorldSettingComparisonTarget],
    ) -> tuple[Any, dict]: ...


@dataclass
class RuntimeUsageCounter:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    def snapshot(self) -> tuple[int, int, int]:
        return self.input_tokens, self.cached_input_tokens, self.output_tokens


@dataclass(frozen=True)
class RuntimePricing:
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def estimate(self, usage: tuple[int, int, int]) -> Decimal:
        input_tokens, cached_tokens, output_tokens = usage
        uncached_tokens = max(0, input_tokens - cached_tokens)
        return (
            Decimal(uncached_tokens) * self.input_usd_per_million
            + Decimal(cached_tokens) * self.cached_input_usd_per_million
            + Decimal(output_tokens) * self.output_usd_per_million
        ) / Decimal(1_000_000)


class UsageRecordingTextGenerationClient:
    def __init__(
        self,
        client: TextGenerationClient,
        usage: RuntimeUsageCounter,
    ) -> None:
        self.client = client
        self.usage = usage

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
        response_schema: LlmResponseSchema | None = None,
    ) -> LlmTextResponse:
        try:
            response = await self.client.create_text_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_output_tokens=max_output_tokens,
                prompt_cache_key=prompt_cache_key,
                response_schema=response_schema,
            )
        except LlmResponseValidationError as exc:
            self._record_usage(
                exc.input_token_count,
                exc.cached_input_token_count,
                exc.output_token_count,
            )
            raise
        self._record_usage(
            response.input_token_count,
            response.cached_input_token_count,
            response.output_token_count,
        )
        return response

    def _record_usage(
        self,
        input_tokens: int | None,
        cached_input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        self.usage.input_tokens += input_tokens or 0
        self.usage.cached_input_tokens += cached_input_tokens or 0
        self.usage.output_tokens += output_tokens or 0


@dataclass(frozen=True)
class RuntimeComponents:
    character_comparator: CharacterFactBatchComparator
    world_comparator: WorldComparatorApi
    character_extractor: CharacterSettingExtractor | Any | None = None
    character_subject_resolver: CharacterSubjectResolver | Any | None = None
    world_extractor: WorldSettingExtractor | Any | None = None
    world_subject_resolver: WorldSettingSubjectResolver | Any | None = None
    usage: RuntimeUsageCounter | None = None


@dataclass(frozen=True)
class _CharacterRecord:
    candidate_id: str
    candidate: ExtractedSettingCandidate
    canonical_fact_type: CharacterFactType | None = None
    raw_fact_key: str | None = None
    canonical_key_resolution: Literal["EXACT", "ALIAS", "PATTERN"] | None = None
    sort_order: int = 0


@dataclass(frozen=True)
class _CharacterBatchSource:
    source: CharacterStage1Gold | CharacterStage1Prediction
    raw_fact_key: str
    canonical_key_resolution: Literal["EXACT", "ALIAS", "PATTERN"]


@dataclass(frozen=True)
class _WorldTargetSet:
    targets: list[WorkerWorldSettingComparisonTarget]
    state_entries_by_target_id: dict[UUID, list[Any]]


@dataclass(frozen=True)
class _WorldBatchSource:
    source_id: str
    candidate: WorkerWorldSettingCandidatePayload
    target_set: _WorldTargetSet


def create_default_runtime_components(
    *,
    analysis_model: str | None = None,
    subject_resolution_model: str | None = None,
    comparison_model: str | None = None,
) -> RuntimeComponents:
    """운영 extractor/comparator 구현으로 평가 runtime을 구성한다."""

    usage = RuntimeUsageCounter()
    client = UsageRecordingTextGenerationClient(
        OpenAIResponsesClient.from_settings(),
        usage,
    )
    return RuntimeComponents(
        character_extractor=CharacterSettingExtractor(
            llm_client=client,
            model=analysis_model,
        ),
        character_subject_resolver=CharacterSubjectResolver(
            llm_client=client,
            model=subject_resolution_model,
        ),
        world_extractor=WorldSettingExtractor(
            llm_client=client,
            model=analysis_model,
        ),
        world_subject_resolver=WorldSettingSubjectResolver(
            llm_client=client,
            model=subject_resolution_model,
        ),
        character_comparator=CharacterFactComparator(
            llm_client=client,
            model=comparison_model,
        ),
        world_comparator=WorldSettingComparator(
            llm_client=client,
            model=comparison_model,
        ),
        usage=usage,
    )


async def run_multi_stage_predictions(
    gold: GoldSnapshotV3,
    *,
    mode: EvaluationMode,
    components: RuntimeComponents,
    character_schema_hints: tuple[CharacterSettingSchemaHint, ...] = (),
    max_chunks: int | None = None,
    analysis_model: str | None = None,
    subject_resolution_model: str | None = None,
    comparison_model: str | None = None,
    domains: set[EvaluationDomain] | None = None,
    episode_numbers: set[int] | None = None,
    pricing: RuntimePricing | None = None,
) -> PredictionBundleV3:
    """Gold fixture를 운영 LLM 경계에 넣어 ORACLE/FIXED/ROLLING 예측을 생성한다.

    ORACLE은 Gold Stage1과 Gold beforeState만 사용한다. FIXED와 ROLLING은 실제 추출기,
    subject resolver, 저장 직전 dedupe/consolidation을 거친 handoff 후보를 비교기에 넣는다.
    """

    if max_chunks is not None and max_chunks < 1:
        raise ValueError("max_chunks must be at least 1.")
    if gold.fixture_hash is None:
        gold = gold.with_fixture_hash()
    selected_scenarios = [
        scenario
        for scenario in gold.scenarios
        if scenario.scenario_id in gold.evaluation_scenario_ids
        and (episode_numbers is None or scenario.episode_no in episode_numbers)
    ]
    if episode_numbers is not None:
        found = {scenario.episode_no for scenario in selected_scenarios}
        missing = sorted(episode_numbers - found)
        if missing:
            raise ValueError(f"Requested episodes are not evaluation scenarios: {missing}")
    selected_available_domains = {
        domain for scenario in selected_scenarios for domain in scenario.target_domains
    }
    enabled_domains = {
        EvaluationDomain(domain)
        for domain in (domains if domains is not None else selected_available_domains)
    }
    if not enabled_domains:
        raise ValueError("At least one evaluation domain is required.")
    unknown_domains = enabled_domains - selected_available_domains
    if unknown_domains:
        raise ValueError(
            "Requested domains are not enabled by the selected scenarios: "
            + ", ".join(sorted(domain.value for domain in unknown_domains))
        )
    selected_ids = {scenario.scenario_id for scenario in selected_scenarios}
    required_ids = (
        _scenario_dependency_closure(gold, selected_ids)
        if mode == EvaluationMode.ROLLING
        else selected_ids
    )
    if mode != EvaluationMode.ORACLE:
        _require_live_components(
            components,
            character_schema_hints,
            enabled_domains,
        )

    gold_chain = build_gold_state_chain(gold)
    predicted_after: dict[str, EvaluationState] = {}
    scenario_predictions: list[ScenarioPrediction] = []
    for scenario in sorted(gold.scenarios, key=lambda item: (item.episode_no, item.scenario_id)):
        if scenario.scenario_id not in required_ids:
            continue
        if mode == EvaluationMode.ROLLING and scenario.previous_scenario_id:
            runtime_before = predicted_after.get(
                scenario.previous_scenario_id,
                gold_chain[scenario.scenario_id].before_state,
            ).model_copy(deep=True)
        else:
            runtime_before = gold_chain[scenario.scenario_id].before_state.model_copy(deep=True)

        usage_before = components.usage.snapshot() if components.usage is not None else (0, 0, 0)
        if mode == EvaluationMode.ORACLE:
            prediction = await _run_oracle_scenario(
                gold,
                scenario,
                runtime_before,
                components,
                enabled_domains,
                character_schema_hints,
            )
        else:
            prediction = await _run_live_scenario(
                scenario,
                runtime_before,
                mode,
                components,
                character_schema_hints,
                max_chunks,
                enabled_domains,
            )
        usage_after = components.usage.snapshot() if components.usage is not None else usage_before
        scenario_usage = (
            usage_after[0] - usage_before[0],
            usage_after[1] - usage_before[1],
            usage_after[2] - usage_before[2],
        )
        prediction = prediction.model_copy(
            update={
                "input_tokens": scenario_usage[0],
                "cached_input_tokens": scenario_usage[1],
                "output_tokens": scenario_usage[2],
                "estimated_cost_usd": (
                    pricing.estimate(scenario_usage) if pricing is not None else None
                ),
            }
        )
        scenario_predictions.append(prediction)
        predicted_after[scenario.scenario_id] = _apply_runtime_scenario(
            scenario,
            runtime_before,
            prediction,
        )

    return PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode=mode,
        state_application_policy=(
            StateApplicationPolicy.ACCEPT_ALL_PREDICTIONS
            if mode == EvaluationMode.ROLLING
            else StateApplicationPolicy.SCENARIO_LOCAL
        ),
        evaluation_domains=enabled_domains,
        evaluation_scenario_ids=[scenario.scenario_id for scenario in selected_scenarios],
        analysis_model=analysis_model,
        subject_resolution_model=subject_resolution_model,
        comparison_model=comparison_model,
        prompt_versions={
            "characterExtraction": SETTING_EXTRACTION_CACHE_KEY_VERSION,
            "characterSubjectResolution": "subject-resolution:v1",
            "characterComparison": CHARACTER_FACT_COMPARISON_BATCH_CACHE_KEY,
            "worldExtraction": "world-setting-extraction:v2",
            "worldSubjectResolution": "world-setting-subject-resolution:v1",
            "worldComparison": "world-setting-comparison:v7",
        },
        character_schema_hash=(
            _character_schema_hash(character_schema_hints)
            if EvaluationDomain.CHARACTER in enabled_domains
            and character_schema_hints
            else None
        ),
        max_chunks=max_chunks,
        scenarios=scenario_predictions,
    )


async def _run_oracle_scenario(
    gold: GoldSnapshotV3,
    scenario: ScenarioGold,
    before_state: EvaluationState,
    components: RuntimeComponents,
    enabled_domains: set[EvaluationDomain],
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
) -> ScenarioPrediction:
    stage1_gold = [
        row
        for row in gold.stage1
        if row.scenario_id == scenario.scenario_id
        and row.decision == "EXTRACT"
        and row.domain in enabled_domains
    ]
    stage1 = [_prediction_from_gold(row) for row in stage1_gold]
    stage1_by_id = {row.gold_id: row for row in stage1_gold}
    stage2: list[Stage2Prediction] = []
    failures: list[RuntimeFailure] = []
    decisions = sorted(
        [
            row
            for row in gold.stage2
            if row.scenario_id == scenario.scenario_id and row.domain in enabled_domains
        ],
        key=lambda item: (item.sort_order, item.decision_id),
    )
    character_sources: list[_CharacterBatchSource] = []
    world_decisions: list[WorldStage2Gold] = []
    for decision in decisions:
        sources = [stage1_by_id[source_id] for source_id in decision.source_gold_ids]
        if isinstance(decision, CharacterStage2Gold):
            source = cast(CharacterStage1Gold, sources[0])
            assert source.fact_key is not None
            character_sources.append(
                _CharacterBatchSource(
                    source=source,
                    raw_fact_key=source.fact_key,
                    canonical_key_resolution=_oracle_canonical_key_resolution(
                        source,
                        schema_hints,
                    ),
                )
            )
        else:
            world_decisions.append(cast(WorldStage2Gold, decision))

    character_stage2, character_failures = await _run_character_batches(
        scenario,
        before_state,
        character_sources,
        components.character_comparator,
    )
    stage2.extend(character_stage2)
    failures.extend(character_failures)

    world_batch_sources: list[_WorldBatchSource] = []
    for decision in world_decisions:
        try:
            sources = [stage1_by_id[source_id] for source_id in decision.source_gold_ids]
            world_sources = [cast(WorldStage1Gold, source) for source in sources]
            world_batch_sources.append(
                _oracle_world_batch_source(
                    scenario,
                    before_state,
                    world_sources,
                    decision,
                )
            )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:  # 개별 모델 출력 실패는 다음 후보와 격리한다.
            failures.append(_runtime_failure(decision.domain, 2, decision.decision_id, exc))
    world_stage2, world_failures = await _run_world_batches(
        world_batch_sources,
        components.world_comparator,
    )
    stage2.extend(world_stage2)
    failures.extend(world_failures)
    return ScenarioPrediction(
        scenario_id=scenario.scenario_id,
        raw_stage1=stage1,
        stage1=stage1,
        stage2=_sort_stage2_by_stage1(stage1, stage2),
        failures=failures,
    )


async def _run_live_scenario(
    scenario: ScenarioGold,
    before_state: EvaluationState,
    mode: EvaluationMode,
    components: RuntimeComponents,
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
    max_chunks: int | None,
    enabled_domains: set[EvaluationDomain],
) -> ScenarioPrediction:
    if scenario.source_text is None:
        raise ValueError(
            f"Scenario {scenario.scenario_id} has no sourceText; load it with source_root."
        )
    drafts = split_into_chunks(scenario.source_text)
    if max_chunks is not None:
        drafts = drafts[:max_chunks]
    known_characters, character_ref_by_id = _runtime_known_characters(
        before_state,
    )
    raw_stage1: list[Stage1Prediction] = []
    handoff_stage1: list[Stage1Prediction] = []
    stage2: list[Stage2Prediction] = []
    failures: list[RuntimeFailure] = []

    character_records: list[_CharacterRecord] = []
    character_drafts = (
        drafts
        if EvaluationDomain.CHARACTER in scenario.target_domains
        and EvaluationDomain.CHARACTER in enabled_domains
        else []
    )
    for chunk_position, draft in enumerate(character_drafts):
        chunk_id = _stable_uuid(scenario.scenario_id, "character-chunk", str(draft.chunk_index))
        try:
            extraction = await components.character_extractor.extract_from_chunk(
                source_chunk_id=chunk_id,
                chunk_text=draft.chunk_text,
                episode_no=scenario.episode_no,
                episode_title=scenario.episode_title,
                schema_hints=schema_hints,
                known_characters=tuple(known_characters),
            )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:
            failures.append(
                _runtime_failure(EvaluationDomain.CHARACTER, 1, str(draft.chunk_index), exc)
            )
            return _pipeline_failed_prediction(
                scenario.scenario_id,
                raw_stage1,
                failures,
                failed_stage="CHARACTER_STAGE1",
            )

        raw_candidates = extraction.candidates
        candidate_ids = [
            str(
                _stable_uuid(
                    scenario.scenario_id,
                    "character-candidate",
                    str(draft.chunk_index),
                    str(candidate_position),
                )
            )
            for candidate_position in range(len(raw_candidates))
        ]
        candidate_sort_orders = [
            draft.chunk_index * 1_000_000 + candidate_position + 1
            for candidate_position in range(len(raw_candidates))
        ]
        raw_stage1.extend(
            _character_prediction(candidate_id, candidate, sort_order=sort_order)
            for candidate_id, candidate, sort_order in zip(
                candidate_ids,
                raw_candidates,
                candidate_sort_orders,
                strict=True,
            )
        )
        try:
            resolved_offsets = resolve_candidate_evidence_offsets(
                raw_candidates,
                draft.chunk_text,
                draft.start_offset,
            )
            subject_result = await components.character_subject_resolver.resolve_candidates(
                context=_subject_context(character_drafts, chunk_position),
                candidates=resolved_offsets,
                known_characters=known_characters,
            )
            for candidate_id, resolved, sort_order in zip(
                candidate_ids,
                subject_result.candidates,
                candidate_sort_orders,
                strict=True,
            ):
                character_records.append(
                    _CharacterRecord(
                        candidate_id=candidate_id,
                        candidate=resolved,
                        sort_order=sort_order,
                    )
                )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:
            failures.append(
                _runtime_failure(EvaluationDomain.CHARACTER, 1, str(draft.chunk_index), exc)
            )
            return _pipeline_failed_prediction(
                scenario.scenario_id,
                raw_stage1,
                failures,
                failed_stage="CHARACTER_STAGE1",
            )

    canonical_character_records: list[_CharacterRecord] = []
    for record in character_records:
        try:
            candidate, fact_type, resolution = _canonicalize_character_schema(
                record.candidate,
                schema_hints,
            )
            canonical_character_records.append(
                _CharacterRecord(
                    candidate_id=record.candidate_id,
                    candidate=candidate,
                    canonical_fact_type=fact_type,
                    raw_fact_key=record.candidate.attribute_name,
                    canonical_key_resolution=resolution,
                    sort_order=record.sort_order,
                )
            )
        except ValueError as exc:
            failures.append(
                _runtime_failure(
                    EvaluationDomain.CHARACTER,
                    1,
                    record.candidate_id,
                    exc,
                )
            )

    prepared = prepare_setting_candidates(
        [record.candidate for record in canonical_character_records],
        known_characters,
    )
    character_runtime_by_id: dict[
        str,
        tuple[CharacterStage1Prediction, UUID | None, str | None, str | None],
    ] = {}
    for item in prepared:
        record = canonical_character_records[item.source_index]
        matched_id = item.character_match.matched_character_id
        entity_ref = character_ref_by_id.get(matched_id) if matched_id is not None else None
        if item.candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
            entity_ref = f"prediction-character:{_safe_ref_part(item.candidate.entity_name)}"
        matched_name = next(
            (
                character.name
                for character in known_characters
                if character.character_id == matched_id
            ),
            None,
        )
        prediction = _character_prediction(
            record.candidate_id,
            item.candidate,
            entity_ref=entity_ref,
            matched_character_name=matched_name,
            match_status=item.character_match.match_status.value,
            canonical_fact_type=record.canonical_fact_type,
            sort_order=record.sort_order,
        )
        handoff_stage1.append(prediction)
        character_runtime_by_id[prediction.candidate_id] = (
            prediction,
            matched_id,
            record.raw_fact_key,
            record.canonical_key_resolution,
        )

    character_batch_sources: list[_CharacterBatchSource] = []
    for prediction, matched_id, raw_fact_key, resolution in character_runtime_by_id.values():
        if (
            prediction.candidate_kind != CandidateKind.SETTING
            or matched_id is None
            or prediction.entity_ref is None
            or prediction.fact_type is None
            or prediction.fact_key is None
            or raw_fact_key is None
            or resolution is None
        ):
            continue
        character_batch_sources.append(
            _CharacterBatchSource(
                source=prediction,
                raw_fact_key=raw_fact_key,
                canonical_key_resolution=cast(
                    Literal["EXACT", "ALIAS", "PATTERN"],
                    resolution,
                ),
            )
        )

    character_stage2, character_failures = await _run_character_batches(
        scenario,
        before_state,
        character_batch_sources,
        components.character_comparator,
    )
    stage2.extend(character_stage2)
    failures.extend(character_failures)

    world_publish_items = []
    world_drafts = (
        drafts
        if EvaluationDomain.WORLD in scenario.target_domains
        and EvaluationDomain.WORLD in enabled_domains
        else []
    )
    for draft in world_drafts:
        try:
            extraction = await components.world_extractor.extract_from_chunk(
                chunk_text=draft.chunk_text,
                episode_no=scenario.episode_no,
                episode_title=scenario.episode_title,
            )
            chunk = cast(EpisodeChunk, _chunk_view(scenario, draft))
            for candidate_position, candidate in enumerate(extraction.candidates):
                candidate_id = str(
                    _stable_uuid(
                        scenario.scenario_id,
                        "world-raw-candidate",
                        str(draft.chunk_index),
                        str(candidate_position),
                    )
                )
                raw_stage1.append(
                    WorldStage1Prediction(
                        candidate_id=candidate_id,
                        sort_order=draft.chunk_index * 1_000_000 + candidate_position + 1,
                        domain="WORLD",
                        category=candidate.category,
                        subject_name=candidate.subject_name,
                        scope_name=candidate.scope_name,
                        setting_name=candidate.setting_name,
                        source_values=[candidate.extracted_value],
                        evidence_spans=_prediction_evidence(candidate.evidence_spans),
                        confidence=candidate.confidence,
                    )
                )
                world_publish_items.append(
                    WorldSettingCandidateMapper.to_publish_item(candidate, chunk)
                )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:
            failures.append(
                _runtime_failure(EvaluationDomain.WORLD, 1, str(draft.chunk_index), exc)
            )
            return _pipeline_failed_prediction(
                scenario.scenario_id,
                raw_stage1,
                failures,
                failed_stage="WORLD_STAGE1",
                stage1=handoff_stage1,
                stage2=stage2,
            )

    try:
        consolidated = WorldSettingCandidateMapper.consolidate_by_key(world_publish_items)
    except Exception as exc:
        failures.append(_runtime_failure(EvaluationDomain.WORLD, 1, None, exc))
        return _pipeline_failed_prediction(
            scenario.scenario_id,
            raw_stage1,
            failures,
            failed_stage="WORLD_STAGE1",
            stage1=handoff_stage1,
            stage2=stage2,
        )
    world_batch_sources: list[_WorldBatchSource] = []
    for consolidated_order, item in enumerate(consolidated, start=1):
        source_values = _publish_source_values(item)
        candidate_id = str(
            _stable_uuid(
                scenario.scenario_id,
                "world-candidate",
                *world_path_key(
                    item.category,
                    item.subject_name,
                    item.scope_name,
                    item.setting_name,
                ),
                *sorted(normalize_world_setting_name(value) for value in source_values),
            )
        )
        prediction = WorldStage1Prediction(
            candidate_id=candidate_id,
            sort_order=consolidated_order,
            domain="WORLD",
            category=item.category,
            subject_name=item.subject_name,
            scope_name=item.scope_name,
            setting_name=item.setting_name,
            source_values=source_values,
            evidence_spans=_prediction_evidence(item.evidence_spans),
            confidence=item.extraction_confidence,
        )
        handoff_stage1.append(prediction)
        try:
            world_batch_sources.append(
                await _live_world_batch_source(
                    scenario,
                    before_state,
                    prediction,
                    components.world_subject_resolver,
                )
            )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:
            failures.append(
                _runtime_failure(EvaluationDomain.WORLD, 2, prediction.candidate_id, exc)
            )

    world_stage2, world_failures = await _run_world_batches(
        world_batch_sources,
        components.world_comparator,
    )
    stage2.extend(world_stage2)
    failures.extend(world_failures)

    return ScenarioPrediction(
        scenario_id=scenario.scenario_id,
        raw_stage1=raw_stage1,
        stage1=handoff_stage1,
        stage2=_sort_stage2_by_stage1(handoff_stage1, stage2),
        failures=failures,
    )


async def _run_character_batches(
    scenario: ScenarioGold,
    state: EvaluationState,
    sources: list[_CharacterBatchSource],
    comparator: CharacterFactBatchComparator,
) -> tuple[list[CharacterStage2Prediction], list[RuntimeFailure]]:
    """Run one production-equivalent batch per character and FactType in this episode."""

    grouped: dict[tuple[str, CharacterFactType], list[_CharacterBatchSource]] = {}
    for item in sorted(sources, key=_character_batch_source_sort_key):
        source = item.source
        if source.entity_ref is None or source.fact_type is None or source.fact_key is None:
            continue
        grouped.setdefault((source.entity_ref, source.fact_type), []).append(item)

    predictions: list[CharacterStage2Prediction] = []
    failures: list[RuntimeFailure] = []
    for (entity_ref, fact_type), group in grouped.items():
        candidates = [
            _worker_character_batch_candidate(scenario, item, index)
            for index, item in enumerate(group, start=1)
        ]
        snapshots, stable_ref_by_request_ref = _character_batch_snapshots(
            state,
            entity_ref,
            fact_type,
            [candidate.initial_canonical_fact_key for candidate in candidates],
        )
        source_by_candidate_ref = {
            candidate.candidate_ref: item
            for candidate, item in zip(candidates, group, strict=True)
        }
        candidate_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
        try:
            execution = await execute_character_fact_comparison_batch(
                comparator,
                matched_character_name=_character_batch_matched_name(group[0].source),
                canonical_fact_type=fact_type,
                candidates=candidates,
                snapshot_entries=snapshots,
            )
            group_predictions: list[CharacterStage2Prediction] = []
            for decision in execution.decisions:
                item = source_by_candidate_ref[decision.candidate_ref]
                group_predictions.append(
                    _character_batch_stage2_prediction(
                        _character_batch_source_id(item.source),
                        decision,
                        stable_ref_by_request_ref,
                    )
                )
                if decision.operation in {
                    CharacterFactComparisonOperation.ADD,
                    CharacterFactComparisonOperation.UPDATE,
                    CharacterFactComparisonOperation.MERGE,
                }:
                    projected_ref = candidate_by_ref[
                        decision.candidate_ref
                    ].projected_snapshot_ref
                    stable_ref_by_request_ref[projected_ref] = character_state_ref(
                        entity_ref,
                        fact_type,
                        decision.resolved_canonical_fact_key,
                    )
            predictions.extend(group_predictions)
            for failure in execution.failures:
                item = source_by_candidate_ref[failure.candidate_ref]
                failures.append(
                    RuntimeFailure(
                        stage="CHARACTER_STAGE2",
                        source_id=_character_batch_source_id(item.source),
                        error_type=failure.failure_code.value,
                        message=failure.error_message,
                    )
                )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:
            failures.extend(
                _runtime_failure(
                    EvaluationDomain.CHARACTER,
                    2,
                    _character_batch_source_id(item.source),
                    exc,
                )
                for item in group
            )
    return predictions, failures


def _worker_character_batch_candidate(
    scenario: ScenarioGold,
    item: _CharacterBatchSource,
    index: int,
) -> WorkerCharacterFactComparisonBatchCandidate:
    source = item.source
    assert source.fact_key is not None and source.value_type is not None
    evidence = (
        _worker_evidence_from_quotes(source.evidence_quotes)
        if isinstance(source, CharacterStage1Gold)
        else _worker_evidence(source.evidence_spans)
    )
    return WorkerCharacterFactComparisonBatchCandidate(
        candidate_ref=f"C{index}",
        projected_snapshot_ref=f"Q{index}",
        source_episode_no=scenario.episode_no,
        attribute_value=source.display_value,
        value_json=source.value_json,
        value_type=source.value_type,
        evidence_spans=evidence,
        raw_fact_key=item.raw_fact_key,
        initial_canonical_fact_key=source.fact_key,
        canonical_key_resolution=item.canonical_key_resolution,
        confidence=(
            None if isinstance(source, CharacterStage1Gold) else source.confidence
        ),
    )


def _character_batch_source_sort_key(
    item: _CharacterBatchSource,
) -> tuple[int, int, int, str]:
    return _character_source_chronology_key(item.source)


def _character_source_chronology_key(
    source: CharacterStage1Gold | CharacterStage1Prediction,
) -> tuple[int, int, int, str]:
    """Mirror production's evidence-first chronology with deterministic fallbacks."""

    source_id = _character_batch_source_id(source)
    if isinstance(source, CharacterStage1Prediction):
        offsets = [
            span.start_offset
            for span in source.evidence_spans
            if span.start_offset is not None
        ]
        if offsets:
            return 0, min(offsets), source.sort_order, source_id
    return 1, 0, source.sort_order, source_id


def _character_batch_source_id(
    source: CharacterStage1Gold | CharacterStage1Prediction,
) -> str:
    return source.gold_id if isinstance(source, CharacterStage1Gold) else source.candidate_id


def _character_batch_matched_name(
    source: CharacterStage1Gold | CharacterStage1Prediction,
) -> str:
    if isinstance(source, CharacterStage1Prediction):
        return source.matched_character_name or source.entity_name
    return source.entity_name


def _oracle_world_batch_source(
    scenario: ScenarioGold,
    state: EvaluationState,
    sources: list[WorldStage1Gold],
    decision: WorldStage2Gold,
) -> _WorldBatchSource:
    primary = sources[0]
    source_values = _unique_values(value for source in sources for value in source.source_values)
    evidence = [quote for source in sources for quote in source.evidence_quotes]
    candidate_id = _stable_uuid(scenario.scenario_id, "oracle-world", decision.decision_id)
    runtime_candidate = WorkerWorldSettingCandidatePayload(
        candidate_id=candidate_id,
        work_id=_stable_uuid("work", gold_version_key(scenario)),
        source_episode_id=_stable_uuid("episode", scenario.scenario_id),
        category=primary.category,
        subject_name=primary.subject_name,
        scope_name=primary.scope_name,
        setting_name=primary.setting_name,
        extracted_value="\n".join(source_values),
        evidence_spans=_worker_evidence_from_quotes(evidence),
    )
    target_set = _oracle_world_targets(state, primary, decision)
    return _WorldBatchSource(
        source_id=primary.gold_id,
        candidate=runtime_candidate,
        target_set=target_set,
    )


async def _live_world_batch_source(
    scenario: ScenarioGold,
    state: EvaluationState,
    source: WorldStage1Prediction,
    subject_resolver: WorldSettingSubjectResolver | Any | None,
) -> _WorldBatchSource:
    runtime_candidate = WorkerWorldSettingCandidatePayload(
        candidate_id=UUID(source.candidate_id),
        work_id=_stable_uuid("work", gold_version_key(scenario)),
        source_episode_id=_stable_uuid("episode", scenario.scenario_id),
        category=source.category,
        subject_name=source.subject_name,
        scope_name=source.scope_name,
        setting_name=source.setting_name,
        extracted_value="\n".join(source.source_values),
        evidence_spans=_worker_evidence(source.evidence_spans),
        extraction_confidence=source.confidence,
    )
    target_set = await _live_world_targets(state, runtime_candidate, subject_resolver)
    return _WorldBatchSource(
        source_id=source.candidate_id,
        candidate=runtime_candidate,
        target_set=target_set,
    )


async def _run_world_batches(
    sources: list[_WorldBatchSource],
    comparator: WorldComparatorApi,
) -> tuple[list[WorldStage2Prediction], list[RuntimeFailure]]:
    grouped: dict[tuple[Any, ...], list[_WorldBatchSource]] = {}
    for source in sources:
        grouped.setdefault(_world_batch_key(source), []).append(source)

    predictions: list[WorldStage2Prediction] = []
    failures: list[RuntimeFailure] = []
    for group in grouped.values():
        candidates = [
            WorkerWorldSettingComparisonBatchCandidate(
                candidate_ref=f"C{index}",
                candidate_id=item.candidate.candidate_id,
                subject_name=item.candidate.subject_name,
                scope_name=item.candidate.scope_name,
                setting_name=item.candidate.setting_name,
                extracted_value=item.candidate.extracted_value,
                evidence_spans=item.candidate.evidence_spans,
                extraction_confidence=item.candidate.extraction_confidence,
            )
            for index, item in enumerate(group, start=1)
        ]
        source_by_ref = {
            candidate.candidate_ref: item
            for candidate, item in zip(candidates, group, strict=True)
        }
        target_set = group[0].target_set
        try:
            result, _ = await comparator.compare_batch(
                group[0].candidate.category.value,
                candidates,
                target_set.targets,
            )
            for decision in result.decisions:
                first_ref = min(
                    decision.source_candidate_refs,
                    key=lambda reference: int(reference[1:]),
                )
                predictions.append(
                    _world_stage2_prediction(
                        source_by_ref[first_ref].source_id,
                        decision,
                        target_set,
                    )
                )
        except (httpx.HTTPError, AiTokenQuotaExhaustedError, LlmIncompleteResponseError):
            raise
        except Exception as exc:
            failures.extend(
                _runtime_failure(EvaluationDomain.WORLD, 2, item.source_id, exc)
                for item in group
            )
    return _link_projected_world_subject_adds(sources, predictions), failures


def _link_projected_world_subject_adds(
    sources: list[_WorldBatchSource],
    predictions: list[WorldStage2Prediction],
) -> list[WorldStage2Prediction]:
    """Link later ADDs to a subject created earlier in the same scenario.

    Production completes a world comparison batch atomically.  The evaluator applies
    its decisions one by one, so after the first property creates a new subject, each
    later property must carry that projected subject ref.  The comparator cannot emit
    the ref because the subject did not exist in its before-state context.
    """

    source_by_id = {source.source_id: source for source in sources}
    source_order = {source.source_id: index for index, source in enumerate(sources)}
    ordered = sorted(
        predictions,
        key=lambda prediction: source_order.get(
            prediction.source_candidate_id,
            len(source_order),
        ),
    )
    projected_subject_refs: dict[tuple[str, str], str] = {}
    linked: list[WorldStage2Prediction] = []
    for prediction in ordered:
        source = source_by_id.get(prediction.source_candidate_id)
        if source is None:
            linked.append(prediction)
            continue
        subject_key = (
            source.candidate.category.value,
            normalize_world_setting_name(source.candidate.subject_name),
        )
        if (
            prediction.operation == WorldSettingOperation.ADD
            and prediction.consolidation_status != WorldSettingConsolidationStatus.CONFLICT
        ):
            projected_ref = projected_subject_refs.get(subject_key)
            if prediction.target_ref is None and projected_ref is not None:
                prediction = prediction.model_copy(
                    update={"target_ref": projected_ref},
                )
            elif prediction.target_ref is None:
                projected_subject_refs[subject_key] = world_subject_ref(
                    source.candidate.category,
                    source.candidate.subject_name,
                )
        linked.append(prediction)
    return linked


def _world_batch_key(source: _WorldBatchSource) -> tuple[Any, ...]:
    target_ids = tuple(
        sorted(str(target.world_setting_id) for target in source.target_set.targets)
    )
    canonical_subject = (
        source.target_set.targets[0].subject_name
        if len(source.target_set.targets) == 1
        else source.candidate.subject_name
    )
    return (
        source.candidate.category,
        normalize_world_setting_name(canonical_subject),
        normalize_world_setting_name(source.candidate.scope_name or ""),
        target_ids,
    )


def _character_batch_stage2_prediction(
    source_id: str,
    decision: WorkerCharacterFactComparisonBatchDecision,
    state_ref_by_request_ref: dict[str, str],
) -> CharacterStage2Prediction:
    return CharacterStage2Prediction(
        source_candidate_id=source_id,
        domain="CHARACTER",
        operation=decision.operation,
        resolved_canonical_fact_key=decision.resolved_canonical_fact_key,
        target_ref=(
            None
            if decision.target_snapshot_ref is None
            else state_ref_by_request_ref[decision.target_snapshot_ref]
        ),
        removed_snapshot_refs=[
            state_ref_by_request_ref[reference] for reference in decision.removed_snapshot_refs
        ],
        proposed_value=decision.proposed_fact_value,
        proposed_value_json=decision.proposed_value_json,
        temporal_scope=decision.temporal_scope,
        comparison_reason=decision.comparison_reason,
    )


def _world_stage2_prediction(
    source_id: str,
    decision: Any,
    target_set: _WorldTargetSet,
) -> WorldStage2Prediction:
    stable_target = None
    if decision.target_ref is not None:
        index = int(decision.target_ref[1:]) - 1
        selected = target_set.targets[index]
        entries = target_set.state_entries_by_target_id[selected.world_setting_id]
        matched = next(
            (
                item
                for item in entries
                if normalize_world_setting_name(item.scope_name or "")
                == normalize_world_setting_name(decision.matched_scope_name or "")
                and normalize_world_setting_name(item.setting_name)
                == normalize_world_setting_name(decision.matched_property_name or "")
            ),
            None,
        )
        stable_target = (
            matched.ref
            if matched is not None
            else world_subject_ref(entries[0].category, selected.subject_name)
        )
    return WorldStage2Prediction(
        source_candidate_id=source_id,
        domain="WORLD",
        consolidation_status=decision.consolidation_status,
        operation=decision.operation,
        target_ref=stable_target,
        matched_scope_name=decision.matched_scope_name,
        matched_property_name=decision.matched_property_name,
        proposed_scope_name=decision.proposed_scope_name,
        proposed_setting_name=decision.proposed_setting_name,
        proposed_value=decision.proposed_value,
        comparison_reason=decision.comparison_reason,
    )


def _prediction_from_gold(
    source: CharacterStage1Gold | WorldStage1Gold,
) -> CharacterStage1Prediction | WorldStage1Prediction:
    if isinstance(source, CharacterStage1Gold):
        return CharacterStage1Prediction(
            candidate_id=source.gold_id,
            sort_order=source.sort_order,
            domain="CHARACTER",
            candidate_kind=source.candidate_kind,
            entity_ref=source.entity_ref,
            entity_name=source.entity_name,
            matched_character_name=source.entity_name,
            match_status="ORACLE",
            raw_entity_mention=source.raw_entity_mention,
            fact_type=source.fact_type,
            fact_key=source.fact_key,
            value_type=source.value_type,
            display_value=source.display_value,
            value_json=source.value_json,
            evidence_spans=[PredictionEvidence(quote=quote) for quote in source.evidence_quotes],
        )
    return WorldStage1Prediction(
        candidate_id=source.gold_id,
        sort_order=source.sort_order,
        domain="WORLD",
        category=source.category,
        subject_name=source.subject_name,
        scope_name=source.scope_name,
        setting_name=source.setting_name,
        source_values=source.source_values,
        evidence_spans=[PredictionEvidence(quote=quote) for quote in source.evidence_quotes],
    )


def _canonicalize_character_schema(
    candidate: ExtractedSettingCandidate,
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
) -> tuple[
    ExtractedSettingCandidate,
    CharacterFactType | None,
    Literal["EXACT", "ALIAS", "PATTERN"] | None,
]:
    """Mirror Spring's schema resolver before a candidate reaches comparison."""

    if candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
        return candidate, None, None
    assert candidate.attribute_name is not None and candidate.value_type is not None
    attribute_name = candidate.attribute_name.strip()

    exact = [hint for hint in schema_hints if hint.schema_key.strip() == attribute_name]
    matches = exact
    canonical_key_resolution: Literal["EXACT", "ALIAS", "PATTERN"] = "EXACT"
    preserve_attribute_name = False
    if not matches:
        matches = [
            hint for hint in schema_hints if _character_schema_alias_matches(hint, attribute_name)
        ]
        canonical_key_resolution = "ALIAS"
    if not matches:
        matches = [
            hint
            for hint in schema_hints
            if _character_schema_pattern_matches(hint.attribute_pattern, attribute_name)
        ]
        preserve_attribute_name = bool(matches)
        canonical_key_resolution = "PATTERN"
    if len(matches) != 1:
        reason = "ambiguous" if matches else "not matched"
        raise ValueError(f"Character setting schema is {reason}.")
    matched = matches[0]
    if candidate.value_type != matched.value_type:
        raise ValueError("Character setting candidate valueType differs from its schema.")
    canonical_key = attribute_name if preserve_attribute_name else matched.schema_key.strip()
    fact_type = (
        CharacterFactType(matched.canonical_fact_type)
        if matched.canonical_fact_type is not None
        else infer_character_fact_type(matched.schema_key)
    )
    if fact_type is None:
        raise ValueError("Character setting schema has no supported canonical factType.")
    return (
        candidate.model_copy(update={"attribute_name": canonical_key}),
        fact_type,
        canonical_key_resolution,
    )


def _character_schema_alias_matches(
    schema: CharacterSettingSchemaHint,
    attribute_name: str,
) -> bool:
    schema_key = schema.schema_key.strip()
    separator = schema_key.rfind(".")
    namespace = "" if separator < 0 else schema_key[: separator + 1]
    return any(
        alias.strip()
        and "." not in alias.strip()
        and attribute_name in {alias.strip(), namespace + alias.strip()}
        for alias in schema.aliases
    )


def _character_schema_pattern_matches(
    attribute_pattern: str | None,
    attribute_name: str,
) -> bool:
    if attribute_pattern is None:
        return False
    pattern = attribute_pattern.strip()
    wildcard_index = pattern.find("*")
    if not pattern.endswith(".*") or wildcard_index != len(pattern) - 1:
        return False
    prefix = pattern[:wildcard_index]
    return attribute_name.startswith(prefix) and len(attribute_name) > len(prefix)


def _character_prediction(
    candidate_id: str,
    candidate: ExtractedSettingCandidate,
    *,
    entity_ref: str | None = None,
    matched_character_name: str | None = None,
    match_status: str | None = None,
    canonical_fact_type: CharacterFactType | None = None,
    sort_order: int = 0,
) -> CharacterStage1Prediction:
    display_value = (
        normalize_setting_display_value(
            candidate.value_type,
            candidate.value_json,
            candidate.attribute_value,
        )
        if candidate.candidate_kind == SettingCandidateKind.SETTING
        else None
    )
    return CharacterStage1Prediction(
        candidate_id=candidate_id,
        sort_order=sort_order,
        domain="CHARACTER",
        candidate_kind=candidate.candidate_kind,
        entity_ref=entity_ref,
        entity_name=candidate.entity_name,
        matched_character_name=matched_character_name,
        match_status=match_status,
        raw_entity_mention=candidate.raw_entity_mention,
        fact_type=(
            canonical_fact_type or infer_character_fact_type(candidate.attribute_name)
            if candidate.attribute_name is not None
            else None
        ),
        fact_key=candidate.attribute_name,
        value_type=candidate.value_type,
        display_value=display_value,
        value_json=candidate.value_json,
        evidence_spans=_prediction_evidence(candidate.evidence_spans),
        confidence=candidate.confidence,
    )


def _character_batch_snapshots(
    state: EvaluationState,
    entity_ref: str,
    fact_type: CharacterFactType,
    initial_fact_keys: list[str],
) -> tuple[list[WorkerCharacterFactComparisonBatchSnapshotEntry], dict[str, str]]:
    """Mirror Java's exact-slot-first, then factKey ordered 30-entry selection."""

    matching = [
        item
        for item in state.character_facts
        if item.entity_ref == entity_ref and item.fact_type == fact_type
    ]
    entry_by_key = {item.fact_key: item for item in matching}
    entries = []
    selected_refs: set[str] = set()
    for fact_key in dict.fromkeys(initial_fact_keys):
        entry = entry_by_key.get(fact_key)
        if entry is not None and len(entries) < MAX_CHARACTER_CONTEXT_ENTRIES:
            entries.append(entry)
            selected_refs.add(entry.ref)
    entries.extend(
        item
        for item in sorted(matching, key=lambda value: (value.fact_key, value.ref))
        if item.ref not in selected_refs
    )
    entries = entries[:MAX_CHARACTER_CONTEXT_ENTRIES]
    return (
        [
            WorkerCharacterFactComparisonBatchSnapshotEntry(
                snapshot_ref=f"P{index}",
                origin="PERSISTED",
                fact_type=item.fact_type,
                fact_key=item.fact_key,
                fact_value=item.value,
                value_json=item.value_json,
            )
            for index, item in enumerate(entries, start=1)
        ],
        {f"P{index}": item.ref for index, item in enumerate(entries, start=1)},
    )


def _oracle_canonical_key_resolution(
    source: CharacterStage1Gold,
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
) -> Literal["EXACT", "PATTERN"]:
    assert source.fact_key is not None
    if any(hint.schema_key.strip() == source.fact_key for hint in schema_hints):
        return "EXACT"
    if any(
        _character_schema_pattern_matches(hint.attribute_pattern, source.fact_key)
        for hint in schema_hints
    ):
        return "PATTERN"
    # ORACLE historically requires no schema fixture. Standard dynamic STATUS keys
    # therefore retain the production pattern-key normalization capability.
    return "PATTERN" if source.fact_type == "STATUS" else "EXACT"


def _sort_stage2_by_stage1(
    stage1: list[Stage1Prediction],
    stage2: list[Stage2Prediction],
) -> list[Stage2Prediction]:
    order = {
        item.candidate_id: (
            _character_source_chronology_key(item)
            if isinstance(item, CharacterStage1Prediction)
            else (1, 0, item.sort_order, item.candidate_id)
        )
        for item in stage1
    }
    return sorted(
        stage2,
        key=lambda item: order.get(
            item.source_candidate_id,
            (2, 0, 2**63 - 1, item.source_candidate_id),
        ),
    )


def _oracle_world_targets(
    state: EvaluationState,
    source: WorldStage1Gold,
    decision: WorldStage2Gold,
) -> _WorldTargetSet:
    entries = [item for item in state.world_facts if item.category == source.category]
    if decision.target_ref is not None:
        target = next((item for item in entries if item.ref == decision.target_ref), None)
        if target is None:
            subject_entries = [
                item
                for item in entries
                if world_subject_ref(item.category, item.subject_name) == decision.target_ref
            ]
            if not subject_entries:
                raise ValueError(f"Oracle target does not exist: {decision.target_ref}")
            target = subject_entries[0]
        entries = [
            item
            for item in entries
            if normalize_world_setting_name(item.subject_name)
            == normalize_world_setting_name(target.subject_name)
        ]
    else:
        exact = [
            item
            for item in entries
            if normalize_world_setting_name(item.subject_name)
            == normalize_world_setting_name(source.subject_name)
        ]
        entries = exact
    return _world_target_set(entries)


async def _live_world_targets(
    state: EvaluationState,
    candidate: WorkerWorldSettingCandidatePayload,
    subject_resolver: WorldSettingSubjectResolver | Any | None,
) -> _WorldTargetSet:
    category_entries = [item for item in state.world_facts if item.category == candidate.category]
    exact = [
        item
        for item in category_entries
        if normalize_world_setting_name(item.subject_name)
        == normalize_world_setting_name(candidate.subject_name)
    ]
    if exact:
        return _world_target_set(exact)
    if not category_entries or subject_resolver is None:
        return _world_target_set([])
    subject_entries = _group_world_entries(category_entries)
    subjects = [
        WorkerWorldSettingSubject(
            world_setting_id=_world_subject_id(candidate.category, name),
            subject_name=name,
        )
        for name in subject_entries
    ]
    selected = await subject_resolver.select_subjects(candidate, subjects)
    selected_ids = {item.world_setting_id for item in selected}
    chosen = [
        entry
        for name, entries in subject_entries.items()
        if _world_subject_id(candidate.category, name) in selected_ids
        for entry in entries
    ]
    return _world_target_set(chosen)


def _world_target_set(entries: list[Any]) -> _WorldTargetSet:
    grouped = _group_world_entries(entries)
    targets: list[WorkerWorldSettingComparisonTarget] = []
    entries_by_target: dict[UUID, list[Any]] = {}
    for subject_name in sorted(grouped, key=normalize_world_setting_name):
        subject_entries = sorted(
            grouped[subject_name],
            key=lambda item: (
                normalize_world_setting_name(item.scope_name or ""),
                normalize_world_setting_name(item.setting_name),
            ),
        )
        target_id = _world_subject_id(subject_entries[0].category, subject_name)
        targets.append(
            WorkerWorldSettingComparisonTarget(
                world_setting_id=target_id,
                subject_name=subject_name,
                properties=[
                    WorkerWorldSettingProperty(
                        scope_name=item.scope_name,
                        setting_name=item.setting_name,
                        value=item.value,
                    )
                    for item in subject_entries
                ],
                version=0,
            )
        )
        entries_by_target[target_id] = subject_entries
    return _WorldTargetSet(targets, entries_by_target)


def _group_world_entries(entries: list[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    normalized_to_name: dict[str, str] = {}
    for entry in entries:
        normalized = normalize_world_setting_name(entry.subject_name)
        canonical_name = normalized_to_name.setdefault(normalized, entry.subject_name)
        grouped.setdefault(canonical_name, []).append(entry)
    return grouped


def _apply_runtime_scenario(
    scenario: ScenarioGold,
    before_state: EvaluationState,
    prediction: ScenarioPrediction,
) -> EvaluationState:
    state = before_state
    stage1_by_id = {item.candidate_id: item for item in prediction.stage1}
    for decision in prediction.stage2:
        source = stage1_by_id.get(decision.source_candidate_id)
        if source is None:
            continue
        try:
            state, _ = apply_prediction_decision(state, scenario, source, decision)
        except StateApplicationError:
            # evaluator가 같은 오류를 STATE_APPLICATION_ERROR로 보고하므로 runtime은 다음
            # 후보와 회차 진행을 계속한다.
            continue
    known_by_ref = {item.entity_ref: item for item in state.known_characters}
    for source in prediction.stage1:
        if not isinstance(source, CharacterStage1Prediction):
            continue
        if source.candidate_kind != CandidateKind.CHARACTER_DISCOVERY or source.entity_ref is None:
            continue
        known_by_ref.setdefault(
            source.entity_ref,
            KnownCharacter(
                entity_ref=source.entity_ref,
                name=source.entity_name,
                creation_order=scenario.episode_no * 1_000_000 + source.sort_order,
            ),
        )
    for fact in state.character_facts:
        known_by_ref.setdefault(
            fact.entity_ref,
            KnownCharacter(
                entity_ref=fact.entity_ref,
                name=fact.entity_name,
                creation_order=(
                    None
                    if fact.source_episode_no is None or fact.source_sort_order is None
                    else fact.source_episode_no * 1_000_000 + fact.source_sort_order
                ),
            ),
        )
    return state.model_copy(update={"known_characters": list(known_by_ref.values())}).canonical()


def _runtime_known_characters(
    state: EvaluationState,
) -> tuple[list[RuntimeKnownCharacter], dict[UUID, str]]:
    characters: list[RuntimeKnownCharacter] = []
    ref_by_id: dict[UUID, str] = {}
    for state_character in known_characters_for_runtime(state):
        character_id = _stable_uuid("character", state_character.entity_ref)
        active_statuses = tuple(
            ActiveCharacterStatus(fact_key=fact.fact_key, fact_value=fact.value)
            for fact in sorted(
                (
                    item
                    for item in state.character_facts
                    if item.entity_ref == state_character.entity_ref
                    and item.fact_type == "STATUS"
                    and not is_explicit_inactive_status(item.fact_type, item.value_json)
                ),
                key=lambda item: (item.fact_key, item.ref),
            )
        )
        characters.append(
            RuntimeKnownCharacter(
                character_id=character_id,
                name=state_character.name,
                active_statuses=active_statuses,
            )
        )
        ref_by_id[character_id] = state_character.entity_ref
    return characters, ref_by_id


def _subject_context(
    drafts: list[EpisodeChunkDraft],
    position: int,
) -> SubjectResolutionChunkContext:
    return SubjectResolutionChunkContext(
        previous_chunk_text=drafts[position - 1].chunk_text if position > 0 else None,
        current_chunk_text=drafts[position].chunk_text,
        next_chunk_text=(drafts[position + 1].chunk_text if position + 1 < len(drafts) else None),
    )


def _chunk_view(scenario: ScenarioGold, draft: EpisodeChunkDraft) -> SimpleNamespace:
    return SimpleNamespace(
        id=_stable_uuid(scenario.scenario_id, "world-chunk", str(draft.chunk_index)),
        episode_id=_stable_uuid("episode", scenario.scenario_id),
        chunk_index=draft.chunk_index,
        chunk_text=draft.chunk_text,
        start_offset=draft.start_offset,
        end_offset=draft.end_offset,
        paragraph_start_index=draft.paragraph_start_index,
        paragraph_end_index=draft.paragraph_end_index,
    )


def _prediction_evidence(spans: list[Any]) -> list[PredictionEvidence]:
    return [
        PredictionEvidence(
            quote=span.quote,
            start_offset=span.start_offset,
            end_offset=span.end_offset,
        )
        for span in spans
    ]


def _worker_evidence(spans: list[PredictionEvidence]) -> list[WorkerEvidenceSpan]:
    return [
        WorkerEvidenceSpan(
            quote=span.quote,
            start_offset=span.start_offset,
            end_offset=span.end_offset,
        )
        for span in spans
    ]


def _worker_evidence_from_quotes(quotes: list[str]) -> list[WorkerEvidenceSpan]:
    return [WorkerEvidenceSpan(quote=quote) for quote in quotes]


def _publish_source_values(item: Any) -> list[str]:
    raw = item.raw_extraction_json
    if isinstance(raw, dict) and isinstance(raw.get("sourceValues"), list):
        values = [str(value).strip() for value in raw["sourceValues"] if str(value).strip()]
        if values:
            return values
    return [item.extracted_value]


def _unique_values(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        normalized = normalize_world_setting_name(value)
        if value and normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result


def _world_subject_id(category: Any, subject_name: str) -> UUID:
    return _stable_uuid(
        "world-subject",
        str(category),
        normalize_world_setting_name(subject_name),
    )


def _stable_uuid(*parts: str) -> UUID:
    return uuid5(RUNTIME_UUID_NAMESPACE, "\x1f".join(parts))


def _character_schema_hash(
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
) -> str:
    payload = [
        {
            "schemaKey": hint.schema_key,
            "displayName": hint.display_name,
            "attributePattern": hint.attribute_pattern,
            "aliases": sorted(hint.aliases),
            "valueType": hint.value_type,
            "canonicalFactType": hint.canonical_fact_type,
        }
        for hint in sorted(
            schema_hints,
            key=lambda item: (
                item.schema_key,
                item.display_name,
                item.attribute_pattern or "",
                tuple(sorted(item.aliases)),
                item.value_type,
                item.canonical_fact_type or "",
            ),
        )
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def gold_version_key(scenario: ScenarioGold) -> str:
    return f"{scenario.gold_version}:{scenario.scenario_id}"


def _safe_ref_part(value: str) -> str:
    return "-".join(value.strip().casefold().split()) or "unknown"


def _pipeline_failed_prediction(
    scenario_id: str,
    raw_stage1: list[Stage1Prediction],
    failures: list[RuntimeFailure],
    *,
    failed_stage: Literal["CHARACTER_STAGE1", "WORLD_STAGE1"],
    stage1: list[Stage1Prediction] | None = None,
    stage2: list[Stage2Prediction] | None = None,
) -> ScenarioPrediction:
    """Keep diagnostic raw outputs while exposing the production handoff boundary."""

    return ScenarioPrediction(
        scenario_id=scenario_id,
        pipeline_status=ScenarioPipelineStatus.PIPELINE_FAILED,
        failed_stage=failed_stage,
        raw_stage1=raw_stage1,
        stage1=[] if stage1 is None else stage1,
        stage2=[] if stage2 is None else stage2,
        failures=failures,
    )


def _runtime_failure(
    domain: EvaluationDomain,
    stage: int,
    source_id: str | None,
    exc: Exception,
) -> RuntimeFailure:
    return RuntimeFailure(
        stage=f"{domain.value}_STAGE{stage}",
        source_id=source_id,
        error_type=exc.__class__.__name__,
        message=(str(exc) or exc.__class__.__name__)[:500],
    )


def _require_live_components(
    components: RuntimeComponents,
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
    enabled_domains: set[EvaluationDomain],
) -> None:
    required = []
    if EvaluationDomain.CHARACTER in enabled_domains:
        required.extend(
            (
                ("character_extractor", components.character_extractor),
                ("character_subject_resolver", components.character_subject_resolver),
            )
        )
    if EvaluationDomain.WORLD in enabled_domains:
        required.append(("world_extractor", components.world_extractor))
    missing = [name for name, value in required if value is None]
    if missing:
        raise ValueError("Live runtime components are missing: " + ", ".join(missing))
    if EvaluationDomain.CHARACTER in enabled_domains and not schema_hints:
        raise ValueError("Live runtime requires character_schema_hints.")


def _scenario_dependency_closure(
    gold: GoldSnapshotV3,
    selected_ids: set[str],
) -> set[str]:
    scenario_by_id = {scenario.scenario_id: scenario for scenario in gold.scenarios}
    result = set(selected_ids)
    pending = list(selected_ids)
    while pending:
        previous_id = scenario_by_id[pending.pop()].previous_scenario_id
        if previous_id is not None and previous_id not in result:
            result.add(previous_id)
            pending.append(previous_id)
    return result
