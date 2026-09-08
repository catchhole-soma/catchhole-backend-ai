import asyncio

import pytest

from app.domain.enums import CharacterFactComparisonOperation
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Gold,
    CharacterStage2Prediction,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    StartStateMode,
    character_state_ref,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.matching import match_stage1
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeBatchResult,
    SemanticOutcomeDecision,
)

ENTITY = "character:hero"
EXPECTED_KEY = "status.right_arm_injury"
ACTUAL_KEY = "status.injured_right_arm"
EXPECTED_TEXT = "오른팔을 다쳤다."
ACTUAL_TEXT = "오른팔에 부상을 입었다."
EXPECTED_DETAIL = "오른팔을 움직이면 아프다."
ACTUAL_DETAIL = "오른팔을 움직일 때 통증이 있다."


class _Judge:
    def __init__(self, *, same_setting: bool | None = True, values: bool | None = True):
        self.same_setting = same_setting
        self.values = values
        self.cases = []

    async def judge_many(self, cases):
        self.cases.extend(cases)
        return SemanticOutcomeBatchResult(
            decisions=tuple(
                SemanticOutcomeDecision(
                    caseId=case.case_id,
                    valueResolved=self.values is not None,
                    coreMeaningCovered=self.values is not False,
                    requiredFactsCovered=True,
                    forbiddenFactsAbsent=True,
                    contradiction=self.values is False,
                    unsupportedDetail=False,
                    reason="value independently checked",
                    sameSetting=self.same_setting if case.character_context else None,
                    settingReason="status identity checked" if case.character_context else None,
                )
                for case in cases
            )
        )


def _fixture(
    *,
    mode="FIXED",
    expected_key=EXPECTED_KEY,
    actual_key=ACTUAL_KEY,
    fact_type="STATUS",
    value_type="STRING",
    expected_value=EXPECTED_TEXT,
    actual_value=ACTUAL_TEXT,
    expected_json=None,
    actual_json=None,
):
    if expected_json is None:
        expected_json = {"value": EXPECTED_DETAIL, "active": True}
    if actual_json is None:
        actual_json = {"value": ACTUAL_DETAIL, "active": True}
    source = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        review_status="FINAL",
        evidence_quotes=[EXPECTED_TEXT],
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref=ENTITY,
        entity_name="주인공",
        fact_type=fact_type,
        fact_key=expected_key,
        value_type=value_type,
        display_value=expected_value,
        value_json=expected_json,
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )
    decision = CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="ADD",
        temporal_scope="PRESENT",
        proposed_value=expected_value,
        proposed_value_json=expected_json,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="character semantic regression",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01화.txt",
                source_text=EXPECTED_TEXT,
                target_domains={"CHARACTER"},
                gold_version="v3",
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
        stage1=[source],
        stage2=[decision],
    ).with_fixture_hash()
    prediction = CharacterStage1Prediction(
        candidate_id="P1",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref=ENTITY,
        entity_name="주인공",
        matched_character_name="주인공",
        match_status="MATCHED",
        fact_type=fact_type,
        fact_key=actual_key,
        value_type=value_type,
        display_value=actual_value,
        value_json=actual_json,
        evidence_spans=[{"quote": EXPECTED_TEXT}],
    )
    proposed = CharacterStage2Prediction(
        source_candidate_id="C1" if mode == "ORACLE" else "P1",
        domain="CHARACTER",
        operation="ADD",
        resolved_canonical_fact_key=actual_key,
        temporal_scope="PRESENT",
        proposed_value=actual_value,
        proposed_value_json=actual_json,
    )
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode=mode,
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[] if mode == "ORACLE" else [prediction],
                stage2=[proposed],
            )
        ],
    )
    return gold, bundle


def _evaluate(gold, bundle, judge):
    return asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))


@pytest.mark.parametrize("mode", ["FIXED", "ORACLE"])
@pytest.mark.parametrize("actual_key", ["profile.가족_관계", "profile.가족 관계"])
def test_family_relation_spelling_matches_all_stages_without_llm_or_raw_key_changes(mode, actual_key):
    gold, bundle = _fixture(
        mode=mode,
        fact_type="PROFILE",
        expected_key="profile.가족관계",
        actual_key=actual_key,
        expected_value="얀델의 아들",
        actual_value="얀델의 아들",
        expected_json={"value": "얀델의 아들"},
        actual_json={"value": "얀델의 아들"},
    )
    before = bundle.model_dump(mode="json")
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    if mode == "FIXED":
        stage1 = report["stages"]["character"]["stage1"]
        assert stage1["metrics"]["candidateF1"] == 1
        assert stage1["metrics"]["valueAccuracy"] == 1
        raw_matching = match_stage1(
            gold.stage1, bundle.scenarios[0].stage1,
            domain="CHARACTER", source_text=gold.scenarios[0].source_text,
            semantic_scoring=False,
        )
        assert raw_matching.matches[0].path_or_fact_matched is False
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert report["endToEnd"]["scenarios"][0]["predictedStateHash"] != (
        report["endToEnd"]["scenarios"][0]["expectedStateHash"]
    )
    assert judge.cases == []
    assert bundle.model_dump(mode="json") == before


def test_matching_key_spelling_does_not_override_wrong_stage_two_value():
    gold, bundle = _fixture(
        fact_type="PROFILE",
        expected_key="profile.가족관계",
        actual_key="profile.가족_관계",
        expected_value="얀델의 아들",
        actual_value="얀델의 아들",
        expected_json={"value": "얀델의 아들"},
        actual_json={"value": "얀델의 아들"},
    )
    bundle.scenarios[0].stage2[0] = bundle.scenarios[0].stage2[0].model_copy(update={
        "proposed_value": "얀델의 형",
        "proposed_value_json": {"value": "얀델의 형"},
    })

    report = _evaluate(gold, bundle, _Judge(values=False))

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["characterCanonicalFactKeyResolutionAccuracy"] == 1
    assert metrics["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] < 1


@pytest.mark.parametrize("mode", ["FIXED", "ORACLE"])
def test_dynamic_status_and_narrative_json_match_across_all_evaluated_stages(mode):
    gold, bundle = _fixture(mode=mode)
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    stage1 = report["stages"]["character"]["stage1"]
    if mode == "FIXED":
        assert stage1["metrics"]["candidateF1"] == 1
        assert stage1["metrics"]["valueAccuracy"] == 1
        assert stage1["metrics"]["structuredValueAccuracy"] == 1
    else:
        assert stage1["evaluated"] is False
    stage2 = report["stages"]["character"]["stage2"]["metrics"]
    assert stage2["fullDecisionAccuracy"] == 1
    assert stage2["proposedValueJsonAccuracy"] == 1
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert any(case.character_context for case in judge.cases)
    assert any(
        case.expected_value == EXPECTED_DETAIL and case.actual_value == ACTUAL_DETAIL
        for case in judge.cases
    )
    assert all(case.setting_context is None for case in judge.cases)
    assert all(case.scenario_id == "S1" for case in judge.cases)


def test_dynamic_stage1_key_with_exact_stage2_key_keeps_history_scoring_aligned():
    gold, bundle = _fixture()
    bundle.scenarios[0].stage2[0] = (
        bundle.scenarios[0]
        .stage2[0]
        .model_copy(update={"resolved_canonical_fact_key": EXPECTED_KEY})
    )
    before = bundle.model_dump(mode="json")
    raw_report = _evaluate(gold, bundle, None)

    report = _evaluate(gold, bundle, _Judge())

    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert bundle.model_dump(mode="json") == before
    # Raw history retains prediction:P1; its correspondence to C1 is scoring-only.
    assert (
        report["endToEnd"]["scenarios"][0]["predictedStateHash"]
        == raw_report["endToEnd"]["scenarios"][0]["predictedStateHash"]
    )


@pytest.mark.parametrize(
    ("value_type", "actual_type", "fact_type", "key", "value"),
    [
        ("NUMBER", None, "STAT", "stat.strength", 3),
        ("NUMBER", "STRING", "STAT", "stat.strength", 3),
        ("BOOLEAN", None, "PROFILE", "profile.is_alive", True),
        ("STRING", None, "STATUS", EXPECTED_KEY, EXPECTED_TEXT),
    ],
)
def test_missing_or_wrong_predicted_type_fails_stage1_and_stored_state(
    value_type, actual_type, fact_type, key, value
):
    display = str(value).lower() if isinstance(value, bool) else str(value)
    gold, bundle = _fixture(
        expected_key=key,
        actual_key=key,
        fact_type=fact_type,
        value_type=value_type,
        expected_value=display,
        actual_value=display,
        expected_json={"value": value},
        actual_json={"value": value},
    )
    bundle.scenarios[0].stage1[0] = (
        bundle.scenarios[0].stage1[0].model_copy(update={"value_type": actual_type})
    )
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    metrics = report["stages"]["character"]["stage1"]["metrics"]
    assert metrics["valueTypeAccuracy"] == 0
    assert metrics["valueAccuracy"] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] < 1
    assert report["endToEnd"]["metrics"]["transitionF1"] < 1
    assert judge.cases == []


def test_fixed_profile_keys_cannot_be_rescued_by_matching_text_or_judge():
    gold, bundle = _fixture(
        expected_key="profile.species",
        actual_key="profile.race",
        fact_type="PROFILE",
        expected_value="바바리안",
        actual_value="바바리안",
        expected_json={"value": "바바리안"},
        actual_json={"value": "바바리안"},
    )
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    assert report["stages"]["character"]["stage1"]["counts"]["identityTruePositive"] == 0
    assert not any(case.character_context for case in judge.cases)
    assert report["stages"]["character"]["stage2"]["counts"]["upstreamReached"] == 0


def test_dynamic_status_does_not_cross_character_identity():
    gold, bundle = _fixture()
    bundle.scenarios[0].stage1[0] = (
        bundle.scenarios[0]
        .stage1[0]
        .model_copy(update={"entity_ref": "character:other", "entity_name": "다른 인물"})
    )
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    assert report["stages"]["character"]["stage1"]["counts"]["identityTruePositive"] == 0
    assert not any(case.character_context for case in judge.cases)


@pytest.mark.parametrize("mode", ["FIXED", "ORACLE"])
def test_unknown_dynamic_status_stays_pending_in_decision_and_state(mode):
    gold, bundle = _fixture(mode=mode)

    report = _evaluate(gold, bundle, _Judge(same_setting=None))

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["fullDecisionAccuracy"] is None
    assert metrics["fullDecisionLowerBoundAccuracy"] == 0
    assert metrics["semanticCoverage"] == 0
    assert report["scenarios"][0]["stage2"][0]["result"] == "SEMANTIC_PENDING"
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] is None
    assert report["endToEnd"]["metrics"]["transitionF1"] is None


@pytest.mark.parametrize(
    ("value_type", "fact_type", "key", "expected", "actual"),
    [
        ("NUMBER", "STAT", "stat.strength", 3, 4),
        ("BOOLEAN", "PROFILE", "profile.is_alive", True, False),
    ],
)
def test_number_and_boolean_value_types_are_not_rescued_by_text_judgment(
    value_type,
    fact_type,
    key,
    expected,
    actual,
):
    gold, bundle = _fixture(
        mode="ORACLE",
        expected_key=key,
        actual_key=key,
        fact_type=fact_type,
        value_type=value_type,
        expected_value=str(expected).lower(),
        actual_value=str(actual).lower(),
        expected_json={"value": expected},
        actual_json={"value": actual},
    )
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["proposedValueAccuracy"] == 0
    assert metrics["proposedValueJsonAccuracy"] == 0
    assert metrics["fullDecisionAccuracy"] == 0
    assert judge.cases == []


def test_unknown_narrative_json_leaf_is_pending_even_when_display_text_is_exact():
    gold, bundle = _fixture(mode="ORACLE", actual_key=EXPECTED_KEY, actual_value=EXPECTED_TEXT)

    report = _evaluate(gold, bundle, _Judge(values=None))

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["proposedValueAccuracy"] == 1
    assert metrics["proposedValueJsonAccuracy"] is None
    assert metrics["fullDecisionAccuracy"] is None
    assert report["scenarios"][0]["stage2"][0]["result"] == "SEMANTIC_PENDING"
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] is None
    assert report["endToEnd"]["metrics"]["transitionF1"] is None


def test_semantic_narrative_mismatch_fails_decision_and_state_without_changing_identity():
    gold, bundle = _fixture(mode="ORACLE", actual_key=EXPECTED_KEY, actual_value=EXPECTED_TEXT)

    report = _evaluate(gold, bundle, _Judge(values=False))

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["proposedValueAccuracy"] == 1
    assert metrics["proposedValueJsonAccuracy"] == 0
    assert metrics["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] < 1
    assert report["endToEnd"]["metrics"]["transitionF1"] < 1


@pytest.mark.parametrize("expected_scalar, actual_scalar", [(3, 4), (True, False), (3, "3")])
def test_native_json_scalars_and_types_remain_strict_despite_positive_judge(
    expected_scalar,
    actual_scalar,
):
    gold, bundle = _fixture(
        mode="ORACLE",
        actual_key=EXPECTED_KEY,
        actual_value=EXPECTED_TEXT,
        expected_json={"value": EXPECTED_DETAIL, "severity": expected_scalar},
        actual_json={"value": ACTUAL_DETAIL, "severity": actual_scalar},
    )

    report = _evaluate(gold, bundle, _Judge())

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["proposedValueJsonAccuracy"] == 0
    assert metrics["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] < 1


@pytest.mark.parametrize("violation", ["target", "removal"])
def test_existing_target_and_removal_references_are_not_semantically_repaired(violation):
    gold, bundle = _fixture(mode="ORACLE", actual_key=EXPECTED_KEY)
    target_ref = character_state_ref(ENTITY, "STATUS", EXPECTED_KEY)
    other_ref = character_state_ref(ENTITY, "STATUS", "status.other_injury")
    entries = [
        CharacterStateEntry(
            ref=ref,
            entity_ref=ENTITY,
            entity_name="주인공",
            fact_type="STATUS",
            fact_key=key,
            value_type="STRING",
            value=EXPECTED_TEXT,
            value_json={"value": EXPECTED_DETAIL},
        )
        for ref, key in [(target_ref, EXPECTED_KEY), (other_ref, "status.other_injury")]
    ]
    gold.scenarios[0] = gold.scenarios[0].model_copy(
        update={
            "start_state_mode": StartStateMode.SEED,
            "seed_state": EvaluationState(character_facts=entries),
        }
    )
    if violation == "target":
        gold.stage2[0] = gold.stage2[0].model_copy(
            update={"operation": CharacterFactComparisonOperation.UPDATE, "target_ref": target_ref}
        )
        bundle.scenarios[0].stage2[0] = (
            bundle.scenarios[0]
            .stage2[0]
            .model_copy(
                update={
                    "operation": CharacterFactComparisonOperation.UPDATE,
                    "target_ref": other_ref,
                }
            )
        )
    else:
        gold.stage2[0] = gold.stage2[0].model_copy(
            update={
                "operation": CharacterFactComparisonOperation.REMOVE,
                "removed_snapshot_refs": [target_ref],
                "proposed_value": None,
                "proposed_value_json": None,
            }
        )
        bundle.scenarios[0].stage2[0] = (
            bundle.scenarios[0]
            .stage2[0]
            .model_copy(
                update={
                    "operation": CharacterFactComparisonOperation.REMOVE,
                    "removed_snapshot_refs": [other_ref],
                    "proposed_value": None,
                    "proposed_value_json": None,
                }
            )
        )
    gold = gold.with_fixture_hash()
    bundle = bundle.model_copy(update={"fixture_hash": gold.fixture_hash})

    report = _evaluate(gold, bundle, _Judge())

    case = report["scenarios"][0]["stage2"][0]
    assert case["fields"]["target" if violation == "target" else "removedSet"] == "MISMATCH"
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0


def test_character_judge_verdict_does_not_rewrite_inputs_or_raw_state_hashes():
    gold, bundle = _fixture()
    gold_before = gold.model_dump(mode="json")
    prediction_before = bundle.model_dump(mode="json")
    reports = [
        _evaluate(gold, bundle, judge)
        for judge in (
            None,
            _Judge(),
            _Judge(same_setting=False, values=False),
            _Judge(same_setting=None, values=None),
        )
    ]

    assert gold.model_dump(mode="json") == gold_before
    assert bundle.model_dump(mode="json") == prediction_before
    assert (
        len({report["endToEnd"]["scenarios"][0]["predictedStateHash"] for report in reports}) == 1
    )
    assert len({report["endToEnd"]["counts"]["stateApplicationErrors"] for report in reports}) == 1
    # Transition counts project only Gold-scorable JSON refs. Semantic identity can
    # change that evaluation projection while the stored prediction/hash stays fixed.
