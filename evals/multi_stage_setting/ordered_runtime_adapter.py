"""Build an ordered experiment from actual predictions and Java-sealed journals.

No Gold model is accepted here. The projector is a deterministic domain adapter
from Backend state to evaluation state; it must not consult annotations. This is
also the saved-prediction replay path, so it does not invoke a model or a judge.
"""

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from app.domain.enums import SettingValueType
from evals.multi_stage_setting.contracts import (
    CharacterHistoryEntry,
    CharacterStage1Prediction,
    CharacterStage2Prediction,
    EvaluationDomain,
    EvaluationState,
    PredictionBundleV3,
    RuntimeStateTrace,
    ScenarioPipelineStatus,
    ScenarioPrediction,
    character_state_ref,
)
from evals.multi_stage_setting.ordered_journal import (
    SealedJournal,
    journal_state_hash,
    replay_sealed_journals,
)


@dataclass(frozen=True)
class OrderedScenarioObservation:
    episode_no: int
    source_hash: str
    prediction: ScenarioPrediction
    elapsed_seconds: float


def build_ordered_prediction_bundle(
    *,
    fixture_hash: str,
    initial_backend_state: dict[str, Any],
    journals: list[SealedJournal],
    observations: list[OrderedScenarioObservation],
    project_state: Callable[[dict[str, Any]], EvaluationState],
    run_id: UUID,
    work_id: UUID,
    generation: int,
    domains: set[EvaluationDomain],
    runtime_policy_version: str,
    analysis_model: str | None = None,
    subject_resolution_model: str | None = None,
    comparison_model: str | None = None,
    prompt_versions: dict[str, str] | None = None,
    character_schema_hash: str | None = None,
    initial_character_value_types: dict[str, SettingValueType] | None = None,
) -> PredictionBundleV3:
    """Only completed, fully sealed observations may feed a following episode.

    Candidate comparison errors cannot be converted to REVIEW_REQUIRED here. A
    final failed observation remains in scoring denominators with unchanged state;
    it has no sealed journal and no following observation may proceed.
    """
    if not observations:
        raise ValueError("At least one observed episode is required.")
    failed = [
        index for index, observation in enumerate(observations)
        if observation.prediction.failures
        or observation.prediction.pipeline_status != ScenarioPipelineStatus.COMPLETED
    ]
    if failed and failed != [len(observations) - 1]:
        raise ValueError("A failed episode must block every following observation.")
    if len(journals) != len(observations) - bool(failed):
        raise ValueError("Only fully completed observations may have sealed journals.")
    if not runtime_policy_version.startswith("java-journal/v1:"):
        raise ValueError("Record the Java domain reducer revision as java-journal/v1:<revision>.")
    frozen = project_state(replay_sealed_journals(
        initial_backend_state, [], run_id=run_id, work_id=work_id, generation=generation,
        before_sequence=0, before_episode_no=observations[0].episode_no,
    )).canonical()
    # Java journal v1 has no valueType. Initial metadata comes from the exported
    # runtime schema/snapshot, never Gold or a later episode's predictions.
    initial_types = dict(initial_character_value_types or {})
    if set(initial_types) - {item.ref for item in frozen.character_facts}:
        raise ValueError("Initial character type metadata addresses an absent fact.")
    frozen = _with_character_value_types(frozen, initial_types)
    predictions: list[ScenarioPrediction] = []
    previous_state = frozen
    previous_backend_hash = journal_state_hash(initial_backend_state)
    previous_episode = 0
    committed_history: list[CharacterHistoryEntry] = list(frozen.character_history)
    for sequence, observation in enumerate(observations):
        prediction = observation.prediction
        if observation.episode_no <= previous_episode:
            raise ValueError("Observations must be in strict episode order.")
        previous_episode = observation.episode_no
        journal = journals[sequence] if sequence < len(journals) else None
        if journal is not None:
            if observation.episode_no != journal.episode_no:
                raise ValueError("Observation episode differs from its sealed journal.")
            backend_after = replay_sealed_journals(
                initial_backend_state, journals[:sequence + 1],
                run_id=run_id, work_id=work_id, generation=generation,
                before_sequence=sequence + 1, before_episode_no=observation.episode_no + 1,
            )
            after = project_state(backend_after).canonical()
            types = {item.ref: item.value_type for item in previous_state.character_facts}
            sources = {item.candidate_id: item for item in prediction.stage1}
            writes = {
                item.source_candidate_id: item for item in prediction.stage2
                if isinstance(item, CharacterStage2Prediction)
                and item.operation in {"ADD", "UPDATE", "MERGE"}
            }
            # Follow sealed write order, including multiple writes to the same
            # slot. Rejected/review/history-only and unsealed candidates cannot
            # supply current-state metadata.
            for change in journal.changes:
                for candidate_id in change.source_candidate_ids:
                    source = sources.get(str(candidate_id))
                    decision = writes.get(str(candidate_id))
                    if decision is None or decision.operation != change.operation:
                        continue
                    if not isinstance(source, CharacterStage1Prediction) or not (
                        source.entity_ref and source.fact_type and source.value_type
                    ):
                        raise ValueError("Sealed character write requires observed type metadata.")
                    slot_path = ["characters", source.entity_ref, "slots",
                                 f"{source.fact_type}:{decision.resolved_canonical_fact_key}"]
                    if change.remove or change.path != slot_path[:len(change.path)]:
                        continue
                    ref = character_state_ref(source.entity_ref, source.fact_type,
                                              decision.resolved_canonical_fact_key)
                    types[ref] = source.value_type
            after = _with_character_value_types(after, types)
            committed_history.extend(_validated_character_history(observation, journal))
            after = _with_committed_history(after, committed_history)
        else:
            after = previous_state.model_copy(deep=True)
        trace = RuntimeStateTrace(
            input_state=previous_state.model_copy(deep=True),
            output_state=after.model_copy(deep=True),
            input_state_hash=previous_state.content_hash(),
            output_state_hash=after.content_hash(),
            source_hash=observation.source_hash,
            applied_event_ids=(
                list(dict.fromkeys(change.event_id for change in journal.changes))
                if journal is not None else []
            ),
            elapsed_seconds=observation.elapsed_seconds,
            state_readiness="SEALED" if journal is not None else "FAILED",
            runtime_sequence=sequence,
            backend_input_state_hash=previous_backend_hash,
            backend_output_state_hash=(
                journal.output_state_hash if journal is not None else previous_backend_hash
            ),
        )
        predictions.append(prediction.model_copy(update={"runtime_state_trace": trace}))
        previous_state = after
        previous_backend_hash = trace.backend_output_state_hash
    return PredictionBundleV3(
        fixture_hash=fixture_hash, mode="ORDERED_PROVISIONAL",
        evaluation_domains=domains,
        evaluation_scenario_ids=[item.scenario_id for item in predictions],
        runtime_policy_version=runtime_policy_version, frozen_start_state=frozen,
        runtime_run_id=str(run_id), runtime_generation=generation,
        analysis_model=analysis_model, subject_resolution_model=subject_resolution_model,
        comparison_model=comparison_model, prompt_versions=prompt_versions or {},
        character_schema_hash=character_schema_hash, scenarios=predictions,
    )


def _with_character_value_types(
    state: EvaluationState, types: dict[str, SettingValueType],
) -> EvaluationState:
    """Enrich only the scoring DTO; the source journal and its hash stay intact."""
    facts = []
    for fact in state.character_facts:
        recorded = types.get(fact.ref)
        if recorded is not None:
            recorded = SettingValueType(recorded)
        if fact.value_type is not None and recorded is not None and fact.value_type != recorded:
            raise ValueError("Projected character type differs from observed runtime metadata.")
        value_type = fact.value_type or recorded
        if value_type is None:
            raise ValueError("Ordered scoring requires runtime type metadata for every current fact.")
        facts.append(fact.model_copy(update={"value_type": value_type}))
    return state.model_copy(update={"character_facts": facts}).canonical()


def _validated_character_history(
    observation: OrderedScenarioObservation,
    journal: SealedJournal,
) -> list[CharacterHistoryEntry]:
    """Preserve the existing scorer's mutation-history axis using actual sealed outputs."""
    sources = {item.candidate_id: item for item in observation.prediction.stage1}
    result = []
    for decision in observation.prediction.stage2:
        if not isinstance(decision, CharacterStage2Prediction) or decision.operation in {
            "EXCLUDE", "REVIEW_REQUIRED",
        }:
            continue
        source = sources.get(decision.source_candidate_id)
        if not isinstance(source, CharacterStage1Prediction) or not source.entity_ref:
            raise ValueError("Ordered character history requires the actual source target ref.")
        if not any(
            change.operation == decision.operation
            and decision.source_candidate_id in {str(item) for item in change.source_candidate_ids}
            for change in journal.changes
        ):
            raise ValueError("Sealed journal lacks matching character decision provenance.")
        upsert = decision.operation in {"ADD", "UPDATE", "MERGE"}
        result.append(CharacterHistoryEntry(
            scenario_id=observation.prediction.scenario_id,
            source_gold_id=f"prediction:{source.candidate_id}",
            entity_ref=source.entity_ref, entity_name=source.matched_character_name or source.entity_name,
            fact_type=source.fact_type, fact_key=decision.resolved_canonical_fact_key,
            value=decision.proposed_value if upsert else source.display_value,
            value_json=decision.proposed_value_json if upsert else source.value_json,
            operation=decision.operation, temporal_scope=decision.temporal_scope,
        ))
    return result


def _with_committed_history(
    state: EvaluationState,
    committed: list[CharacterHistoryEntry],
) -> EvaluationState:
    history = {}
    for item in [*state.character_history, *committed]:
        key = (item.scenario_id, item.source_gold_id)
        if key in history and history[key] != item:
            raise ValueError("Projected history differs from the immutable sealed observation.")
        history[key] = item
    return state.model_copy(update={"character_history": list(history.values())}).canonical()
