import asyncio
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Prediction, CharacterStage2Prediction, EvaluationState, ScenarioPrediction,
    WorldStage1Prediction, WorldStage2Prediction, character_state_ref, world_state_ref,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.ordered_journal import SealedJournal, journal_state_hash
from evals.multi_stage_setting.ordered_runtime_adapter import (
    OrderedScenarioObservation,
    build_ordered_prediction_bundle,
)
from evals.multi_stage_setting.ordered_state_projection import project_backend_state


S0 = {"characters": {}, "worldSettings": {}, "references": {}}


def _observation(episode=1, *, failure=False):
    return OrderedScenarioObservation(
        episode_no=episode, source_hash=f"source-{episode}", elapsed_seconds=2,
        prediction=ScenarioPrediction(
            scenario_id=f"S{episode}",
            failures=[{
                "stage": "WORLD_STAGE2", "source_id": "candidate",
                "error_type": "PROVIDER_FAILURE", "message": "Synthetic failure",
            }] if failure else [],
        ),
    )


def _journal(episode=1):
    return SealedJournal(
        format_version=1, status="SEALED", run_id=UUID(int=1), work_id=UUID(int=2),
        generation=1, sequence=episode - 1, episode_no=episode, job_id=UUID(int=10 + episode),
        input_state_hash=journal_state_hash(S0), output_state_hash=journal_state_hash(S0),
        changes=[],
    )


def _build(observations, journals, projector=lambda state: EvaluationState()):
    return build_ordered_prediction_bundle(
        fixture_hash="fixture-reference-only", initial_backend_state=S0,
        observations=observations, journals=journals, project_state=projector,
        run_id=UUID(int=1), work_id=UUID(int=2), generation=1, domains={"WORLD"},
        runtime_policy_version="java-journal/v1:synthetic-domain-revision",
    )


def test_saved_ordered_adapter_replays_only_backend_state_without_accepting_gold():
    projected = []

    def projector(state):
        projected.append(state)
        return EvaluationState()

    bundle = _build([_observation(), _observation(2)], [_journal(), _journal(2)], projector)

    assert bundle.mode == "ORDERED_PROVISIONAL"
    assert bundle.state_application_policy == "VALIDATED_PROVISIONAL"
    assert projected == [S0, S0, S0]
    assert bundle.scenarios[1].runtime_state_trace.state_readiness == "SEALED"
    assert bundle.scenarios[1].runtime_state_trace.backend_input_state_hash == journal_state_hash(S0)


def test_final_failure_remains_in_denominator_but_never_advances_state():
    bundle = _build([_observation(), _observation(2, failure=True)], [_journal()])

    failed = bundle.scenarios[-1]
    assert len(failed.failures) == 1
    assert failed.runtime_state_trace.state_readiness == "FAILED"
    assert failed.runtime_state_trace.output_state == failed.runtime_state_trace.input_state
    assert failed.runtime_state_trace.applied_event_ids == []


def test_failure_cannot_be_sealed_or_followed_by_another_observation():
    with pytest.raises(ValueError, match="fully completed"):
        _build([_observation(failure=True)], [_journal()])
    with pytest.raises(ValueError, match="must block"):
        _build([_observation(failure=True), _observation(2)], [_journal(2)])


def test_missing_journal_and_tampered_backend_hash_are_rejected():
    with pytest.raises(ValueError, match="fully completed"):
        _build([_observation()], [])
    with pytest.raises(ValueError, match="outputStateHash"):
        _build([_observation()], [_journal().model_copy(update={"output_state_hash": "tampered"})])


def test_real_prediction_journal_projection_and_scoring_preserve_mutation_history_without_gold_input():
    """Synthetic Java-shaped exports exercise the complete saved B adapter boundary."""
    character_ref = f"provisional-character:{UUID(int=30)}"
    world_ref = f"provisional-world:{UUID(int=31)}"
    slot_ref = character_state_ref(character_ref, "STATUS", "status.arm_injury")
    property_ref = world_state_ref("LOCATION", "등대", None, "불빛", subject_ref=world_ref)
    initial = deepcopy(S0)
    after_first = {
        "characters": {character_ref: {
            "actualCharacterId": None, "provisionalSubjectKey": character_ref, "name": "가람",
            "slots": {"STATUS:status.arm_injury": {
                "factType": "STATUS", "factKey": "status.arm_injury", "factValue": "왼팔 부상",
                "valueJson": {"value": "왼팔 부상", "active": True},
                "provenance": {"confirmationStatus": "PROVISIONAL", "sourceEpisodeNo": 1},
            }},
        }},
        "worldSettings": {world_ref: {
            "actualWorldSettingId": None, "provisionalSubjectKey": world_ref,
            "category": "LOCATION", "subjectName": "등대", "propertiesJson": {"불빛": "파란색"},
        }}, "references": {},
    }
    after_second = deepcopy(after_first)
    after_second["characters"][character_ref]["slots"] = {}
    candidate_ids = [str(UUID(int=100 + i)) for i in range(4)]
    observations = []
    for episode, char_id, world_id in ((1, *candidate_ids[:2]), (2, *candidate_ids[2:])):
        value = "왼팔 부상" if episode == 1 else "왼팔 회복"
        active = episode == 1
        character_source = CharacterStage1Prediction(
            candidate_id=char_id, entity_ref=character_ref, entity_name="가람", matched_character_name="가람",
            match_status="MATCHED", sort_order=1, domain="CHARACTER", candidate_kind="SETTING",
            fact_type="STATUS", fact_key="status.arm_injury", value_type="STRING", display_value=value,
            value_json={"value": value, "active": active},
            evidence_spans=[{"quote": "가람은 왼팔을 다쳤다." if active else "가람의 왼팔이 회복되었다."}],
        )
        world_source = WorldStage1Prediction(
            candidate_id=world_id, domain="WORLD", category="LOCATION", subject_name="등대",
            subject_ref=world_ref, setting_name="불빛", source_values=["파란색"], sort_order=2,
            evidence_spans=[{"quote": "등대의 불빛은 파란색이다." if active else "등대의 불빛은 여전히 파란색이다."}],
        )
        character_decision = CharacterStage2Prediction(
            source_candidate_id=char_id, domain="CHARACTER", operation="ADD" if active else "REMOVE",
            resolved_canonical_fact_key="status.arm_injury", temporal_scope="PRESENT",
            proposed_value=value if active else None,
            proposed_value_json={"value": value, "active": True} if active else None,
            removed_snapshot_refs=[] if active else [slot_ref],
        )
        world_decision = WorldStage2Prediction(
            source_candidate_id=world_id, domain="WORLD", operation="ADD" if active else "EXCLUDE",
            consolidation_status="SINGLE", proposed_setting_name="불빛", proposed_value="파란색",
            target_ref=None if active else property_ref, matched_property_name=None if active else "불빛",
        )
        observations.append(OrderedScenarioObservation(
            episode_no=episode, source_hash=f"source-{episode}", elapsed_seconds=1,
            prediction=ScenarioPrediction(scenario_id=f"S{episode}",
                                          stage1=[character_source, world_source],
                                          stage2=[character_decision, world_decision]),
        ))
    journals = [
        SealedJournal(
            format_version=1, status="SEALED", run_id=UUID(int=1), work_id=UUID(int=2), generation=1,
            sequence=0, episode_no=1, job_id=UUID(int=10), input_state_hash=journal_state_hash(initial),
            output_state_hash=journal_state_hash(after_first), changes=[
                {"eventId": "character-add", "path": ["characters", character_ref],
                 "operation": "ADD", "value": after_first["characters"][character_ref],
                 "sourceCandidateIds": [candidate_ids[0]]},
                {"eventId": "world-add", "path": ["worldSettings", world_ref],
                 "operation": "ADD", "value": after_first["worldSettings"][world_ref],
                 "sourceCandidateIds": [candidate_ids[1]]},
            ],
        ),
        SealedJournal(
            format_version=1, status="SEALED", run_id=UUID(int=1), work_id=UUID(int=2), generation=1,
            sequence=1, episode_no=2, job_id=UUID(int=11), input_state_hash=journal_state_hash(after_first),
            output_state_hash=journal_state_hash(after_second), changes=[
                {"eventId": "character-remove", "path": ["characters", character_ref, "slots", "STATUS:status.arm_injury"],
                 "operation": "REMOVE", "remove": True, "sourceCandidateIds": [candidate_ids[2]]},
            ],
        ),
    ]
    gold = load_gold_snapshot_v3(
        Path(__file__).parent / "fixtures" / "multi_stage_setting_saved_predictions" / "gold.json"
    )
    bundle = build_ordered_prediction_bundle(
        fixture_hash=gold.fixture_hash, initial_backend_state=initial, journals=journals,
        observations=observations,
        project_state=lambda state: project_backend_state(state, scenario_id_by_episode={1: "S1", 2: "S2"}),
        run_id=UUID(int=1), work_id=UUID(int=2), generation=1, domains={"CHARACTER", "WORLD"},
        runtime_policy_version="java-journal/v1:synthetic-export",
    )
    untouched = bundle.model_dump()

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["endToEnd"]["metrics"]["afterStateF1"] == 1
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert bundle.model_dump() == untouched
    assert bundle.scenarios[0].runtime_state_trace.output_state.character_facts[0].value_type == "STRING"
    assert "valueType" not in after_first["characters"][character_ref]["slots"]["STATUS:status.arm_injury"]
    assert len(bundle.scenarios[1].runtime_state_trace.output_state.character_history) == 2


def test_initial_types_require_exported_metadata_instead_of_gold_or_json_guessing():
    from evals.multi_stage_setting.contracts import CharacterStateEntry
    from evals.multi_stage_setting.ordered_runtime_adapter import _with_character_value_types

    ref = character_state_ref("character:runtime", "PROFILE", "profile.height")
    state = EvaluationState(character_facts=[CharacterStateEntry(
        ref=ref, entity_ref="character:runtime", entity_name="가람", fact_type="PROFILE",
        fact_key="profile.height", value="170", value_json={"value": 170},
    )])
    original = state.model_dump()
    with pytest.raises(ValueError, match="runtime type metadata"):
        _with_character_value_types(state, {})
    projected = _with_character_value_types(state, {ref: "NUMBER"})
    assert projected.character_facts[0].value_type == "NUMBER"
    assert state.model_dump() == original
    # Do not coerce an observed wrong type to match either the JSON or Gold.
    observed_wrong = _with_character_value_types(state, {ref: "STRING"})
    assert observed_wrong.character_facts[0].value_type == "STRING"
    with pytest.raises(ValueError, match="differs from observed"):
        _with_character_value_types(projected, {ref: "STRING"})


def test_initial_runtime_type_catalog_is_required_and_does_not_rewrite_backend_hash():
    character_id = str(UUID(int=45))
    entity = "character:" + character_id
    ref = character_state_ref(entity, "PROFILE", "profile.height")
    initial = deepcopy(S0)
    initial["characters"][entity] = {"actualCharacterId": character_id, "provisionalSubjectKey": None,
        "name": "가람", "slots": {"PROFILE:profile.height": {"factType": "PROFILE",
            "factKey": "profile.height", "factValue": "170", "valueJson": {"value": 170}}}}
    original = deepcopy(initial)
    arguments = dict(fixture_hash="synthetic", initial_backend_state=initial, journals=[],
        observations=[_observation(failure=True)],
        project_state=lambda state: project_backend_state(state, scenario_id_by_episode={}),
        run_id=UUID(int=1), work_id=UUID(int=2), generation=1, domains={"CHARACTER"},
        runtime_policy_version="java-journal/v1:synthetic-export")
    with pytest.raises(ValueError, match="runtime type metadata"):
        build_ordered_prediction_bundle(**arguments)
    with pytest.raises(ValueError, match="absent fact"):
        build_ordered_prediction_bundle(**arguments, initial_character_value_types={"missing": "STRING"})
    bundle = build_ordered_prediction_bundle(**arguments, initial_character_value_types={ref: "NUMBER"})
    trace = bundle.scenarios[0].runtime_state_trace
    assert trace.input_state.character_facts[0].value_type == "NUMBER"
    assert trace.output_state == trace.input_state
    assert trace.backend_input_state_hash == trace.backend_output_state_hash == journal_state_hash(initial)
    assert initial == original
