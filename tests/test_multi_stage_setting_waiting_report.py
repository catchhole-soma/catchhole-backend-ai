import asyncio
import json

import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Gold,
    CharacterStage2Prediction,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    Stage2Policy,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.report_cli import (
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)

WAIT_EXPLANATION = "인물 연결 대기 (답지는 인물 연결 전 2차 비교를 요구하지 않음)"


def _fixture(*, waiting: bool = True) -> GoldSnapshotV3:
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
        entity_ref="character:unknown" if waiting else "character:bjorn",
        entity_name="미상" if waiting else "비요른",
        fact_type="PROFILE",
        fact_key="profile.species",
        value_type="STRING",
        display_value="바바리안",
        value_json={"value": "바바리안"},
        stage2_policy=(
            Stage2Policy.WAIT_FOR_CHARACTER_MATCH if waiting else Stage2Policy.REQUIRED
        ),
    )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="waiting-character-report",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01.txt",
                source_text="나는 바바리안이다. SECRET_MANUSCRIPT",
                target_domains={"CHARACTER"},
                gold_version="v3",
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
        stage1=[source],
        stage2=[] if waiting else [
            CharacterStage2Gold(
                decision_id="D1",
                scenario_id="S1",
                episode_no=1,
                sort_order=1,
                source_gold_ids=["C1"],
                domain="CHARACTER",
                operation="ADD",
                temporal_scope="PRESENT",
                proposed_value="바바리안",
                proposed_value_json={"value": "바바리안"},
                review_status="FINAL",
            )
        ],
    ).with_fixture_hash()


def _report(
    *,
    waiting: bool = True,
    include_prediction: bool = True,
    match_status: str = "AMBIGUOUS",
    fact_key: str = "profile.species",
    actual_value: str = "바바리안",
    unexpected_stage2: bool = False,
    mode: str = "FIXED",
    domains: set[str] | None = None,
):
    gold = _fixture(waiting=waiting)
    prediction = CharacterStage1Prediction(
        candidate_id="P1",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_name=gold.stage1[0].entity_name,
        match_status=match_status,
        fact_type="PROFILE",
        fact_key=fact_key,
        value_type="STRING",
        display_value=actual_value,
        value_json={"value": actual_value},
        evidence_spans=[{"quote": "나는 바바리안이다."}],
    )
    predictions = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode=mode,
        evaluation_domains=domains or {"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[prediction] if include_prediction else [],
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="P1",
                        domain="CHARACTER",
                        operation="ADD",
                        resolved_canonical_fact_key="profile.species",
                        temporal_scope="PRESENT",
                        proposed_value="바바리안",
                        proposed_value_json={"value": "바바리안"},
                    )
                ] if unexpected_stage2 else [],
            )
        ],
    )
    return asyncio.run(evaluate_multi_stage(gold, predictions))


@pytest.mark.parametrize("match_status", ["AMBIGUOUS", "UNRESOLVED", "WAITING_FOR_CHARACTER_MATCH"])
def test_expected_wait_is_a_correct_extraction_without_stage2_gold(match_status: str) -> None:
    report = _report(match_status=match_status)

    stage1 = report["stages"]["character"]["stage1"]
    stage2 = report["stages"]["character"]["stage2"]
    case = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]
    assert stage1["metrics"]["candidateF1"] == 1
    assert case["result"] == "FULL_MATCH"
    assert case["stage2Policy"] == "WAIT_FOR_CHARACTER_MATCH"
    assert case["upstreamOutcome"] == "UPSTREAM_BLOCKED_SUBJECT"
    assert stage2["counts"]["gold"] == 0
    assert stage2["counts"]["waitingForCharacterMatch"] == 1
    assert stage2["counts"]["upstreamOutcomes"] == {}
    assert stage2["metrics"]["fullDecisionAccuracy"] is None
    assert report["scenarios"][0]["stage2"] == []
    assert report["failureCauses"].get("EXTRACTION_MISS", 0) == 0
    markdown = render_markdown_summary(report)
    assert WAIT_EXPLANATION in markdown
    assert "| Gold (정답지의 처리 결정) | 0개 | 미평가 |" in markdown
    assert "| 인물 연결 대기 (정답지가 2차 진행을 보류한 1차 항목) | 1개 | 미평가 |" in markdown
    assert "정답지가 정한 정책 수" in markdown


def test_wait_policy_does_not_hide_a_missing_extraction() -> None:
    report = _report(include_prediction=False)

    assert report["stages"]["character"]["stage1"]["counts"]["missed"] == 1
    assert report["stages"]["character"]["stage1"]["metrics"]["candidateRecall"] == 0
    assert report["stages"]["character"]["stage2"]["counts"]["waitingForCharacterMatch"] == 1
    assert report["failureCauses"]["EXTRACTION_MISS"] == 1
    case = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]
    assert case["result"] == "MISSED"
    assert case["upstreamOutcome"] == "UPSTREAM_MISSING"
    markdown = render_markdown_summary(report)
    assert "대응하는 모델 추출 결과를 찾지 못했습니다" in markdown
    assert WAIT_EXPLANATION not in markdown


def test_waiting_status_with_unresolved_identity_is_not_an_extraction_failure() -> None:
    gold = _fixture()
    gold.stage1[0] = CharacterStage1Gold.model_validate(
        gold.stage1[0].model_dump() | {
            "fact_type": "STATUS", "fact_key": "status.피로", "value_type": "JSON",
            "display_value": "피로가 쌓였다.", "value_json": {"name": "피로"},
        }
    )
    gold = gold.with_fixture_hash()
    prediction = CharacterStage1Prediction(
        candidate_id="P1", domain="CHARACTER", candidate_kind="SETTING",
        entity_name="미상", match_status="AMBIGUOUS", fact_type="STATUS",
        fact_key="status.누적_피로", value_type="JSON", display_value="피로가 쌓였다.",
        value_json={"name": "피로"},
    )
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="FIXED", evaluation_domains={"CHARACTER"},
        scenarios=[ScenarioPrediction(scenario_id="S1", stage1=[prediction])],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    stage1 = report["stages"]["character"]["stage1"]
    assert stage1["counts"]["semanticPending"] == 1
    assert stage1["metrics"]["candidateF1"] is None
    assert report["failureCauses"].get("EXTRACTION_MISS", 0) == 0


def test_wait_policy_does_not_hide_a_wrong_fact_path() -> None:
    report = _report(fact_key="profile.gender")

    case = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]
    assert case["result"] == "PARTIAL_MATCH"
    assert case["fields"]["path"] == "MISMATCH"
    assert report["failureCauses"]["EXTRACTION_MISS"] == 1
    assert WAIT_EXPLANATION not in render_markdown_summary(report)


def test_wait_policy_does_not_turn_semantic_pending_into_a_success_or_failure() -> None:
    report = _report(actual_value="바바리안 종족에 속한다.")

    case = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]
    assert case["result"] == "PARTIAL_MATCH"
    assert case["fields"]["value"] == "PENDING"
    assert report["failureCauses"].get("EXTRACTION_MISS", 0) == 0
    assert WAIT_EXPLANATION not in render_markdown_summary(report)


def test_required_named_character_keeps_its_subject_blocked_diagnostic() -> None:
    report = _report(waiting=False)

    counts = report["stages"]["character"]["stage2"]["counts"]
    assert counts["gold"] == 1
    assert counts["waitingForCharacterMatch"] == 0
    assert counts["upstreamOutcomes"] == {"UPSTREAM_BLOCKED_SUBJECT": 1}
    assert report["scenarios"][0]["stage2"][0]["result"] == "UPSTREAM_BLOCKED"
    assert report["failureCauses"]["EXTRACTION_MISS"] == 1
    assert WAIT_EXPLANATION not in render_markdown_summary(report)


def test_wait_count_remains_a_gold_policy_in_oracle_mode() -> None:
    report = _report(mode="ORACLE", include_prediction=False)

    assert report["stages"]["character"]["stage1"]["evaluated"] is False
    assert report["stages"]["character"]["stage2"]["counts"]["waitingForCharacterMatch"] == 1
    assert report["stages"]["character"]["stage2"]["counts"]["gold"] == 0


def test_wait_policy_is_not_counted_for_a_disabled_character_domain() -> None:
    report = _report(domains={"WORLD"})

    assert report["stages"]["character"]["stage2"]["evaluated"] is False
    assert report["stages"]["world"]["stage2"]["counts"]["waitingForCharacterMatch"] == 0


def test_wait_count_and_extraction_failures_only_include_selected_scenarios() -> None:
    first = _fixture()
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="selected-waiting-report",
        scenarios=[
            first.scenarios[0],
            first.scenarios[0].model_copy(update={"scenario_id": "S2", "episode_no": 2}),
        ],
        stage1=[
            first.stage1[0],
            first.stage1[0].model_copy(
                update={"gold_id": "C2", "scenario_id": "S2", "episode_no": 2}
            ),
        ],
    ).with_fixture_hash()
    predictions = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        evaluation_scenario_ids=["S2"],
        scenarios=[ScenarioPrediction(scenario_id="S2")],
    )

    report = asyncio.run(evaluate_multi_stage(gold, predictions))

    assert report["stages"]["character"]["stage2"]["counts"]["waitingForCharacterMatch"] == 1
    assert report["failureCauses"]["EXTRACTION_MISS"] == 1
    assert [scenario["scenarioId"] for scenario in report["scenarios"]] == ["S2"]


def test_an_unexpected_stage2_answer_is_still_checked_for_state_side_effects() -> None:
    report = _report(unexpected_stage2=True)

    case = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]
    assert case["result"] == "FULL_MATCH"
    counts = report["endToEnd"]["counts"]
    assert counts["stateApplicationErrors"] == 0
    assert counts["predictedTransitions"] > 0
    assert counts["matchedTransitions"] == 0
    assert counts["expectedTransitions"] == 0
    assert report["stages"]["character"]["stage2"]["counts"]["gold"] == 0


def test_waiting_report_allowlists_policy_and_keeps_aggregate_json_source_free() -> None:
    report = _report()
    case = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]
    case["waitingReason"] = "SECRET_REASON"
    case["rawAiResult"] = "SECRET_RAW"
    case["expected"]["evidenceQuotes"] = ["SECRET_QUOTE"]
    case["actual"]["value"] = "허용된 값 | <script>x</script> ![leak](https://example.invalid)"

    summary = build_source_free_summary(report)
    serialized = json.dumps(summary, ensure_ascii=False)
    assert summary["stages"]["character"]["stage2"]["counts"]["waitingForCharacterMatch"] == 1
    assert "stage2Policy" not in serialized
    assert "SECRET_" not in serialized
    public_case = build_public_diagnostics(report)[0]["stage1"]["character"]["cases"][0]
    assert public_case["stage2Policy"] == "WAIT_FOR_CHARACTER_MATCH"
    assert "waitingReason" not in public_case
    markdown = render_markdown_summary(report)
    assert "SECRET_" not in markdown
    assert "<script>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "&#124;" in markdown
    assert "![leak]" not in markdown

    case["stage2Policy"] = "SECRET_INVALID_POLICY"
    public_case = build_public_diagnostics(report)[0]["stage1"]["character"]["cases"][0]
    assert "stage2Policy" not in public_case
    assert "SECRET_" not in render_markdown_summary(report)
