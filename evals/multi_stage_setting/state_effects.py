from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import (
    CharacterFactComparisonOperation,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
)
from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name
from evals.multi_stage_setting.contracts import (
    CandidateKind,
    CharacterHistoryEntry,
    CharacterStage1Gold,
    CharacterStage2Gold,
    CharacterStage1Prediction,
    CharacterStage2Prediction,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    HeldWorldConflict,
    KnownCharacter,
    ScenarioGold,
    StartStateMode,
    Stage2Gold,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStage1Prediction,
    WorldStage2Prediction,
    WorldStateEntry,
    character_state_ref,
    infer_character_fact_type,
    validate_world_state_properties,
    world_entry_subject_ref,
    world_subject_ref,
    world_state_ref,
)
from evals.setting_extraction.models import GoldDecision, Importance


@dataclass(frozen=True)
class ResolvedDecisionBefore:
    decision_id: str
    value: str | None
    value_json: dict[str, Any] | None


@dataclass(frozen=True)
class ScenarioStateTransition:
    scenario_id: str
    before_state: EvaluationState
    after_state: EvaluationState
    applied_decision_ids: tuple[str, ...]
    held_decision_ids: tuple[str, ...]
    resolved_decision_befores: tuple[ResolvedDecisionBefore, ...] = ()


class StateApplicationError(ValueError):
    pass


def build_gold_state_chain(snapshot: GoldSnapshotV3) -> dict[str, ScenarioStateTransition]:
    scenario_by_id = {item.scenario_id: item for item in snapshot.scenarios}
    stage1_by_id = {item.gold_id: item for item in snapshot.stage1}
    decisions_by_scenario: dict[str, list[Stage2Gold]] = {}
    for decision in snapshot.stage2:
        decisions_by_scenario.setdefault(decision.scenario_id, []).append(decision)

    transitions: dict[str, ScenarioStateTransition] = {}
    for scenario in sorted(snapshot.scenarios, key=lambda item: item.episode_no):
        before_state = _resolve_before_state(scenario, transitions, scenario_by_id)
        state = before_state
        applied: list[str] = []
        held: list[str] = []
        resolved_befores: list[ResolvedDecisionBefore] = []
        scenario_decisions = sorted(
            decisions_by_scenario.get(scenario.scenario_id, []),
            key=lambda item: (item.sort_order, item.decision_id),
        )
        _validate_world_root_move_plans(before_state, scenario_decisions)
        for decision in scenario_decisions:
            sources = [stage1_by_id[source_id] for source_id in decision.source_gold_ids]
            # 캐릭터 batch는 앞선 결정이 만든 메모리상 projected snapshot을 다음
            # 후보의 비교 문맥으로 사용한다. 세계관 reducer는 기존처럼 회차 시작
            # beforeState에 대해 각 결정을 검증한다.
            comparison_state = state if isinstance(decision, CharacterStage2Gold) else before_state
            resolved_befores.append(_resolve_decision_before(comparison_state, sources, decision))
            state, was_held = apply_gold_decision(
                state,
                scenario,
                sources,
                decision,
                comparison_state=comparison_state,
            )
            (held if was_held else applied).append(decision.decision_id)
        _validate_generated_world_scopes(
            state,
            scenario_decisions,
            stage1_by_id,
        )
        state = _register_discovered_characters(
            state,
            [
                row
                for row in snapshot.stage1
                if row.scenario_id == scenario.scenario_id
                and isinstance(row, CharacterStage1Gold)
                and row.candidate_kind == CandidateKind.CHARACTER_DISCOVERY
                and row.decision == "EXTRACT"
            ],
        )
        after_state = state.canonical()
        _verify_declared_state_hashes(scenario, before_state, after_state)
        transitions[scenario.scenario_id] = ScenarioStateTransition(
            scenario_id=scenario.scenario_id,
            before_state=before_state.canonical(),
            after_state=after_state,
            applied_decision_ids=tuple(applied),
            held_decision_ids=tuple(held),
            resolved_decision_befores=tuple(resolved_befores),
        )
    return transitions


def _resolve_decision_before(
    state: EvaluationState,
    sources: list[CharacterStage1Gold | WorldStage1Gold],
    decision: CharacterStage2Gold | WorldStage2Gold,
) -> ResolvedDecisionBefore:
    if isinstance(decision, CharacterStage2Gold):
        source = sources[0]
        assert isinstance(source, CharacterStage1Gold)
        assert source.fact_type is not None and source.fact_key is not None
        exact_ref = character_state_ref(source.entity_ref, source.fact_type, source.fact_key)
        entries = {item.ref: item for item in state.character_facts}
        entry = entries.get(decision.target_ref or "") or entries.get(exact_ref)
        return ResolvedDecisionBefore(
            decision_id=decision.decision_id,
            value=None if entry is None else entry.value,
            value_json=None if entry is None else entry.value_json,
        )

    target = next(
        (item for item in state.world_facts if item.ref == decision.target_ref),
        None,
    )
    return ResolvedDecisionBefore(
        decision_id=decision.decision_id,
        value=None if target is None else target.value,
        value_json=None,
    )


def apply_gold_decision(
    state: EvaluationState,
    scenario: ScenarioGold,
    sources: list[CharacterStage1Gold | WorldStage1Gold],
    decision: CharacterStage2Gold | WorldStage2Gold,
    *,
    comparison_state: EvaluationState | None = None,
) -> tuple[EvaluationState, bool]:
    resolved_comparison_state = state if comparison_state is None else comparison_state
    if isinstance(decision, CharacterStage2Gold):
        if len(sources) != 1 or not isinstance(sources[0], CharacterStage1Gold):
            raise StateApplicationError(
                f"Character decision {decision.decision_id} requires one character source."
            )
        return (
            _apply_character(
                state,
                resolved_comparison_state,
                scenario,
                sources[0],
                decision,
            ),
            False,
        )
    world_sources = [source for source in sources if isinstance(source, WorldStage1Gold)]
    if len(world_sources) != len(sources) or not world_sources:
        raise StateApplicationError(
            f"World decision {decision.decision_id} requires world sources."
        )
    return _apply_world(
        state,
        resolved_comparison_state,
        scenario,
        world_sources,
        decision,
    )


def apply_prediction_decision(
    state: EvaluationState,
    scenario: ScenarioGold,
    source: CharacterStage1Prediction | WorldStage1Prediction,
    decision: CharacterStage2Prediction | WorldStage2Prediction,
    *,
    matched_gold_source: CharacterStage1Gold | WorldStage1Gold | None = None,
    matched_gold_decision: Stage2Gold | None = None,
) -> tuple[EvaluationState, bool]:
    """운영 출력 DTO를 Gold와 같은 reference reducer에 적용한다.

    매칭된 Gold가 있으면 안정적인 entityRef/canonical slot만 빌리고 값과 operation은
    예측을 그대로 사용한다. 추가 예측은 별도 prediction ref로 상태에 남아 E2E FP가 된다.
    """

    try:
        if isinstance(source, CharacterStage1Prediction) and isinstance(
            decision, CharacterStage2Prediction
        ):
            if source.candidate_kind != CandidateKind.SETTING:
                raise StateApplicationError("CHARACTER_DISCOVERY cannot feed Stage2.")
            matched_character = (
                matched_gold_source
                if isinstance(matched_gold_source, CharacterStage1Gold)
                else None
            )
            # #152 comparator가 pattern STATUS alias를 최종 canonical key로 해소한다.
            # Gold와 매칭됐다는 이유로 Gold key를 빌리면 잘못된 해소 결과가 E2E에서
            # 가려지므로 prediction이 실제 반환한 slot을 그대로 적용한다.
            fact_key = decision.resolved_canonical_fact_key
            if fact_key is None:
                raise StateApplicationError("Character prediction has no canonical factKey.")
            fact_type = (
                matched_character.fact_type
                if matched_character is not None
                else source.fact_type or infer_character_fact_type(fact_key)
            )
            if fact_type is None:
                raise StateApplicationError("Character prediction has no canonical factType.")
            entity_name = source.matched_character_name or source.entity_name
            pseudo_source = CharacterStage1Gold(
                gold_id=(
                    matched_gold_source.gold_id
                    if isinstance(matched_gold_source, CharacterStage1Gold)
                    else f"prediction:{source.candidate_id}"
                ),
                scenario_id=scenario.scenario_id,
                episode_no=scenario.episode_no,
                sort_order=source.sort_order,
                decision=GoldDecision.EXTRACT,
                importance=Importance.MUST,
                context_tags=[],
                evidence_quotes=[span.quote for span in source.evidence_spans]
                or ["prediction-output"],
                review_status="FINAL",
                domain="CHARACTER",
                candidate_kind="SETTING",
                entity_ref=(
                    matched_character.entity_ref
                    if matched_character is not None
                    else source.entity_ref or f"prediction:{_safe_ref_part(entity_name)}"
                ),
                entity_name=entity_name,
                fact_type=fact_type,
                fact_key=fact_key,
                value_type=source.value_type,
                display_value=source.display_value or "prediction-value-unavailable",
                value_json=source.value_json,
                structured_scorable=source.value_json is not None,
            )
            pseudo_decision = CharacterStage2Gold(
                decision_id=(
                    matched_gold_decision.decision_id
                    if isinstance(matched_gold_decision, CharacterStage2Gold)
                    else f"prediction:{source.candidate_id}"
                ),
                scenario_id=scenario.scenario_id,
                episode_no=scenario.episode_no,
                sort_order=source.sort_order,
                source_gold_ids=[pseudo_source.gold_id],
                domain="CHARACTER",
                operation=decision.operation,
                target_ref=decision.target_ref,
                removed_snapshot_refs=decision.removed_snapshot_refs,
                proposed_value=decision.proposed_value,
                proposed_value_json=decision.proposed_value_json,
                temporal_scope=decision.temporal_scope,
                review_status="FINAL",
            )
            return _apply_character(
                state,
                state,
                scenario,
                pseudo_source,
                pseudo_decision,
            ), False

        if isinstance(source, WorldStage1Prediction) and isinstance(
            decision, WorldStage2Prediction
        ):
            matched_world = (
                matched_gold_source if isinstance(matched_gold_source, WorldStage1Gold) else None
            )
            matched_world_decision = (
                matched_gold_decision
                if isinstance(matched_gold_decision, WorldStage2Gold)
                else None
            )
            pseudo_source = WorldStage1Gold(
                gold_id=(
                    matched_gold_source.gold_id
                    if isinstance(matched_gold_source, WorldStage1Gold)
                    else f"prediction:{source.candidate_id}"
                ),
                scenario_id=scenario.scenario_id,
                episode_no=scenario.episode_no,
                sort_order=0,
                decision=GoldDecision.EXTRACT,
                importance=Importance.MUST,
                context_tags=[],
                evidence_quotes=[span.quote for span in source.evidence_spans]
                or ["prediction-output"],
                review_status="FINAL",
                domain="WORLD",
                candidate_kind="WORLD_SETTING",
                category=matched_world.category if matched_world is not None else source.category,
                subject_name=(
                    matched_world.subject_name if matched_world is not None else source.subject_name
                ),
                scope_name=(
                    matched_world.scope_name if matched_world is not None else source.scope_name
                ),
                setting_name=(
                    matched_world.setting_name if matched_world is not None else source.setting_name
                ),
                source_values=source.source_values,
            )
            pseudo_decision = WorldStage2Gold(
                decision_id=(
                    matched_gold_decision.decision_id
                    if isinstance(matched_gold_decision, WorldStage2Gold)
                    else f"prediction:{source.candidate_id}"
                ),
                scenario_id=scenario.scenario_id,
                episode_no=scenario.episode_no,
                sort_order=0,
                source_gold_ids=[pseudo_source.gold_id],
                domain="WORLD",
                operation=decision.operation,
                consolidation_status=decision.consolidation_status,
                target_ref=decision.target_ref,
                matched_scope_name=_canonical_world_name_if_equivalent(
                    decision.matched_scope_name,
                    None
                    if matched_world_decision is None
                    else matched_world_decision.matched_scope_name,
                ),
                matched_property_name=_canonical_world_name_if_equivalent(
                    decision.matched_property_name,
                    None
                    if matched_world_decision is None
                    else matched_world_decision.matched_property_name,
                ),
                proposed_scope_name=_canonical_world_name_if_equivalent(
                    decision.proposed_scope_name,
                    None
                    if matched_world_decision is None
                    else matched_world_decision.proposed_scope_name,
                ),
                proposed_setting_name=(
                    _canonical_world_name_if_equivalent(
                        decision.proposed_setting_name,
                        None
                        if matched_world_decision is None
                        else matched_world_decision.proposed_setting_name,
                    )
                    or decision.proposed_setting_name
                ),
                proposed_value=decision.proposed_value,
                existing_root_property_names_to_move=(
                    decision.existing_root_property_names_to_move
                ),
                review_status="FINAL",
            )
            return _apply_world(
                state,
                state,
                scenario,
                [pseudo_source],
                pseudo_decision,
            )
    except ValueError as exc:
        if isinstance(exc, StateApplicationError):
            raise
        raise StateApplicationError(
            f"Invalid prediction decision for candidate {source.candidate_id}."
        ) from None
    raise StateApplicationError("Stage1/Stage2 prediction domains do not match.")


def _canonical_world_name_if_equivalent(
    prediction: str | None,
    gold: str | None,
) -> str | None:
    if prediction is None or gold is None:
        return prediction
    if normalize_world_setting_name(prediction) == normalize_world_setting_name(gold):
        return gold
    return prediction


def _resolve_before_state(
    scenario: ScenarioGold,
    transitions: dict[str, ScenarioStateTransition],
    scenario_by_id: dict[str, ScenarioGold],
) -> EvaluationState:
    if scenario.start_state_mode == StartStateMode.EMPTY:
        return EvaluationState()
    if scenario.start_state_mode == StartStateMode.SEED:
        if scenario.seed_state is None:
            raise StateApplicationError(
                f"SEED scenario {scenario.scenario_id} has no loaded seed state."
            )
        return scenario.seed_state.canonical()
    previous_id = scenario.previous_scenario_id
    if previous_id is None or previous_id not in scenario_by_id:
        raise StateApplicationError(
            f"Scenario {scenario.scenario_id} has no valid previous Gold scenario."
        )
    transition = transitions.get(previous_id)
    if transition is None:
        raise StateApplicationError(
            f"Previous state for scenario {scenario.scenario_id} was not generated."
        )
    return transition.after_state.model_copy(deep=True)


def _apply_character(
    state: EvaluationState,
    comparison_state: EvaluationState,
    scenario: ScenarioGold,
    source: CharacterStage1Gold,
    decision: CharacterStage2Gold,
) -> EvaluationState:
    if source.fact_type is None or source.fact_key is None:
        raise StateApplicationError(
            f"Character source {source.gold_id} has no canonical setting slot."
        )
    exact_ref = character_state_ref(source.entity_ref, source.fact_type, source.fact_key)
    facts = {item.ref: item for item in state.character_facts}
    target = facts.get(decision.target_ref or "")
    comparison_facts = {item.ref: item for item in comparison_state.character_facts}
    comparison_target = comparison_facts.get(decision.target_ref or "")
    comparison_exact_existing = comparison_facts.get(exact_ref)
    if decision.target_ref is not None and target is None:
        raise StateApplicationError(
            f"Decision {decision.decision_id} targets a missing character state ref."
        )
    if target is not None and target.ref != exact_ref:
        raise StateApplicationError(
            f"Decision {decision.decision_id} must target its exact canonical slot."
        )
    if decision.before_value is not None:
        comparison_entry = comparison_target or comparison_exact_existing
        actual_before = None if comparison_entry is None else comparison_entry.value
        if actual_before != decision.before_value:
            raise StateApplicationError(
                f"Decision {decision.decision_id} beforeValue does not match comparison state."
            )
    if decision.before_value_json is not None:
        comparison_entry = comparison_target or comparison_exact_existing
        actual_before_json = None if comparison_entry is None else comparison_entry.value_json
        if actual_before_json != decision.before_value_json:
            raise StateApplicationError(
                f"Decision {decision.decision_id} beforeValueJson does not match comparison state."
            )

    operation = decision.operation
    if operation == CharacterFactComparisonOperation.ADD:
        if exact_ref in facts:
            raise StateApplicationError(f"ADD decision {decision.decision_id} slot already exists.")
        facts[exact_ref] = _character_entry(scenario, source, decision, exact_ref)
    elif operation in {
        CharacterFactComparisonOperation.UPDATE,
        CharacterFactComparisonOperation.MERGE,
    }:
        facts[exact_ref] = _character_entry(scenario, source, decision, exact_ref)
    elif operation == CharacterFactComparisonOperation.REMOVE:
        if source.fact_type != "STATUS":
            raise StateApplicationError("Character REMOVE is allowed only for STATUS.")
    elif operation in {
        CharacterFactComparisonOperation.HISTORY_ONLY,
        CharacterFactComparisonOperation.EXCLUDE,
        CharacterFactComparisonOperation.REVIEW_REQUIRED,
    }:
        pass
    else:  # pragma: no cover - StrEnum/Pydantic keeps this unreachable
        raise StateApplicationError(f"Unsupported character operation: {operation}")

    for removed_ref in decision.removed_snapshot_refs:
        if operation != CharacterFactComparisonOperation.REMOVE and removed_ref == exact_ref:
            raise StateApplicationError(
                "A snapshot upsert must not also remove the candidate's exact slot."
            )
        removed = facts.get(removed_ref)
        if removed is None:
            raise StateApplicationError(
                f"Decision {decision.decision_id} removes a missing character state ref."
            )
        if removed.fact_type != "STATUS":
            raise StateApplicationError("Only STATUS entries may be removed as side effects.")
        if source.fact_type != "STATUS" or removed.entity_ref != source.entity_ref:
            raise StateApplicationError(
                "STATUS side effects must belong to the source character and STATUS candidate."
            )
        facts.pop(removed_ref)

    history = list(state.character_history)
    if operation not in {
        CharacterFactComparisonOperation.EXCLUDE,
        CharacterFactComparisonOperation.REVIEW_REQUIRED,
    }:
        history.append(
            CharacterHistoryEntry(
                scenario_id=scenario.scenario_id,
                source_gold_id=source.gold_id,
                entity_ref=source.entity_ref,
                entity_name=source.entity_name,
                fact_type=source.fact_type,
                fact_key=source.fact_key,
                value=(
                    decision.proposed_value
                    if operation
                    in {
                        CharacterFactComparisonOperation.ADD,
                        CharacterFactComparisonOperation.UPDATE,
                        CharacterFactComparisonOperation.MERGE,
                    }
                    else source.display_value
                ),
                value_json=(
                    decision.proposed_value_json
                    if operation
                    in {
                        CharacterFactComparisonOperation.ADD,
                        CharacterFactComparisonOperation.UPDATE,
                        CharacterFactComparisonOperation.MERGE,
                    }
                    else source.value_json
                ),
                operation=operation,
                temporal_scope=decision.temporal_scope,
            )
        )
    return state.model_copy(
        update={
            "character_facts": list(facts.values()),
            "character_history": history,
        }
    )


def _character_entry(
    scenario: ScenarioGold,
    source: CharacterStage1Gold,
    decision: CharacterStage2Gold,
    ref: str,
) -> CharacterStateEntry:
    return CharacterStateEntry(
        ref=ref,
        entity_ref=source.entity_ref,
        entity_name=source.entity_name,
        fact_type=source.fact_type or "",
        fact_key=source.fact_key or "",
        value_type=source.value_type,
        value=decision.proposed_value,
        value_json=(
            decision.proposed_value_json
            if decision.proposed_value_json is not None
            else source.value_json
        ),
        source_episode_no=scenario.episode_no,
        source_sort_order=source.sort_order,
    )


def _apply_world(
    state: EvaluationState,
    comparison_state: EvaluationState,
    scenario: ScenarioGold,
    sources: list[WorldStage1Gold],
    decision: WorldStage2Gold,
) -> tuple[EvaluationState, bool]:
    primary = sources[0]
    primary_group = (
        str(primary.category),
        normalize_world_setting_name(primary.subject_name),
        normalize_world_setting_name(primary.scope_name or ""),
    )
    if any(
        (
            str(source.category),
            normalize_world_setting_name(source.subject_name),
            normalize_world_setting_name(source.scope_name or ""),
        )
        != primary_group
        for source in sources[1:]
    ):
        raise StateApplicationError(
            f"World decision {decision.decision_id} sources do not share one "
            "category, subject, and raw scope."
        )
    unique_values = []
    normalized_values: set[str] = set()
    for source in sources:
        for value in source.source_values:
            normalized = normalize_world_setting_name(value)
            if normalized in normalized_values:
                continue
            normalized_values.add(normalized)
            unique_values.append(value)
    if len(unique_values) == 1 and (
        decision.consolidation_status != WorldSettingConsolidationStatus.SINGLE
    ):
        raise StateApplicationError("One unique world source value requires SINGLE.")
    if len(unique_values) > 1 and (
        decision.consolidation_status == WorldSettingConsolidationStatus.SINGLE
    ):
        raise StateApplicationError("Multiple world source values require MERGED or CONFLICT.")

    facts = {item.ref: item for item in state.world_facts}
    canonical_subject_entries = [
        item
        for item in facts.values()
        if item.category == primary.category
        and normalize_world_setting_name(item.subject_name)
        == normalize_world_setting_name(primary.subject_name)
    ]
    target = facts.get(decision.target_ref or "")
    comparison_target = next(
        (item for item in comparison_state.world_facts if item.ref == decision.target_ref),
        None,
    )
    subject_entries = [
        item for item in facts.values() if decision.target_ref == world_entry_subject_ref(item)
    ]
    is_subject_target = bool(subject_entries)
    if decision.target_ref is not None and target is None and not is_subject_target:
        raise StateApplicationError(
            f"Decision {decision.decision_id} targets a missing world state ref."
        )
    resolved_target = target or (subject_entries[0] if subject_entries else None)
    if resolved_target is not None and resolved_target.category != primary.category:
        raise StateApplicationError("World comparison targets must use the source category.")
    if decision.operation == WorldSettingOperation.ADD and (
        decision.target_ref is not None and not is_subject_target
    ):
        raise StateApplicationError("World ADD may target only an existing subject ref.")
    if (
        decision.operation == WorldSettingOperation.ADD
        and decision.target_ref is None
        and canonical_subject_entries
    ):
        raise StateApplicationError(
            "World ADD for an existing canonical subject requires a subject target ref."
        )
    if decision.matched_property_name is not None:
        if target is None:
            raise StateApplicationError(
                "A matched world property requires a property-level target ref."
            )
        if (
            target.scope_name != decision.matched_scope_name
            or target.setting_name != decision.matched_property_name
        ):
            raise StateApplicationError(
                f"Decision {decision.decision_id} matched path differs from target state."
            )
    elif target is not None:
        raise StateApplicationError("A property-level world target requires matchedPropertyName.")
    if decision.before_value is not None:
        actual_before = None if comparison_target is None else comparison_target.value
        if actual_before != decision.before_value:
            raise StateApplicationError(
                f"Decision {decision.decision_id} beforeValue does not match beforeState."
            )

    if decision.consolidation_status == WorldSettingConsolidationStatus.CONFLICT:
        conflict_scope = (
            decision.proposed_scope_name
            if decision.proposed_scope_name is not None
            else primary.scope_name
        )
        held = list(state.held_world_conflicts)
        held.append(
            HeldWorldConflict(
                scenario_id=scenario.scenario_id,
                decision_id=decision.decision_id,
                subject_ref=(resolved_target.subject_ref if resolved_target is not None else None),
                category=(
                    resolved_target.category if resolved_target is not None else primary.category
                ),
                subject_name=(
                    resolved_target.subject_name
                    if resolved_target is not None
                    else primary.subject_name
                ),
                scope_name=conflict_scope,
                setting_name=decision.proposed_setting_name or primary.setting_name,
                source_values=unique_values,
            )
        )
        return state.model_copy(update={"held_world_conflicts": held}), True

    if decision.operation == WorldSettingOperation.EXCLUDE:
        return state, False
    if decision.operation == WorldSettingOperation.REVIEW_REQUIRED:
        return state, True
    proposed_scope = (
        decision.proposed_scope_name
        if decision.proposed_scope_name is not None
        else primary.scope_name
    )
    proposed_setting = decision.proposed_setting_name or primary.setting_name
    canonical_subject = resolved_target
    result_category = (
        canonical_subject.category if canonical_subject is not None else primary.category
    )
    result_subject_name = (
        canonical_subject.subject_name if canonical_subject is not None else primary.subject_name
    )
    result_subject_ref = canonical_subject.subject_ref if canonical_subject is not None else None
    ref = world_state_ref(
        result_category,
        result_subject_name,
        proposed_scope,
        proposed_setting,
        subject_ref=result_subject_ref,
    )
    if decision.operation == WorldSettingOperation.ADD:
        if ref in facts:
            raise StateApplicationError(f"World ADD {decision.decision_id} path already exists.")
        _apply_world_root_property_moves(
            facts,
            decision,
            result_category,
            result_subject_name,
            result_subject_ref,
            proposed_scope,
            proposed_setting,
        )
    elif decision.operation in {WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}:
        if target is None:
            raise StateApplicationError(f"World {decision.operation} requires a target.")
        facts.pop(target.ref)
    facts[ref] = WorldStateEntry(
        ref=ref,
        subject_ref=result_subject_ref,
        category=result_category,
        subject_name=result_subject_name,
        scope_name=proposed_scope,
        setting_name=proposed_setting,
        value=decision.proposed_value or primary.display_value,
    )
    try:
        validate_world_state_properties(list(facts.values()))
    except ValueError as exc:
        raise StateApplicationError(str(exc)) from None
    return state.model_copy(update={"world_facts": list(facts.values())}), False


def _apply_world_root_property_moves(
    facts: dict[str, WorldStateEntry],
    decision: WorldStage2Gold,
    category: Any,
    subject_name: str,
    subject_ref: str | None,
    proposed_scope: str | None,
    proposed_setting: str,
) -> None:
    """Apply a scoped ADD's root-to-child plan without changing stored values."""

    move_names = decision.existing_root_property_names_to_move
    if not move_names:
        return
    if proposed_scope is None:
        raise StateApplicationError(
            "Existing root properties may move only with a scoped World ADD target."
        )

    normalized_scope = normalize_world_setting_name(proposed_scope)
    normalized_proposal = normalize_world_setting_name(proposed_setting)
    subject_entries = [
        entry
        for entry in facts.values()
        if entry.category == category
        and world_entry_subject_ref(entry)
        == world_subject_ref(category, subject_name, subject_ref=subject_ref)
    ]
    root_by_name = {
        normalize_world_setting_name(entry.setting_name): entry
        for entry in subject_entries
        if entry.scope_name is None
    }
    existing_paths = {
        (
            world_entry_subject_ref(entry),
            normalize_world_setting_name(entry.scope_name or ""),
            normalize_world_setting_name(entry.setting_name),
        )
        for entry in facts.values()
    }
    moves: list[tuple[WorldStateEntry, WorldStateEntry]] = []
    for requested_name in move_names:
        normalized_name = normalize_world_setting_name(requested_name)
        existing = root_by_name.get(normalized_name)
        if existing is None:
            raise StateApplicationError(
                f"World ADD {decision.decision_id} requests a missing root property move."
            )
        if normalized_name in {normalized_scope, normalized_proposal}:
            raise StateApplicationError(
                "A moved root property must be a distinct child of the proposed scope."
            )
        moved = WorldStateEntry(
            ref=world_state_ref(
                existing.category,
                existing.subject_name,
                proposed_scope,
                existing.setting_name,
                subject_ref=existing.subject_ref,
            ),
            subject_ref=existing.subject_ref,
            category=existing.category,
            subject_name=existing.subject_name,
            scope_name=proposed_scope,
            setting_name=existing.setting_name,
            value=existing.value,
        )
        moved_path = (
            world_entry_subject_ref(moved),
            normalize_world_setting_name(moved.scope_name or ""),
            normalize_world_setting_name(moved.setting_name),
        )
        if moved_path in existing_paths:
            raise StateApplicationError(
                f"World ADD {decision.decision_id} root move conflicts at its destination."
            )
        existing_paths.add(moved_path)
        moves.append((existing, moved))

    # All move targets are validated before the copied state is changed, so one bad
    # entry cannot leave a partially relocated evaluation snapshot.
    for existing, moved in moves:
        facts.pop(existing.ref)
        facts[moved.ref] = moved


def _validate_world_root_move_plans(
    before_state: EvaluationState,
    decisions: list[Stage2Gold],
) -> None:
    """Validate root moves against the shared pre-batch world snapshot."""

    moved_refs: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, WorldStage2Gold):
            continue
        for requested_name in decision.existing_root_property_names_to_move:
            requested_key = normalize_world_setting_name(requested_name)
            matching_roots = [
                entry
                for entry in before_state.world_facts
                if entry.scope_name is None
                and decision.target_ref == world_entry_subject_ref(entry)
                and normalize_world_setting_name(entry.setting_name) == requested_key
            ]
            if len(matching_roots) != 1:
                raise StateApplicationError(
                    f"World ADD {decision.decision_id} requests a missing root property move."
                )
            root_ref = matching_roots[0].ref
            if root_ref in moved_refs:
                raise StateApplicationError(
                    "An existing root property may move only once per scenario batch."
                )
            moved_refs.add(root_ref)

    if not moved_refs:
        return
    for decision in decisions:
        if (
            isinstance(decision, WorldStage2Gold)
            and decision.operation in {WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}
            and decision.target_ref in moved_refs
        ):
            raise StateApplicationError("A moved root property must not also be updated or merged.")


def _validate_generated_world_scopes(
    state: EvaluationState,
    decisions: list[Stage2Gold],
    stage1_by_id: dict[str, CharacterStage1Gold | WorldStage1Gold],
) -> None:
    """Validate generated scope plans after all sibling decisions were applied."""

    for decision in decisions:
        if not isinstance(decision, WorldStage2Gold):
            continue
        if (
            decision.operation != WorldSettingOperation.ADD
            or decision.consolidation_status == WorldSettingConsolidationStatus.CONFLICT
            or decision.proposed_scope_name is None
        ):
            continue
        sources = [stage1_by_id[source_id] for source_id in decision.source_gold_ids]
        primary = sources[0]
        assert isinstance(primary, WorldStage1Gold)
        if normalize_world_setting_name(primary.scope_name or "") == (
            normalize_world_setting_name(decision.proposed_scope_name)
        ):
            continue

        subject_entry = next(
            (
                entry
                for entry in state.world_facts
                if decision.target_ref == world_entry_subject_ref(entry)
            ),
            None,
        )
        category = subject_entry.category if subject_entry is not None else primary.category
        subject_name = (
            subject_entry.subject_name if subject_entry is not None else primary.subject_name
        )
        subject_ref = subject_entry.subject_ref if subject_entry is not None else None
        member_names = {
            normalize_world_setting_name(entry.setting_name)
            for entry in state.world_facts
            if entry.category == category
            and world_entry_subject_ref(entry)
            == world_subject_ref(category, subject_name, subject_ref=subject_ref)
            and normalize_world_setting_name(entry.scope_name or "")
            == normalize_world_setting_name(decision.proposed_scope_name)
        }
        if len(member_names) < 2:
            raise StateApplicationError(
                f"World ADD {decision.decision_id} generated scope requires at least "
                "two distinct final child properties."
            )


def _register_discovered_characters(
    state: EvaluationState,
    discoveries: list[CharacterStage1Gold],
) -> EvaluationState:
    known = {item.entity_ref: item for item in state.known_characters}
    for discovery in sorted(discoveries, key=lambda item: (item.sort_order, item.gold_id)):
        known.setdefault(
            discovery.entity_ref,
            KnownCharacter(
                entity_ref=discovery.entity_ref,
                name=discovery.entity_name,
                creation_order=_creation_order(discovery.episode_no, discovery.sort_order),
            ),
        )
    # 설정이 최초로 ADD된 캐릭터도 이후 회차의 canonical name context에 포함한다.
    for fact in state.character_facts:
        known.setdefault(
            fact.entity_ref,
            KnownCharacter(
                entity_ref=fact.entity_ref,
                name=fact.entity_name,
                creation_order=(
                    None
                    if fact.source_episode_no is None or fact.source_sort_order is None
                    else _creation_order(fact.source_episode_no, fact.source_sort_order)
                ),
            ),
        )
    return state.model_copy(update={"known_characters": list(known.values())})


def _creation_order(episode_no: int, sort_order: int) -> int:
    # 회차와 사람의 정렬 순서를 하나의 단조 증가 값으로 보존한다. 같은 값의 tie는
    # runtime helper가 entityRef로 결정적으로 해소한다.
    return episode_no * 1_000_000 + sort_order


def _verify_declared_state_hashes(
    scenario: ScenarioGold,
    before_state: EvaluationState,
    after_state: EvaluationState,
) -> None:
    if scenario.before_state_hash is not None:
        expected = scenario.before_state_hash.removeprefix("sha256:")
        if before_state.content_hash() != expected:
            raise StateApplicationError(
                f"Scenario {scenario.scenario_id} beforeState hash does not match reducer state."
            )
    if scenario.after_state_hash is not None:
        expected = scenario.after_state_hash.removeprefix("sha256:")
        if after_state.content_hash() != expected:
            raise StateApplicationError(
                f"Scenario {scenario.scenario_id} afterState hash does not match reducer state."
            )


def _safe_ref_part(value: str) -> str:
    return "-".join(value.strip().casefold().split()) or "unknown"
