from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from app.domain.enums import (
    CharacterFactComparisonOperation,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
)
from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name
from evals.multi_stage_setting.character_semantics import (
    character_setting_ref_mapping,
    compare_structured_semantics,
    dynamic_status_key_pair,
)
from evals.multi_stage_setting.contracts import (
    CandidateKind,
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Gold,
    CharacterStage2Prediction,
    EvaluationDomain,
    EvaluationMode,
    EvaluationState,
    FailureCause,
    GoldDecision,
    GoldSnapshotV3,
    PredictionBundleV3,
    PredictionEvidence,
    ScenarioGold,
    ScenarioPrediction,
    Stage1Gold,
    Stage1Prediction,
    Stage2Gold,
    Stage2Policy,
    Stage2Prediction,
    StartStateMode,
    UpstreamOutcome,
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStage2Prediction,
    character_state_ref,
    world_entry_subject_ref,
    world_subject_ref,
)
from evals.multi_stage_setting.matching import (
    FieldMatchStatus,
    Stage1Match,
    Stage1MatchingResult,
    character_setting_name_pairs,
    match_stage1,
    world_setting_context_matches,
    world_setting_name_pairs,
)
from evals.multi_stage_setting.semantic_outcome import (
    CharacterSettingContext,
    SemanticOutcomeCase,
    SemanticOutcomeJudge,
    WorldSettingNameContext,
)
from evals.multi_stage_setting.state_effects import (
    ScenarioStateTransition,
    StateApplicationError,
    apply_gold_decision,
    apply_prediction_decision,
    build_gold_state_chain,
)
from evals.multi_stage_setting.world_name_state import world_setting_ref_mapping
from evals.setting_extraction.normalization import normalize_text
from evals.setting_extraction.value_comparator import (
    ValueComparisonStatus,
    compare_typed_value,
)


@dataclass
class Stage2Case:
    scenario_id: str
    gold: CharacterStage2Gold | WorldStage2Gold
    prediction: CharacterStage2Prediction | WorldStage2Prediction | None
    upstream_outcome: UpstreamOutcome
    failure_cause: FailureCause | None
    operation_matched: bool | None = None
    canonical_fact_key_matched: bool | None = None
    target_matched: bool | None = None
    removed_matched: bool | None = None
    temporal_matched: bool | None = None
    consolidation_matched: bool | None = None
    proposed_path_matched: bool | None = None
    root_property_moves_matched: bool | None = None
    value_matched: bool | None = None
    structured_value_matched: bool | None = None
    full_decision_matched: bool | None = None
    semantic_case_id: str | None = None
    setting_name_match: dict[str, str | None] | None = None
    matched_property_name_match: dict[str, str | None] | None = None
    proposed_setting_semantic_case_id: str | None = None
    matched_property_semantic_case_id: str | None = None
    world_target_context_matched: bool | None = None
    world_proposed_scope_matched: bool | None = None
    world_path_preserved_matched: bool | None = None
    world_application_matched: bool | None = None
    proposed_scope_semantic_case_id: str | None = None
    upstream_semantic_pending: bool = False
    related_world_paths: tuple[dict[str, object], ...] = ()
    character_context: CharacterSettingContext | None = None
    character_setting_semantic_case_id: str | None = None
    structured_semantic_case_ids: tuple[str, ...] = ()


@dataclass
class StatePair:
    scenario_id: str
    domain: EvaluationDomain
    ref: str
    expected_value: str | None
    actual_value: str | None
    expected_present: bool
    actual_present: bool
    matched: bool | None
    semantic_case_id: str | None = None
    structured_semantic_case_ids: tuple[str, ...] = ()
    identity_matched: bool | None = True


@dataclass(frozen=True)
class ScoringRefMaps:
    before: dict[str, str]
    after: dict[str, str]
    pending_before: frozenset[str] = frozenset()
    pending_after: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StateSemanticContext:
    before_value: str | None
    source_values: tuple[str, ...]
    required_facts: tuple[str, ...]
    forbidden_facts: tuple[str, ...]
    evidence_quotes: tuple[str, ...]


async def evaluate_multi_stage(
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
    *,
    semantic_judge: SemanticOutcomeJudge | None = None,
) -> dict[str, Any]:
    if gold.fixture_hash is None:
        gold = gold.with_fixture_hash()
    if predictions.fixture_hash != gold.fixture_hash:
        raise ValueError("Prediction bundle fixtureHash does not match Gold.")

    gold_chain = build_gold_state_chain(gold)
    scenario_by_id = {item.scenario_id: item for item in gold.scenarios}
    prediction_by_scenario = {item.scenario_id: item for item in predictions.scenarios}
    unknown_prediction_scenarios = sorted(set(prediction_by_scenario) - scenario_by_id.keys())
    if unknown_prediction_scenarios:
        raise ValueError(f"Predictions reference unknown scenarios: {unknown_prediction_scenarios}")
    _validate_oracle_stage2_sources(gold, predictions)

    selected_ids = set(predictions.evaluation_scenario_ids or gold.evaluation_scenario_ids)
    unknown_selected_ids = sorted(selected_ids - scenario_by_id.keys())
    if unknown_selected_ids:
        raise ValueError(f"Prediction bundle selects unknown scenarios: {unknown_selected_ids}")
    enabled_domains = predictions.evaluation_domains
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult] = {}
    state_stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult] = {}
    semantic_cases: list[SemanticOutcomeCase] = []
    semantic_decisions: dict[str, Any] = {}
    judge_usage = {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0}

    async def judge_cases(cases: list[SemanticOutcomeCase]) -> None:
        if semantic_judge is None or not cases:
            return
        judged = await semantic_judge.judge_many(cases)
        semantic_decisions.update({item.case_id: item for item in judged.decisions})
        judge_usage["inputTokens"] += judged.input_tokens
        judge_usage["cachedInputTokens"] += judged.cached_input_tokens
        judge_usage["outputTokens"] += judged.output_tokens

    name_cases: list[SemanticOutcomeCase] = []
    name_pairs_by_scenario = {}
    active_ids = _state_dependency_ids(gold, predictions.mode, selected_ids)
    if predictions.mode != EvaluationMode.ORACLE:
        for scenario in gold.scenarios:
            scenario_prediction = prediction_by_scenario.get(scenario.scenario_id)
            if scenario.scenario_id not in active_ids or scenario_prediction is None:
                continue
            rows = [item for item in gold.stage1 if item.scenario_id == scenario.scenario_id]
            domains = enabled_domains & scenario.target_domains
            pairs = []
            if EvaluationDomain.WORLD in domains:
                pairs.extend(world_setting_name_pairs(rows, scenario_prediction.stage1))
            if EvaluationDomain.CHARACTER in domains:
                pairs.extend(character_setting_name_pairs(rows, scenario_prediction.stage1))
            name_pairs_by_scenario[scenario.scenario_id] = pairs
            for expected, actual in pairs:
                name_cases.append(
                    SemanticOutcomeCase(
                        case_id=_stage1_name_case_id(expected, actual),
                        expected_value=_stage1_display_value(expected),
                        actual_value=_stage1_display_value(actual),
                        source_values=tuple(_stage1_source_values(expected)),
                        evidence_quotes=tuple(expected.evidence_quotes),
                        setting_context=WorldSettingNameContext(
                            category=expected.category.value,
                            subject_name=expected.subject_name,
                            scope_name=expected.scope_name,
                            expected_setting_name=expected.setting_name,
                            actual_setting_name=actual.setting_name,
                            actual_scope_name=actual.scope_name,
                            related_paths=_world_extraction_paths(
                                expected, rows, scenario_prediction.stage1
                            ),
                        )
                        if isinstance(expected, WorldStage1Gold)
                        else None,
                        character_context=CharacterSettingContext(
                            entity_id=expected.entity_ref,
                            fact_type=expected.fact_type.value,
                            expected_fact_key=expected.fact_key,
                            actual_fact_key=actual.fact_key,
                            schema_pattern="status.*",
                        )
                        if isinstance(expected, CharacterStage1Gold)
                        else None,
                    )
                )
    await judge_cases(name_cases)
    if predictions.mode != EvaluationMode.ORACLE:
        for scenario in gold.scenarios:
            scenario_prediction = prediction_by_scenario.get(
                scenario.scenario_id,
                ScenarioPrediction(scenario_id=scenario.scenario_id),
            )
            rows = [item for item in gold.stage1 if item.scenario_id == scenario.scenario_id]
            for domain in EvaluationDomain:
                if domain not in scenario.target_domains or domain not in enabled_domains:
                    continue
                raw_source = scenario_prediction.raw_stage1 or scenario_prediction.stage1
                result = match_stage1(
                    rows,
                    scenario_prediction.stage1,
                    domain=domain,
                    source_text=scenario.source_text,
                    raw_prediction_count=sum(item.domain == domain for item in raw_source),
                    world_setting_name_matches={
                        (expected.gold_id, actual.candidate_id): getattr(
                            semantic_decisions.get(_stage1_name_case_id(expected, actual)),
                            "same_setting",
                            None,
                        )
                        for expected, actual in name_pairs_by_scenario.get(scenario.scenario_id, [])
                    },
                    world_scope_matches={
                        (expected.gold_id, actual.candidate_id): getattr(
                            semantic_decisions.get(_stage1_name_case_id(expected, actual)),
                            "scope_equivalent",
                            None,
                        )
                        for expected, actual in name_pairs_by_scenario.get(scenario.scenario_id, [])
                    },
                    character_setting_matches={
                        (expected.gold_id, actual.candidate_id): getattr(
                            semantic_decisions.get(_stage1_name_case_id(expected, actual)),
                            "same_setting",
                            None,
                        )
                        for expected, actual in name_pairs_by_scenario.get(scenario.scenario_id, [])
                        if isinstance(expected, CharacterStage1Gold)
                    },
                )
                stage1_results[(scenario.scenario_id, domain)] = result
                # Judge output must not change reducer inputs, application order, or raw hashes.
                state_stage1_results[(scenario.scenario_id, domain)] = match_stage1(
                    rows,
                    scenario_prediction.stage1,
                    domain=domain,
                    source_text=scenario.source_text,
                    raw_prediction_count=sum(item.domain == domain for item in raw_source),
                    semantic_scoring=False,
                )
                for match in result.matches:
                    if (
                        scenario.scenario_id in active_ids
                        and isinstance(match.gold, CharacterStage1Gold)
                        and match.gold.structured_scorable
                    ):
                        semantic_cases.extend(
                            _structured_semantic_cases(
                                f"stage1-json:{scenario.scenario_id}:{match.gold.gold_id}",
                                match.gold.value_json,
                                match.prediction.value_json,
                                source_values=tuple(_stage1_source_values(match.gold)),
                                evidence_quotes=tuple(match.gold.evidence_quotes),
                            )
                        )
                    if (
                        scenario.scenario_id in active_ids
                        and match.value_status == FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED
                    ):
                        case_id = f"stage1:{scenario.scenario_id}:{match.gold.gold_id}"
                        name_decision = semantic_decisions.get(
                            _stage1_name_case_id(match.gold, match.prediction)
                        )
                        if name_decision is not None:
                            semantic_decisions[case_id] = name_decision
                            continue
                        semantic_cases.append(
                            SemanticOutcomeCase(
                                case_id=case_id,
                                expected_value=_stage1_display_value(match.gold),
                                actual_value=_stage1_display_value(match.prediction),
                                source_values=tuple(_stage1_source_values(match.gold)),
                                evidence_quotes=tuple(match.gold.evidence_quotes),
                            )
                        )

    predicted_chain, state_application_errors = _build_predicted_state_chain(
        gold,
        predictions,
        gold_chain,
        state_stage1_results,
        prediction_by_scenario,
        selected_ids,
        enabled_domains,
    )
    stage2_cases = _evaluate_stage2_cases(
        gold,
        predictions,
        stage1_results,
        prediction_by_scenario,
        selected_ids,
        semantic_cases,
        enabled_domains,
        state_application_errors,
        gold_chain,
        predicted_chain,
    )
    dependency_cases: list[Stage2Case] = []
    if active_ids - selected_ids:
        dependency_semantics: list[SemanticOutcomeCase] = []
        dependency_cases = _evaluate_stage2_cases(
            gold,
            predictions,
            stage1_results,
            prediction_by_scenario,
            active_ids - selected_ids,
            dependency_semantics,
            enabled_domains,
            state_application_errors,
            gold_chain,
            predicted_chain,
        )
        # Dependency decisions establish inherited names, not an extra stage score.
        semantic_cases.extend(dependency_semantics)
    await judge_cases(semantic_cases)
    _apply_stage1_structured_results(stage1_results, semantic_decisions)
    _apply_semantic_results(stage2_cases + dependency_cases, [], semantic_decisions)
    _reclassify_semantic_upstream(
        stage2_cases + dependency_cases, stage1_results, semantic_decisions, predictions.mode
    )
    scoring_ref_maps = _world_scoring_ref_maps(
        gold, stage2_cases + dependency_cases, gold_chain, predicted_chain, selected_ids
    )
    _add_character_scoring_ref_maps(
        scoring_ref_maps,
        gold,
        stage2_cases + dependency_cases,
        gold_chain,
        predicted_chain,
        selected_ids,
    )
    state_semantic_cases: list[SemanticOutcomeCase] = []
    state_pairs = _build_state_pairs(
        gold,
        gold_chain,
        predicted_chain,
        selected_ids,
        state_semantic_cases,
        enabled_domains,
        scoring_ref_maps,
    )
    await judge_cases(state_semantic_cases)
    _apply_semantic_results([], state_pairs, semantic_decisions)
    _reclassify_semantic_upstream(
        stage2_cases,
        stage1_results,
        semantic_decisions,
        predictions.mode,
    )

    stage1_report = _build_stage1_report(
        gold,
        stage1_results,
        selected_ids,
        semantic_decisions,
        evaluated=predictions.mode != EvaluationMode.ORACLE,
        enabled_domains=enabled_domains,
    )
    stage2_report = _build_stage2_report(
        stage2_cases,
        gold,
        predictions,
        stage1_results,
        selected_ids,
        enabled_domains,
    )
    end_to_end_report = _build_end_to_end_report(
        gold,
        gold_chain,
        predicted_chain,
        state_pairs,
        state_application_errors,
        selected_ids,
        enabled_domains,
        scoring_ref_maps,
    )
    failure_causes = Counter(
        case.failure_cause.value for case in stage2_cases if case.failure_cause is not None
    )
    failure_causes[FailureCause.STATE_APPLICATION_ERROR] += sum(
        item["scenarioId"] in selected_ids for item in state_application_errors
    )
    for (scenario_id, _), result in stage1_results.items():
        if scenario_id not in selected_ids:
            continue
        failure_causes[FailureCause.UPSTREAM_FALSE_POSITIVE] += len(result.extra_predictions)
        waiting_failures = sum(_is_waiting_character_gold(row) for row in result.missed_gold)
        waiting_failures += sum(
            _is_waiting_character_gold(match.gold)
            and (
                not match.identity_matched
                or _resolved_stage1_value_status(match, semantic_decisions)
                == FieldMatchStatus.MISMATCH.value
            )
            for match in result.matches
        )
        if waiting_failures:
            failure_causes[FailureCause.EXTRACTION_MISS] += waiting_failures

    selected_scenarios = [
        scenario for scenario in gold.scenarios if scenario.scenario_id in selected_ids
    ]
    return {
        "reportVersion": "setting-eval-report/v3",
        "run": {
            "mode": predictions.mode,
            "stateApplicationPolicy": (
                predictions.state_application_policy
                or (
                    "ACCEPT_ALL_PREDICTIONS"
                    if predictions.mode == EvaluationMode.ROLLING
                    else "SCENARIO_LOCAL"
                )
            ),
            "domains": sorted(domain.value for domain in enabled_domains),
            "analysisModel": predictions.analysis_model,
            "subjectResolutionModel": predictions.subject_resolution_model,
            "comparisonModel": predictions.comparison_model,
            "promptVersions": predictions.prompt_versions,
            "characterSchemaHash": predictions.character_schema_hash,
            "maxChunks": predictions.max_chunks,
            "runtimeFailures": _runtime_failure_summary(predictions),
            "semanticJudgeEnabled": semantic_judge is not None,
            "worldSettingNamePolicy": "item-scope-value-contextual/v2",
            "semanticJudgeUsage": judge_usage,
            **_prediction_usage(predictions),
        },
        "dataset": {
            "schemaVersion": gold.schema_version,
            "name": gold.name,
            "version": gold.dataset_version,
            "fixtureHash": gold.fixture_hash,
            "scorable": gold.scorable,
            "scenarioCount": len(selected_scenarios),
            "dependencyScenarioCount": len(gold.scenarios) - len(selected_scenarios),
            "episodes": [scenario.episode_no for scenario in selected_scenarios],
        },
        "stages": {
            "character": {
                "stage1": stage1_report[EvaluationDomain.CHARACTER],
                "stage2": stage2_report[EvaluationDomain.CHARACTER],
            },
            "world": {
                "stage1": stage1_report[EvaluationDomain.WORLD],
                "stage2": stage2_report[EvaluationDomain.WORLD],
            },
            "macroAverage": _macro_stage_scores(stage1_report, stage2_report),
        },
        "endToEnd": end_to_end_report,
        "failureCauses": dict(sorted(failure_causes.items())),
        "scenarios": _scenario_details(
            gold,
            stage1_results,
            stage2_cases,
            gold_chain,
            predicted_chain,
            state_application_errors,
            selected_ids,
            semantic_decisions,
        ),
    }


def _stage1_name_case_id(expected: Stage1Gold, actual: Stage1Prediction) -> str:
    return "stage1-name:" + json.dumps(
        [expected.scenario_id, expected.gold_id, actual.candidate_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _world_extraction_paths(
    source: WorldStage1Gold, expected_rows: list[Stage1Gold], actual_rows: list[Stage1Prediction]
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "side": side,
            "kind": "EXTRACTED",
            "scopeName": row.scope_name,
            "settingName": row.setting_name,
            "values": list(row.source_values),
        }
        for side, rows in (("expected", expected_rows), ("actual", actual_rows))
        for row in rows
        if isinstance(row, (WorldStage1Gold, WorldStage1Prediction))
        and (not isinstance(row, WorldStage1Gold) or row.scenario_id == source.scenario_id)
        and row.category == source.category
        and _same_world_name(row.subject_name, source.subject_name)
    )


def _stage1_setting_name_diagnostic(
    match: Stage1Match, decisions: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(match.gold, WorldStage1Gold) or not isinstance(
        match.prediction, WorldStage1Prediction
    ):
        return {}
    if match.setting_name_match_method is not None:
        return {"settingNameMatch": {"status": "MATCH", "method": match.setting_name_match_method}}
    if not world_setting_context_matches(match.gold, match.prediction):
        return {}
    decision = decisions.get(_stage1_name_case_id(match.gold, match.prediction))
    same_setting = getattr(decision, "same_setting", None)
    return {
        "settingNameMatch": {
            "status": "MISMATCH" if same_setting is False else "PENDING",
            "method": "SEMANTIC" if same_setting is False else "UNRESOLVED",
        }
    }


def _world_scoring_ref_maps(
    gold: GoldSnapshotV3,
    cases: list[Stage2Case],
    gold_chain: dict[str, ScenarioStateTransition],
    predicted_chain: dict[str, ScenarioStateTransition],
    selected_ids: set[str],
) -> dict[str, ScoringRefMaps]:
    """Align WORLD scoring keys; pending pairs retain uncertainty without changing stored state."""
    source_by_id = {item.gold_id: item for item in gold.stage1}
    scenario_by_id = {item.scenario_id: item for item in gold.scenarios}
    result = {}
    for scenario_id in selected_ids:
        ancestors = {scenario_id}
        previous_id = scenario_by_id[scenario_id].previous_scenario_id
        while previous_id is not None:
            ancestors.add(previous_id)
            previous_id = scenario_by_id[previous_id].previous_scenario_id
        expected_transition = gold_chain[scenario_id]
        actual_transition = predicted_chain[scenario_id]
        expected_entries = (
            expected_transition.before_state.world_facts
            + expected_transition.after_state.world_facts
        )
        actual_entries = (
            actual_transition.before_state.world_facts + actual_transition.after_state.world_facts
        )
        approved_pairs = []
        pending_pairs = []
        for case in cases:
            if (
                case.scenario_id not in ancestors
                or not isinstance(case.gold, WorldStage2Gold)
                or not isinstance(case.prediction, WorldStage2Prediction)
                or case.target_matched is not True
            ):
                continue
            source = source_by_id[case.gold.source_gold_ids[0]]
            assert isinstance(source, WorldStage1Gold)
            target_subject = world_subject_ref(source.category, source.subject_name)
            if case.gold.target_ref is not None:
                decision_transition = gold_chain[case.scenario_id]
                decision_entries = (
                    decision_transition.before_state.world_facts
                    + decision_transition.after_state.world_facts
                )
                target_entry = next(
                    (item for item in decision_entries if item.ref == case.gold.target_ref), None
                )
                target_subject = (
                    world_entry_subject_ref(target_entry)
                    if target_entry is not None
                    else case.gold.target_ref
                )
            names = []
            if case.proposed_path_matched is not False:
                names.append(
                    (
                        case.gold.proposed_scope_name,
                        case.prediction.proposed_scope_name,
                        case.gold.proposed_setting_name,
                        case.prediction.proposed_setting_name,
                        case.proposed_path_matched is None or case.upstream_semantic_pending,
                    )
                )
            if (getattr(case, "matched_property_name_match", None) or {}).get("status") == "MATCH":
                names.append(
                    (
                        case.gold.matched_scope_name,
                        case.prediction.matched_scope_name,
                        case.gold.matched_property_name,
                        case.prediction.matched_property_name,
                        case.upstream_semantic_pending,
                    )
                )
            for expected_scope, actual_scope, expected_name, actual_name, pending in names:
                if not expected_name or not actual_name:
                    continue
                for expected in expected_entries:
                    if (
                        expected.category != source.category
                        or world_entry_subject_ref(expected) != target_subject
                        or not _same_world_name(expected.subject_name, source.subject_name)
                        or not _same_world_name(expected.scope_name, expected_scope)
                        or not _same_world_name(expected.setting_name, expected_name)
                    ):
                        continue
                    for actual in actual_entries:
                        if (
                            actual.category == expected.category
                            and (
                                world_entry_subject_ref(actual) == world_entry_subject_ref(expected)
                                or (
                                    actual.subject_ref is None
                                    and expected.subject_ref is None
                                    and _same_world_name(actual.subject_name, expected.subject_name)
                                )
                            )
                            and _same_world_name(actual.scope_name, actual_scope)
                            and _same_world_name(actual.setting_name, actual_name)
                        ):
                            (pending_pairs if pending else approved_pairs).append(
                                (f"fact:{expected.ref}", f"fact:{actual.ref}")
                            )
        before_refs = {f"fact:{item.ref}" for item in expected_transition.before_state.world_facts}
        after_refs = {f"fact:{item.ref}" for item in expected_transition.after_state.world_facts}
        actual_before_refs = {
            f"fact:{item.ref}" for item in actual_transition.before_state.world_facts
        }
        actual_after_refs = {
            f"fact:{item.ref}" for item in actual_transition.after_state.world_facts
        }
        before_map = world_setting_ref_mapping(
            before_refs, actual_before_refs, approved_pairs + pending_pairs
        )
        after_map = world_setting_ref_mapping(
            after_refs, actual_after_refs, approved_pairs + pending_pairs
        )
        result[scenario_id] = ScoringRefMaps(
            before=before_map,
            after=after_map,
            pending_before=_pending_refs(
                pending_pairs, before_refs, actual_before_refs, before_map
            ),
            pending_after=_pending_refs(pending_pairs, after_refs, actual_after_refs, after_map),
        )
    return result


def _pending_refs(
    pending_pairs: list[tuple[str, str]],
    expected_refs: set[str],
    actual_refs: set[str],
    ref_map: dict[str, str],
) -> frozenset[str]:
    return frozenset(
        ref
        for expected, actual in pending_pairs
        if expected in expected_refs
        and actual in actual_refs
        and (expected == actual or (expected not in actual_refs and actual not in expected_refs))
        for ref in (expected, ref_map.get(actual, actual))
    )


def _add_character_scoring_ref_maps(
    maps: dict[str, ScoringRefMaps],
    gold: GoldSnapshotV3,
    cases: list[Stage2Case],
    gold_chain: dict[str, ScenarioStateTransition],
    predicted_chain: dict[str, ScenarioStateTransition],
    selected_ids: set[str],
) -> None:
    sources = {row.gold_id: row for row in gold.stage1}
    scenarios = {row.scenario_id: row for row in gold.scenarios}
    for scenario_id in selected_ids:
        ancestors = {scenario_id}
        previous = scenarios[scenario_id].previous_scenario_id
        while previous is not None:
            ancestors.add(previous)
            previous = scenarios[previous].previous_scenario_id
        expected = gold_chain[scenario_id]
        actual = predicted_chain[scenario_id]
        expected_before = set(
            _evaluation_state_values(expected.before_state, EvaluationDomain.CHARACTER)
        )
        expected_after = set(
            _evaluation_state_values(expected.after_state, EvaluationDomain.CHARACTER)
        )
        actual_before = set(
            _evaluation_state_values(actual.before_state, EvaluationDomain.CHARACTER)
        )
        actual_after = set(_evaluation_state_values(actual.after_state, EvaluationDomain.CHARACTER))
        approved, pending = [], []
        history_sources = set()
        for case in cases:
            if (
                case.scenario_id not in ancestors
                or not isinstance(case.gold, CharacterStage2Gold)
                or not isinstance(case.prediction, CharacterStage2Prediction)
                or case.canonical_fact_key_matched is False
                or case.target_matched is not True
            ):
                continue
            source = sources[case.gold.source_gold_ids[0]]
            expected_ref = character_state_ref(source.entity_ref, source.fact_type, source.fact_key)
            actual_ref = character_state_ref(
                source.entity_ref, source.fact_type, case.prediction.resolved_canonical_fact_key
            )
            pairs = [
                (f"{prefix}:{expected_ref}", f"{prefix}:{actual_ref}")
                for prefix in ("fact", "fact-json")
            ]
            for ref in expected_before | expected_after:
                kind, _, tail = ref.partition(":")
                if kind not in {"history", "history-json"}:
                    continue
                identity = json.loads(tail)
                if identity[2:5] == [source.entity_ref, source.fact_type, source.fact_key]:
                    identity[4] = case.prediction.resolved_canonical_fact_key
                    pairs.append((ref, _effect_ref(kind, tuple(identity))))
                    identity[1] = f"prediction:{case.prediction.source_candidate_id}"
                    history_sources.add((identity[0], source.gold_id, identity[1]))
                    pairs.append((ref, _effect_ref(kind, tuple(identity))))
            (
                pending
                if case.canonical_fact_key_matched is None or case.upstream_semantic_pending
                else approved
            ).extend(pairs)
        before_map = character_setting_ref_mapping(
            expected_before,
            actual_before,
            approved + pending,
            history_source_matches=history_sources,
        )
        after_map = character_setting_ref_mapping(
            expected_after, actual_after, approved + pending, history_source_matches=history_sources
        )
        world = maps[scenario_id]
        maps[scenario_id] = ScoringRefMaps(
            before=world.before | before_map,
            after=world.after | after_map,
            pending_before=world.pending_before
            | _pending_refs(pending, expected_before, actual_before, before_map),
            pending_after=world.pending_after
            | _pending_refs(pending, expected_after, actual_after, after_map),
        )


def _validate_oracle_stage2_sources(
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
) -> None:
    if predictions.mode != EvaluationMode.ORACLE:
        return
    gold_source_relations = {
        source_id: (decision.scenario_id, decision.domain)
        for decision in gold.stage2
        for source_id in decision.source_gold_ids
    }
    for scenario in predictions.scenarios:
        for decision in scenario.stage2:
            relation = gold_source_relations.get(decision.source_candidate_id)
            if relation is None:
                raise ValueError(
                    f"ORACLE Stage2 prediction in {scenario.scenario_id} references "
                    f"unknown Gold Stage2 source {decision.source_candidate_id}."
                )
            source_scenario_id, source_domain = relation
            if source_scenario_id != scenario.scenario_id:
                raise ValueError(
                    f"ORACLE Stage2 prediction in {scenario.scenario_id} references Gold "
                    f"source {decision.source_candidate_id} from {source_scenario_id}."
                )
            if source_domain != decision.domain:
                raise ValueError(
                    f"ORACLE Stage2 prediction in {scenario.scenario_id} has a different "
                    f"domain from Gold source {decision.source_candidate_id}."
                )


def _gold_world_projections_before_decision(
    gold: GoldSnapshotV3,
) -> dict[str, frozenset[str]]:
    """Track subjects created by earlier Gold ADDs in each scenario."""

    source_by_id = {item.gold_id: item for item in gold.stage1}
    result: dict[str, frozenset[str]] = {}
    for scenario in gold.scenarios:
        projected: set[str] = set()
        decisions = sorted(
            (
                item
                for item in gold.stage2
                if item.scenario_id == scenario.scenario_id and isinstance(item, WorldStage2Gold)
            ),
            key=lambda item: (item.sort_order, item.decision_id),
        )
        for decision in decisions:
            result[decision.decision_id] = frozenset(projected)
            if (
                decision.operation != WorldSettingOperation.ADD
                or decision.target_ref is not None
                or decision.consolidation_status == WorldSettingConsolidationStatus.CONFLICT
            ):
                continue
            source = source_by_id[decision.source_gold_ids[0]]
            assert isinstance(source, WorldStage1Gold)
            projected.add(world_subject_ref(source.category, source.subject_name))
    return result


def _prediction_world_projections_before_decision(
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
    prediction_by_scenario: dict[str, ScenarioPrediction],
    state_application_errors: list[dict[str, str]],
) -> dict[tuple[str, str], frozenset[str]]:
    """Track only earlier prediction ADDs accepted by the reference reducer."""

    gold_source_by_id = {item.gold_id: item for item in gold.stage1}
    gold_decision_by_source = {
        source_id: decision for decision in gold.stage2 for source_id in decision.source_gold_ids
    }
    failed_sources = {
        (item["scenarioId"], item["sourceCandidateId"]) for item in state_application_errors
    }
    result: dict[tuple[str, str], frozenset[str]] = {}
    for scenario_id, scenario_prediction in prediction_by_scenario.items():
        source_by_id = {item.candidate_id: item for item in scenario_prediction.stage1}
        ordered = sorted(
            enumerate(
                item
                for item in scenario_prediction.stage2
                if isinstance(item, WorldStage2Prediction)
            ),
            key=lambda pair: (
                _prediction_decision_order(
                    pair[1],
                    gold_decision_by_source,
                    default=10**9 + pair[0],
                ),
                pair[0],
            ),
        )
        projected: set[str] = set()
        for _, decision in ordered:
            key = (scenario_id, decision.source_candidate_id)
            result[key] = frozenset(projected)
            if (
                key in failed_sources
                or decision.operation != WorldSettingOperation.ADD
                or decision.target_ref is not None
                or decision.consolidation_status == WorldSettingConsolidationStatus.CONFLICT
            ):
                continue
            source = (
                gold_source_by_id.get(decision.source_candidate_id)
                if predictions.mode == EvaluationMode.ORACLE
                else source_by_id.get(decision.source_candidate_id)
            )
            if not isinstance(source, (WorldStage1Gold, WorldStage1Prediction)):
                continue
            projected.add(world_subject_ref(source.category, source.subject_name))
    return result


def _allows_projected_world_target_equivalence(
    gold: WorldStage2Gold,
    prediction: WorldStage2Prediction,
    expected_subject_ref: str | None,
    gold_projected_before: frozenset[str],
    prediction_projected_before: frozenset[str],
) -> bool:
    """Allow null↔subject-ref only when that side already created the subject."""

    if expected_subject_ref is None:
        return False
    if gold.target_ref is None and prediction.target_ref == expected_subject_ref:
        return expected_subject_ref in prediction_projected_before
    if gold.target_ref == expected_subject_ref and prediction.target_ref is None:
        return expected_subject_ref in gold_projected_before
    return False


def _evaluate_stage2_cases(
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    prediction_by_scenario: dict[str, ScenarioPrediction],
    selected_ids: set[str],
    semantic_cases: list[SemanticOutcomeCase],
    enabled_domains: set[EvaluationDomain],
    state_application_errors: list[dict[str, str]],
    gold_chain: dict[str, ScenarioStateTransition],
    predicted_chain: dict[str, ScenarioStateTransition],
) -> list[Stage2Case]:
    cases: list[Stage2Case] = []
    gold_world_projections = _gold_world_projections_before_decision(gold)
    prediction_world_projections = _prediction_world_projections_before_decision(
        gold,
        predictions,
        prediction_by_scenario,
        state_application_errors,
    )
    for decision in gold.stage2:
        if decision.scenario_id not in selected_ids or decision.domain not in enabled_domains:
            continue
        scenario_prediction = prediction_by_scenario.get(
            decision.scenario_id,
            ScenarioPrediction(scenario_id=decision.scenario_id),
        )
        if predictions.mode == EvaluationMode.ORACLE:
            outcome = UpstreamOutcome.REACHED
            candidate_ids = set(decision.source_gold_ids)
        else:
            matching = stage1_results[(decision.scenario_id, decision.domain)]
            outcomes = [
                matching.outcome_by_gold_id.get(source_id, UpstreamOutcome.UPSTREAM_MISSING)
                for source_id in decision.source_gold_ids
            ]
            outcome = _combined_upstream_outcome(outcomes)
            candidate_ids = {
                matching.prediction_id_by_gold_id[source_id]
                for source_id in decision.source_gold_ids
                if source_id in matching.prediction_id_by_gold_id
            }
        if outcome != UpstreamOutcome.REACHED:
            cases.append(
                Stage2Case(
                    scenario_id=decision.scenario_id,
                    gold=decision,
                    prediction=None,
                    upstream_outcome=outcome,
                    failure_cause=FailureCause.EXTRACTION_MISS,
                )
            )
            continue
        prediction = next(
            (
                item
                for item in scenario_prediction.stage2
                if item.domain == decision.domain and item.source_candidate_id in candidate_ids
            ),
            None,
        )
        if prediction is None:
            cases.append(
                Stage2Case(
                    scenario_id=decision.scenario_id,
                    gold=decision,
                    prediction=None,
                    upstream_outcome=outcome,
                    failure_cause=FailureCause.COMPARISON_ERROR,
                    operation_matched=False,
                    full_decision_matched=False,
                )
            )
            continue
        expected_character_fact_key = None
        expected_character_source = None
        expected_world_subject_ref = None
        expected_world_source = None
        source_rows = [item for item in gold.stage1 if item.gold_id in decision.source_gold_ids]
        if isinstance(decision, CharacterStage2Gold):
            source = next(
                item for item in gold.stage1 if item.gold_id == decision.source_gold_ids[0]
            )
            assert isinstance(source, CharacterStage1Gold)
            expected_character_fact_key = source.fact_key
            expected_character_source = source
        elif isinstance(decision, WorldStage2Gold):
            source = next(
                item for item in gold.stage1 if item.gold_id == decision.source_gold_ids[0]
            )
            assert isinstance(source, WorldStage1Gold)
            expected_world_subject_ref = world_subject_ref(
                source.category,
                source.subject_name,
            )
            expected_world_source = _world_stage2_source(decision, source_rows)
        case = _score_stage2_case(
            decision,
            prediction,
            expected_character_fact_key=expected_character_fact_key,
            expected_character_source=expected_character_source,
            expected_world_subject_ref=expected_world_subject_ref,
            expected_world_source=expected_world_source,
            allow_projected_world_target_equivalence=(
                isinstance(decision, WorldStage2Gold)
                and isinstance(prediction, WorldStage2Prediction)
                and _allows_projected_world_target_equivalence(
                    decision,
                    prediction,
                    expected_world_subject_ref,
                    gold_world_projections.get(decision.decision_id, frozenset()),
                    prediction_world_projections.get(
                        (decision.scenario_id, prediction.source_candidate_id),
                        frozenset(),
                    ),
                )
            ),
        )
        case.scenario_id = decision.scenario_id
        case.upstream_outcome = outcome
        if isinstance(decision, WorldStage2Gold):
            case.related_world_paths = _world_decision_paths(
                expected_world_source,
                gold,
                scenario_prediction,
                gold_chain[decision.scenario_id].before_state,
                predicted_chain[decision.scenario_id].before_state,
            )
            case.world_application_matched = not any(
                error.get("scenarioId") == decision.scenario_id
                and error.get("sourceCandidateId") == prediction.source_candidate_id
                for error in state_application_errors
            )
            case.full_decision_matched = _all_or_pending(_stage2_scoring_fields(case))
        if case.target_matched is False and _target_required(decision):
            case.failure_cause = FailureCause.RETRIEVAL_MISS
        elif case.full_decision_matched is False:
            case.failure_cause = FailureCause.COMPARISON_ERROR
        semantic_cases.extend(_stage2_semantic_cases(case, source_rows))
        cases.append(case)
    return cases


def _world_decision_paths(
    source: WorldStage1Gold | None,
    gold: GoldSnapshotV3,
    prediction: ScenarioPrediction,
    expected_before: EvaluationState,
    actual_before: EvaluationState,
) -> tuple[dict[str, object], ...]:
    if source is None:
        return ()
    paths = list(_world_extraction_paths(source, gold.stage1, prediction.stage1))
    gold_sources = {row.gold_id: row for row in gold.stage1}
    actual_sources = {row.candidate_id: row for row in prediction.stage1}
    for side, decisions, sources, before in (
        ("expected", gold.stage2, gold_sources, expected_before),
        ("actual", prediction.stage2, actual_sources, actual_before),
    ):
        for entry in before.world_facts:
            if entry.category == source.category and _same_world_name(
                entry.subject_name, source.subject_name
            ):
                paths.append(
                    {
                        "side": side,
                        "kind": "EXISTING",
                        "scopeName": entry.scope_name,
                        "settingName": entry.setting_name,
                        "value": entry.value,
                    }
                )
        for decision in decisions:
            if not isinstance(decision, (WorldStage2Gold, WorldStage2Prediction)):
                continue
            if isinstance(decision, WorldStage2Gold):
                if decision.scenario_id != source.scenario_id:
                    continue
                row = sources.get(decision.source_gold_ids[0])
            else:
                row = sources.get(decision.source_candidate_id) or gold_sources.get(
                    decision.source_candidate_id
                )
            if (
                not isinstance(row, (WorldStage1Gold, WorldStage1Prediction))
                or row.category != source.category
                or not _same_world_name(row.subject_name, source.subject_name)
            ):
                continue
            paths.append(
                {
                    "side": side,
                    "kind": "PROPOSED",
                    "operation": decision.operation.value,
                    "scopeName": decision.proposed_scope_name,
                    "settingName": decision.proposed_setting_name,
                    "value": decision.proposed_value,
                }
            )
    return tuple(paths)


def _score_stage2_case(
    gold: CharacterStage2Gold | WorldStage2Gold,
    prediction: CharacterStage2Prediction | WorldStage2Prediction,
    *,
    expected_character_fact_key: str | None = None,
    expected_character_source: CharacterStage1Gold | None = None,
    expected_world_subject_ref: str | None = None,
    expected_world_source: WorldStage1Gold | None = None,
    allow_projected_world_target_equivalence: bool = False,
) -> Stage2Case:
    if isinstance(gold, CharacterStage2Gold) and isinstance(prediction, CharacterStage2Prediction):
        value_comparison = compare_typed_value(
            value_type=None if gold.proposed_value is None else _character_source_value_type(gold),
            expected_display_value=gold.proposed_value,
            actual_display_value=prediction.proposed_value,
            expected_value_json=gold.proposed_value_json,
            actual_value_json=prediction.proposed_value_json,
        )
        structured_value_matched = (
            _structured_semantic_result(gold.proposed_value_json, prediction.proposed_value_json)
            if gold.proposed_value_json is not None
            else None
        )
        if gold.proposed_value is None and prediction.proposed_value is None:
            value_matched: bool | None = True
        elif value_comparison.status == ValueComparisonStatus.MATCH:
            value_matched = True
        elif value_comparison.status == ValueComparisonStatus.MISMATCH:
            value_matched = False
        else:
            value_matched = None
        assert expected_character_fact_key is not None
        canonical_fact_key_matched = (
            prediction.resolved_canonical_fact_key == expected_character_fact_key
        )
        character_context = None
        if (
            not canonical_fact_key_matched
            and expected_character_source is not None
            and dynamic_status_key_pair(
                expected_character_source.fact_type,
                expected_character_fact_key,
                prediction.resolved_canonical_fact_key,
            )
        ):
            canonical_fact_key_matched = None
            character_context = CharacterSettingContext(
                entity_id=expected_character_source.entity_ref,
                fact_type=expected_character_source.fact_type.value,
                expected_fact_key=expected_character_fact_key,
                actual_fact_key=prediction.resolved_canonical_fact_key,
                schema_pattern="status.*",
            )
        operation_matched = gold.operation == prediction.operation
        target_matched = _same_ref(gold.target_ref, prediction.target_ref)
        removed_matched = (
            _same_ref_set(gold.removed_snapshot_refs, prediction.removed_snapshot_refs)
            if gold.removed_snapshot_refs or prediction.removed_snapshot_refs
            else None
        )
        temporal_matched = gold.temporal_scope == prediction.temporal_scope
        fields: list[bool | None] = [
            operation_matched,
            canonical_fact_key_matched,
            target_matched,
            temporal_matched,
            value_matched,
        ]
        if removed_matched is not None:
            fields.append(removed_matched)
        if gold.proposed_value_json:
            fields.append(structured_value_matched)
        return Stage2Case(
            scenario_id=gold.scenario_id,
            gold=gold,
            prediction=prediction,
            upstream_outcome=UpstreamOutcome.REACHED,
            failure_cause=None,
            operation_matched=operation_matched,
            canonical_fact_key_matched=canonical_fact_key_matched,
            target_matched=target_matched,
            removed_matched=removed_matched,
            temporal_matched=temporal_matched,
            value_matched=value_matched,
            structured_value_matched=structured_value_matched,
            full_decision_matched=_all_or_pending(fields),
            semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}"
                if value_matched is None or canonical_fact_key_matched is None
                else None
            ),
            character_context=character_context,
            character_setting_semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}" if character_context else None
            ),
            structured_semantic_case_ids=_structured_case_ids(
                f"stage2-json:{gold.scenario_id}:{gold.decision_id}",
                gold.proposed_value_json,
                prediction.proposed_value_json,
            )
            if gold.proposed_value_json is not None
            else (),
        )
    if isinstance(gold, WorldStage2Gold) and isinstance(prediction, WorldStage2Prediction):
        exact_value = normalize_text(gold.proposed_value) == normalize_text(
            prediction.proposed_value
        )
        values_absent = gold.proposed_value is None and not prediction.proposed_value
        value_matched = True if exact_value or values_absent else None
        target_ref_matched = _same_ref(gold.target_ref, prediction.target_ref)
        if (
            not target_ref_matched
            and gold.operation == WorldSettingOperation.ADD
            and prediction.operation == WorldSettingOperation.ADD
            and expected_world_subject_ref is not None
            and allow_projected_world_target_equivalence
        ):
            allowed_add_refs = {"", expected_world_subject_ref}
            target_ref_matched = (gold.target_ref or "").strip() in allowed_add_refs and (
                prediction.target_ref or ""
            ).strip() in allowed_add_refs
        target_context_matched = target_ref_matched and _same_world_name(
            gold.matched_scope_name, prediction.matched_scope_name
        )
        scope_matched: bool | None = _same_world_name(
            gold.proposed_scope_name, prediction.proposed_scope_name
        )
        if (
            not scope_matched
            and gold.operation == WorldSettingOperation.ADD
            and prediction.operation == WorldSettingOperation.ADD
            and expected_world_source is not None
        ):
            scope_matched = None
        aliases_allowed = expected_world_source is not None and (
            _same_world_name(expected_world_source.scope_name, gold.proposed_scope_name)
            and _same_world_name(expected_world_source.setting_name, gold.proposed_setting_name)
        )
        property_matched, property_match = _world_stage2_name_match(
            gold.matched_property_name,
            prediction.matched_property_name,
            expected_scope=gold.matched_scope_name,
            context_matched=target_context_matched,
            source=expected_world_source,
            aliases_allowed=aliases_allowed,
        )
        setting_matched, setting_match = _world_stage2_name_match(
            gold.proposed_setting_name,
            prediction.proposed_setting_name,
            expected_scope=gold.proposed_scope_name,
            context_matched=expected_world_source is not None,
            source=expected_world_source,
            aliases_allowed=aliases_allowed,
        )
        target_matched = _all_or_pending([target_context_matched, property_matched])
        path_matched = _all_or_pending([scope_matched, setting_matched])
        path_preserved_matched = (
            _same_world_name(prediction.matched_scope_name, prediction.proposed_scope_name)
            and _same_world_name(prediction.matched_property_name, prediction.proposed_setting_name)
            if prediction.operation in {WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}
            else None
        )
        root_property_moves_matched = (
            _same_world_name_set(
                gold.existing_root_property_names_to_move,
                prediction.existing_root_property_names_to_move,
            )
            if gold.existing_root_property_names_to_move
            or prediction.existing_root_property_names_to_move
            else None
        )
        fields = [
            gold.operation == prediction.operation,
            target_matched,
            gold.consolidation_status == prediction.consolidation_status,
            path_matched,
            value_matched,
        ]
        if root_property_moves_matched is not None:
            fields.append(root_property_moves_matched)
        if path_preserved_matched is not None:
            fields.append(path_preserved_matched)
        return Stage2Case(
            scenario_id=gold.scenario_id,
            gold=gold,
            prediction=prediction,
            upstream_outcome=UpstreamOutcome.REACHED,
            failure_cause=None,
            operation_matched=fields[0],
            target_matched=target_matched,
            consolidation_matched=fields[2],
            proposed_path_matched=path_matched,
            root_property_moves_matched=root_property_moves_matched,
            value_matched=value_matched,
            full_decision_matched=_all_or_pending(fields),
            semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}"
                if value_matched is None or setting_matched is None or scope_matched is None
                else None
            ),
            setting_name_match=setting_match,
            matched_property_name_match=property_match,
            proposed_setting_semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}" if setting_matched is None else None
            ),
            proposed_scope_semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}" if scope_matched is None else None
            ),
            matched_property_semantic_case_id=(
                f"stage2-property:{gold.scenario_id}:{gold.decision_id}"
                if property_matched is None
                else None
            ),
            world_target_context_matched=target_context_matched,
            world_proposed_scope_matched=scope_matched,
            world_path_preserved_matched=path_preserved_matched,
        )
    return Stage2Case(
        scenario_id=gold.scenario_id,
        gold=gold,
        prediction=prediction,
        upstream_outcome=UpstreamOutcome.REACHED,
        failure_cause=FailureCause.COMPARISON_ERROR,
        operation_matched=False,
        full_decision_matched=False,
    )


def _world_stage2_source(gold: WorldStage2Gold, source_rows: list[Stage1Gold]) -> WorldStage1Gold:
    world_rows = [row for row in source_rows if isinstance(row, WorldStage1Gold)]
    source = next(
        (
            row
            for row in world_rows
            if _same_world_name(row.scope_name, gold.proposed_scope_name)
            and _same_world_name(row.setting_name, gold.proposed_setting_name)
        ),
        world_rows[0],
    )
    aliases = list(
        dict.fromkeys(
            alias
            for row in world_rows
            if _same_world_name(row.scope_name, source.scope_name)
            and _same_world_name(row.setting_name, source.setting_name)
            for alias in row.accepted_setting_name_aliases
        )
    )
    return source.model_copy(update={"accepted_setting_name_aliases": aliases})


def _world_stage2_name_match(
    expected_name: str | None,
    actual_name: str | None,
    *,
    expected_scope: str | None,
    context_matched: bool,
    source: WorldStage1Gold | None,
    aliases_allowed: bool,
) -> tuple[bool | None, dict[str, str | None]]:
    if _same_world_name(expected_name, actual_name):
        return True, {"status": "MATCH", "method": "EXACT"}
    if not context_matched or source is None or not expected_name or not actual_name:
        return False, {"status": "MISMATCH", "method": "UNRESOLVED"}
    if (
        aliases_allowed
        and _same_world_name(source.scope_name, expected_scope)
        and _same_world_name(source.setting_name, expected_name)
        and any(_same_world_name(alias, actual_name) for alias in source.accepted_setting_names)
    ):
        return True, {"status": "MATCH", "method": "ALIAS"}
    return None, {"status": "PENDING", "method": "UNRESOLVED"}


def _stage2_semantic_cases(
    case: Stage2Case, source_rows: list[Stage1Gold]
) -> list[SemanticOutcomeCase]:
    if case.prediction is None:
        return []
    source = next((row for row in source_rows if isinstance(row, WorldStage1Gold)), None)
    common = {
        "before_value": case.gold.before_value,
        "source_values": tuple(
            value for row in source_rows for value in _stage1_source_values(row)
        ),
        "expected_value": case.gold.proposed_value,
        "actual_value": _stage2_prediction_value(case.prediction),
        "required_facts": tuple(case.gold.required_facts),
        "forbidden_facts": tuple(case.gold.forbidden_facts),
        "evidence_quotes": tuple(quote for row in source_rows for quote in row.evidence_quotes),
    }
    result: list[SemanticOutcomeCase] = []
    if case.semantic_case_id is not None:
        setting_context = None
        if (
            case.proposed_setting_semantic_case_id is not None
            or case.proposed_scope_semantic_case_id is not None
        ):
            assert source is not None
            assert isinstance(case.gold, WorldStage2Gold)
            assert isinstance(case.prediction, WorldStage2Prediction)
            assert case.gold.proposed_setting_name is not None
            setting_context = WorldSettingNameContext(
                category=source.category.value,
                subject_name=source.subject_name,
                scope_name=case.gold.proposed_scope_name,
                expected_setting_name=case.gold.proposed_setting_name,
                actual_setting_name=case.prediction.proposed_setting_name,
                actual_scope_name=case.prediction.proposed_scope_name,
                related_paths=case.related_world_paths,
                operation=case.prediction.operation.value,
            )
        result.append(
            SemanticOutcomeCase(
                case_id=case.semantic_case_id,
                setting_context=setting_context,
                character_context=case.character_context,
                **common,
            )
        )
    if case.matched_property_semantic_case_id is not None:
        assert source is not None
        assert isinstance(case.gold, WorldStage2Gold)
        assert isinstance(case.prediction, WorldStage2Prediction)
        assert case.gold.matched_property_name is not None
        assert case.prediction.matched_property_name is not None
        result.append(
            SemanticOutcomeCase(
                case_id=case.matched_property_semantic_case_id,
                setting_context=WorldSettingNameContext(
                    category=source.category.value,
                    subject_name=source.subject_name,
                    scope_name=case.gold.matched_scope_name,
                    expected_setting_name=case.gold.matched_property_name,
                    actual_setting_name=case.prediction.matched_property_name,
                    actual_scope_name=case.prediction.matched_scope_name,
                    related_paths=case.related_world_paths,
                    operation=case.prediction.operation.value,
                ),
                **common,
            )
        )
    if isinstance(case.gold, CharacterStage2Gold) and case.gold.proposed_value_json is not None:
        result.extend(
            _structured_semantic_cases(
                f"stage2-json:{case.gold.scenario_id}:{case.gold.decision_id}",
                case.gold.proposed_value_json,
                case.prediction.proposed_value_json,
                **{
                    key: value
                    for key, value in common.items()
                    if key not in {"expected_value", "actual_value"}
                },
            )
        )
    return result


def _build_predicted_state_chain(
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
    gold_chain: dict[str, ScenarioStateTransition],
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    prediction_by_scenario: dict[str, ScenarioPrediction],
    selected_ids: set[str],
    enabled_domains: set[EvaluationDomain],
) -> tuple[dict[str, ScenarioStateTransition], list[dict[str, str]]]:
    result: dict[str, ScenarioStateTransition] = {}
    errors: list[dict[str, str]] = []
    gold_stage1_by_id = {item.gold_id: item for item in gold.stage1}
    gold_decision_by_source = {
        source_id: decision for decision in gold.stage2 for source_id in decision.source_gold_ids
    }
    active_ids = _state_dependency_ids(gold, predictions.mode, selected_ids)
    for scenario in sorted(gold.scenarios, key=lambda item: item.episode_no):
        if predictions.mode == EvaluationMode.ROLLING and scenario.previous_scenario_id:
            previous = result.get(scenario.previous_scenario_id)
            before = (
                previous.after_state.model_copy(deep=True)
                if previous is not None
                else gold_chain[scenario.scenario_id].before_state.model_copy(deep=True)
            )
        else:
            before = gold_chain[scenario.scenario_id].before_state.model_copy(deep=True)
        state = before
        scenario_prediction = (
            prediction_by_scenario.get(
                scenario.scenario_id,
                ScenarioPrediction(scenario_id=scenario.scenario_id),
            )
            if scenario.scenario_id in active_ids
            else ScenarioPrediction(scenario_id=scenario.scenario_id)
        )
        stage1_by_candidate = {
            item.candidate_id: item
            for item in scenario_prediction.stage1
            if item.domain in enabled_domains and item.domain in scenario.target_domains
        }
        applied: list[str] = []
        held: list[str] = []
        ordered_predictions = sorted(
            enumerate(
                [
                    item
                    for item in scenario_prediction.stage2
                    if item.domain in enabled_domains and item.domain in scenario.target_domains
                ]
            ),
            key=lambda pair: (
                _prediction_decision_order(
                    pair[1], gold_decision_by_source, default=10**9 + pair[0]
                ),
                pair[0],
            ),
        )
        for _, decision_prediction in ordered_predictions:
            gold_decision = None
            matched_gold_source = None
            source_prediction = stage1_by_candidate.get(decision_prediction.source_candidate_id)
            if predictions.mode == EvaluationMode.ORACLE:
                gold_decision = gold_decision_by_source.get(decision_prediction.source_candidate_id)
                source_gold = gold_stage1_by_id.get(decision_prediction.source_candidate_id)
                if source_gold is not None:
                    matched_gold_source = source_gold
                    source_prediction = _prediction_from_gold(source_gold)
                    if isinstance(source_prediction, WorldStage1Prediction) and isinstance(
                        gold_decision, WorldStage2Gold
                    ):
                        grouped_sources = [
                            gold_stage1_by_id[source_id]
                            for source_id in gold_decision.source_gold_ids
                        ]
                        source_prediction = source_prediction.model_copy(
                            update={
                                "source_values": [
                                    value
                                    for grouped_source in grouped_sources
                                    if isinstance(grouped_source, WorldStage1Gold)
                                    for value in grouped_source.source_values
                                ],
                                "evidence_spans": [
                                    PredictionEvidence(quote=quote)
                                    for grouped_source in grouped_sources
                                    for quote in grouped_source.evidence_quotes
                                ],
                            }
                        )
            else:
                for domain in EvaluationDomain:
                    if domain not in scenario.target_domains:
                        continue
                    matching = stage1_results.get((scenario.scenario_id, domain))
                    if matching is None:
                        continue
                    matched = next(
                        (
                            item
                            for item in matching.matches
                            if item.prediction.candidate_id
                            == decision_prediction.source_candidate_id
                        ),
                        None,
                    )
                    if matched is not None:
                        if matched.identity_matched:
                            matched_gold_source = matched.gold
                            gold_decision = gold_decision_by_source.get(matched.gold.gold_id)
                        break
            if source_prediction is None:
                errors.append(
                    {
                        "scenarioId": scenario.scenario_id,
                        "sourceCandidateId": decision_prediction.source_candidate_id,
                        "reason": "Stage2 prediction has no Stage1 handoff candidate.",
                    }
                )
                continue
            try:
                state, was_held = apply_prediction_decision(
                    state,
                    scenario,
                    source_prediction,
                    decision_prediction,
                    matched_gold_source=matched_gold_source,
                    matched_gold_decision=gold_decision,
                )
            except StateApplicationError:
                errors.append(
                    {
                        "scenarioId": scenario.scenario_id,
                        "sourceCandidateId": decision_prediction.source_candidate_id,
                        "reason": "Prediction decision violates the reference reducer contract.",
                    }
                )
                continue
            identifier = (
                gold_decision.decision_id
                if gold_decision is not None
                else f"prediction:{decision_prediction.source_candidate_id}"
            )
            (held if was_held else applied).append(identifier)
        if (
            EvaluationDomain.CHARACTER in enabled_domains
            and EvaluationDomain.CHARACTER in scenario.target_domains
        ):
            state = _register_prediction_discoveries(
                state,
                scenario,
                [
                    item
                    for item in scenario_prediction.stage1
                    if item.domain == EvaluationDomain.CHARACTER
                ],
                stage1_results,
            ).canonical()
        else:
            state = state.canonical()
        result[scenario.scenario_id] = ScenarioStateTransition(
            scenario_id=scenario.scenario_id,
            before_state=before.canonical(),
            after_state=state,
            applied_decision_ids=tuple(applied),
            held_decision_ids=tuple(held),
        )
    return result, errors


def _state_dependency_ids(
    gold: GoldSnapshotV3,
    mode: EvaluationMode,
    selected_ids: set[str],
) -> set[str]:
    if mode != EvaluationMode.ROLLING:
        return set(selected_ids)
    scenario_by_id = {scenario.scenario_id: scenario for scenario in gold.scenarios}
    result = set(selected_ids)
    pending = list(selected_ids)
    while pending:
        previous_id = scenario_by_id[pending.pop()].previous_scenario_id
        if previous_id is not None and previous_id not in result:
            result.add(previous_id)
            pending.append(previous_id)
    return result


def _build_state_pairs(
    gold: GoldSnapshotV3,
    gold_chain: dict[str, ScenarioStateTransition],
    predicted_chain: dict[str, ScenarioStateTransition],
    selected_ids: set[str],
    semantic_cases: list[SemanticOutcomeCase],
    enabled_domains: set[EvaluationDomain],
    scoring_ref_maps: dict[str, ScoringRefMaps] | None = None,
) -> list[StatePair]:
    pairs: list[StatePair] = []
    scenario_by_id = {scenario.scenario_id: scenario for scenario in gold.scenarios}
    semantic_contexts = _build_state_semantic_contexts(gold, gold_chain)
    for scenario_id in sorted(
        selected_ids,
        key=lambda item: (
            scenario_by_id[item].episode_no,
            scenario_by_id[item].scenario_id,
        ),
    ):
        scenario = scenario_by_id[scenario_id]
        expected = gold_chain[scenario_id].after_state
        actual = predicted_chain[scenario_id].after_state
        for domain in EvaluationDomain:
            if domain not in enabled_domains or domain not in scenario.target_domains:
                continue
            expected_items = _evaluation_state_values(expected, domain)
            actual_items = _evaluation_state_values(actual, domain)
            pending_refs = frozenset()
            maps = (scoring_ref_maps or {}).get(scenario_id)
            ref_map = maps.after if maps else {}
            actual_items = {ref_map.get(ref, ref): value for ref, value in actual_items.items()}
            pending_refs = maps.pending_after if maps else frozenset()
            actual_ref_by_scoring_ref = {
                ref_map.get(ref, ref): ref for ref in _evaluation_state_values(actual, domain)
            }
            for ref in sorted(set(expected_items) | set(actual_items)):
                expected_value = expected_items.get(ref)
                actual_value = actual_items.get(ref)
                if _is_structured_state_ref(ref) and ref not in expected_items:
                    # Gold가 구조화 값을 지정하지 않은 effect는 별도 품질 축에서 제외한다.
                    continue
                identity_matched = None if ref in pending_refs else True
                if ref not in expected_items or ref not in actual_items:
                    pairs.append(
                        StatePair(
                            scenario_id=scenario_id,
                            domain=domain,
                            ref=ref,
                            expected_value=expected_value,
                            actual_value=actual_value,
                            expected_present=ref in expected_items,
                            actual_present=ref in actual_items,
                            matched=False if identity_matched else None,
                            identity_matched=identity_matched,
                        )
                    )
                    continue
                typed_match = (
                    _character_state_value_match(
                        gold, expected, actual, ref, actual_ref_by_scoring_ref.get(ref, ref)
                    )
                    if domain == EvaluationDomain.CHARACTER
                    else None
                )
                if typed_match is not None or normalize_text(expected_value) == normalize_text(
                    actual_value
                ):
                    pairs.append(
                        StatePair(
                            scenario_id=scenario_id,
                            domain=domain,
                            ref=ref,
                            expected_value=expected_value,
                            actual_value=actual_value,
                            expected_present=True,
                            actual_present=True,
                            matched=_all_or_pending(
                                [identity_matched, typed_match if typed_match is not None else True]
                            ),
                            identity_matched=identity_matched,
                        )
                    )
                    continue
                if _is_structured_state_ref(ref):
                    prefix = f"state-json:{scenario_id}:{len(pairs)}"
                    expected_json, actual_json = (
                        json.loads(expected_value),
                        json.loads(actual_value),
                    )
                    semantic_cases.extend(
                        _structured_semantic_cases(
                            prefix,
                            expected_json,
                            actual_json,
                            **_semantic_context_kwargs(
                                semantic_contexts.get(
                                    (scenario_id, domain, _structured_base_ref(ref))
                                )
                            ),
                        )
                    )
                    pairs.append(
                        StatePair(
                            scenario_id=scenario_id,
                            domain=domain,
                            ref=ref,
                            expected_value=expected_value,
                            actual_value=actual_value,
                            expected_present=True,
                            actual_present=True,
                            matched=_all_or_pending(
                                [
                                    identity_matched,
                                    _structured_semantic_result(expected_json, actual_json),
                                ]
                            ),
                            identity_matched=identity_matched,
                            structured_semantic_case_ids=_structured_case_ids(
                                prefix, expected_json, actual_json
                            ),
                        )
                    )
                    continue
                case_id = f"state:{scenario_id}:{domain}:{len(pairs)}"
                pairs.append(
                    StatePair(
                        scenario_id=scenario_id,
                        domain=domain,
                        ref=ref,
                        expected_value=expected_value,
                        actual_value=actual_value,
                        expected_present=True,
                        actual_present=True,
                        matched=None,
                        identity_matched=identity_matched,
                        semantic_case_id=case_id,
                    )
                )
                semantic_cases.append(
                    SemanticOutcomeCase(
                        case_id=case_id,
                        expected_value=expected_value,
                        actual_value=actual_value,
                        **_semantic_context_kwargs(
                            semantic_contexts.get((scenario_id, domain, ref))
                        ),
                    )
                )
    return pairs


def _character_state_value_match(
    gold: GoldSnapshotV3,
    expected: EvaluationState,
    actual: EvaluationState,
    expected_ref: str,
    actual_ref: str,
) -> bool | None:
    if expected_ref.startswith("fact:"):
        left = next(
            (entry for entry in expected.character_facts if f"fact:{entry.ref}" == expected_ref),
            None,
        )
        right = next(
            (entry for entry in actual.character_facts if f"fact:{entry.ref}" == actual_ref), None
        )
        if left is None or right is None:
            return None
        value_type = left.value_type
        if value_type is not None and value_type != right.value_type:
            return False
        expected_value, actual_value = left.value, right.value
    elif expected_ref.startswith("history:"):
        left_id, right_id = (
            json.loads(expected_ref.partition(":")[2]),
            json.loads(actual_ref.partition(":")[2]),
        )
        left = next(
            (
                entry
                for entry in expected.character_history
                if [
                    entry.scenario_id,
                    entry.source_gold_id,
                    entry.entity_ref,
                    entry.fact_type,
                    entry.fact_key,
                    entry.operation,
                ]
                == left_id
            ),
            None,
        )
        right = next(
            (
                entry
                for entry in actual.character_history
                if [
                    entry.scenario_id,
                    entry.source_gold_id,
                    entry.entity_ref,
                    entry.fact_type,
                    entry.fact_key,
                    entry.operation,
                ]
                == right_id
            ),
            None,
        )
        if left is None or right is None:
            return None
        if left.temporal_scope != right.temporal_scope:
            return False
        source = next((row for row in gold.stage1 if row.gold_id == left.source_gold_id), None)
        value_type = source.value_type if isinstance(source, CharacterStage1Gold) else None
        expected_value, actual_value = left.value, right.value
    else:
        return None
    if value_type not in {"NUMBER", "BOOLEAN"}:
        return None
    comparison = compare_typed_value(
        value_type=value_type,
        expected_display_value=expected_value,
        actual_display_value=actual_value,
    )
    return comparison.status == ValueComparisonStatus.MATCH


def _build_state_semantic_contexts(
    gold: GoldSnapshotV3,
    gold_chain: dict[str, ScenarioStateTransition],
) -> dict[tuple[str, EvaluationDomain, str], StateSemanticContext]:
    """Track which reviewed Stage2 decision owns each Gold after-state effect.

    State scoring can require semantic comparison after a paraphrase. The state case
    must retain the same required/forbidden facts as the decision that produced that
    value; otherwise a fluent merge can pass while dropping a required exception.
    """

    stage1_by_id = {row.gold_id: row for row in gold.stage1}
    decisions_by_scenario: dict[str, list[Stage2Gold]] = {}
    for decision in gold.stage2:
        decisions_by_scenario.setdefault(decision.scenario_id, []).append(decision)

    contexts_after: dict[
        str,
        dict[tuple[EvaluationDomain, str], StateSemanticContext],
    ] = {}
    result: dict[tuple[str, EvaluationDomain, str], StateSemanticContext] = {}
    for scenario in sorted(gold.scenarios, key=lambda item: item.episode_no):
        if (
            scenario.start_state_mode == StartStateMode.PREVIOUS_GOLD
            and scenario.previous_scenario_id is not None
        ):
            contexts = dict(contexts_after[scenario.previous_scenario_id])
        else:
            contexts = {}

        comparison_state = gold_chain[scenario.scenario_id].before_state
        state = comparison_state
        for decision in sorted(
            decisions_by_scenario.get(scenario.scenario_id, []),
            key=lambda item: (item.sort_order, item.decision_id),
        ):
            sources = [stage1_by_id[source_id] for source_id in decision.source_gold_ids]
            before_values = _evaluation_state_values(state, decision.domain)
            decision_comparison_state = (
                state if isinstance(decision, CharacterStage2Gold) else comparison_state
            )
            state, _ = apply_gold_decision(
                state,
                scenario,
                sources,
                decision,
                comparison_state=decision_comparison_state,
            )
            after_values = _evaluation_state_values(state, decision.domain)
            context = StateSemanticContext(
                before_value=decision.before_value,
                source_values=tuple(
                    value for source in sources for value in _stage1_source_values(source)
                ),
                required_facts=tuple(decision.required_facts),
                forbidden_facts=tuple(decision.forbidden_facts),
                evidence_quotes=tuple(
                    quote for source in sources for quote in source.evidence_quotes
                ),
            )

            changed_refs = {
                ref
                for ref in set(before_values) | set(after_values)
                if before_values.get(ref) != after_values.get(ref)
            }
            changed_refs.update(_primary_gold_effect_refs(decision, sources))
            for ref in changed_refs:
                key = (decision.domain, ref)
                if ref not in after_values:
                    contexts.pop(key, None)
                else:
                    # Keep source/evidence even without extra annotation constraints;
                    # the new decision replaces any older required/forbidden claims.
                    contexts[key] = context

        contexts_after[scenario.scenario_id] = contexts
        result.update(
            {
                (scenario.scenario_id, domain, ref): context
                for (domain, ref), context in contexts.items()
            }
        )
    return result


def _primary_gold_effect_refs(
    decision: Stage2Gold,
    sources: list[Stage1Gold],
) -> set[str]:
    if isinstance(decision, CharacterStage2Gold):
        source = sources[0]
        assert isinstance(source, CharacterStage1Gold)
        if decision.operation not in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
        }:
            return set()
        assert source.fact_type is not None and source.fact_key is not None
        return {
            "fact:"
            + character_state_ref(
                source.entity_ref,
                source.fact_type,
                source.fact_key,
            )
        }
    if (
        decision.operation
        in {
            WorldSettingOperation.UPDATE,
            WorldSettingOperation.MERGE,
        }
        and decision.target_ref is not None
    ):
        return {f"fact:{decision.target_ref}"}
    return set()


def _semantic_context_kwargs(
    context: StateSemanticContext | None,
) -> dict[str, Any]:
    if context is None:
        return {}
    return {
        "before_value": context.before_value,
        "source_values": context.source_values,
        "required_facts": context.required_facts,
        "forbidden_facts": context.forbidden_facts,
        "evidence_quotes": context.evidence_quotes,
    }


def _structured_case_ids(prefix: str, expected: Any, actual: Any) -> tuple[str, ...]:
    comparison = compare_structured_semantics(expected, actual)
    if not comparison.structural_matched:
        return ()
    return tuple(f"{prefix}#{pair.path}" for pair in comparison.text_pairs)


def _structured_semantic_cases(
    prefix: str, expected: Any, actual: Any, **context: Any
) -> list[SemanticOutcomeCase]:
    comparison = compare_structured_semantics(expected, actual)
    if not comparison.structural_matched:
        return []
    # Required facts constrain the complete outcome, not every individual JSON leaf.
    # Each leaf is compared to its own Gold text, with the source/evidence as context.
    context.pop("required_facts", None)
    context.pop("forbidden_facts", None)
    return [
        SemanticOutcomeCase(
            case_id=f"{prefix}#{pair.path}",
            expected_value=pair.expected,
            actual_value=pair.actual,
            **context,
        )
        for pair in comparison.text_pairs
    ]


def _structured_semantic_result(expected: Any, actual: Any) -> bool | None:
    comparison = compare_structured_semantics(expected, actual)
    if not comparison.structural_matched:
        return False
    return None if comparison.text_pairs else True


def _semantic_results(case_ids: tuple[str, ...], decisions: dict[str, Any]) -> bool | None:
    return _all_or_pending(
        [getattr(decisions.get(case_id), "matched", None) for case_id in case_ids]
    )


def _apply_stage1_structured_results(results: dict, decisions: dict[str, Any]) -> None:
    for key, result in results.items():
        matches = []
        for match in result.matches:
            if isinstance(match.gold, CharacterStage1Gold) and match.gold.structured_scorable:
                case_ids = _structured_case_ids(
                    f"stage1-json:{match.gold.scenario_id}:{match.gold.gold_id}",
                    match.gold.value_json,
                    match.prediction.value_json,
                )
                if case_ids:
                    match = replace(
                        match, structured_value_matched=_semantic_results(case_ids, decisions)
                    )
            matches.append(match)
        results[key] = replace(result, matches=tuple(matches))


def _apply_semantic_results(
    stage2_cases: list[Stage2Case],
    state_pairs: list[StatePair],
    decisions: dict[str, Any],
) -> None:
    for case in stage2_cases:
        if case.prediction is None:
            continue
        decision = decisions.get(case.semantic_case_id) if case.semantic_case_id else None
        if decision is not None and case.value_matched is None:
            case.value_matched = decision.matched
        if case.character_setting_semantic_case_id is not None:
            case.canonical_fact_key_matched = getattr(
                decisions.get(case.character_setting_semantic_case_id), "same_setting", None
            )
        if case.structured_semantic_case_ids:
            case.structured_value_matched = _semantic_results(
                case.structured_semantic_case_ids, decisions
            )
        if case.proposed_scope_semantic_case_id is not None:
            scope_decision = decisions.get(case.proposed_scope_semantic_case_id)
            case.world_proposed_scope_matched = getattr(scope_decision, "scope_equivalent", None)
        if case.proposed_setting_semantic_case_id is not None:
            name_matched = _stage2_semantic_name_result(
                case.setting_name_match,
                decisions.get(case.proposed_setting_semantic_case_id),
            )
        else:
            name_matched = {"MATCH": True, "MISMATCH": False}.get(
                (case.setting_name_match or {}).get("status")
            )
        if isinstance(case.gold, WorldStage2Gold):
            case.proposed_path_matched = _all_or_pending(
                [case.world_proposed_scope_matched, name_matched]
            )
        if case.matched_property_semantic_case_id is not None:
            property_matched = _stage2_semantic_name_result(
                case.matched_property_name_match,
                decisions.get(case.matched_property_semantic_case_id),
            )
            case.target_matched = _all_or_pending(
                [case.world_target_context_matched, property_matched]
            )
        fields = _stage2_scoring_fields(case)
        case.full_decision_matched = _all_or_pending(fields)
        if case.upstream_outcome != UpstreamOutcome.REACHED:
            continue
        if case.target_matched is False and _target_required(case.gold):
            case.failure_cause = FailureCause.RETRIEVAL_MISS
        elif case.full_decision_matched is False:
            case.failure_cause = FailureCause.COMPARISON_ERROR
        else:
            case.failure_cause = None
    for pair in state_pairs:
        if pair.structured_semantic_case_ids:
            pair.matched = _all_or_pending(
                [
                    pair.identity_matched,
                    _semantic_results(pair.structured_semantic_case_ids, decisions),
                ]
            )
        if pair.semantic_case_id is None:
            continue
        decision = decisions.get(pair.semantic_case_id)
        if decision is not None:
            pair.matched = _all_or_pending([pair.identity_matched, decision.matched])


def _stage2_semantic_name_result(match: dict[str, str | None] | None, decision: Any) -> bool | None:
    if match is None:
        return None
    same_setting = getattr(decision, "same_setting", None)
    if type(same_setting) is bool:
        match.update(
            status="MATCH" if same_setting else "MISMATCH",
            method="SEMANTIC",
        )
    return {"MATCH": True, "MISMATCH": False}.get(match["status"])


def _reclassify_semantic_upstream(
    stage2_cases: list[Stage2Case],
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    semantic_decisions: dict[str, Any],
    mode: EvaluationMode,
) -> None:
    if mode == EvaluationMode.ORACLE:
        return
    for case in stage2_cases:
        if case.upstream_outcome != UpstreamOutcome.REACHED:
            continue
        matching = stage1_results.get((case.scenario_id, case.gold.domain))
        if matching is None:
            continue
        source_ids = set(case.gold.source_gold_ids)
        failed_semantic_source = any(
            bool(set(match.source_gold_ids) & source_ids)
            and match.value_status == FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED
            and (
                decision := semantic_decisions.get(
                    f"stage1:{match.gold.scenario_id}:{match.gold.gold_id}"
                )
            )
            is not None
            and decision.matched is False
            for match in matching.matches
        )
        if failed_semantic_source:
            case.upstream_outcome = UpstreamOutcome.UPSTREAM_VALUE_ERROR
            case.failure_cause = FailureCause.EXTRACTION_MISS
        else:
            case.upstream_semantic_pending = any(
                bool(set(match.source_gold_ids) & source_ids)
                and (
                    match.identity_matched is None
                    or _resolved_stage1_value_status(match, semantic_decisions) == "PENDING"
                )
                for match in matching.matches
            )
            if case.upstream_semantic_pending and case.full_decision_matched is True:
                case.full_decision_matched = None


def _build_stage1_report(
    gold: GoldSnapshotV3,
    results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    selected_ids: set[str],
    semantic_decisions: dict[str, Any],
    *,
    evaluated: bool,
    enabled_domains: set[EvaluationDomain],
) -> dict[EvaluationDomain, dict[str, Any]]:
    report = {}
    for domain in EvaluationDomain:
        if domain not in enabled_domains:
            report[domain] = {"evaluated": False, "reason": "Domain not selected."}
            continue
        if not evaluated:
            report[domain] = {"evaluated": False, "reason": "ORACLE isolates Stage2."}
            continue
        domain_results = [
            result
            for (scenario_id, result_domain), result in results.items()
            if scenario_id in selected_ids and result_domain == domain
        ]
        matches = [match for result in domain_results for match in result.matches]
        gold_positive = sum(result.gold_group_count for result in domain_results)
        prediction_count = sum(result.grouped_prediction_count for result in domain_results)
        true_positive = sum(match.identity_matched is True for match in matches)
        precision, recall, f1 = _prf(true_positive, prediction_count, gold_positive)
        identity_pending = any(match.identity_matched is None for match in matches)
        if identity_pending:
            precision = recall = f1 = None
        value_results: list[bool] = []
        pending = 0
        for match in matches:
            if match.value_status == FieldMatchStatus.NOT_APPLICABLE:
                continue
            if match.value_status == FieldMatchStatus.MATCH:
                value_results.append(True)
            elif match.value_status == FieldMatchStatus.MISMATCH:
                value_results.append(False)
            else:
                case_id = f"stage1:{match.gold.scenario_id}:{match.gold.gold_id}"
                semantic = semantic_decisions.get(case_id)
                if semantic is None or semantic.matched is None:
                    pending += 1
                else:
                    value_results.append(semantic.matched)
        weighted_gold = sum(
            (match.gold.importance.weight if match.gold.importance else 1) for match in matches
        ) + sum(
            (missed.importance.weight if missed.importance else 1)
            for result in domain_results
            for missed in result.missed_gold
        )
        weighted_hit = sum(
            (match.gold.importance.weight if match.gold.importance else 1)
            for match in matches
            if match.identity_matched
        )
        resolved_value_accuracy = _accuracy(value_results)
        total_value_cases = len(value_results) + pending
        report[domain] = {
            "evaluated": True,
            "metrics": {
                "candidatePrecision": precision,
                "candidateRecall": recall,
                "candidateF1": f1,
                "weightedRecall": None if identity_pending else _ratio(weighted_hit, weighted_gold),
                "entityOrSubjectAccuracy": _accuracy(
                    [match.entity_or_subject_matched for match in matches]
                ),
                "pathOrFactAccuracy": None
                if identity_pending
                else _accuracy([match.path_or_fact_matched for match in matches]),
                "valueAccuracy": None if pending else resolved_value_accuracy,
                "resolvedValueAccuracy": resolved_value_accuracy,
                "valueLowerBoundAccuracy": _ratio(
                    sum(value_results),
                    total_value_cases,
                ),
                "valueSemanticCoverage": _ratio(
                    len(value_results),
                    total_value_cases,
                ),
                "valueTypeAccuracy": _accuracy(
                    [
                        match.value_type_matched
                        for match in matches
                        if match.value_type_matched is not None
                    ]
                ),
                "structuredValueAccuracy": None
                if any(
                    isinstance(match.gold, CharacterStage1Gold)
                    and match.gold.structured_scorable
                    and match.structured_value_matched is None
                    for match in matches
                )
                else _accuracy(
                    [
                        match.structured_value_matched
                        for match in matches
                        if match.structured_value_matched is not None
                    ]
                ),
                "evidenceLocatableRate": _ratio(
                    sum(match.evidence.locatable_quote_count for match in matches),
                    sum(match.evidence.quote_count for match in matches),
                ),
                "evidenceCoverageRate": _ratio(
                    sum(match.evidence.covered_gold_quote_count for match in matches),
                    sum(match.evidence.gold_quote_count for match in matches),
                ),
            },
            "counts": {
                "gold": gold_positive,
                "predictions": prediction_count,
                "matches": len(matches),
                "identityTruePositive": true_positive,
                "missed": sum(len(result.missed_source_gold_ids) for result in domain_results),
                "extra": sum(len(result.extra_predictions) for result in domain_results),
                "hardNegativeHits": sum(
                    len(result.hard_negative_hits) for result in domain_results
                ),
                "semanticPending": sum(
                    match.identity_matched is None
                    or _resolved_stage1_value_status(match, semantic_decisions) == "PENDING"
                    or (
                        isinstance(match.gold, CharacterStage1Gold)
                        and match.gold.structured_scorable
                        and match.structured_value_matched is None
                    )
                    for match in matches
                ),
                "rawPredictions": sum(result.raw_prediction_count for result in domain_results),
                "handoffPredictions": sum(
                    result.handoff_prediction_count for result in domain_results
                ),
                "groupedPredictions": prediction_count,
            },
        }
    return report


def _build_stage2_report(
    cases: list[Stage2Case],
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    selected_ids: set[str],
    enabled_domains: set[EvaluationDomain],
) -> dict[EvaluationDomain, dict[str, Any]]:
    report = {}
    prediction_by_scenario = {item.scenario_id: item for item in predictions.scenarios}
    for domain in EvaluationDomain:
        if domain not in enabled_domains:
            report[domain] = {"evaluated": False, "reason": "Domain not selected."}
            continue
        domain_cases = [case for case in cases if case.gold.domain == domain]
        upstream_reached = [
            case for case in domain_cases if case.upstream_outcome == UpstreamOutcome.REACHED
        ]
        reached = [case for case in upstream_reached if case.prediction is not None]
        outcome_counts = Counter(case.upstream_outcome.value for case in domain_cases)
        operation_values = [case.operation_matched for case in upstream_reached]
        full_values = [
            case.full_decision_matched
            for case in upstream_reached
            if case.full_decision_matched is not None
        ]
        semantic_pending = sum(case.full_decision_matched is None for case in upstream_reached)
        proposed_value_values = [
            case.value_matched for case in reached if case.value_matched is not None
        ]
        proposed_value_pending = sum(case.value_matched is None for case in reached)
        safe_cases = [case for case in upstream_reached if _is_safe_noop(case.gold)]
        harmful = sum(
            _is_mutating_prediction(case.prediction)
            for case in safe_cases
            if case.prediction is not None
        )
        auto_cases = [
            case
            for case in reached
            if case.prediction is not None and not _is_review_prediction(case.prediction)
        ]
        auto_values = [
            case.full_decision_matched
            for case in auto_cases
            if case.full_decision_matched is not None
        ]
        auto_pending = sum(case.full_decision_matched is None for case in auto_cases)
        review_gold = [case for case in upstream_reached if _is_review_decision(case.gold)]
        review_correct = sum(
            case.prediction is not None and _is_review_prediction(case.prediction)
            for case in review_gold
        )
        extra_predictions, suppressed_extras = _extra_suppression_counts(
            domain,
            selected_ids,
            prediction_by_scenario,
            stage1_results,
        )
        resolved_full_accuracy = _accuracy(full_values)
        resolved_proposed_value_accuracy = _accuracy(proposed_value_values)
        resolved_selective_accuracy = _accuracy(auto_values)
        metrics = {
            "upstreamReachRate": _ratio(len(upstream_reached), len(domain_cases)),
            "operationAccuracy": _accuracy(operation_values),
            "characterCanonicalFactKeyResolutionAccuracy": None
            if any(
                isinstance(case.gold, CharacterStage2Gold)
                and case.canonical_fact_key_matched is None
                for case in reached
            )
            else _accuracy(
                [
                    case.canonical_fact_key_matched
                    for case in reached
                    if case.canonical_fact_key_matched is not None
                ]
            ),
            "targetAccuracy": None
            if any(case.target_matched is None for case in reached)
            else _accuracy(
                [case.target_matched for case in reached if case.target_matched is not None]
            ),
            "removedSnapshotSetAccuracy": _accuracy(
                [case.removed_matched for case in reached if case.removed_matched is not None]
            ),
            "temporalAccuracy": _accuracy(
                [case.temporal_matched for case in reached if case.temporal_matched is not None]
            ),
            "consolidationAccuracy": _accuracy(
                [
                    case.consolidation_matched
                    for case in reached
                    if case.consolidation_matched is not None
                ]
            ),
            "proposedPathAccuracy": None
            if any(
                isinstance(case.gold, WorldStage2Gold) and case.proposed_path_matched is None
                for case in reached
            )
            else _accuracy(
                [
                    case.proposed_path_matched
                    for case in reached
                    if case.proposed_path_matched is not None
                ]
            ),
            "existingRootPropertyMoveSetAccuracy": _accuracy(
                [
                    case.root_property_moves_matched
                    for case in reached
                    if case.root_property_moves_matched is not None
                ]
            ),
            "proposedValueAccuracy": (
                None if proposed_value_pending else resolved_proposed_value_accuracy
            ),
            "resolvedProposedValueAccuracy": resolved_proposed_value_accuracy,
            "proposedValueLowerBoundAccuracy": _ratio(
                sum(proposed_value_values),
                len(proposed_value_values) + proposed_value_pending,
            ),
            "proposedValueSemanticCoverage": _ratio(
                len(proposed_value_values),
                len(proposed_value_values) + proposed_value_pending,
            ),
            "proposedValueJsonAccuracy": None
            if any(
                isinstance(case.gold, CharacterStage2Gold)
                and case.gold.proposed_value_json is not None
                and case.structured_value_matched is None
                for case in reached
            )
            else _accuracy(
                [
                    case.structured_value_matched
                    for case in reached
                    if case.structured_value_matched is not None
                ]
            ),
            "fullDecisionAccuracy": (None if semantic_pending else resolved_full_accuracy),
            "resolvedFullDecisionAccuracy": resolved_full_accuracy,
            "fullDecisionLowerBoundAccuracy": _ratio(
                sum(full_values),
                len(full_values) + semantic_pending,
            ),
            "semanticCoverage": _ratio(
                len(full_values),
                len(full_values) + semantic_pending,
            ),
            "selectiveCoverage": _ratio(len(auto_cases), len(upstream_reached)),
            "selectiveAccuracy": (None if auto_pending else resolved_selective_accuracy),
            "resolvedSelectiveAccuracy": resolved_selective_accuracy,
            "selectiveLowerBoundAccuracy": _ratio(
                sum(auto_values),
                len(auto_values) + auto_pending,
            ),
            "reviewRequiredRecall": _ratio(review_correct, len(review_gold)),
            "falsePositiveSuppressionRate": _ratio(suppressed_extras, extra_predictions),
            "harmfulActionRate": _ratio(harmful, len(safe_cases)),
        }
        if predictions.mode == EvaluationMode.ORACLE:
            metrics["oracleAccuracy"] = metrics["fullDecisionAccuracy"]
            metrics["liveConditionalAccuracy"] = None
        else:
            metrics["oracleAccuracy"] = None
            metrics["liveConditionalAccuracy"] = metrics["fullDecisionAccuracy"]
        report[domain] = {
            "metrics": metrics,
            "counts": {
                "gold": len(domain_cases),
                "waitingForCharacterMatch": sum(
                    _is_waiting_character_gold(row)
                    and row.scenario_id in selected_ids
                    and row.domain == domain
                    for row in gold.stage1
                ),
                "upstreamReached": len(upstream_reached),
                "reachedAndCompared": len(reached),
                "semanticPending": semantic_pending,
                "upstreamOutcomes": dict(sorted(outcome_counts.items())),
                "safeNoopCases": len(safe_cases),
                "harmfulActions": harmful,
                "extraStage1Predictions": extra_predictions,
                "suppressedExtraPredictions": suppressed_extras,
            },
        }
    return report


def _build_end_to_end_report(
    gold: GoldSnapshotV3,
    gold_chain: dict[str, ScenarioStateTransition],
    predicted_chain: dict[str, ScenarioStateTransition],
    state_pairs: list[StatePair],
    state_application_errors: list[dict[str, str]],
    selected_ids: set[str],
    enabled_domains: set[EvaluationDomain],
    scoring_ref_maps: dict[str, ScoringRefMaps] | None = None,
) -> dict[str, Any]:
    domain_reports = {}
    for domain in EvaluationDomain:
        if domain not in enabled_domains:
            domain_reports[domain] = {"evaluated": False, "reason": "Domain not selected."}
            continue
        pairs = [pair for pair in state_pairs if pair.domain == domain]
        state_metrics = _state_pair_metrics(pairs)
        domain_reports[domain] = {
            "afterStatePrecision": state_metrics["precision"],
            "afterStateRecall": state_metrics["recall"],
            "afterStateF1": state_metrics["f1"],
            "resolvedAfterStatePrecision": state_metrics["resolvedPrecision"],
            "resolvedAfterStateRecall": state_metrics["resolvedRecall"],
            "resolvedAfterStateF1": state_metrics["resolvedF1"],
            "afterStateLowerBoundF1": state_metrics["lowerBoundF1"],
            "semanticCoverage": state_metrics["semanticCoverage"],
            "semanticPending": state_metrics["semanticPending"],
        }
    transition_counts = Counter()
    scenario_rows = []
    for scenario in gold.scenarios:
        if scenario.scenario_id not in selected_ids:
            continue
        gold_transition = gold_chain[scenario.scenario_id]
        predicted_transition = predicted_chain[scenario.scenario_id]
        expected_delta = _state_delta(
            gold_transition.before_state,
            gold_transition.after_state,
            structured_reference_before=gold_transition.before_state,
            structured_reference_after=gold_transition.after_state,
        )
        actual_delta = _state_delta(
            predicted_transition.before_state,
            predicted_transition.after_state,
            structured_reference_before=gold_transition.before_state,
            structured_reference_after=gold_transition.after_state,
            scoring_ref_maps=(scoring_ref_maps or {}).get(scenario.scenario_id),
        )
        scenario_domains = enabled_domains & scenario.target_domains
        expected_delta = {
            key: value
            for key, value in expected_delta.items()
            if EvaluationDomain(key[0]) in scenario_domains
        }
        actual_delta = {
            key: value
            for key, value in actual_delta.items()
            if EvaluationDomain(key[0]) in scenario_domains
        }
        scorable_state_refs = {
            (pair.domain.value, pair.ref)
            for pair in state_pairs
            if pair.scenario_id == scenario.scenario_id
        }
        expected_delta = {
            key: value
            for key, value in expected_delta.items()
            if not _is_structured_state_ref(key[2]) or (key[0], key[2]) in scorable_state_refs
        }
        actual_delta = {
            key: value
            for key, value in actual_delta.items()
            if not _is_structured_state_ref(key[2]) or (key[0], key[2]) in scorable_state_refs
        }
        transition_counts["expected"] += len(expected_delta)
        transition_counts["predicted"] += len(actual_delta)
        scenario_pairs = [pair for pair in state_pairs if pair.scenario_id == scenario.scenario_id]
        pair_by_ref = {(pair.domain.value, pair.ref): pair for pair in scenario_pairs}
        pending_expected = pending_actual = 0
        maps = (scoring_ref_maps or {}).get(scenario.scenario_id)
        for key in set(expected_delta) | set(actual_delta):
            pair = pair_by_ref.get((key[0], key[2]))
            identity_pending = (
                key[1] == "REMOVE" and maps is not None and key[2] in maps.pending_before
            )
            if (pair is not None and pair.matched is None) or identity_pending:
                pending_expected += key in expected_delta
                pending_actual += key in actual_delta
                continue
            if key not in expected_delta or key not in actual_delta:
                continue
            expected_value = expected_delta[key]
            actual_value = actual_delta[key]
            if key[1] in {"ADD", "UPDATE"} and pair is not None and pair.matched is False:
                continue
            if normalize_text(expected_value) == normalize_text(actual_value):
                transition_counts["matched"] += 1
                continue
            if key[1] in {"ADD", "UPDATE"} and pair is not None and pair.matched is True:
                transition_counts["matched"] += 1
        transition_counts["semanticPending"] += max(pending_expected, pending_actual)
        transition_counts["pendingExpected"] += pending_expected
        transition_counts["pendingActual"] += pending_actual
        scenario_state_metrics = _state_pair_metrics(scenario_pairs)
        scenario_f1 = scenario_state_metrics["f1"]
        scenario_rows.append(
            {
                "scenarioId": scenario.scenario_id,
                "episodeNo": scenario.episode_no,
                "afterStateF1": scenario_f1,
                "afterStateLowerBoundF1": scenario_state_metrics["lowerBoundF1"],
                "semanticPending": scenario_state_metrics["semanticPending"],
                "rollingStateDivergence": (None if scenario_f1 is None else 1 - scenario_f1),
                "expectedStateHash": gold_transition.after_state.content_hash(),
                "predictedStateHash": predicted_transition.after_state.content_hash(),
            }
        )
    lower_transition = _prf(
        transition_counts["matched"],
        transition_counts["predicted"],
        transition_counts["expected"],
    )
    pending_transitions = transition_counts["semanticPending"]
    resolved_transition = _prf(
        transition_counts["matched"],
        transition_counts["predicted"] - transition_counts["pendingActual"],
        transition_counts["expected"] - transition_counts["pendingExpected"],
    )
    transition_precision, transition_recall, transition_f1 = (
        (None, None, None) if pending_transitions else lower_transition
    )
    enabled_reports = [
        domain_reports[domain] for domain in EvaluationDomain if domain in enabled_domains
    ]
    macro_f1 = (
        None
        if any(report.get("semanticPending", 0) for report in enabled_reports)
        else _mean([report.get("afterStateF1") for report in enabled_reports])
    )
    lower_bound_macro_f1 = _mean(
        [report.get("afterStateLowerBoundF1") for report in enabled_reports]
    )
    resolved_macro_f1 = _mean([report.get("resolvedAfterStateF1") for report in enabled_reports])
    selected_state_errors = sum(
        item["scenarioId"] in selected_ids for item in state_application_errors
    )
    return {
        "metrics": {
            "afterStateF1": macro_f1,
            "resolvedAfterStateF1": resolved_macro_f1,
            "afterStateLowerBoundF1": lower_bound_macro_f1,
            "transitionPrecision": transition_precision,
            "transitionRecall": transition_recall,
            "transitionF1": transition_f1,
            "resolvedTransitionPrecision": resolved_transition[0],
            "resolvedTransitionRecall": resolved_transition[1],
            "resolvedTransitionF1": resolved_transition[2],
            "transitionLowerBoundF1": lower_transition[2],
            "rollingStateDivergence": (None if macro_f1 is None else 1 - macro_f1),
        },
        "domains": domain_reports,
        "counts": {
            "stateApplicationErrors": selected_state_errors,
            "dependencyStateApplicationErrors": (
                len(state_application_errors) - selected_state_errors
            ),
            "expectedTransitions": transition_counts["expected"],
            "predictedTransitions": transition_counts["predicted"],
            "matchedTransitions": transition_counts["matched"],
            "semanticPendingTransitions": pending_transitions,
        },
        "scenarios": scenario_rows,
    }


def _state_pair_metrics(pairs: list[StatePair]) -> dict[str, float | int | None]:
    correct = sum(pair.matched is True for pair in pairs)
    expected = sum(pair.expected_present for pair in pairs)
    predicted = sum(pair.actual_present for pair in pairs)
    pending = sum(pair.matched is None for pair in pairs)
    lower_precision, lower_recall, lower_f1 = _prf(correct, predicted, expected)
    resolved_precision, resolved_recall, resolved_f1 = _prf(
        correct,
        predicted - sum(pair.actual_present and pair.matched is None for pair in pairs),
        expected - sum(pair.expected_present and pair.matched is None for pair in pairs),
    )
    precision, recall, f1 = (
        (None, None, None) if pending else (lower_precision, lower_recall, lower_f1)
    )
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "resolvedPrecision": resolved_precision,
        "resolvedRecall": resolved_recall,
        "resolvedF1": resolved_f1,
        "lowerBoundF1": lower_f1,
        "semanticCoverage": _ratio(len(pairs) - pending, len(pairs)),
        "semanticPending": pending,
    }


def _scenario_details(
    gold: GoldSnapshotV3,
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    stage2_cases: list[Stage2Case],
    gold_chain: dict[str, ScenarioStateTransition],
    predicted_chain: dict[str, ScenarioStateTransition],
    state_errors: list[dict[str, str]],
    selected_ids: set[str],
    semantic_decisions: dict[str, Any],
) -> list[dict[str, Any]]:
    details = []
    stage1_gold_by_id = {item.gold_id: item for item in gold.stage1}
    for scenario in gold.scenarios:
        if scenario.scenario_id not in selected_ids:
            continue
        domain_stage1 = {}
        for domain in EvaluationDomain:
            if domain not in scenario.target_domains:
                continue
            result = stage1_results.get((scenario.scenario_id, domain))
            if result is not None:
                domain_stage1[domain.value] = {
                    "missedGoldIds": [
                        gold_id
                        for group_ids in result.missed_source_gold_ids
                        for gold_id in group_ids
                    ],
                    "extraPredictionIds": [item.candidate_id for item in result.extra_predictions],
                    "upstreamOutcomes": {
                        gold_id: item.upstream_outcome.value
                        for item in result.matches
                        for gold_id in item.source_gold_ids
                    }
                    | {
                        gold_id: UpstreamOutcome.UPSTREAM_MISSING.value
                        for group_ids in result.missed_source_gold_ids
                        for gold_id in group_ids
                    },
                    "cases": _stage1_diagnostic_cases(result, semantic_decisions),
                }
        cases = [case for case in stage2_cases if case.scenario_id == scenario.scenario_id]
        details.append(
            {
                "scenarioId": scenario.scenario_id,
                "episodeNo": scenario.episode_no,
                "stage1": domain_stage1,
                "stage2": [
                    {
                        "decisionId": case.gold.decision_id,
                        "domain": case.gold.domain,
                        "upstreamOutcome": case.upstream_outcome,
                        "failureCause": case.failure_cause,
                        "operationMatched": case.operation_matched,
                        "characterCanonicalFactKeyResolutionMatched": (
                            case.canonical_fact_key_matched
                        ),
                        "targetMatched": case.target_matched,
                        "removedSnapshotSetMatched": case.removed_matched,
                        "existingRootPropertyMoveSetMatched": (case.root_property_moves_matched),
                        "valueMatched": case.value_matched,
                        "fullDecisionMatched": case.full_decision_matched,
                        **_stage2_diagnostic_fields(
                            case,
                            stage1_gold_by_id,
                            gold_chain[scenario.scenario_id].before_state,
                            predicted_chain[scenario.scenario_id].before_state,
                        ),
                    }
                    for case in cases
                ],
                "beforeStateHash": gold_chain[scenario.scenario_id].before_state.content_hash(),
                "expectedAfterStateHash": gold_chain[
                    scenario.scenario_id
                ].after_state.content_hash(),
                "predictedAfterStateHash": predicted_chain[
                    scenario.scenario_id
                ].after_state.content_hash(),
                "stateErrors": [
                    item for item in state_errors if item["scenarioId"] == scenario.scenario_id
                ],
            }
        )
    return details


def _stage1_diagnostic_cases(
    result: Stage1MatchingResult,
    semantic_decisions: dict[str, Any],
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for match in result.matches:
        value_status = _resolved_stage1_value_status(match, semantic_decisions)
        full_match = match.identity_matched and value_status in {
            FieldMatchStatus.MATCH.value,
            FieldMatchStatus.NOT_APPLICABLE.value,
        }
        upstream_outcome = match.upstream_outcome
        if (
            match.value_status == FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED
            and value_status == FieldMatchStatus.MISMATCH.value
        ):
            upstream_outcome = UpstreamOutcome.UPSTREAM_VALUE_ERROR
        cases.append(
            {
                "result": "FULL_MATCH" if full_match else "PARTIAL_MATCH",
                "sortOrder": match.gold.sort_order,
                "goldIds": list(match.source_gold_ids),
                "predictionId": match.prediction.candidate_id,
                "expected": _stage1_diagnostic_summary(match.gold),
                "actual": _stage1_diagnostic_summary(match.prediction),
                "fields": {
                    "subject": _boolean_match_status(match.entity_or_subject_matched),
                    "path": _boolean_match_status(
                        match.path_or_fact_matched, none_status="PENDING"
                    ),
                    "value": value_status,
                },
                "upstreamOutcome": upstream_outcome.value,
                **_stage1_stage2_policy_diagnostic(match.gold),
                **_stage1_setting_name_diagnostic(match, semantic_decisions),
            }
        )
    for missed, source_gold_ids in zip(
        result.missed_gold,
        result.missed_source_gold_ids,
        strict=True,
    ):
        cases.append(
            {
                "result": "MISSED",
                "sortOrder": missed.sort_order,
                "goldIds": list(source_gold_ids),
                "predictionId": None,
                "expected": _stage1_diagnostic_summary(missed),
                "actual": None,
                "fields": {
                    "subject": "MISSING",
                    "path": "MISSING",
                    "value": "MISSING",
                },
                "upstreamOutcome": UpstreamOutcome.UPSTREAM_MISSING.value,
                **_stage1_stage2_policy_diagnostic(missed),
            }
        )
    for index, prediction in enumerate(result.extra_predictions):
        cases.append(
            {
                "result": "EXTRA",
                "sortOrder": prediction.sort_order,
                "goldIds": [],
                "predictionId": prediction.candidate_id,
                "expected": None,
                "actual": _stage1_diagnostic_summary(prediction),
                "fields": {
                    "subject": "UNMATCHED",
                    "path": "UNMATCHED",
                    "value": "UNMATCHED",
                },
                "upstreamOutcome": UpstreamOutcome.UPSTREAM_EXTRA.value,
                "extraOrder": index,
            }
        )
    return sorted(
        cases,
        key=lambda item: (
            item["sortOrder"],
            item.get("extraOrder", -1),
            item["result"],
            item["predictionId"] or "",
        ),
    )


def _is_waiting_character_gold(gold: Stage1Gold) -> bool:
    return (
        isinstance(gold, CharacterStage1Gold)
        and gold.decision == GoldDecision.EXTRACT
        and gold.stage2_policy == Stage2Policy.WAIT_FOR_CHARACTER_MATCH
    )


def _stage1_stage2_policy_diagnostic(gold: Stage1Gold) -> dict[str, str]:
    if _is_waiting_character_gold(gold):
        return {"stage2Policy": gold.stage2_policy.value}
    return {}


def _resolved_stage1_value_status(
    match: Stage1Match,
    semantic_decisions: dict[str, Any],
) -> str:
    if match.value_status != FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED:
        return match.value_status.value
    case_id = f"stage1:{match.gold.scenario_id}:{match.gold.gold_id}"
    decision = semantic_decisions.get(case_id)
    if decision is None or decision.matched is None:
        return "PENDING"
    return FieldMatchStatus.MATCH.value if decision.matched else FieldMatchStatus.MISMATCH.value


def _stage1_diagnostic_summary(item: Stage1Gold | Stage1Prediction) -> dict[str, str | None]:
    if isinstance(item, (CharacterStage1Gold, CharacterStage1Prediction)):
        subject = (
            item.matched_character_name or item.entity_name
            if isinstance(item, CharacterStage1Prediction)
            else item.entity_name
        )
        if item.candidate_kind == CandidateKind.CHARACTER_DISCOVERY:
            path = CandidateKind.CHARACTER_DISCOVERY.value
        else:
            fact_type = item.fact_type.value if item.fact_type is not None else "-"
            path = f"{fact_type} › {item.fact_key or '-'}"
        return {
            "subject": subject,
            "path": path,
            "value": item.display_value,
        }
    return {
        "subject": f"{item.category.value} · {item.subject_name}",
        "path": _world_diagnostic_path(item.scope_name, item.setting_name),
        "value": item.display_value,
    }


def _stage2_diagnostic_fields(
    case: Stage2Case,
    stage1_gold_by_id: dict[str, Stage1Gold],
    expected_before_state: EvaluationState,
    actual_before_state: EvaluationState,
) -> dict[str, Any]:
    prediction = case.prediction
    if case.upstream_outcome != UpstreamOutcome.REACHED:
        result = "UPSTREAM_BLOCKED"
    elif prediction is None:
        result = "COMPARATOR_MISSING"
    elif case.full_decision_matched is None:
        result = "SEMANTIC_PENDING"
    elif case.full_decision_matched:
        result = "FULL_MATCH"
    else:
        result = "DECISION_MISMATCH"

    source = stage1_gold_by_id.get(case.gold.source_gold_ids[0])
    expected: dict[str, Any] = {
        "operation": case.gold.operation.value,
        "target": _stage2_target_label(case.gold.target_ref, expected_before_state),
        "path": _stage2_gold_path(case.gold, source),
        "value": case.gold.proposed_value,
    }
    if isinstance(case.gold, CharacterStage2Gold):
        expected["temporalScope"] = case.gold.temporal_scope.value
        expected["removedCount"] = len(case.gold.removed_snapshot_refs)
        expected["removedPaths"] = _character_removal_paths(
            case.gold.removed_snapshot_refs,
            expected_before_state,
        )
    elif isinstance(case.gold, WorldStage2Gold):
        expected["consolidationStatus"] = case.gold.consolidation_status.value
        expected["rootMoveCount"] = len(case.gold.existing_root_property_names_to_move)
        expected["rootMoveNames"] = sorted(
            case.gold.existing_root_property_names_to_move,
            key=normalize_world_setting_name,
        )
    actual: dict[str, Any] | None = None
    fields: dict[str, str] = {}
    if prediction is None and case.upstream_outcome == UpstreamOutcome.REACHED:
        fields["operation"] = FieldMatchStatus.MISMATCH.value
    elif prediction is not None:
        actual = {
            "operation": prediction.operation.value,
            "target": _stage2_target_label(prediction.target_ref, actual_before_state),
            "path": _stage2_prediction_path(prediction),
            "value": prediction.proposed_value,
        }
        fields = {
            "operation": _boolean_match_status(case.operation_matched),
            "target": _boolean_match_status(case.target_matched, none_status="PENDING"),
            "value": _boolean_match_status(case.value_matched, none_status="PENDING"),
        }
        if isinstance(case.gold, CharacterStage2Gold):
            assert isinstance(prediction, CharacterStage2Prediction)
            actual["temporalScope"] = prediction.temporal_scope.value
            actual["removedCount"] = len(prediction.removed_snapshot_refs)
            actual["removedPaths"] = _character_removal_paths(
                prediction.removed_snapshot_refs,
                actual_before_state,
            )
            fields["canonicalPath"] = _boolean_match_status(
                case.canonical_fact_key_matched, none_status="PENDING"
            )
            fields["temporal"] = _boolean_match_status(case.temporal_matched)
            if case.removed_matched is not None:
                fields["removedSet"] = _boolean_match_status(case.removed_matched)
            if case.gold.proposed_value_json is not None:
                fields["structuredValue"] = _boolean_match_status(
                    case.structured_value_matched, none_status="PENDING"
                )
        elif isinstance(case.gold, WorldStage2Gold):
            assert isinstance(prediction, WorldStage2Prediction)
            actual["consolidationStatus"] = prediction.consolidation_status.value
            actual["rootMoveCount"] = len(prediction.existing_root_property_names_to_move)
            actual["rootMoveNames"] = sorted(
                prediction.existing_root_property_names_to_move,
                key=normalize_world_setting_name,
            )
            fields["consolidation"] = _boolean_match_status(case.consolidation_matched)
            fields["proposedPath"] = _boolean_match_status(
                case.proposed_path_matched, none_status="PENDING"
            )
            if case.world_path_preserved_matched is not None:
                fields["pathPreservation"] = _boolean_match_status(
                    case.world_path_preserved_matched
                )
            if case.world_application_matched is not None:
                fields["stateApplication"] = _boolean_match_status(case.world_application_matched)
            if case.root_property_moves_matched is not None:
                fields["rootMoveSet"] = _boolean_match_status(case.root_property_moves_matched)

    return {
        "result": result,
        "sourceGoldIds": list(case.gold.source_gold_ids),
        "sourceCandidateId": prediction.source_candidate_id if prediction else None,
        "expected": expected,
        "actual": actual,
        "fields": fields,
        **({"settingNameMatch": case.setting_name_match} if case.setting_name_match else {}),
        **(
            {"matchedPropertyNameMatch": case.matched_property_name_match}
            if case.matched_property_name_match
            else {}
        ),
    }


def _character_removal_paths(
    refs: list[str],
    state: EvaluationState,
) -> list[str]:
    entries = {item.ref: item for item in state.character_facts}
    paths = []
    unresolved = 0
    for ref in refs:
        entry = entries.get(ref)
        if entry is None:
            fallback_path = _character_path_from_state_ref(ref)
            if fallback_path is None:
                unresolved += 1
            else:
                paths.append(fallback_path)
            continue
        paths.append(f"{entry.entity_name} · {entry.fact_type.value} › {entry.fact_key}")
    if unresolved:
        paths.append(f"현재 상태에서 식별되지 않은 대상 {unresolved}건")
    return sorted(paths, key=normalize_text)


def _character_path_from_state_ref(ref: str) -> str | None:
    parts = ref.split(":", 4)
    if len(parts) != 5 or parts[:2] != ["gold", "character"]:
        return None
    fact_type = _decode_state_ref_segment(parts[3])
    fact_key = _decode_state_ref_segment(parts[4])
    if not fact_type or not fact_key:
        return None
    return f"{fact_type} › {fact_key} (ref에서 canonical 경로 해석)"


def _decode_state_ref_segment(value: str) -> str:
    return value.replace("%3A", ":").replace("%25", "%")


def _stage2_target_label(ref: str | None, state: EvaluationState) -> str | None:
    if ref is None:
        return None
    character = next((item for item in state.character_facts if item.ref == ref), None)
    if character is not None:
        return f"{character.entity_name} · {character.fact_type.value} › {character.fact_key}"
    world_property = next((item for item in state.world_facts if item.ref == ref), None)
    if world_property is not None:
        return (
            f"{world_property.category.value} · {world_property.subject_name} · "
            f"{_world_diagnostic_path(world_property.scope_name, world_property.setting_name)}"
        )
    world_subject = next(
        (item for item in state.world_facts if world_entry_subject_ref(item) == ref),
        None,
    )
    if world_subject is not None:
        return f"{world_subject.category.value} · {world_subject.subject_name} (주체)"
    return _state_target_label_from_ref(ref) or "현재 상태에서 식별되지 않은 대상"


def _state_target_label_from_ref(ref: str) -> str | None:
    character_path = _character_path_from_state_ref(ref)
    if character_path is not None:
        return character_path
    parts = ref.split(":")
    if parts[:2] == ["gold", "world-subject"] and len(parts) == 4:
        return (
            f"{_decode_state_ref_segment(parts[2])} · "
            f"{_decode_state_ref_segment(parts[3])} (주체, ref에서 해석)"
        )
    if parts[:2] == ["gold", "world-subject-ref"] and len(parts) == 3:
        return "WORLD 주체 (ref에서 해석)"
    if parts[:2] not in (["gold", "world"], ["gold", "world-by-subject-ref"]):
        return None
    if len(parts) not in {5, 6}:
        return None
    category = _decode_state_ref_segment(parts[2])
    subject = _decode_state_ref_segment(parts[3]) if parts[1] == "world" else "canonical 주체"
    path_parts = parts[4:] if len(parts) == 6 else parts[-1:]
    path = " › ".join(_decode_state_ref_segment(item) for item in path_parts)
    return f"{category} · {subject} · {path} (ref에서 해석)"


def _stage2_gold_path(
    decision: Stage2Gold,
    source: Stage1Gold | None,
) -> str | None:
    if isinstance(decision, CharacterStage2Gold):
        return source.fact_key if isinstance(source, CharacterStage1Gold) else None
    return _world_diagnostic_path(
        decision.proposed_scope_name,
        decision.proposed_setting_name,
    )


def _stage2_prediction_path(decision: Stage2Prediction) -> str:
    if isinstance(decision, CharacterStage2Prediction):
        return decision.resolved_canonical_fact_key
    return _world_diagnostic_path(
        decision.proposed_scope_name,
        decision.proposed_setting_name,
    )


def _world_diagnostic_path(scope_name: str | None, setting_name: str) -> str:
    return f"{scope_name} › {setting_name}" if scope_name else setting_name


def _boolean_match_status(
    value: bool | None,
    *,
    none_status: str = "NOT_APPLICABLE",
) -> str:
    if value is None:
        return none_status
    return FieldMatchStatus.MATCH.value if value else FieldMatchStatus.MISMATCH.value


def _combined_upstream_outcome(outcomes: list[UpstreamOutcome]) -> UpstreamOutcome:
    priority = (
        UpstreamOutcome.UPSTREAM_MISSING,
        UpstreamOutcome.UPSTREAM_BLOCKED_SUBJECT,
        UpstreamOutcome.UPSTREAM_PARTIAL,
        UpstreamOutcome.UPSTREAM_VALUE_ERROR,
    )
    for outcome in priority:
        if outcome in outcomes:
            return outcome
    return UpstreamOutcome.REACHED


def _stage2_scoring_fields(case: Stage2Case) -> list[bool | None]:
    if isinstance(case.gold, CharacterStage2Gold):
        fields = [
            case.operation_matched,
            case.canonical_fact_key_matched,
            case.target_matched,
            case.temporal_matched,
            case.value_matched,
        ]
        if case.removed_matched is not None:
            fields.append(case.removed_matched)
        if case.gold.proposed_value_json:
            fields.append(case.structured_value_matched)
        return fields
    fields = [
        case.operation_matched,
        case.target_matched,
        case.consolidation_matched,
        case.proposed_path_matched,
        case.value_matched,
    ]
    if case.root_property_moves_matched is not None:
        fields.append(case.root_property_moves_matched)
    if case.world_path_preserved_matched is not None:
        fields.append(case.world_path_preserved_matched)
    if case.world_application_matched is not None:
        fields.append(case.world_application_matched)
    return fields


def _all_or_pending(values: list[bool | None]) -> bool | None:
    applicable = [value for value in values if value is not None]
    if any(value is False for value in applicable):
        return False
    if len(applicable) != len(values):
        # None은 N/A와 pending을 함께 표현한다. 호출부는 N/A 필드를 목록에서 제거해야 한다.
        return None
    return all(applicable)


def _same_ref(expected: str | None, actual: str | None) -> bool:
    return (expected or "").strip() == (actual or "").strip()


def _same_ref_set(expected: list[str], actual: list[str]) -> bool:
    return {item.strip() for item in expected} == {item.strip() for item in actual}


def _same_world_name(expected: str | None, actual: str | None) -> bool:
    return normalize_world_setting_name(expected or "") == normalize_world_setting_name(
        actual or ""
    )


def _same_world_name_set(expected: list[str], actual: list[str]) -> bool:
    return {normalize_world_setting_name(item) for item in expected} == {
        normalize_world_setting_name(item) for item in actual
    }


def _target_required(decision: Stage2Gold) -> bool:
    return decision.operation in {
        CharacterFactComparisonOperation.UPDATE,
        CharacterFactComparisonOperation.MERGE,
        WorldSettingOperation.UPDATE,
        WorldSettingOperation.MERGE,
    }


def _is_safe_noop(decision: Stage2Gold) -> bool:
    return (
        isinstance(decision, CharacterStage2Gold)
        and decision.operation
        in {
            CharacterFactComparisonOperation.HISTORY_ONLY,
            CharacterFactComparisonOperation.EXCLUDE,
            CharacterFactComparisonOperation.REVIEW_REQUIRED,
        }
    ) or (
        isinstance(decision, WorldStage2Gold)
        and (
            decision.operation
            in {
                WorldSettingOperation.EXCLUDE,
                WorldSettingOperation.REVIEW_REQUIRED,
            }
            or decision.consolidation_status == WorldSettingConsolidationStatus.CONFLICT
        )
    )


def _is_mutating_prediction(prediction: Stage2Prediction | None) -> bool:
    if isinstance(prediction, CharacterStage2Prediction):
        return prediction.operation in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
            CharacterFactComparisonOperation.REMOVE,
        }
    if isinstance(prediction, WorldStage2Prediction):
        return (
            prediction.operation
            in {
                WorldSettingOperation.ADD,
                WorldSettingOperation.UPDATE,
                WorldSettingOperation.MERGE,
            }
            and prediction.consolidation_status != WorldSettingConsolidationStatus.CONFLICT
        )
    return False


def _is_review_prediction(prediction: Stage2Prediction) -> bool:
    return (
        isinstance(prediction, CharacterStage2Prediction)
        and prediction.operation == CharacterFactComparisonOperation.REVIEW_REQUIRED
    ) or (
        isinstance(prediction, WorldStage2Prediction)
        and prediction.operation == WorldSettingOperation.REVIEW_REQUIRED
    )


def _is_review_decision(decision: Stage2Gold) -> bool:
    return (
        isinstance(decision, CharacterStage2Gold)
        and decision.operation == CharacterFactComparisonOperation.REVIEW_REQUIRED
    ) or (
        isinstance(decision, WorldStage2Gold)
        and decision.operation == WorldSettingOperation.REVIEW_REQUIRED
    )


def _extra_suppression_counts(
    domain: EvaluationDomain,
    selected_ids: set[str],
    prediction_by_scenario: dict[str, ScenarioPrediction],
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
) -> tuple[int, int]:
    extra_ids: set[tuple[str, str]] = set()
    for (scenario_id, result_domain), result in stage1_results.items():
        if scenario_id in selected_ids and result_domain == domain:
            extra_ids.update(
                (scenario_id, prediction.candidate_id) for prediction in result.extra_predictions
            )
    suppressed = 0
    for scenario_id, candidate_id in extra_ids:
        scenario = prediction_by_scenario.get(scenario_id)
        if scenario is None:
            continue
        decision = next(
            (item for item in scenario.stage2 if item.source_candidate_id == candidate_id),
            None,
        )
        if isinstance(decision, CharacterStage2Prediction):
            suppressed += decision.operation in {
                CharacterFactComparisonOperation.EXCLUDE,
                CharacterFactComparisonOperation.REVIEW_REQUIRED,
            }
        elif isinstance(decision, WorldStage2Prediction):
            suppressed += decision.operation in {
                WorldSettingOperation.EXCLUDE,
                WorldSettingOperation.REVIEW_REQUIRED,
            }
    return len(extra_ids), suppressed


def _prediction_from_gold(gold: Stage1Gold) -> Stage1Prediction:
    if isinstance(gold, CharacterStage1Gold):
        return CharacterStage1Prediction(
            candidate_id=gold.gold_id,
            domain="CHARACTER",
            candidate_kind=gold.candidate_kind,
            entity_name=gold.entity_name,
            matched_character_name=gold.entity_name,
            match_status="MATCHED",
            raw_entity_mention=gold.raw_entity_mention,
            fact_type=gold.fact_type,
            fact_key=gold.fact_key,
            value_type=gold.value_type,
            display_value=gold.display_value,
            value_json=gold.value_json,
            evidence_spans=[{"quote": quote} for quote in gold.evidence_quotes],
        )
    return WorldStage1Prediction(
        candidate_id=gold.gold_id,
        domain="WORLD",
        category=gold.category,
        subject_name=gold.subject_name,
        scope_name=gold.scope_name,
        setting_name=gold.setting_name,
        source_values=gold.source_values,
        evidence_spans=[{"quote": quote} for quote in gold.evidence_quotes],
    )


def _register_prediction_discoveries(
    state: EvaluationState,
    scenario: ScenarioGold,
    predictions: list[Stage1Prediction],
    matching_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
) -> EvaluationState:
    from evals.multi_stage_setting.contracts import KnownCharacter

    known = {item.entity_ref: item for item in state.known_characters}
    matching = matching_results.get((scenario.scenario_id, EvaluationDomain.CHARACTER))
    gold_by_prediction = (
        {
            item.prediction.candidate_id: item.gold
            for item in matching.matches
            if item.identity_matched
        }
        if matching is not None
        else {}
    )
    for prediction in predictions:
        if not isinstance(prediction, CharacterStage1Prediction) or (
            prediction.candidate_kind != CandidateKind.CHARACTER_DISCOVERY
        ):
            continue
        gold_source = gold_by_prediction.get(prediction.candidate_id)
        entity_ref = (
            gold_source.entity_ref
            if isinstance(gold_source, CharacterStage1Gold)
            else prediction.entity_ref
            or f"prediction:{normalize_text(prediction.entity_name).replace(' ', '-')}"
        )
        known.setdefault(
            entity_ref,
            KnownCharacter(entity_ref=entity_ref, name=prediction.entity_name),
        )
    for fact in state.character_facts:
        known.setdefault(
            fact.entity_ref,
            KnownCharacter(entity_ref=fact.entity_ref, name=fact.entity_name),
        )
    return state.model_copy(update={"known_characters": list(known.values())})


def _prediction_decision_order(
    prediction: Stage2Prediction,
    gold_by_source: dict[str, Stage2Gold],
    *,
    default: int,
) -> int:
    gold = gold_by_source.get(prediction.source_candidate_id)
    return default if gold is None else gold.sort_order


def _evaluation_state_values(
    state: EvaluationState,
    domain: EvaluationDomain,
) -> dict[str, str | None]:
    if domain == EvaluationDomain.CHARACTER:
        values: dict[str, str | None] = {}
        for item in state.character_facts:
            values[f"fact:{item.ref}"] = item.value
            if item.value_json:
                values[f"fact-json:{item.ref}"] = _canonical_json(item.value_json)
        values.update(
            {f"known-character:{item.entity_ref}": item.name for item in state.known_characters}
        )
        for item in state.character_history:
            identity = (
                item.scenario_id,
                item.source_gold_id,
                item.entity_ref,
                item.fact_type,
                item.fact_key,
                item.operation.value,
            )
            values[_effect_ref("history", identity)] = (
                f"{item.temporal_scope.value}\n{item.value or ''}"
            )
            if item.value_json:
                values[_effect_ref("history-json", identity)] = _canonical_json(item.value_json)
        return values
    values = {f"fact:{item.ref}": item.value for item in state.world_facts}
    values.update(
        {
            _effect_ref(
                "held-conflict",
                (item.scenario_id, item.decision_id),
            ): "\n".join(item.source_values)
            for item in state.held_world_conflicts
        }
    )
    return values


def _effect_ref(prefix: str, parts: tuple[str, ...]) -> str:
    """Build an unambiguous internal E2E effect identity."""

    return (
        prefix
        + ":"
        + json.dumps(
            parts,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_structured_state_ref(ref: str) -> bool:
    return ref.startswith(("fact-json:", "history-json:"))


def _structured_state_matches(
    expected: str | None,
    actual: str | None,
) -> bool:
    if expected is None or actual is None:
        return False
    try:
        expected_json = json.loads(expected)
        actual_json = json.loads(actual)
    except (TypeError, ValueError):  # pragma: no cover - generated internally
        return False
    return _json_contains_native(expected_json, actual_json)


def _json_contains_native(expected: Any, actual: Any) -> bool:
    """Compare a Gold JSON subset without coercing native JSON scalar types."""

    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _json_contains_native(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _json_contains_native(left, right)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    if isinstance(expected, bool):
        return isinstance(actual, bool) and expected == actual
    if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        return (
            isinstance(actual, (int, float, Decimal))
            and not isinstance(actual, bool)
            and Decimal(str(expected)) == Decimal(str(actual))
        )
    if expected is None:
        return actual is None
    return (
        isinstance(expected, str)
        and isinstance(actual, str)
        and normalize_text(expected) == normalize_text(actual)
    )


def _state_delta(
    before: EvaluationState,
    after: EvaluationState,
    *,
    structured_reference_before: EvaluationState,
    structured_reference_after: EvaluationState,
    scoring_ref_maps: ScoringRefMaps | None = None,
) -> dict[tuple[str, str, str], str | None]:
    result: dict[tuple[str, str, str], str | None] = {}
    for domain in EvaluationDomain:
        before_items = _evaluation_state_values(before, domain)
        after_items = _evaluation_state_values(after, domain)
        before_map = scoring_ref_maps.before if scoring_ref_maps else {}
        after_map = scoring_ref_maps.after if scoring_ref_maps else {}
        reference_before_items = _evaluation_state_values(
            structured_reference_before,
            domain,
        )
        reference_after_items = _evaluation_state_values(
            structured_reference_after,
            domain,
        )
        scorable_structured_refs = {
            ref
            for ref in set(reference_before_items) | set(reference_after_items)
            if _is_structured_state_ref(ref)
        }
        before_items = _project_structured_state_items(
            before_items,
            reference_before_items,
            scorable_structured_refs,
            before_map,
        )
        after_items = _project_structured_state_items(
            after_items,
            reference_after_items,
            scorable_structured_refs,
            after_map,
        )
        for ref in set(before_items) | set(after_items):
            if ref not in before_items:
                result[(domain.value, "ADD", after_map.get(ref, ref))] = after_items[ref]
            elif ref not in after_items:
                result[(domain.value, "REMOVE", before_map.get(ref, ref))] = before_items[ref]
            elif normalize_text(before_items[ref]) != normalize_text(after_items[ref]):
                result[(domain.value, "UPDATE", after_map.get(ref, ref))] = after_items[ref]
    return result


def _project_structured_state_items(
    actual_items: dict[str, str | None],
    reference_items: dict[str, str | None],
    scorable_refs: set[str],
    scoring_ref_map: dict[str, str] | None = None,
) -> dict[str, str | None]:
    projected = {
        ref: value for ref, value in actual_items.items() if not _is_structured_state_ref(ref)
    }
    for raw_ref, actual in actual_items.items():
        ref = (scoring_ref_map or {}).get(raw_ref, raw_ref)
        if ref not in scorable_refs:
            continue
        if ref not in reference_items:
            # JSON이 단순 미기재된 상태라면 그 경계에서는 평가하지 않는다. 반면
            # 기반 fact/history 자체가 없어야 하는 경계에 예측 JSON이 남아 있으면
            # presence marker로 ADD/REMOVE 실패를 보존한다.
            if _structured_base_ref(ref) in reference_items:
                continue
            projected[raw_ref] = '{"$present":true}'
            continue
        expected = reference_items[ref]
        if expected is None or actual is None:
            projected[raw_ref] = actual
            continue
        try:
            projected_value = _project_json_value(
                json.loads(expected),
                json.loads(actual),
            )
        except (TypeError, ValueError):  # pragma: no cover - generated internally
            projected[raw_ref] = actual
        else:
            projected[raw_ref] = json.dumps(
                projected_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    return projected


def _structured_base_ref(ref: str) -> str:
    if ref.startswith("fact-json:"):
        return "fact:" + ref.removeprefix("fact-json:")
    return "history:" + ref.removeprefix("history-json:")


def _project_json_value(expected: Any, actual: Any) -> Any:
    """Project prediction JSON onto the Gold subset with typed scalar identity."""

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return {"$typeMismatch": actual}
        return {
            "$object": {
                key: (
                    _project_json_value(value, actual[key]) if key in actual else {"$missing": True}
                )
                for key, value in sorted(expected.items())
            }
        }
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return {"$typeMismatch": actual}
        values = [
            _project_json_value(value, actual[index]) if index < len(actual) else {"$missing": True}
            for index, value in enumerate(expected)
        ]
        return {"$list": values, "$length": len(actual)}
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            return {"$typeMismatch": actual}
        return {"$boolean": actual}
    if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float, Decimal)) or isinstance(actual, bool):
            return {"$typeMismatch": actual}
        parsed = Decimal(str(actual))
        canonical = "0" if parsed == 0 else format(parsed.normalize(), "f")
        return {"$number": canonical}
    if expected is None:
        return {"$null": actual is None}
    if not isinstance(actual, str):
        return {"$typeMismatch": actual}
    return {"$string": normalize_text(actual)}


def _stage1_display_value(item: Stage1Gold | Stage1Prediction) -> str | None:
    if isinstance(item, (CharacterStage1Gold, CharacterStage1Prediction)):
        return item.display_value
    return item.display_value


def _stage1_source_values(item: Stage1Gold) -> list[str]:
    if isinstance(item, WorldStage1Gold):
        return item.source_values
    return [item.display_value] if item.display_value is not None else []


def _stage2_prediction_value(item: Stage2Prediction) -> str | None:
    return item.proposed_value


def _character_source_value_type(gold: CharacterStage2Gold) -> str:
    # Stage2 JSON 자체에는 type을 중복 저장하지 않는다. text는 semantic fallback으로
    # 비교하고 구조화 JSON은 별도 subset 지표로 본다.
    if gold.proposed_value_json and isinstance(gold.proposed_value_json.get("value"), bool):
        return "BOOLEAN"
    if gold.proposed_value_json and isinstance(gold.proposed_value_json.get("value"), (int, float)):
        return "NUMBER"
    return "STRING"


def _prediction_usage(predictions: PredictionBundleV3) -> dict[str, Any]:
    input_tokens = sum(item.input_tokens for item in predictions.scenarios)
    cached_tokens = sum(item.cached_input_tokens for item in predictions.scenarios)
    output_tokens = sum(item.output_tokens for item in predictions.scenarios)
    costs = [
        item.estimated_cost_usd
        for item in predictions.scenarios
        if item.estimated_cost_usd is not None
    ]
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_tokens,
        "outputTokens": output_tokens,
        "estimatedCostUsd": (str(sum(costs, Decimal(0))) if costs else None),
    }


def _runtime_failure_summary(predictions: PredictionBundleV3) -> dict[str, Any]:
    failures = [failure for scenario in predictions.scenarios for failure in scenario.failures]
    by_stage = Counter(failure.stage for failure in failures)
    by_error_type = Counter(failure.error_type for failure in failures)
    return {
        "total": len(failures),
        "byStage": dict(sorted(by_stage.items())),
        "byErrorType": dict(sorted(by_error_type.items())),
    }


def _macro_stage_scores(
    stage1: dict[EvaluationDomain, dict[str, Any]],
    stage2: dict[EvaluationDomain, dict[str, Any]],
) -> dict[str, float | None]:
    evaluated_stage2 = [
        stage2[domain] for domain in EvaluationDomain if stage2[domain].get("evaluated", True)
    ]
    has_stage2_pending = any(
        report.get("counts", {}).get("semanticPending", 0) for report in evaluated_stage2
    )
    return {
        "stage1CandidateF1": _mean(
            [stage1[domain].get("metrics", {}).get("candidateF1") for domain in EvaluationDomain]
        ),
        "stage2FullDecisionAccuracy": (
            None
            if has_stage2_pending
            else _mean(
                [
                    report.get("metrics", {}).get("fullDecisionAccuracy")
                    for report in evaluated_stage2
                ]
            )
        ),
        "stage2ResolvedFullDecisionAccuracy": _mean(
            [
                report.get("metrics", {}).get("resolvedFullDecisionAccuracy")
                for report in evaluated_stage2
            ]
        ),
        "stage2FullDecisionLowerBoundAccuracy": _mean(
            [
                report.get("metrics", {}).get("fullDecisionLowerBoundAccuracy")
                for report in evaluated_stage2
            ]
        ),
    }


def _prf(
    true_positive: int,
    predicted_count: int,
    gold_count: int,
) -> tuple[float | None, float | None, float | None]:
    precision = _ratio(true_positive, predicted_count)
    recall = _ratio(true_positive, gold_count)
    f1 = _ratio(2 * true_positive, predicted_count + gold_count)
    return precision, recall, f1


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _accuracy(values: list[bool | None]) -> float | None:
    resolved = [value for value in values if value is not None]
    return _ratio(sum(resolved), len(resolved))


def _mean(values: list[float | None]) -> float | None:
    resolved = [value for value in values if value is not None]
    return None if not resolved else sum(resolved) / len(resolved)
