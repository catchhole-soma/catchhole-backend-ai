import asyncio

import pytest

from evals.multi_stage_setting.contracts import (
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
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeBatchResult,
    SemanticOutcomeDecision,
)

HEIGHT = "평균 키는 180cm다."
WEIGHT = "평균 몸무게는 80kg다."
COMBINED = HEIGHT + " " + WEIGHT


class _Judge:
    def __init__(self, matched=True):
        self.matched = matched
        self.cases = []

    async def judge_many(self, cases):
        self.cases.extend(cases)
        return SemanticOutcomeBatchResult(decisions=tuple(
            SemanticOutcomeDecision(
                caseId=case.case_id,
                valueResolved=self.matched is not None,
                coreMeaningCovered=self.matched is not False,
                requiredFactsCovered=self.matched is not False,
                forbiddenFactsAbsent=True,
                contradiction=self.matched is False,
                unsupportedDetail=False,
                reason="synthetic content independently checked",
            ) for case in cases
        ))


def _fixture(*, gold_values, actual_values, actual_status, gold_status=None, batch=False):
    scenario = ScenarioGold(
        scenario_id="S1", episode_no=1, source_identifier="synthetic.txt",
        source_text=COMBINED, target_domains={"WORLD"}, gold_version="v3",
        start_state_mode="EMPTY", cumulative_through_episode=0, review_status="FINAL",
    )
    source = WorldStage1Gold(
        gold_id="W1", scenario_id="S1", episode_no=1, sort_order=1,
        decision="EXTRACT", importance="MUST", review_status="FINAL",
        evidence_quotes=[COMBINED], domain="WORLD", candidate_kind="WORLD_SETTING",
        category="RACE", subject_name="합성 종족", setting_name="체격", source_values=gold_values,
    )
    decision = WorldStage2Gold(
        decision_id="D1", scenario_id="S1", episode_no=1, sort_order=1,
        source_gold_ids=["W1"], domain="WORLD", operation="ADD",
        consolidation_status=gold_status or ("SINGLE" if len(gold_values) == 1 else "MERGED"),
        proposed_setting_name="체격", proposed_value="\n".join(gold_values)
        if gold_status == "CONFLICT" else COMBINED, review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3", name="synthetic consolidation", scenarios=[scenario],
        stage1=[source], stage2=[decision],
    ).with_fixture_hash()
    groups = [[value] for value in actual_values] if batch else [actual_values]
    sources = [WorldStage1Prediction(
        candidate_id=f"P{index}", domain="WORLD", category="RACE", subject_name="합성 종족",
        setting_name="체격", source_values=values, evidence_spans=[{"quote": COMBINED}],
    ) for index, values in enumerate(groups, 1)]
    proposed = WorldStage2Prediction(
        source_candidate_id="P1", source_candidate_ids=[row.candidate_id for row in sources]
        if batch else [], domain="WORLD", operation="ADD", consolidation_status=actual_status,
        proposed_setting_name="체격", proposed_value=decision.proposed_value,
    )
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash, mode="FIXED", evaluation_domains={"WORLD"},
        scenarios=[ScenarioPrediction(scenario_id="S1", stage1=sources, stage2=[proposed])],
    )
    return gold, bundle


def _evaluate(gold, bundle, judge=None):
    return asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))


@pytest.mark.parametrize(("gold_values", "actual_values", "status"), [
    ([HEIGHT, WEIGHT], [COMBINED], "SINGLE"),
    ([COMBINED], [HEIGHT, WEIGHT], "MERGED"),
])
def test_equivalent_world_content_uses_actual_source_packaging(gold_values, actual_values, status):
    gold, bundle = _fixture(
        gold_values=gold_values, actual_values=actual_values, actual_status=status,
    )
    original = bundle.model_dump(mode="json")
    pending = _evaluate(gold, bundle)
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    assert report["stages"]["world"]["stage2"]["metrics"]["consolidationAccuracy"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1
    case = report["scenarios"][0]["stage2"][0]
    assert case["expected"]["consolidationStatus"] == gold.stage2[0].consolidation_status.value
    assert case["actual"]["consolidationStatus"] == status
    assert case["fields"]["consolidation"] == "MATCH"
    assert pending["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "PENDING"
    assert report["endToEnd"]["scenarios"][0]["predictedStateHash"] == (
        pending["endToEnd"]["scenarios"][0]["predictedStateHash"]
    )
    assert bundle.model_dump(mode="json") == original
    # Existing value judgment is reused; source counting adds no semantic case.
    assert [case.case_id for case in judge.cases] == ["stage1:S1:W1"]


@pytest.mark.parametrize(("actual_values", "status"), [
    ([COMBINED], "MERGED"), ([COMBINED], "CONFLICT"),
    ([HEIGHT, WEIGHT], "SINGLE"), ([HEIGHT, WEIGHT], "CONFLICT"),
])
def test_incorrect_consolidation_is_not_rescued_by_equal_final_value(actual_values, status):
    gold, bundle = _fixture(gold_values=[COMBINED], actual_values=actual_values,
                            actual_status=status)
    report = _evaluate(gold, bundle, _Judge())
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0


def test_multiple_conflicting_inputs_are_not_compatible_merely_because_of_count():
    gold, bundle = _fixture(gold_values=[COMBINED], actual_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_status="MERGED")
    report = _evaluate(gold, bundle, _Judge(matched=False))
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 0


def test_single_input_value_error_does_not_fail_independent_single_consolidation():
    gold, bundle = _fixture(gold_values=[COMBINED], actual_values=["평균 키는 50cm다."],
                            actual_status="SINGLE")
    bundle.scenarios[0].stage2[0] = bundle.scenarios[0].stage2[0].model_copy(update={
        "proposed_value": "평균 키는 50cm다.",
    })
    original = bundle.model_dump(mode="json")
    pending = _evaluate(gold, bundle)

    report = _evaluate(gold, bundle, _Judge(matched=False))

    case = report["scenarios"][0]["stage2"][0]
    assert case["fields"]["consolidation"] == "MATCH"
    assert pending["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MATCH"
    assert case["fields"]["value"] == "MISMATCH"
    assert case["fullDecisionMatched"] is False
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 0
    assert bundle.model_dump(mode="json") == original
    assert report["endToEnd"]["scenarios"][0]["predictedStateHash"] == (
        pending["endToEnd"]["scenarios"][0]["predictedStateHash"]
    )


def test_repackaged_single_input_value_error_still_fails_consolidation_approval():
    gold, bundle = _fixture(gold_values=[HEIGHT, WEIGHT],
                            actual_values=["평균 키는 50cm다."], actual_status="SINGLE")

    report = _evaluate(gold, bundle, _Judge(matched=False))

    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 0


def test_batch_consolidation_uses_all_linked_sources_for_scoring_and_actual_state():
    gold, bundle = _fixture(gold_values=[COMBINED], actual_values=[HEIGHT, WEIGHT],
                            actual_status="MERGED", batch=True)
    report = _evaluate(gold, bundle, _Judge())
    case = report["scenarios"][0]["stage2"][0]
    assert case["fields"]["consolidation"] == "MATCH"
    assert case["fields"]["stateApplication"] == "MATCH"
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0


@pytest.mark.parametrize(("status", "expected"), [("SINGLE", "MISMATCH"), ("MERGED", "MATCH")])
def test_equal_values_from_two_candidates_still_require_merged_batch_status(status, expected):
    gold, bundle = _fixture(gold_values=[COMBINED], actual_values=[COMBINED, COMBINED],
                            actual_status=status, batch=True)
    report = _evaluate(gold, bundle)
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == expected
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == int(status == "SINGLE")


@pytest.mark.parametrize("invalid", ["missing", "unknown", "duplicate", "duplicate_ref", "other_fact"])
def test_batch_source_linkage_must_cover_the_approved_stage1_group(invalid):
    gold, bundle = _fixture(gold_values=[COMBINED], actual_values=[HEIGHT, WEIGHT],
                            actual_status="MERGED", batch=True)
    scenario = bundle.scenarios[0]
    proposed = scenario.stage2[0]
    if invalid == "missing":
        proposed = proposed.model_copy(update={"source_candidate_ids": ["P1"]})
    elif invalid == "unknown":
        proposed = proposed.model_copy(update={"source_candidate_ids": ["P1", "P2", "missing"]})
    elif invalid == "duplicate":
        scenario.stage2.append(proposed.model_copy(update={
            "source_candidate_id": "P2", "source_candidate_ids": ["P2"],
        }))
    elif invalid == "duplicate_ref":
        proposed = proposed.model_copy(update={"source_candidate_ids": ["P1", "P2", "P2"]})
    else:
        scenario.stage1[1] = scenario.stage1[1].model_copy(update={"setting_name": "무관한 설정"})
    scenario.stage2[0] = proposed
    report = _evaluate(gold, bundle, _Judge())
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"


@pytest.mark.parametrize(("actual_values", "status"), [
    ([HEIGHT, "평균 키는 50cm다."], "MERGED"),
    (["평균 키는 180cm 또는 50cm다."], "SINGLE"),
])
def test_gold_conflict_requires_conflict_and_multiple_preserved_alternatives(actual_values, status):
    gold, bundle = _fixture(gold_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_values=actual_values, actual_status=status, gold_status="CONFLICT")
    report = _evaluate(gold, bundle, _Judge())
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"


def test_gold_conflict_dropped_batch_alternative_is_not_hidden_by_correct_proposed_text():
    gold, bundle = _fixture(gold_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_status="CONFLICT", gold_status="CONFLICT", batch=True)
    bundle.scenarios[0].stage2[0] = bundle.scenarios[0].stage2[0].model_copy(update={
        "source_candidate_ids": ["P1"],
    })
    report = _evaluate(gold, bundle, _Judge())
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"


def test_gold_conflict_preserves_all_alternatives_in_held_state():
    gold, bundle = _fixture(gold_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_status="CONFLICT", gold_status="CONFLICT")
    report = _evaluate(gold, bundle)
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MATCH"
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["scenarios"][0]["expectedStateHash"] == (
        report["endToEnd"]["scenarios"][0]["predictedStateHash"]
    )


def test_gold_conflict_missing_content_fails_existing_semantic_preservation_check():
    gold, bundle = _fixture(gold_values=[HEIGHT, "평균 키는 50cm다.", "평균 키는 100cm다."],
                            actual_values=[HEIGHT, "평균 키는 50cm다."],
                            actual_status="CONFLICT", gold_status="CONFLICT")
    report = _evaluate(gold, bundle, _Judge(matched=False))
    assert report["scenarios"][0]["stage2"][0]["fields"]["consolidation"] == "MISMATCH"
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 0
