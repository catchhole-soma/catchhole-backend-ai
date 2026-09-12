import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

import evals.multi_stage_setting.runtime_adapter as runtime
from evals.multi_stage_setting.contracts import (
    EvaluationMode,
    EvaluationState,
    GoldSnapshotV3,
    PredictionBundleV3,
    RuntimeStateTrace,
    ScenarioGold,
    ScenarioPrediction,
    WorldStage1Prediction,
    WorldStage2Prediction,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3


FIXTURES = Path(__file__).parent / "fixtures" / "multi_stage_setting_saved_predictions"


def _gold():
    # Poisoned per-episode Gold state is deliberately irrelevant to runtime S0.
    return GoldSnapshotV3(
        dataset_version="test", name="common-start isolation",
        scenarios=[
            ScenarioGold(
                scenario_id=f"S{i}", episode_no=i, source_identifier=f"{i}.txt",
                source_text=f"회차 {i}의 원문", target_domains={"WORLD"}, gold_version="test",
                start_state_mode="EMPTY" if i == 1 else "PREVIOUS_GOLD",
                previous_scenario_id=None if i == 1 else "S1",
                cumulative_through_episode=i - 1, candidate_free=True, review_status="FINAL",
            ) for i in (1, 2)
        ],
    ).with_fixture_hash()


def _components():
    return runtime.RuntimeComponents(
        character_comparator=object(), world_comparator=object(),
        world_extractor=object(), world_subject_resolver=object(),
    )


def _trace(before, after):
    return RuntimeStateTrace(
        input_state=before, output_state=after,
        input_state_hash=before.content_hash(), output_state_hash=after.content_hash(),
        source_hash="synthetic-source", elapsed_seconds=0,
    )


def test_common_start_uses_explicit_s0_for_every_episode_without_gold_lookup(monkeypatch):
    seen = []

    def forbid_gold(*args):
        raise AssertionError("Gold state must never initialize COMMON_START runtime.")

    async def live(scenario, before, *args, trace):
        seen.append(before.model_copy(deep=True))
        return ScenarioPrediction(
            scenario_id=scenario.scenario_id,
            stage1=[WorldStage1Prediction(
                candidate_id="P", domain="WORLD", category="LOCATION", subject_name="등대",
                setting_name="불빛", source_values=["파란색"],
            )],
            stage2=[WorldStage2Prediction(
                source_candidate_id="P", domain="WORLD", operation="ADD",
                consolidation_status="SINGLE", proposed_setting_name="불빛", proposed_value="파란색",
            )],
        )

    monkeypatch.setattr(runtime, "build_gold_state_chain", forbid_gold)
    monkeypatch.setattr(runtime, "_run_live_scenario", live)
    s0 = EvaluationState()
    bundle = asyncio.run(runtime.run_multi_stage_predictions(
        _gold(), mode=EvaluationMode.COMMON_START, components=_components(), frozen_start_state=s0,
    ))

    assert seen == [s0, s0]
    assert s0 == EvaluationState()
    assert bundle.state_application_policy == "COMMON_START"
    assert all(item.runtime_state_trace.input_state == s0 for item in bundle.scenarios)
    assert all(item.runtime_state_trace.output_state.world_facts for item in bundle.scenarios)


def test_common_start_never_defaults_to_gold_and_legacy_rejects_new_state():
    with pytest.raises(ValueError, match="explicit frozen_start_state"):
        asyncio.run(runtime.run_multi_stage_predictions(
            _gold(), mode=EvaluationMode.COMMON_START, components=_components(),
        ))
    with pytest.raises(ValueError, match="Legacy modes"):
        asyncio.run(runtime.run_multi_stage_predictions(
            _gold(), mode=EvaluationMode.FIXED, components=_components(),
            frozen_start_state=EvaluationState(),
        ))


def test_ordered_runtime_cannot_silently_use_accept_all_rolling_reducer():
    with pytest.raises(ValueError, match="sealed-journal adapter"):
        asyncio.run(runtime.run_multi_stage_predictions(
            _gold(), mode=EvaluationMode.ORDERED_PROVISIONAL, components=_components(),
        ))


def test_experimental_state_trace_requires_matching_content_hash():
    with pytest.raises(ValidationError, match="inputStateHash"):
        RuntimeStateTrace(
            input_state=EvaluationState(), output_state=EvaluationState(),
            input_state_hash="tampered", output_state_hash=EvaluationState().content_hash(),
            source_hash="source", elapsed_seconds=0,
        )


def test_experimental_scorer_uses_runtime_trace_instead_of_gold_before_state():
    gold = load_gold_snapshot_v3(FIXTURES / "gold.json")
    s0 = EvaluationState()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="COMMON_START", frozen_start_state=s0,
        runtime_policy_version="common-start/v1",
        scenarios=[ScenarioPrediction(scenario_id=f"S{i}", runtime_state_trace=_trace(s0, s0))
                   for i in (1, 2)],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["scenarios"][1]["runtimeInputStateHash"] == s0.content_hash()
    assert report["scenarios"][1]["beforeStateHash"] != s0.content_hash()
    assert report["scenarios"][1]["predictedAfterStateHash"] == s0.content_hash()
    assert report["run"]["runtimePolicyVersion"] == "common-start/v1"


def test_ordered_scorer_requires_predecessor_trace_and_matching_state_chain():
    gold = _gold()
    s0 = EvaluationState()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="ORDERED_PROVISIONAL", frozen_start_state=s0,
        runtime_policy_version="java-journal/v1", evaluation_scenario_ids=["S2"],
        runtime_run_id="synthetic-run", runtime_generation=1,
        scenarios=[ScenarioPrediction(scenario_id="S2", runtime_state_trace=_trace(s0, s0).model_copy(
            update={"state_readiness": "SEALED", "backend_input_state_hash": "backend-s0",
                    "backend_output_state_hash": "backend-s0", "runtime_sequence": 1},
        ))],
    )
    with pytest.raises(ValueError, match="missing runtime traces"):
        asyncio.run(evaluate_multi_stage(gold, bundle))


def test_legacy_mode_rejects_experimental_trace_injection():
    s0 = EvaluationState()
    with pytest.raises(ValidationError, match="Legacy modes"):
        PredictionBundleV3(
            fixture_hash="f", mode="FIXED",
            scenarios=[ScenarioPrediction(scenario_id="S1", runtime_state_trace=_trace(s0, s0))],
        )


def test_new_ordered_run_does_not_implicitly_include_gold_predecessor():
    gold = _gold()
    s0 = EvaluationState()
    trace = _trace(s0, s0).model_copy(update={
        "state_readiness": "SEALED", "runtime_sequence": 0,
        "backend_input_state_hash": "backend-s0", "backend_output_state_hash": "backend-s0",
        "source_hash": hashlib.sha256(gold.scenarios[1].source_text.encode()).hexdigest(),
    })
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="ORDERED_PROVISIONAL", frozen_start_state=s0,
        runtime_policy_version="java-journal/v1:new-run", runtime_run_id="new-run", runtime_generation=1,
        evaluation_scenario_ids=["S2"],
        scenarios=[ScenarioPrediction(scenario_id="S2", runtime_state_trace=trace)],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert [item["scenarioId"] for item in report["scenarios"]] == ["S2"]


def test_experimental_scorer_rechecks_trace_hashes_after_in_memory_mutation():
    gold = load_gold_snapshot_v3(FIXTURES / "gold.json")
    s0 = EvaluationState()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="COMMON_START", frozen_start_state=s0,
        runtime_policy_version="common-start/v1", evaluation_scenario_ids=["S1"],
        scenarios=[ScenarioPrediction(scenario_id="S1", runtime_state_trace=_trace(s0, s0))],
    )
    bundle.scenarios[0].runtime_state_trace.output_state_hash = "tampered-after-construction"

    with pytest.raises(ValidationError, match="outputStateHash"):
        asyncio.run(evaluate_multi_stage(gold, bundle))
