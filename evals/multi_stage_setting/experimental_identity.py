"""Scorer-only, one-to-one identity alignment; never use this module in a runtime adapter."""

from collections import defaultdict
from dataclasses import dataclass

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Prediction,
    EvaluationState,
    GoldSnapshotV3,
    PredictionBundleV3,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.state_effects import ScenarioStateTransition
from evals.setting_extraction.normalization import normalize_text


@dataclass(frozen=True)
class ScoringIdentityAlignment:
    characters: dict[str, str]
    worlds: dict[str, tuple[str | None, str]]
    state_refs: dict[str, str]

    def state(self, state: EvaluationState) -> EvaluationState:
        """Return a separate scoring view; raw state and trace hashes are untouched."""
        known = [item.model_copy(update={"entity_ref": self.characters.get(item.entity_ref, item.entity_ref)})
                 for item in state.known_characters]
        facts = []
        for item in state.character_facts:
            ref = self.characters.get(item.entity_ref, item.entity_ref)
            facts.append(item.model_copy(update={
                "entity_ref": ref, "ref": character_state_ref(ref, item.fact_type, item.fact_key),
            }))
        worlds = []
        for item in state.world_facts:
            aligned = self.worlds.get(item.subject_ref or "")
            subject_ref, name = aligned if aligned is not None else (item.subject_ref, item.subject_name)
            worlds.append(item.model_copy(update={
                "subject_ref": subject_ref,
                "ref": world_state_ref(item.category, name, item.scope_name, item.setting_name,
                                       subject_ref=subject_ref),
            }))
        history = [item.model_copy(update={
            "entity_ref": self.characters.get(item.entity_ref, item.entity_ref),
        }) for item in state.character_history]
        return state.model_copy(update={
            "known_characters": known, "character_facts": facts,
            "world_facts": worlds, "character_history": history,
        }).canonical()

    def predictions(self, bundle: PredictionBundleV3) -> PredictionBundleV3:
        scenarios = []
        for scenario in bundle.scenarios:
            stage1 = [item.model_copy(update={
                "entity_ref": self.characters.get(item.entity_ref, item.entity_ref),
            }) if isinstance(item, CharacterStage1Prediction) else item for item in scenario.stage1]
            stage2 = []
            for item in scenario.stage2:
                changes = {"target_ref": self.state_refs.get(item.target_ref, item.target_ref)}
                if isinstance(item, CharacterStage2Prediction):
                    changes["removed_snapshot_refs"] = [
                        self.state_refs.get(ref, ref) for ref in item.removed_snapshot_refs
                    ]
                stage2.append(item.model_copy(update=changes))
            scenarios.append(scenario.model_copy(update={"stage1": stage1, "stage2": stage2}))
        return bundle.model_copy(update={"scenarios": scenarios})


def build_scoring_identity_alignment(
    gold: GoldSnapshotV3,
    gold_chain: dict[str, ScenarioStateTransition],
    predictions: PredictionBundleV3,
) -> ScoringIdentityAlignment:
    """Use unique name correspondences only for scoring, never merge ambiguous targets.

    Gold and runtime can assign different opaque IDs to the same one-to-one named
    subject. Two actual refs or two Gold refs for a normalized name remain distinct
    and unmatched, including cases where selecting one would improve the score.
    """
    actual_char = defaultdict(set)
    expected_char = defaultdict(set)
    actual_world = defaultdict(set)
    expected_world = defaultdict(set)
    actual_states = []
    for scenario in predictions.scenarios:
        trace = scenario.runtime_state_trace
        assert trace is not None
        actual_states.extend([trace.input_state, trace.output_state])
        for candidate in scenario.stage1:
            if isinstance(candidate, CharacterStage1Prediction) and candidate.entity_ref:
                actual_char[normalize_text(candidate.matched_character_name or candidate.entity_name)].add(
                    candidate.entity_ref
                )
    for state in actual_states:
        for item in state.known_characters:
            actual_char[normalize_text(item.name)].add(item.entity_ref)
        for item in state.character_facts:
            actual_char[normalize_text(item.entity_name)].add(item.entity_ref)
        for item in state.world_facts:
            if item.subject_ref:
                actual_world[(item.category, normalize_text(item.subject_name))].add(item.subject_ref)
    for candidate in gold.stage1:
        if isinstance(candidate, CharacterStage1Gold):
            expected_char[normalize_text(candidate.entity_name)].add(candidate.entity_ref)
    for transition in gold_chain.values():
        for state in (transition.before_state, transition.after_state):
            for item in state.known_characters:
                expected_char[normalize_text(item.name)].add(item.entity_ref)
            for item in state.world_facts:
                expected_world[(item.category, normalize_text(item.subject_name))].add(
                    (item.subject_ref, item.subject_name)
                )
    characters = _one_to_one(actual_char, expected_char)
    worlds = _one_to_one(actual_world, expected_world)
    state_refs = {}
    for state in actual_states:
        for item in state.character_facts:
            entity = characters.get(item.entity_ref, item.entity_ref)
            state_refs[item.ref] = character_state_ref(entity, item.fact_type, item.fact_key)
        for item in state.world_facts:
            target = worlds.get(item.subject_ref or "")
            subject_ref = target[0] if target is not None else item.subject_ref
            name = target[1] if target is not None else item.subject_name
            state_refs[item.ref] = world_state_ref(
                item.category, name, item.scope_name, item.setting_name, subject_ref=subject_ref,
            )
    return ScoringIdentityAlignment(characters, worlds, state_refs)


def _one_to_one(actual: dict, expected: dict) -> dict:
    candidates = defaultdict(set)
    for key, actual_refs in actual.items():
        expected_refs = expected.get(key, set())
        if len(actual_refs) == len(expected_refs) == 1:
            candidates[next(iter(actual_refs))].add(next(iter(expected_refs)))
    unique = {ref: next(iter(targets)) for ref, targets in candidates.items() if len(targets) == 1}
    counts = defaultdict(int)
    for target in unique.values():
        counts[target] += 1
    return {ref: target for ref, target in unique.items() if counts[target] == 1}
