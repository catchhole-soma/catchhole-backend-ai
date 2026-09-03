from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Any

from app.domain.enums import (
    CharacterFactComparisonOperation,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
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
    GoldSnapshotV3,
    PredictionBundleV3,
    PredictionEvidence,
    ScenarioGold,
    ScenarioPrediction,
    StartStateMode,
    Stage1Gold,
    Stage1Prediction,
    Stage2Gold,
    Stage2Prediction,
    UpstreamOutcome,
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStage2Prediction,
    character_state_ref,
)
from evals.multi_stage_setting.matching import (
    FieldMatchStatus,
    Stage1MatchingResult,
    match_stage1,
)
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeCase,
    SemanticOutcomeJudge,
)
from evals.multi_stage_setting.state_effects import (
    ScenarioStateTransition,
    StateApplicationError,
    apply_gold_decision,
    apply_prediction_decision,
    build_gold_state_chain,
)
from evals.setting_extraction.normalization import (
    normalize_text,
    parse_boolean,
    parse_decimal,
)
from evals.setting_extraction.value_comparator import (
    ValueComparisonStatus,
    compare_typed_value,
    json_contains,
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
    value_matched: bool | None = None
    structured_value_matched: bool | None = None
    full_decision_matched: bool | None = None
    semantic_case_id: str | None = None


@dataclass
class StatePair:
    scenario_id: str
    domain: EvaluationDomain
    ref: str
    expected_value: str | None
    actual_value: str | None
    matched: bool | None
    semantic_case_id: str | None = None


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
    unknown_prediction_scenarios = sorted(
        set(prediction_by_scenario) - scenario_by_id.keys()
    )
    if unknown_prediction_scenarios:
        raise ValueError(
            f"Predictions reference unknown scenarios: {unknown_prediction_scenarios}"
        )
    _validate_oracle_stage2_sources(gold, predictions)

    selected_ids = set(
        predictions.evaluation_scenario_ids or gold.evaluation_scenario_ids
    )
    unknown_selected_ids = sorted(selected_ids - scenario_by_id.keys())
    if unknown_selected_ids:
        raise ValueError(
            f"Prediction bundle selects unknown scenarios: {unknown_selected_ids}"
        )
    enabled_domains = predictions.evaluation_domains
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult] = {}
    semantic_cases: list[SemanticOutcomeCase] = []
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
                )
                stage1_results[(scenario.scenario_id, domain)] = result
                for match in result.matches:
                    if match.value_status == FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED:
                        case_id = f"stage1:{scenario.scenario_id}:{match.gold.gold_id}"
                        semantic_cases.append(
                            SemanticOutcomeCase(
                                case_id=case_id,
                                expected_value=_stage1_display_value(match.gold),
                                actual_value=_stage1_display_value(match.prediction),
                                source_values=tuple(_stage1_source_values(match.gold)),
                                evidence_quotes=tuple(match.gold.evidence_quotes),
                            )
                        )

    stage2_cases = _evaluate_stage2_cases(
        gold,
        predictions,
        stage1_results,
        prediction_by_scenario,
        selected_ids,
        semantic_cases,
        enabled_domains,
    )
    predicted_chain, state_application_errors = _build_predicted_state_chain(
        gold,
        predictions,
        gold_chain,
        stage1_results,
        prediction_by_scenario,
        selected_ids,
        enabled_domains,
    )
    state_pairs = _build_state_pairs(
        gold,
        gold_chain,
        predicted_chain,
        selected_ids,
        semantic_cases,
        enabled_domains,
    )

    semantic_decisions = {}
    judge_usage = {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0}
    if semantic_judge is not None and semantic_cases:
        judged = await semantic_judge.judge_many(semantic_cases)
        semantic_decisions = {item.case_id: item for item in judged.decisions}
        judge_usage = {
            "inputTokens": judged.input_tokens,
            "cachedInputTokens": judged.cached_input_tokens,
            "outputTokens": judged.output_tokens,
        }

    _apply_semantic_results(stage2_cases, state_pairs, semantic_decisions)
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
    )
    failure_causes = Counter(
        case.failure_cause.value
        for case in stage2_cases
        if case.failure_cause is not None
    )
    failure_causes[FailureCause.STATE_APPLICATION_ERROR] += sum(
        item["scenarioId"] in selected_ids for item in state_application_errors
    )
    for (scenario_id, _), result in stage1_results.items():
        if scenario_id not in selected_ids:
            continue
        failure_causes[FailureCause.UPSTREAM_FALSE_POSITIVE] += len(
            result.extra_predictions
        )

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
        ),
    }


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


def _evaluate_stage2_cases(
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
    stage1_results: dict[tuple[str, EvaluationDomain], Stage1MatchingResult],
    prediction_by_scenario: dict[str, ScenarioPrediction],
    selected_ids: set[str],
    semantic_cases: list[SemanticOutcomeCase],
    enabled_domains: set[EvaluationDomain],
) -> list[Stage2Case]:
    cases: list[Stage2Case] = []
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
                matching.outcome_by_gold_id.get(
                    source_id, UpstreamOutcome.UPSTREAM_MISSING
                )
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
                if item.domain == decision.domain
                and item.source_candidate_id in candidate_ids
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
        if isinstance(decision, CharacterStage2Gold):
            source = next(
                item
                for item in gold.stage1
                if item.gold_id == decision.source_gold_ids[0]
            )
            assert isinstance(source, CharacterStage1Gold)
            expected_character_fact_key = source.fact_key
        case = _score_stage2_case(
            decision,
            prediction,
            expected_character_fact_key=expected_character_fact_key,
        )
        case.scenario_id = decision.scenario_id
        case.upstream_outcome = outcome
        if case.target_matched is False and _target_required(decision):
            case.failure_cause = FailureCause.RETRIEVAL_MISS
        elif case.full_decision_matched is False:
            case.failure_cause = FailureCause.COMPARISON_ERROR
        if case.semantic_case_id is not None:
            source_rows = [
                item for item in gold.stage1 if item.gold_id in decision.source_gold_ids
            ]
            semantic_cases.append(
                SemanticOutcomeCase(
                    case_id=case.semantic_case_id,
                    before_value=decision.before_value,
                    source_values=tuple(
                        value
                        for row in source_rows
                        for value in _stage1_source_values(row)
                    ),
                    expected_value=decision.proposed_value,
                    actual_value=_stage2_prediction_value(prediction),
                    required_facts=tuple(decision.required_facts),
                    forbidden_facts=tuple(decision.forbidden_facts),
                    evidence_quotes=tuple(
                        quote for row in source_rows for quote in row.evidence_quotes
                    ),
                )
            )
        cases.append(case)
    return cases


def _score_stage2_case(
    gold: CharacterStage2Gold | WorldStage2Gold,
    prediction: CharacterStage2Prediction | WorldStage2Prediction,
    *,
    expected_character_fact_key: str | None = None,
) -> Stage2Case:
    if isinstance(gold, CharacterStage2Gold) and isinstance(
        prediction, CharacterStage2Prediction
    ):
        value_comparison = compare_typed_value(
            value_type=None if gold.proposed_value is None else _character_source_value_type(gold),
            expected_display_value=gold.proposed_value,
            actual_display_value=prediction.proposed_value,
            expected_value_json=gold.proposed_value_json,
            actual_value_json=prediction.proposed_value_json,
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
            fields.append(value_comparison.structured_value_matched)
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
            structured_value_matched=value_comparison.structured_value_matched,
            full_decision_matched=_all_or_pending(fields),
            semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}"
                if value_matched is None
                else None
            ),
        )
    if isinstance(gold, WorldStage2Gold) and isinstance(prediction, WorldStage2Prediction):
        exact_value = normalize_text(gold.proposed_value) == normalize_text(
            prediction.proposed_value
        )
        values_absent = gold.proposed_value is None and not prediction.proposed_value
        value_matched = True if exact_value or values_absent else None
        target_matched = (
            _same_ref(gold.target_ref, prediction.target_ref)
            and normalize_text(gold.matched_scope_name)
            == normalize_text(prediction.matched_scope_name)
            and normalize_text(gold.matched_property_name)
            == normalize_text(prediction.matched_property_name)
        )
        path_matched = (
            normalize_text(gold.proposed_scope_name)
            == normalize_text(prediction.proposed_scope_name)
            and normalize_text(gold.proposed_setting_name)
            == normalize_text(prediction.proposed_setting_name)
        )
        fields = [
            gold.operation == prediction.operation,
            target_matched,
            gold.consolidation_status == prediction.consolidation_status,
            path_matched,
            value_matched,
        ]
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
            value_matched=value_matched,
            full_decision_matched=_all_or_pending(fields),
            semantic_case_id=(
                f"stage2:{gold.scenario_id}:{gold.decision_id}"
                if value_matched is None
                else None
            ),
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
        source_id: decision
        for decision in gold.stage2
        for source_id in decision.source_gold_ids
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
                    if item.domain in enabled_domains
                    and item.domain in scenario.target_domains
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
            source_prediction = stage1_by_candidate.get(
                decision_prediction.source_candidate_id
            )
            if predictions.mode == EvaluationMode.ORACLE:
                gold_decision = gold_decision_by_source.get(
                    decision_prediction.source_candidate_id
                )
                source_gold = gold_stage1_by_id.get(
                    decision_prediction.source_candidate_id
                )
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
                            gold_decision = gold_decision_by_source.get(
                                matched.gold.gold_id
                            )
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
            for ref in sorted(set(expected_items) | set(actual_items)):
                expected_value = expected_items.get(ref)
                actual_value = actual_items.get(ref)
                if _is_structured_state_ref(ref) and ref not in expected_items:
                    # Gold가 구조화 값을 지정하지 않은 effect는 별도 품질 축에서 제외한다.
                    continue
                if ref not in expected_items or ref not in actual_items:
                    pairs.append(
                        StatePair(
                            scenario_id,
                            domain,
                            ref,
                            expected_value,
                            actual_value,
                            False,
                        )
                    )
                    continue
                if normalize_text(expected_value) == normalize_text(actual_value):
                    pairs.append(
                        StatePair(
                            scenario_id,
                            domain,
                            ref,
                            expected_value,
                            actual_value,
                            True,
                        )
                    )
                    continue
                if _is_structured_state_ref(ref):
                    pairs.append(
                        StatePair(
                            scenario_id,
                            domain,
                            ref,
                            expected_value,
                            actual_value,
                            _structured_state_matches(expected_value, actual_value),
                        )
                    )
                    continue
                case_id = f"state:{scenario_id}:{domain}:{len(pairs)}"
                pairs.append(
                    StatePair(
                        scenario_id,
                        domain,
                        ref,
                        expected_value,
                        actual_value,
                        None,
                        case_id,
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
                elif context.required_facts or context.forbidden_facts:
                    contexts[key] = context
                else:
                    # A later decision owns the value even when it has no semantic
                    # annotations; do not inherit stale claims from an older value.
                    contexts.pop(key, None)

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
    if decision.operation in {
        WorldSettingOperation.UPDATE,
        WorldSettingOperation.MERGE,
    } and decision.target_ref is not None:
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


def _apply_semantic_results(
    stage2_cases: list[Stage2Case],
    state_pairs: list[StatePair],
    decisions: dict[str, Any],
) -> None:
    for case in stage2_cases:
        if case.semantic_case_id is None:
            continue
        decision = decisions.get(case.semantic_case_id)
        if decision is None:
            continue
        case.value_matched = decision.matched
        fields = _stage2_scoring_fields(case)
        case.full_decision_matched = _all_or_pending(fields)
        if case.full_decision_matched is False and case.failure_cause is None:
            case.failure_cause = FailureCause.COMPARISON_ERROR
    for pair in state_pairs:
        if pair.semantic_case_id is None:
            continue
        decision = decisions.get(pair.semantic_case_id)
        if decision is not None:
            pair.matched = decision.matched


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
            and not decision.matched
            for match in matching.matches
        )
        if failed_semantic_source:
            case.upstream_outcome = UpstreamOutcome.UPSTREAM_VALUE_ERROR
            case.failure_cause = FailureCause.EXTRACTION_MISS


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
        true_positive = sum(match.identity_matched for match in matches)
        precision, recall, f1 = _prf(true_positive, prediction_count, gold_positive)
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
                if semantic is None:
                    pending += 1
                else:
                    value_results.append(semantic.matched)
        weighted_gold = sum(
            (match.gold.importance.weight if match.gold.importance else 1)
            for match in matches
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
                "weightedRecall": _ratio(weighted_hit, weighted_gold),
                "entityOrSubjectAccuracy": _accuracy(
                    [match.entity_or_subject_matched for match in matches]
                ),
                "pathOrFactAccuracy": _accuracy(
                    [match.path_or_fact_matched for match in matches]
                ),
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
                "structuredValueAccuracy": _accuracy(
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
                "missed": sum(
                    len(result.missed_source_gold_ids) for result in domain_results
                ),
                "extra": sum(len(result.extra_predictions) for result in domain_results),
                "hardNegativeHits": sum(
                    len(result.hard_negative_hits) for result in domain_results
                ),
                "semanticPending": pending,
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
            case
            for case in domain_cases
            if case.upstream_outcome == UpstreamOutcome.REACHED
        ]
        reached = [case for case in upstream_reached if case.prediction is not None]
        outcome_counts = Counter(case.upstream_outcome.value for case in domain_cases)
        operation_values = [case.operation_matched for case in upstream_reached]
        full_values = [
            case.full_decision_matched
            for case in upstream_reached
            if case.full_decision_matched is not None
        ]
        semantic_pending = sum(
            case.full_decision_matched is None for case in upstream_reached
        )
        proposed_value_values = [
            case.value_matched
            for case in reached
            if case.value_matched is not None
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
            if case.prediction is not None
            and not _is_review_prediction(case.prediction)
        ]
        auto_values = [
            case.full_decision_matched
            for case in auto_cases
            if case.full_decision_matched is not None
        ]
        auto_pending = sum(case.full_decision_matched is None for case in auto_cases)
        review_gold = [
            case
            for case in upstream_reached
            if isinstance(case.gold, CharacterStage2Gold)
            and case.gold.operation == CharacterFactComparisonOperation.REVIEW_REQUIRED
        ]
        review_correct = sum(
            isinstance(case.prediction, CharacterStage2Prediction)
            and case.prediction.operation
            == CharacterFactComparisonOperation.REVIEW_REQUIRED
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
            "characterCanonicalFactKeyResolutionAccuracy": _accuracy(
                [
                    case.canonical_fact_key_matched
                    for case in reached
                    if case.canonical_fact_key_matched is not None
                ]
            ),
            "targetAccuracy": _accuracy(
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
            "proposedPathAccuracy": _accuracy(
                [
                    case.proposed_path_matched
                    for case in reached
                    if case.proposed_path_matched is not None
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
            "proposedValueJsonAccuracy": _accuracy(
                [
                    case.structured_value_matched
                    for case in reached
                    if case.structured_value_matched is not None
                ]
            ),
            "fullDecisionAccuracy": (
                None if semantic_pending else resolved_full_accuracy
            ),
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
            "selectiveAccuracy": (
                None if auto_pending else resolved_selective_accuracy
            ),
            "resolvedSelectiveAccuracy": resolved_selective_accuracy,
            "selectiveLowerBoundAccuracy": _ratio(
                sum(auto_values),
                len(auto_values) + auto_pending,
            ),
            "reviewRequiredRecall": _ratio(review_correct, len(review_gold)),
            "falsePositiveSuppressionRate": _ratio(
                suppressed_extras, extra_predictions
            ),
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
            if not _is_structured_state_ref(key[2])
            or (key[0], key[2]) in scorable_state_refs
        }
        actual_delta = {
            key: value
            for key, value in actual_delta.items()
            if not _is_structured_state_ref(key[2])
            or (key[0], key[2]) in scorable_state_refs
        }
        transition_counts["expected"] += len(expected_delta)
        transition_counts["predicted"] += len(actual_delta)
        scenario_pairs = [
            pair for pair in state_pairs if pair.scenario_id == scenario.scenario_id
        ]
        pair_by_ref = {
            (pair.domain.value, pair.ref): pair for pair in scenario_pairs
        }
        for key in set(expected_delta) & set(actual_delta):
            expected_value = expected_delta[key]
            actual_value = actual_delta[key]
            if normalize_text(expected_value) == normalize_text(actual_value):
                transition_counts["matched"] += 1
                continue
            pair = pair_by_ref.get((key[0], key[2]))
            if key[1] in {"ADD", "UPDATE"} and pair is not None:
                if pair.matched is True:
                    transition_counts["matched"] += 1
                elif pair.matched is None:
                    transition_counts["semanticPending"] += 1
        scenario_state_metrics = _state_pair_metrics(scenario_pairs)
        scenario_f1 = scenario_state_metrics["f1"]
        scenario_rows.append(
            {
                "scenarioId": scenario.scenario_id,
                "episodeNo": scenario.episode_no,
                "afterStateF1": scenario_f1,
                "afterStateLowerBoundF1": scenario_state_metrics["lowerBoundF1"],
                "semanticPending": scenario_state_metrics["semanticPending"],
                "rollingStateDivergence": (
                    None if scenario_f1 is None else 1 - scenario_f1
                ),
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
        max(0, transition_counts["predicted"] - pending_transitions),
        max(0, transition_counts["expected"] - pending_transitions),
    )
    transition_precision, transition_recall, transition_f1 = (
        (None, None, None) if pending_transitions else lower_transition
    )
    enabled_reports = [
        domain_reports[domain]
        for domain in EvaluationDomain
        if domain in enabled_domains
    ]
    macro_f1 = (
        None
        if any(report.get("semanticPending", 0) for report in enabled_reports)
        else _mean([report.get("afterStateF1") for report in enabled_reports])
    )
    lower_bound_macro_f1 = _mean(
        [report.get("afterStateLowerBoundF1") for report in enabled_reports]
    )
    resolved_macro_f1 = _mean(
        [report.get("resolvedAfterStateF1") for report in enabled_reports]
    )
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
            "rollingStateDivergence": (
                None if macro_f1 is None else 1 - macro_f1
            ),
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
    expected = sum(pair.expected_value is not None for pair in pairs)
    predicted = sum(pair.actual_value is not None for pair in pairs)
    pending = sum(pair.matched is None for pair in pairs)
    lower_precision, lower_recall, lower_f1 = _prf(correct, predicted, expected)
    resolved_precision, resolved_recall, resolved_f1 = _prf(
        correct,
        max(0, predicted - pending),
        max(0, expected - pending),
    )
    precision, recall, f1 = (
        (None, None, None)
        if pending
        else (lower_precision, lower_recall, lower_f1)
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
) -> list[dict[str, Any]]:
    details = []
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
                    "extraPredictionIds": [
                        item.candidate_id for item in result.extra_predictions
                    ],
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
                        "valueMatched": case.value_matched,
                        "fullDecisionMatched": case.full_decision_matched,
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
    return [
        case.operation_matched,
        case.target_matched,
        case.consolidation_matched,
        case.proposed_path_matched,
        case.value_matched,
    ]


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
            decision.operation == WorldSettingOperation.EXCLUDE
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
        return prediction.operation in {
            WorldSettingOperation.ADD,
            WorldSettingOperation.UPDATE,
            WorldSettingOperation.MERGE,
        } and prediction.consolidation_status != WorldSettingConsolidationStatus.CONFLICT
    return False


def _is_review_prediction(prediction: Stage2Prediction) -> bool:
    return isinstance(prediction, CharacterStage2Prediction) and (
        prediction.operation == CharacterFactComparisonOperation.REVIEW_REQUIRED
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
                (scenario_id, prediction.candidate_id)
                for prediction in result.extra_predictions
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
            suppressed += decision.operation == WorldSettingOperation.EXCLUDE
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
            {
                f"known-character:{item.entity_ref}": item.name
                for item in state.known_characters
            }
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
                values[_effect_ref("history-json", identity)] = _canonical_json(
                    item.value_json
                )
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

    return prefix + ":" + json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_structured_state_ref(ref: str) -> bool:
    return ref.startswith("fact-json:") or ref.startswith("history-json:")


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
    return json_contains(expected_json, actual_json)


def _state_delta(
    before: EvaluationState,
    after: EvaluationState,
    *,
    structured_reference_before: EvaluationState,
    structured_reference_after: EvaluationState,
) -> dict[tuple[str, str, str], str | None]:
    result: dict[tuple[str, str, str], str | None] = {}
    for domain in EvaluationDomain:
        before_items = _evaluation_state_values(before, domain)
        after_items = _evaluation_state_values(after, domain)
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
        )
        after_items = _project_structured_state_items(
            after_items,
            reference_after_items,
            scorable_structured_refs,
        )
        for ref in set(before_items) | set(after_items):
            if ref not in before_items:
                result[(domain.value, "ADD", ref)] = after_items[ref]
            elif ref not in after_items:
                result[(domain.value, "REMOVE", ref)] = before_items[ref]
            elif normalize_text(before_items[ref]) != normalize_text(after_items[ref]):
                result[(domain.value, "UPDATE", ref)] = after_items[ref]
    return result


def _project_structured_state_items(
    actual_items: dict[str, str | None],
    reference_items: dict[str, str | None],
    scorable_refs: set[str],
) -> dict[str, str | None]:
    projected = {
        ref: value
        for ref, value in actual_items.items()
        if not _is_structured_state_ref(ref)
    }
    for ref in scorable_refs:
        if ref not in actual_items:
            continue
        if ref not in reference_items:
            # JSON이 단순 미기재된 상태라면 그 경계에서는 평가하지 않는다. 반면
            # 기반 fact/history 자체가 없어야 하는 경계에 예측 JSON이 남아 있으면
            # presence marker로 ADD/REMOVE 실패를 보존한다.
            if _structured_base_ref(ref) in reference_items:
                continue
            projected[ref] = '{"$present":true}'
            continue
        expected = reference_items[ref]
        actual = actual_items[ref]
        if expected is None or actual is None:
            projected[ref] = actual
            continue
        try:
            projected_value = _project_json_value(
                json.loads(expected),
                json.loads(actual),
            )
        except (TypeError, ValueError):  # pragma: no cover - generated internally
            projected[ref] = actual
        else:
            projected[ref] = json.dumps(
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
                    _project_json_value(value, actual[key])
                    if key in actual
                    else {"$missing": True}
                )
                for key, value in sorted(expected.items())
            }
        }
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return {"$typeMismatch": actual}
        values = [
            _project_json_value(value, actual[index])
            if index < len(actual)
            else {"$missing": True}
            for index, value in enumerate(expected)
        ]
        return {"$list": values, "$length": len(actual)}
    if isinstance(expected, bool):
        parsed = parse_boolean(actual)
        return {"$boolean": parsed} if parsed is not None else {"$invalidBoolean": actual}
    if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        parsed = parse_decimal(actual)
        if parsed is None:
            return {"$invalidNumber": actual}
        canonical = "0" if parsed == 0 else format(parsed.normalize(), "f")
        return {"$number": canonical}
    if expected is None:
        return {"$null": actual is None}
    return {"$string": normalize_text(str(actual))}


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
    if gold.proposed_value_json and isinstance(
        gold.proposed_value_json.get("value"), (int, float)
    ):
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
        "estimatedCostUsd": (
            str(sum(costs, Decimal("0"))) if costs else None
        ),
    }


def _runtime_failure_summary(predictions: PredictionBundleV3) -> dict[str, Any]:
    failures = [
        failure
        for scenario in predictions.scenarios
        for failure in scenario.failures
    ]
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
        stage2[domain]
        for domain in EvaluationDomain
        if stage2[domain].get("evaluated", True)
    ]
    has_stage2_pending = any(
        report.get("counts", {}).get("semanticPending", 0)
        for report in evaluated_stage2
    )
    return {
        "stage1CandidateF1": _mean(
            [
                stage1[domain].get("metrics", {}).get("candidateF1")
                for domain in EvaluationDomain
            ]
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
