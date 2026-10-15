import asyncio
import hashlib
import json

import pytest

from evals.multi_stage_setting import cli
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Gold,
    CharacterStage2Prediction,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.report_cli import (
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)

SOURCE = "Mara is a ranger.\nShe remains a ranger."
BOUNDARY = SOURCE.index("She")


def _fixture():
    source = CharacterStage1Gold(
        gold_id="G1", scenario_id="S1", episode_no=1, sort_order=1,
        decision="EXTRACT", importance="MUST", review_status="FINAL",
        domain="CHARACTER", candidate_kind="SETTING", entity_ref="character:mara",
        entity_name="Mara", fact_type="PROFILE", fact_key="profile.occupation",
        value_type="STRING", display_value="ranger", value_json={"value": "ranger"},
        context_tags=["CURRENT", "REPEAT"], evidence_quotes=["Mara is a ranger."],
    )
    gold = GoldSnapshotV3(
        dataset_version="test", name="synthetic reviewed repeats",
        scenarios=[ScenarioGold(
            scenario_id="S1", episode_no=1, source_identifier="synthetic",
            source_text=SOURCE, target_domains={"CHARACTER"}, gold_version="test",
            start_state_mode="EMPTY", cumulative_through_episode=0, review_status="FINAL",
        )],
        stage1=[source],
        stage2=[CharacterStage2Gold(
            decision_id="D1", scenario_id="S1", episode_no=1, sort_order=1,
            source_gold_ids=["G1"], domain="CHARACTER", operation="EXCLUDE",
            temporal_scope="PRESENT", review_status="FINAL",
        )],
    ).with_fixture_hash()
    predictions = [
        CharacterStage1Prediction(
            candidate_id=candidate_id, sort_order=order, domain="CHARACTER",
            candidate_kind="SETTING", entity_ref="character:mara", entity_name="Mara",
            matched_character_name="Mara", match_status="MATCHED", fact_type="PROFILE",
            fact_key="profile.occupation", value_type="STRING", display_value="ranger",
            value_json={"value": "ranger"},
            evidence_spans=[{"quote": quote, "start_offset": start, "end_offset": end}],
        )
        for candidate_id, order, quote, start, end in [
            ("P1", 1, "Mara is a ranger.", 0, BOUNDARY - 1),
            ("P2", 1_000_001, "She remains a ranger.", BOUNDARY, len(SOURCE)),
        ]
    ]
    scenario = ScenarioPrediction(
        scenario_id="S1", raw_stage1=predictions, stage1=predictions,
        stage2=[CharacterStage2Prediction(
            source_candidate_id=source.candidate_id, domain="CHARACTER", operation="EXCLUDE",
            resolved_canonical_fact_key="profile.occupation", temporal_scope="PRESENT",
        ) for source in predictions],
    )
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="FIXED", evaluation_domains={"CHARACTER"},
        scenarios=[scenario],
    )
    return gold, bundle


def _proof(bundle):
    payload = bundle.scenarios[0].model_dump(mode="json", by_alias=True)
    prediction_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {"scenarios": {"S1": {
        "sourceSha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "scenarioPredictionSha256": prediction_hash,
        "chunks": [
            {"chunkIndex": 0, "startOffset": 0, "endOffset": BOUNDARY},
            {"chunkIndex": 1, "startOffset": BOUNDARY, "endOffset": len(SOURCE)},
        ],
        "repeatGroups": [{"candidateIds": ["P1", "P2"], "continuityVerified": True}],
    }}}


def _evaluate(gold, bundle, proof=None):
    return asyncio.run(evaluate_multi_stage(
        gold, bundle, character_repeat_provenance=proof,
    ))


def test_reviewed_cross_chunk_fact_changes_quality_unit_without_hiding_candidate_processing():
    gold, bundle = _fixture()
    original = asyncio.run(evaluate_multi_stage(gold, bundle))
    payload_before = bundle.model_dump_json()

    report = _evaluate(gold, bundle, _proof(bundle))

    stage = report["stages"]["character"]["stage1"]
    assert stage["scoringUnit"] == "REVIEWED_FACT"
    assert stage["metrics"]["candidateF1"] == 1
    assert stage["counts"]["predictions"] == 1
    assert stage["counts"]["extra"] == 0
    assert stage["counts"]["rawPredictions"] == 2
    assert stage["counts"]["handoffPredictions"] == 2
    assert stage["counts"]["repeatedCandidateProcessing"] == 1
    assert stage["candidateProcessing"]["metrics"]["candidateF1"] == pytest.approx(2 / 3)
    assert stage["candidateProcessing"]["counts"]["extra"] == 1
    assert report["stages"]["macroAverage"]["stage1CandidateF1"] == 1
    assert report["stages"]["character"]["stage2"] == original["stages"]["character"]["stage2"]
    assert report["endToEnd"] == original["endToEnd"]
    assert bundle.model_dump_json() == payload_before
    assert report["scenarios"][0]["predictedAfterStateHash"] == (
        original["scenarios"][0]["predictedAfterStateHash"]
    )
    cases = report["scenarios"][0]["stage1"]["CHARACTER"]["cases"]
    assert {row["predictionId"] for row in cases} == {"P1", "P2"}
    repeat = next(row for row in cases if row["predictionId"] == "P2")
    assert repeat["repeatOfPredictionId"] == "P1"
    assert repeat["factGoldIds"] == ["G1"]
    assert repeat["result"] == "EXTRA"
    assert {row["sourceCandidateId"] for row in report["scenarios"][0]["stage2"]} == {"P1", "P2"}
    assert {row["candidateId"] for row in report["scenarios"][0]["processing"]} == {"P1", "P2"}
    public = build_public_diagnostics(report)
    assert next(row for row in public[0]["stage1"]["character"]["cases"]
                if row["predictionId"] == "P2")["repeatOfPredictionId"] == "P1"
    assert build_source_free_summary(report)["stages"]["character"]["stage1"][
        "candidateProcessing"
    ]["counts"]["predictions"] == 2
    assert "검수된 사실" in render_markdown_summary(report)


def test_no_provenance_keeps_original_candidate_denominator_even_with_exclude():
    gold, bundle = _fixture()
    report = _evaluate(gold, bundle)
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2
    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("change, reason", [
    ({"display_value": "mage", "value_json": {"value": "mage"}}, "FACT_PAYLOAD_MISMATCH"),
    ({"entity_ref": "character:other"}, "FACT_PAYLOAD_MISMATCH"),
    ({"fact_key": "profile.gender"}, "FACT_PAYLOAD_MISMATCH"),
    ({"value_type": "JSON"}, "FACT_PAYLOAD_MISMATCH"),
    ({"fact_type": "STATUS", "fact_key": "status.ranger"}, "NOT_RESOLVED_PROFILE"),
    ({"match_status": "AMBIGUOUS"}, "NOT_RESOLVED_PROFILE"),
    ({"entity_ref": None}, "NOT_RESOLVED_PROFILE"),
    ({"sort_order": 2}, "UNVERIFIED_CHUNK_EVIDENCE"),
    ({"evidence_spans": [{"quote": "She remains a ranger."}]}, "UNVERIFIED_CHUNK_EVIDENCE"),
])
def test_review_does_not_waive_payload_identity_or_provenance_errors(change, reason):
    gold, bundle = _fixture()
    bundle.scenarios[0].stage1[1] = CharacterStage1Prediction.model_validate({
        **bundle.scenarios[0].stage1[1].model_dump(), **change,
    })
    report = _evaluate(gold, bundle, _proof(bundle))
    stage = report["stages"]["character"]["stage1"]
    assert stage["counts"]["predictions"] == 2
    group = report["scenarios"][0]["stage1"]["CHARACTER"]["reviewedRepeatGroups"][0]
    assert group["status"] == "NOT_GROUPED"
    assert group["reason"] == reason


@pytest.mark.parametrize("scope", ["PAST", "HYPOTHETICAL", "UNKNOWN"])
def test_nonpresent_temporal_scope_never_becomes_harmless_repeat(scope):
    gold, bundle = _fixture()
    bundle.scenarios[0].stage2[1] = CharacterStage2Prediction.model_validate({
        **bundle.scenarios[0].stage2[1].model_dump(),
        "temporal_scope": scope, "operation": "REVIEW_REQUIRED",
    })
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["reviewedRepeatGroups"][0][
        "reason"
    ] == "TEMPORAL_SCOPE_NOT_PRESENT"


@pytest.mark.parametrize("context", [
    [], ["PAST"], ["CURRENT", "UNKNOWN"], ["CURRENT", "RUMOR"],
    ["CURRENT", "DREAM"], ["CURRENT", "NEGATED"],
])
def test_review_requires_known_current_gold_context(context):
    gold, bundle = _fixture()
    gold.stage1[0].context_tags = context
    gold = gold.with_fixture_hash()
    bundle.fixture_hash = gold.fixture_hash
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2


def test_multiple_gold_facts_for_same_slot_keep_each_independent_matching_unit():
    gold, bundle = _fixture()
    gold.stage1.append(gold.stage1[0].model_copy(update={"gold_id": "G2", "sort_order": 2}))
    gold.stage2.append(gold.stage2[0].model_copy(update={
        "decision_id": "D2", "source_gold_ids": ["G2"], "sort_order": 2,
    }))
    gold = gold.with_fixture_hash()
    bundle.fixture_hash = gold.fixture_hash
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["reviewedRepeatGroups"][0][
        "reason"
    ] == "GOLD_FACT_NOT_UNIQUE_CURRENT"


def test_unmatched_reviewed_group_remains_false_positive_despite_exclude():
    gold, bundle = _fixture()
    gold.stage1 = []
    gold.stage2 = []
    gold = gold.with_fixture_hash()
    bundle.fixture_hash = gold.fixture_hash
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2
    assert report["stages"]["character"]["stage1"]["counts"]["extra"] == 2


def test_intervening_change_then_same_value_is_a_new_fact_even_when_group_order_is_reversed():
    gold, bundle = _fixture()
    changed = bundle.scenarios[0].stage1[0].model_copy(update={
        "candidate_id": "P-change", "sort_order": 2,
        "display_value": "mage", "value_json": {"value": "mage"},
    })
    bundle.scenarios[0].stage1.insert(1, changed)
    proof = _proof(bundle)
    proof["scenarios"]["S1"]["repeatGroups"][0]["candidateIds"] = ["P2", "P1"]
    report = _evaluate(gold, bundle, proof)
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 3
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["reviewedRepeatGroups"][0][
        "reason"
    ] == "INTERVENING_SLOT_CHANGE_OR_UNKNOWN"


def test_intervening_spelling_variant_of_same_profile_slot_still_blocks_repeat_grouping():
    gold, bundle = _fixture()
    gold.stage1[0].fact_key = "profile.가족관계"
    gold = gold.with_fixture_hash()
    bundle.fixture_hash = gold.fixture_hash
    for source in bundle.scenarios[0].stage1:
        source.fact_key = "profile.가족관계"
    for decision in bundle.scenarios[0].stage2:
        decision.resolved_canonical_fact_key = "profile.가족관계"
    changed = bundle.scenarios[0].stage1[0].model_copy(update={
        "candidate_id": "P-change", "sort_order": 2, "fact_key": "profile.가족_관계",
        "display_value": "mage", "value_json": {"value": "mage"},
    })
    bundle.scenarios[0].stage1.insert(1, changed)
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 3
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["reviewedRepeatGroups"][0][
        "reason"
    ] == "INTERVENING_SLOT_CHANGE_OR_UNKNOWN"


@pytest.mark.parametrize("field", ["sourceSha256", "scenarioPredictionSha256"])
def test_provenance_is_pinned_to_exact_source_and_prediction_payload(field):
    gold, bundle = _fixture()
    proof = _proof(bundle)
    proof["scenarios"]["S1"][field] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        _evaluate(gold, bundle, proof)


def test_provenance_rejects_overlapping_chunks_or_unreviewed_continuity():
    gold, bundle = _fixture()
    proof = _proof(bundle)
    proof["scenarios"]["S1"]["chunks"][1]["startOffset"] = BOUNDARY - 1
    with pytest.raises(ValueError, match="overlap"):
        _evaluate(gold, bundle, proof)
    proof = _proof(bundle)
    proof["scenarios"]["S1"]["repeatGroups"][0]["continuityVerified"] = False
    with pytest.raises(ValueError):
        _evaluate(gold, bundle, proof)


def test_missing_repeat_stage2_decision_is_preserved_and_not_used_as_temporal_proof():
    gold, bundle = _fixture()
    bundle.scenarios[0].stage2.pop()
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2
    repeat = next(row for row in report["scenarios"][0]["stage2"]
                  if row["sourceCandidateId"] == "P2")
    assert repeat["result"] == "EXTRA_NO_DECISION"
    assert repeat["processing"]["status"] == "RECORD_UNAVAILABLE"


def test_same_chunk_duplicates_are_not_collapsed_even_with_matching_exact_evidence():
    gold, bundle = _fixture()
    first = bundle.scenarios[0].stage1[0]
    bundle.scenarios[0].stage1[1] = first.model_copy(update={
        "candidate_id": "P2", "sort_order": 2,
    })
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 2
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["reviewedRepeatGroups"][0][
        "reason"
    ] == "SAME_CHUNK_DUPLICATE"


def test_grouped_candidate_keeps_its_incorrect_mutation_and_after_state_effect():
    gold, bundle = _fixture()
    bundle.scenarios[0].stage2[1] = CharacterStage2Prediction(
        source_candidate_id="P2", domain="CHARACTER", operation="ADD",
        resolved_canonical_fact_key="profile.occupation", temporal_scope="PRESENT",
        proposed_value="mage", proposed_value_json={"value": "mage"},
    )
    original = asyncio.run(evaluate_multi_stage(gold, bundle))
    report = _evaluate(gold, bundle, _proof(bundle))
    assert report["stages"]["character"]["stage1"]["counts"]["predictions"] == 1
    repeat = next(row for row in report["scenarios"][0]["stage2"]
                  if row["sourceCandidateId"] == "P2")
    assert repeat["actual"]["operation"] == "ADD"
    assert repeat["repeatOfPredictionId"] == "P1"
    assert report["endToEnd"] == original["endToEnd"]
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 0


def test_score_cli_reads_reviewed_provenance_without_modifying_saved_predictions(tmp_path, monkeypatch):
    gold, bundle = _fixture()
    gold_path = tmp_path / "gold.json"
    prediction_path = tmp_path / "predictions.json"
    proof_path = tmp_path / "repeats.json"
    output_path = tmp_path / "score.json"
    gold_path.write_text(gold.model_dump_json(by_alias=True), encoding="utf-8")
    prediction_path.write_text(bundle.model_dump_json(by_alias=True), encoding="utf-8")
    proof_path.write_text(json.dumps(_proof(bundle)), encoding="utf-8")
    (tmp_path / "01화.txt").write_text(SOURCE, encoding="utf-8")
    before = prediction_path.read_bytes()
    monkeypatch.setattr("sys.argv", [
        "setting-score", "--gold", str(gold_path), "--predictions", str(prediction_path),
        "--source-root", str(tmp_path), "--character-repeat-provenance", str(proof_path),
        "--semantic-judge", "none", "--output", str(output_path), "--quiet",
    ])

    cli.main()

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == 1
    assert prediction_path.read_bytes() == before


def test_repeat_report_labels_quality_exemption_separately_from_raw_candidate_diagnostics():
    gold, bundle = _fixture()
    report = _evaluate(gold, bundle, _proof(bundle))

    markdown = render_markdown_summary(report)

    repeat_rows = [line for line in markdown.splitlines()
                   if line.startswith("|") and "P2" in line]
    assert len(repeat_rows) == 2
    assert all("검수된 반복(품질 감점 제외)" in line for line in repeat_rows)
    assert all("불필요하게 추출한 설정" not in line for line in repeat_rows)
    assert "후보 처리 진단 기준 F1" in markdown
    assert "**1차 후보 처리 진단**" in markdown
    assert "**2차 처리 판단 결과(후보 처리 진단)**" in markdown
    assert "후보 처리 진단의 원시 실패 원인" in markdown
    assert "사실 품질 오류 수와 별도" in markdown
    assert next(row for row in report["scenarios"][0]["stage1"]["CHARACTER"]["cases"]
                if row["predictionId"] == "P2")["result"] == "EXTRA"
    assert next(row for row in report["scenarios"][0]["stage2"]
                if row["sourceCandidateId"] == "P2")["result"] == "EXTRA_PROCESSED"
