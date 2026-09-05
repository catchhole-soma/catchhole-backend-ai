import asyncio
import json

import pytest
from pydantic import ValidationError

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Gold,
    CharacterStage2Prediction,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    WorldStage1Prediction,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage


def test_fixed_prediction_rejects_unknown_stage2_source() -> None:
    with pytest.raises(ValidationError, match="references unknown Stage1 candidate missing"):
        PredictionBundleV3(
            fixture_hash="fixture",
            mode="FIXED",
            scenarios=[
                ScenarioPrediction(
                    scenario_id="S1",
                    stage2=[_character_stage2("missing")],
                )
            ],
        )


def test_rolling_prediction_rejects_stage2_source_domain_mismatch() -> None:
    with pytest.raises(ValidationError, match="different domain from Stage1 candidate p1"):
        PredictionBundleV3(
            fixture_hash="fixture",
            mode="ROLLING",
            scenarios=[
                ScenarioPrediction(
                    scenario_id="S1",
                    stage1=[
                        WorldStage1Prediction(
                            candidate_id="p1",
                            domain="WORLD",
                            category="RACE",
                            subject_name="고블린",
                            setting_name="체격",
                            source_values=["평균 140cm다."],
                        )
                    ],
                    stage2=[_character_stage2("p1")],
                )
            ],
        )


def test_prediction_rejects_multiple_stage2_decisions_for_one_source() -> None:
    with pytest.raises(ValidationError, match="Stage2 source candidate IDs"):
        PredictionBundleV3(
            fixture_hash="fixture",
            mode="FIXED",
            scenarios=[
                ScenarioPrediction(
                    scenario_id="S1",
                    stage1=[_character_stage1("p1")],
                    stage2=[_character_stage2("p1"), _character_stage2("p1")],
                )
            ],
        )


def test_character_stage2_prediction_requires_resolved_canonical_fact_key() -> None:
    with pytest.raises(ValidationError, match="resolvedCanonicalFactKey"):
        CharacterStage2Prediction(
            source_candidate_id="p1",
            domain="CHARACTER",
            operation="ADD",
            proposed_value="바바리안",
            proposed_value_json={"value": "바바리안"},
            temporal_scope="PRESENT",
        )


def test_character_stage2_prediction_rejects_duplicate_removal_refs() -> None:
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        CharacterStage2Prediction(
            source_candidate_id="p1",
            domain="CHARACTER",
            operation="REMOVE",
            resolved_canonical_fact_key="status.회복",
            removed_snapshot_refs=["P1", "P1"],
            temporal_scope="PRESENT",
        )


def test_character_stage2_prediction_rejects_active_false_upsert() -> None:
    with pytest.raises(ValidationError, match="active=false facts"):
        CharacterStage2Prediction(
            source_candidate_id="p1",
            domain="CHARACTER",
            operation="ADD",
            resolved_canonical_fact_key="status.회복",
            proposed_value="회복 완료",
            proposed_value_json={"name": "회복", "active": False},
            temporal_scope="PRESENT",
        )


def test_character_stage1_prediction_requires_complete_setting_fields() -> None:
    incomplete = CharacterStage1Prediction(
        candidate_id="p1",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_name="비요른",
        fact_key="profile.species",
        display_value="바바리안",
    )

    with pytest.raises(ValidationError, match="handoff requires factType, factKey"):
        ScenarioPrediction(
            scenario_id="S1",
            stage1=[incomplete],
        )


def test_character_discovery_prediction_forbids_setting_fields() -> None:
    with pytest.raises(ValidationError, match="must not include setting value fields"):
        CharacterStage1Prediction(
            candidate_id="p1",
            domain="CHARACTER",
            candidate_kind="CHARACTER_DISCOVERY",
            entity_name="카락",
            fact_type="PROFILE",
            fact_key="profile.species",
            value_type="STRING",
            display_value="바바리안",
            value_json={"value": "바바리안"},
        )


@pytest.mark.parametrize(
    ("value_type", "value_json"),
    [
        ("NUMBER", {"value": "180"}),
        ("BOOLEAN", {"value": "false"}),
        ("STRING", {"value": 180}),
        ("NUMBER", {"label": 180}),
    ],
)
def test_character_stage1_prediction_rejects_coerced_scalar_json(
    value_type: str,
    value_json: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="valueJson"):
        CharacterStage1Prediction(
            candidate_id="p1",
            domain="CHARACTER",
            candidate_kind="SETTING",
            entity_name="비요른",
            fact_type="STAT",
            fact_key="stat.height",
            value_type=value_type,
            display_value="180",
            value_json=value_json,
        )


def test_prediction_bundle_rejects_state_policy_inconsistent_with_mode() -> None:
    with pytest.raises(ValidationError, match="inconsistent with evaluation mode"):
        PredictionBundleV3(
            fixture_hash="fixture",
            mode="ROLLING",
            state_application_policy="SCENARIO_LOCAL",
        )


def test_prediction_bundle_derives_state_policy_from_mode() -> None:
    bundle = PredictionBundleV3(fixture_hash="fixture", mode="ROLLING")

    assert bundle.state_application_policy == "ACCEPT_ALL_PREDICTIONS"


def test_prediction_bundle_rejects_coerced_stage2_scalar_json() -> None:
    source = CharacterStage1Prediction(
        candidate_id="p1",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_name="비요른",
        fact_type="STAT",
        fact_key="stat.height",
        value_type="NUMBER",
        display_value="180",
        value_json={"value": 180},
    )
    decision = CharacterStage2Prediction(
        source_candidate_id="p1",
        domain="CHARACTER",
        operation="ADD",
        resolved_canonical_fact_key="stat.height",
        proposed_value="180",
        proposed_value_json={"value": "180"},
        temporal_scope="PRESENT",
    )

    with pytest.raises(ValidationError, match="native JSON scalar type"):
        PredictionBundleV3(
            fixture_hash="fixture",
            mode="FIXED",
            scenarios=[ScenarioPrediction(scenario_id="S1", stage1=[source], stage2=[decision])],
        )


def test_failed_character_stage1_forbids_handoff_and_stage2_outputs() -> None:
    with pytest.raises(ValidationError, match="must not include handoff"):
        ScenarioPrediction(
            scenario_id="S1",
            pipeline_status="PIPELINE_FAILED",
            failed_stage="CHARACTER_STAGE1",
            stage1=[_character_stage1("p1")],
        )


def test_failed_world_stage1_allows_prior_character_outputs_but_forbids_world_outputs() -> None:
    scenario = ScenarioPrediction(
        scenario_id="S1",
        pipeline_status="PIPELINE_FAILED",
        failed_stage="WORLD_STAGE1",
        stage1=[_character_stage1("p1")],
        stage2=[_character_stage2("p1")],
    )
    assert len(scenario.stage2) == 1

    with pytest.raises(ValidationError, match="must not include World"):
        ScenarioPrediction(
            scenario_id="S1",
            pipeline_status="PIPELINE_FAILED",
            failed_stage="WORLD_STAGE1",
            stage1=[
                WorldStage1Prediction(
                    candidate_id="w1",
                    domain="WORLD",
                    category="RACE",
                    subject_name="고블린",
                    setting_name="체격",
                    source_values=["평균 140cm다."],
                )
            ],
        )


def test_fixed_prediction_rejects_removal_from_non_status_source() -> None:
    decision = _character_stage2("p1").model_copy(
        update={
            "operation": "REMOVE",
            "target_ref": None,
            "removed_snapshot_refs": ["P1"],
            "proposed_value": None,
            "proposed_value_json": None,
        }
    )

    with pytest.raises(ValidationError, match="require a STATUS source"):
        PredictionBundleV3(
            fixture_hash="fixture",
            mode="FIXED",
            scenarios=[
                ScenarioPrediction(
                    scenario_id="S1",
                    stage1=[_character_stage1("p1")],
                    stage2=[decision],
                )
            ],
        )


def test_oracle_evaluator_rejects_source_that_is_not_a_gold_stage2_source() -> None:
    gold = _oracle_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[_character_stage2("unknown-gold-source")],
            )
        ],
    )

    with pytest.raises(ValueError, match="unknown Gold Stage2 source"):
        asyncio.run(evaluate_multi_stage(gold, bundle))


def test_report_exposes_models_prompts_and_message_free_runtime_failure_counts() -> None:
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="runtime failures",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01.txt",
                target_domains={"CHARACTER"},
                gold_version="v3",
                candidate_free=True,
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
    ).with_fixture_hash()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        analysis_model="extract-model",
        subject_resolution_model="subject-model",
        comparison_model="compare-model",
        prompt_versions={"characterExtraction": "extract:v1"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                failures=[
                    {
                        "stage": "CHARACTER_STAGE1",
                        "sourceId": "chunk-1",
                        "errorType": "LLM_TIMEOUT",
                        "message": "비밀 원문이 포함된 상세 오류",
                    },
                    {
                        "stage": "CHARACTER_STAGE2",
                        "sourceId": "candidate-1",
                        "errorType": "LLM_TIMEOUT",
                        "message": "내부 provider 응답",
                    },
                    {
                        "stage": "WORLD_STAGE2",
                        "errorType": "INVALID_RESPONSE",
                        "message": "민감한 응답 본문",
                    },
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["run"]["analysisModel"] == "extract-model"
    assert report["run"]["subjectResolutionModel"] == "subject-model"
    assert report["run"]["comparisonModel"] == "compare-model"
    assert report["run"]["promptVersions"] == {
        "characterExtraction": "extract:v1"
    }
    assert report["run"]["runtimeFailures"] == {
        "total": 3,
        "byStage": {
            "CHARACTER_STAGE1": 1,
            "CHARACTER_STAGE2": 1,
            "WORLD_STAGE2": 1,
        },
        "byErrorType": {"INVALID_RESPONSE": 1, "LLM_TIMEOUT": 2},
    }
    serialized_run = json.dumps(report["run"], ensure_ascii=False)
    assert "비밀 원문" not in serialized_run
    assert "provider 응답" not in serialized_run
    assert "민감한 응답" not in serialized_run


def _character_stage1(candidate_id: str) -> CharacterStage1Prediction:
    return CharacterStage1Prediction(
        candidate_id=candidate_id,
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.species",
        value_type="STRING",
        display_value="바바리안",
        value_json={"value": "바바리안"},
    )


def _character_stage2(source_id: str) -> CharacterStage2Prediction:
    return CharacterStage2Prediction(
        source_candidate_id=source_id,
        domain="CHARACTER",
        operation="ADD",
        resolved_canonical_fact_key="profile.species",
        proposed_value="바바리안",
        proposed_value_json={"value": "바바리안"},
        temporal_scope="PRESENT",
    )


def _oracle_gold() -> GoldSnapshotV3:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01.txt",
        target_domains={"CHARACTER"},
        gold_version="v3",
        candidate_free=False,
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    source = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["나는 바바리안이다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjoern",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.species",
        value_type="STRING",
        display_value="바바리안",
        value_json={"value": "바바리안"},
    )
    decision = CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="ADD",
        proposed_value="바바리안",
        proposed_value_json={"value": "바바리안"},
        temporal_scope="PRESENT",
        review_status="FINAL",
    )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="oracle contract",
        scenarios=[scenario],
        stage1=[source],
        stage2=[decision],
    ).with_fixture_hash()
