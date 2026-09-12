from copy import deepcopy
from pathlib import Path

from evals.multi_stage_setting.contracts import (
    CharacterStage1Prediction,
    CharacterStateEntry,
    EvaluationState,
    KnownCharacter,
    PredictionBundleV3,
    RuntimeStateTrace,
    ScenarioPrediction,
    character_state_ref,
)
from evals.multi_stage_setting.experimental_identity import build_scoring_identity_alignment
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.state_effects import build_gold_state_chain


def _bundle(*, duplicate=False):
    ref = "provisional-character:actual-runtime-id"
    state = EvaluationState(
        known_characters=[KnownCharacter(entity_ref=ref, name="가람")]
        + ([KnownCharacter(entity_ref="character:another-person", name="가람")] if duplicate else []),
        character_facts=[CharacterStateEntry(
            ref=character_state_ref(ref, "STATUS", "status.arm_injury"),
            entity_ref=ref, entity_name="가람", fact_type="STATUS", fact_key="status.arm_injury",
            value="왼팔 부상", value_json={"active": True, "value": "왼팔 부상"},
        )],
    )
    return PredictionBundleV3(
        fixture_hash="only-a-scoring-fixture", mode="COMMON_START",
        frozen_start_state=state, runtime_policy_version="common-start/v1",
        scenarios=[ScenarioPrediction(
            scenario_id="S1", runtime_state_trace=RuntimeStateTrace(
                input_state=state, output_state=state,
                input_state_hash=state.content_hash(), output_state_hash=state.content_hash(),
                source_hash="source", elapsed_seconds=0,
            ),
            stage1=[CharacterStage1Prediction(
                candidate_id="runtime-candidate", entity_ref=ref, entity_name="가람",
                domain="CHARACTER", candidate_kind="SETTING", fact_type="STATUS",
                fact_key="status.arm_injury", value_type="STRING", display_value="왼팔 부상",
                value_json={"active": True, "value": "왼팔 부상"},
            )],
        )],
    )


def _gold():
    return load_gold_snapshot_v3(
        Path(__file__).parent / "fixtures" / "multi_stage_setting_saved_predictions" / "gold.json"
    )


def test_scoring_alignment_never_rewrites_runtime_trace_or_next_model_input():
    gold = _gold()
    bundle = _bundle()
    original = deepcopy(bundle)
    alignment = build_scoring_identity_alignment(gold, build_gold_state_chain(gold), bundle)

    scoring_bundle = alignment.predictions(bundle)
    scoring_state = alignment.state(bundle.scenarios[0].runtime_state_trace.output_state)

    assert bundle == original
    assert scoring_bundle.scenarios[0].runtime_state_trace == original.scenarios[0].runtime_state_trace
    assert scoring_bundle.scenarios[0].stage1[0].entity_ref == "character:garam"
    assert scoring_state.character_facts[0].entity_ref == "character:garam"
    assert scoring_state.character_facts[0].value_json == original.frozen_start_state.character_facts[0].value_json
    assert original.frozen_start_state.known_characters[0].entity_ref.startswith("provisional-character:")


def test_two_same_name_runtime_targets_are_not_merged_to_improve_score():
    gold = _gold()
    bundle = _bundle(duplicate=True)
    alignment = build_scoring_identity_alignment(gold, build_gold_state_chain(gold), bundle)

    assert alignment.characters == {}
    assert alignment.state(bundle.frozen_start_state).known_characters == bundle.frozen_start_state.canonical().known_characters
    assert len(alignment.state(bundle.frozen_start_state).known_characters) == 2
