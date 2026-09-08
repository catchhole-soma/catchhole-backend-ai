import asyncio
import json
from copy import deepcopy

import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Prediction,
    CharacterStage2Prediction,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStage2Prediction,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.report_cli import (
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING"])
@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE", "REVIEW_REQUIRED"])
def test_world_extra_keeps_actual_stage2_result_without_rescoring_gold(mode, operation):
    gold = _world_gold()
    sources = [_world_source("P-gold"), _world_source("P-extra", extra=True)]
    decisions = [_world_decision("P-gold"), _world_decision("P-extra", operation, extra=True)]
    bundle = _bundle(gold, sources, decisions, mode=mode)
    original = bundle.model_dump(mode="json")

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert bundle.model_dump(mode="json") == original
    cases = build_public_diagnostics(report)[0]["stage2"]
    assert [case["result"] for case in cases] == ["FULL_MATCH", "EXTRA_PROCESSED"]
    extra = cases[1]
    assert extra["sourceCandidateId"] == "P-extra"
    assert extra["decisionId"] is None
    assert extra["sourceGoldIds"] == []
    assert extra["expected"] is None
    assert extra["fields"] == {}
    assert extra["actual"]["operation"] == operation
    assert extra["actual"]["path"] == "도달 조건"
    assert extra["actual"]["value"] == "심연에 도달하면 튜토리얼이 완료된다."

    stage2 = report["stages"]["world"]["stage2"]
    assert stage2["counts"]["gold"] == stage2["counts"]["reachedAndCompared"] == 1
    assert stage2["counts"]["extraStage1Predictions"] == 1
    assert stage2["counts"]["suppressedExtraPredictions"] == (operation != "ADD")
    assert stage2["metrics"]["fullDecisionAccuracy"] == 1

    markdown = render_markdown_summary(report)
    assert "과추출 항목의 2차 처리 1" in markdown
    extra_row = _stage2_row(markdown, "P-extra")
    assert f"처리 방식: {operation}" in extra_row
    assert "LOCATION (장소) · 심연" in extra_row
    assert "대응하는 2차 답지 없음" in extra_row
    assert "정답·오답은 판정하지 않습니다" in extra_row
    assert "SECRET_" not in markdown
    assert "SECRET_" not in json.dumps(build_public_diagnostics(report))
    without_extra_rows = deepcopy(report)
    without_extra_rows["scenarios"][0]["stage2"] = [report["scenarios"][0]["stage2"][0]]
    assert build_source_free_summary(report) == build_source_free_summary(without_extra_rows)
    assert "sourceCandidateId" not in json.dumps(build_source_free_summary(report))


def test_world_extra_and_missed_gold_both_show_their_own_stage2_rows():
    gold = _world_gold()
    bundle = _bundle(
        gold,
        [_world_source("P-extra", extra=True)],
        [_world_decision("P-extra", extra=True)],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    cases = build_public_diagnostics(report)[0]["stage2"]
    assert [case["result"] for case in cases] == ["UPSTREAM_BLOCKED", "EXTRA_PROCESSED"]
    assert cases[0]["actual"] is None
    assert cases[1]["actual"]["operation"] == "ADD"
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 0
    markdown = render_markdown_summary(report)
    assert "이 답지 항목과 연결된 2차 결과 없음" in markdown
    assert "처리 방식: ADD" in _stage2_row(markdown, "P-extra")


@pytest.mark.parametrize("discovery", [False, True])
def test_character_extra_without_stage2_gold_or_record_still_has_a_trace_row(discovery):
    gold = _empty_gold("CHARACTER")
    source = _character_source(discovery=discovery)
    bundle = _bundle(gold, [source], [])

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    cases = build_public_diagnostics(report)[0]["stage2"]
    assert len(cases) == 1
    assert cases[0]["result"] == "EXTRA_NO_DECISION"
    assert cases[0]["sourceCandidateId"] == "P-character-extra"
    assert cases[0]["actual"] is None
    assert report["stages"]["character"]["stage2"]["counts"]["gold"] == 0
    markdown = render_markdown_summary(report)
    assert "**2차 처리 판단 결과**" in markdown
    row = _stage2_row(markdown, "P-character-extra")
    assert "이 추출 항목에 연결된 2차 결과 없음" in row
    assert "연결된 2차 결과가 기록되지 않았습니다" in row
    assert "오답으로 채점" not in row


@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE", "REVIEW_REQUIRED"])
def test_character_extra_keeps_actual_operation_and_canonical_path(operation):
    gold = _empty_gold("CHARACTER")
    decision = CharacterStage2Prediction(
        source_candidate_id="P-character-extra",
        domain="CHARACTER",
        operation=operation,
        resolved_canonical_fact_key="profile.species",
        proposed_value="바바리안" if operation == "ADD" else None,
        proposed_value_json=(
            {"value": "바바리안", "private": "SECRET_JSON"} if operation == "ADD" else None
        ),
        temporal_scope="PRESENT",
        comparison_reason="SECRET_REASON",
    )
    bundle = _bundle(gold, [_character_source()], [decision])

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    case = build_public_diagnostics(report)[0]["stage2"][0]
    assert case["result"] == "EXTRA_PROCESSED"
    assert case["actual"]["operation"] == operation
    assert case["actual"]["path"] == "profile.species"
    assert case["actual"]["temporalScope"] == "PRESENT"
    markdown = render_markdown_summary(report)
    row = _stage2_row(markdown, "P-character-extra")
    assert "비요른 얀델" in row
    assert "종족" in row
    assert "SECRET_" not in markdown


@pytest.mark.parametrize("with_gold", [False, True])
def test_merged_extra_candidates_share_one_actual_decision_without_duplicating_state(with_gold):
    gold = _world_gold() if with_gold else _empty_gold("WORLD")
    sources = [
        _world_source("P1", extra=not with_gold),
        _world_source("P2", extra=not with_gold),
    ]
    sources[1] = sources[1].model_copy(update={"setting_name": "튜토리얼 종료 조건"})
    decision = _world_decision("P1", extra=not with_gold).model_copy(
        update={"source_candidate_ids": ["P1", "P2"]}
    )
    bundle = _bundle(gold, sources, [decision])
    bundle = PredictionBundleV3.model_validate_json(bundle.model_dump_json())
    legacy_bundle = _bundle(gold, sources, [_world_decision("P1", extra=not with_gold)])

    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    legacy_report = asyncio.run(evaluate_multi_stage(gold, legacy_bundle))

    cases = build_public_diagnostics(report)[0]["stage2"]
    assert [(case["sourceCandidateId"], case["result"]) for case in cases] == [
        ("P1", "FULL_MATCH" if with_gold else "EXTRA_PROCESSED"),
        ("P2", "EXTRA_PROCESSED"),
    ]
    assert cases[0]["actual"] == cases[1]["actual"]
    assert len(bundle.scenarios[0].stage2) == 1
    assert (
        report["scenarios"][0]["predictedAfterStateHash"]
        == (legacy_report["scenarios"][0]["predictedAfterStateHash"])
    )
    assert build_source_free_summary(report) == build_source_free_summary(legacy_report)
    assert "2차 결과 없음" not in _stage2_row(render_markdown_summary(report), "P2")


def test_world_extra_without_record_is_not_hidden_or_marked_as_wrong():
    gold = _empty_gold("WORLD")
    bundle = _bundle(gold, [_world_source("P-extra", extra=True)], [])

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    case = build_public_diagnostics(report)[0]["stage2"][0]
    assert case["result"] == "EXTRA_NO_DECISION"
    assert case["fields"] == {}
    row = _stage2_row(render_markdown_summary(report), "P-extra")
    assert "이 추출 항목에 연결된 2차 결과 없음" in row
    assert "오답으로 채점" not in row


def test_oracle_world_batch_provenance_rejects_an_unknown_secondary_gold_source():
    gold = _world_gold()
    decision = _world_decision("W1").model_copy(update={"source_candidate_ids": ["W1", "missing"]})
    bundle = _bundle(gold, [], [decision], mode="ORACLE")

    with pytest.raises(ValueError, match="unknown Gold Stage2 source missing"):
        asyncio.run(evaluate_multi_stage(gold, bundle))


def _stage2_row(markdown, source_id):
    return next(line for line in markdown.splitlines() if f"모델 추출: {source_id}" in line)


def _empty_gold(domain):
    return GoldSnapshotV3(
        dataset_version="v3",
        name="extra candidate trace",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01.txt",
                target_domains={domain},
                gold_version="v3",
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                candidate_free=True,
                review_status="FINAL",
            )
        ],
    ).with_fixture_hash()


def _world_gold():
    scenario = _empty_gold("WORLD").scenarios[0].model_copy(update={"candidate_free": False})
    return GoldSnapshotV3(
        dataset_version="v3",
        name="extra candidate trace",
        scenarios=[scenario],
        stage1=[
            WorldStage1Gold(
                gold_id="W1",
                scenario_id="S1",
                episode_no=1,
                sort_order=1,
                decision="EXTRACT",
                importance="MUST",
                evidence_quotes=["SECRET_GOLD_QUOTE"],
                review_status="FINAL",
                domain="WORLD",
                candidate_kind="WORLD_SETTING",
                category="WORLD_RULE_HISTORY",
                subject_name="던전 앤 스톤",
                setting_name="튜토리얼 완료 시점",
                source_values=["심연에 도달하면 튜토리얼이 완료된다."],
            )
        ],
        stage2=[
            WorldStage2Gold(
                decision_id="D1",
                scenario_id="S1",
                episode_no=1,
                sort_order=1,
                source_gold_ids=["W1"],
                domain="WORLD",
                operation="ADD",
                consolidation_status="SINGLE",
                proposed_setting_name="튜토리얼 완료 시점",
                proposed_value="심연에 도달하면 튜토리얼이 완료된다.",
                review_status="FINAL",
            )
        ],
    ).with_fixture_hash()


def _world_source(source_id, *, extra=False):
    return WorldStage1Prediction(
        candidate_id=source_id,
        domain="WORLD",
        category="LOCATION" if extra else "WORLD_RULE_HISTORY",
        subject_name="심연" if extra else "던전 앤 스톤",
        setting_name="도달 조건" if extra else "튜토리얼 완료 시점",
        source_values=["심연에 도달하면 튜토리얼이 완료된다."],
        evidence_spans=[{"quote": "SECRET_PREDICTION_QUOTE"}],
    )


def _world_decision(source_id, operation="ADD", *, extra=False):
    return WorldStage2Prediction(
        source_candidate_id=source_id,
        domain="WORLD",
        operation=operation,
        consolidation_status="SINGLE",
        proposed_setting_name="도달 조건" if extra else "튜토리얼 완료 시점",
        proposed_value="심연에 도달하면 튜토리얼이 완료된다.",
        comparison_reason="SECRET_REASON",
    )


def _character_source(*, discovery=False):
    return CharacterStage1Prediction(
        candidate_id="P-character-extra",
        domain="CHARACTER",
        candidate_kind="CHARACTER_DISCOVERY" if discovery else "SETTING",
        entity_ref="character:bjorn-yandel",
        entity_name="비요른 얀델",
        **(
            {}
            if discovery
            else {
                "fact_type": "PROFILE",
                "fact_key": "profile.species",
                "value_type": "STRING",
                "display_value": "바바리안",
                "value_json": {"value": "바바리안"},
            }
        ),
    )


def _bundle(gold, sources, decisions, *, mode="FIXED"):
    return PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode=mode,
        evaluation_domains=gold.scenarios[0].target_domains,
        scenarios=[ScenarioPrediction(scenario_id="S1", stage1=sources, stage2=decisions)],
    )
