"""Project Java domain state for scoring without borrowing Gold identities or values."""

from decimal import Decimal
from typing import Any

from evals.multi_stage_setting.contracts import (
    CharacterHistoryEntry,
    CharacterStateEntry,
    EvaluationState,
    HeldWorldConflict,
    KnownCharacter,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.ordered_journal import ROOTS


def project_backend_state(
    state: dict[str, Any],
    *,
    scenario_id_by_episode: dict[int, str],
) -> EvaluationState:
    """Keep actual/provisional refs distinct; scenario IDs are run metadata only.

    The full Backend state/hash remains the replay authority. EvaluationState is
    the existing scorer's effect projection, not the Worker prompt context.
    """
    if set(state) != ROOTS or any(not isinstance(state[root], dict) for root in ROOTS):
        raise ValueError("Expected Java analysis state with three object roots.")
    known = []
    character_facts = []
    world_facts = []
    history = []
    held = []
    for target_ref, character in state["characters"].items():
        _validate_identity(character, target_ref, "actualCharacterId", "character:")
        known.append(KnownCharacter(entity_ref=target_ref, name=character["name"]))
        for slot_key, slot in character["slots"].items():
            if slot_key != f'{slot["factType"]}:{slot["factKey"]}':
                raise ValueError("Character slot key differs from its typed content.")
            character_facts.append(CharacterStateEntry(
                ref=character_state_ref(target_ref, slot["factType"], slot["factKey"]),
                entity_ref=target_ref, entity_name=character["name"], fact_type=slot["factType"],
                fact_key=slot["factKey"], value=slot.get("factValue"),
                value_json=_evaluation_json(slot.get("valueJson")),
                source_episode_no=slot.get("provenance", {}).get("sourceEpisodeNo"),
            ))
    for target_ref, world in state["worldSettings"].items():
        _validate_identity(world, target_ref, "actualWorldSettingId", "world:")
        for name, value in world["propertiesJson"].items():
            properties = [(None, name, value)] if isinstance(value, str) else [
                (name, property_name, property_value)
                for property_name, property_value in _object(value).items()
            ]
            for scope, property_name, property_value in properties:
                world_facts.append(WorldStateEntry(
                    ref=world_state_ref(world["category"], world["subjectName"], scope,
                                        property_name, subject_ref=target_ref),
                    subject_ref=target_ref, category=world["category"],
                    subject_name=world["subjectName"], scope_name=scope,
                    setting_name=property_name, value=property_value,
                ))
    for event_id, reference in state["references"].items():
        if reference.get("kind") == "HUMAN_REJECTION_POLICY":
            # Private exact-source hashes remain in the replay authority, never in scorer facts/history.
            continue
        if reference.get("domain") == "characters" and reference.get("operation") == "HISTORY_ONLY":
            target_ref = reference["targetRef"]
            character = state["characters"][target_ref]
            history.append(CharacterHistoryEntry(
                scenario_id=scenario_id_by_episode[reference["sourceEpisodeNo"]],
                source_gold_id=f'prediction:{reference["candidateId"]}',
                entity_ref=target_ref, entity_name=character["name"],
                fact_type=reference["factType"], fact_key=reference["factKey"],
                value=reference.get("factValue"), value_json=_evaluation_json(reference.get("valueJson")),
                operation="HISTORY_ONLY", temporal_scope=reference["temporalScope"],
            ))
        elif reference.get("domain") == "worldSettings" and (
            reference.get("consolidationStatus") == "CONFLICT"
        ):
            held.append(HeldWorldConflict(
                scenario_id=scenario_id_by_episode[reference["sourceEpisodeNo"]],
                decision_id=reference.get("decisionId", event_id),
                subject_ref=reference.get("targetRef"), category=reference["category"],
                subject_name=reference["subjectName"], scope_name=reference.get("scopeName"),
                setting_name=reference["settingName"], source_values=reference["sourceValues"],
                source_candidate_ids=reference["sourceCandidateIds"],
            ))
    return EvaluationState(
        known_characters=known, character_facts=character_facts, character_history=history,
        world_facts=world_facts, held_world_conflicts=held,
    ).canonical()


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("World properties must be root text or one-level scoped text.")
    return value


def _evaluation_json(value: Any) -> Any:
    """Bridge exact journal decimals to v3's JSON types without silently losing digits."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Evaluation JSON numbers must be finite.")
        if value == value.to_integral_value():
            return int(value)
        number = float(value)
        if Decimal(str(number)) != value:
            raise ValueError("Evaluation v3 cannot represent this journal decimal without precision loss.")
        return number
    if isinstance(value, dict):
        return {key: _evaluation_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_evaluation_json(item) for item in value]
    return value


def _validate_identity(target: dict, ref: str, actual_field: str, prefix: str) -> None:
    actual = target.get(actual_field)
    provisional = target.get("provisionalSubjectKey")
    if (actual is None) == (provisional is None):
        raise ValueError("Exactly one actual or provisional target identity is required.")
    if actual is not None and ref != prefix + actual:
        raise ValueError("Actual target ref differs from its Backend ID.")
    if provisional is not None and ref != provisional:
        raise ValueError("Provisional target ref differs from provisionalSubjectKey.")
    if provisional is not None and not ref.startswith("provisional-" + prefix):
        raise ValueError("A provisional target must keep its distinct namespace.")
