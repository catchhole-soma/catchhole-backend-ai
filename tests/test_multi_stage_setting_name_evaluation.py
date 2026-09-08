import asyncio

import pytest

from evals.multi_stage_setting.contracts import (
    EvaluationState,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStage2Prediction,
    WorldStateEntry,
    world_state_ref,
    world_subject_ref,
)
from evals.multi_stage_setting.evaluator import (
    Stage2Case,
    _world_scoring_ref_maps,
    evaluate_multi_stage,
)
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeBatchResult,
    SemanticOutcomeDecision,
)
from evals.multi_stage_setting.state_effects import ScenarioStateTransition

VALUE = "체력이 가득해도 판단 실수로 사망할 수 있다."
OPPOSITE_VALUE = "체력이 가득하면 판단 실수를 해도 사망하지 않는다."
BEFORE_VALUE = "전투에서는 체력만 중요하다."
CANONICAL_NAME = "전투 위험성"
ALTERNATE_NAME = "전투 규칙"


@pytest.mark.parametrize("name", [CANONICAL_NAME, ALTERNATE_NAME])
def test_reviewed_world_names_need_no_name_judge_in_either_stage(name: str) -> None:
    gold, bundle = _fixture(extraction_name=name, proposed_name=name, aliases=[ALTERNATE_NAME])
    judge = _Judge()

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    _assert_full_world_result(report)
    assert not any(case.setting_context is not None for case in judge.cases)


@pytest.mark.parametrize(
    ("expected_name", "actual_name", "value"),
    [
        (CANONICAL_NAME, ALTERNATE_NAME, VALUE),
        ("동료 요구 조건", "진행 조건", "게임 진행에는 NPC 동료가 필수다."),
    ],
)
def test_contextual_names_match_both_stages_and_state_without_rewriting_data(
    expected_name: str,
    actual_name: str,
    value: str,
) -> None:
    gold, bundle = _fixture(
        expected_name=expected_name,
        extraction_name=actual_name,
        proposed_name=actual_name,
        expected_value=value,
        prediction_value=value,
    )
    gold_before = gold.model_dump(mode="json")
    prediction_before = bundle.model_dump(mode="json")
    judge = _Judge()

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    _assert_full_world_result(report)
    name_cases = [case for case in judge.cases if case.setting_context is not None]
    assert all(case.scenario_id == "S1" for case in judge.cases)
    assert len(name_cases) == 2
    assert all(case.setting_context.expected_setting_name == expected_name for case in name_cases)
    assert all(case.setting_context.actual_setting_name == actual_name for case in name_cases)
    assert gold.model_dump(mode="json") == gold_before
    assert bundle.model_dump(mode="json") == prediction_before
    state_row = report["endToEnd"]["scenarios"][0]
    assert state_row["expectedStateHash"] != state_row["predictedStateHash"]


def test_same_setting_judgment_does_not_make_opposite_values_correct() -> None:
    gold, bundle = _fixture(prediction_value=OPPOSITE_VALUE)
    judge = _Judge()

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    stage1 = report["stages"]["world"]["stage1"]
    stage2 = report["stages"]["world"]["stage2"]
    case = report["scenarios"][0]["stage1"]["WORLD"]["cases"][0]
    assert stage1["counts"]["identityTruePositive"] == 1
    assert stage1["metrics"]["valueAccuracy"] == 0
    assert case["fields"]["path"] == "MATCH"
    assert case["fields"]["value"] == "MISMATCH"
    assert stage2["counts"]["upstreamReached"] == 0
    assert stage2["counts"]["upstreamOutcomes"] == {"UPSTREAM_VALUE_ERROR": 1}
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 0


@pytest.mark.parametrize("reviewed_alias", [False, True])
@pytest.mark.parametrize("correct_final_value", [False, True])
def test_partial_extraction_still_judges_applied_path_and_value_independently(
    reviewed_alias: bool, correct_final_value: bool,
) -> None:
    second_value = "HP만으로 전투 결과를 결정하지 않는다."
    final_value = VALUE + " " + second_value
    gold, bundle = _fixture(
        expected_value=final_value,
        prediction_value=final_value if correct_final_value else OPPOSITE_VALUE,
        aliases=[ALTERNATE_NAME] if reviewed_alias else [],
    )
    gold.stage1[0].source_values = [VALUE, second_value]
    gold.stage2[0] = WorldStage2Gold.model_validate(
        gold.stage2[0].model_dump() | {"consolidation_status": "MERGED"}
    )
    gold = gold.with_fixture_hash()
    bundle.fixture_hash = gold.fixture_hash
    bundle.scenarios[0].stage1[0].source_values = [VALUE]
    original = bundle.model_dump(mode="json")

    class PartialExtractionJudge(_Judge):
        async def judge_many(self, cases):
            result = await super().judge_many(cases)
            return SemanticOutcomeBatchResult(
                decisions=tuple(
                    decision.model_copy(update={
                        "core_meaning_covered": False,
                        "required_facts_covered": False,
                    }) if decision.case_id.startswith("stage1") else decision
                    for decision in result.decisions
                ),
            )

    judge = PartialExtractionJudge()
    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    stage2 = report["stages"]["world"]["stage2"]
    assert stage2["counts"]["upstreamOutcomes"] == {"UPSTREAM_VALUE_ERROR": 1}
    assert stage2["metrics"]["fullDecisionAccuracy"] is None
    assert report["failureCauses"].get("EXTRACTION_MISS") == 1
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == int(correct_final_value)
    case = report["scenarios"][0]["stage2"][0]
    assert case["result"] == "UPSTREAM_BLOCKED"
    assert case["sourceCandidateId"] == "P1"
    assert case["actual"]["operation"] == "ADD"
    assert case["settingNameMatch"]["status"] == "MATCH"
    if not correct_final_value:
        assert any(
            case.case_id.startswith("state:")
            and case.expected_value == final_value and case.actual_value == OPPOSITE_VALUE
            for case in judge.cases
        )
    assert bundle.model_dump(mode="json") == original


def test_equal_values_do_not_override_judged_different_setting_names() -> None:
    gold, bundle = _fixture()

    report = asyncio.run(
        evaluate_multi_stage(gold, bundle, semantic_judge=_Judge(same_setting=False))
    )

    stage1 = report["stages"]["world"]["stage1"]
    case = report["scenarios"][0]["stage1"]["WORLD"]["cases"][0]
    assert stage1["counts"]["identityTruePositive"] == 0
    assert stage1["metrics"]["valueAccuracy"] == 1
    assert case["settingNameMatch"] == {"status": "MISMATCH", "method": "SEMANTIC"}
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 0


@pytest.mark.parametrize(
    "context_change",
    [{"category": "POWER_SYSTEM"}, {"subject_name": "다른 게임"}],
)
def test_name_judge_cannot_rescue_wrong_stage1_category_or_subject(
    context_change: dict[str, str],
) -> None:
    gold, bundle = _fixture()
    bundle.scenarios[0].stage1[0] = WorldStage1Prediction.model_validate(
        bundle.scenarios[0].stage1[0].model_dump() | context_change
    )
    judge = _Judge()

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    assert report["stages"]["world"]["stage1"]["counts"]["identityTruePositive"] == 0
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 0
    assert not any(case.case_id.startswith("stage1-name:") for case in judge.cases)


@pytest.mark.parametrize("scope_equivalent, accuracy", [(None, None), (False, 0)])
def test_name_approval_does_not_override_unknown_or_rejected_final_scope(
    scope_equivalent: bool | None, accuracy: int | None,
) -> None:
    gold, bundle = _fixture(extraction_name=CANONICAL_NAME)
    bundle.scenarios[0].stage2[0] = (
        bundle.scenarios[0].stage2[0].model_copy(update={"proposed_scope_name": "게임 규칙"})
    )
    judge = _Judge(scope_equivalent=scope_equivalent)

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    stage2 = report["stages"]["world"]["stage2"]
    assert report["stages"]["world"]["stage1"]["counts"]["identityTruePositive"] == 1
    assert stage2["metrics"]["fullDecisionAccuracy"] == accuracy
    assert stage2["metrics"]["proposedPathAccuracy"] == accuracy
    contextual = [case for case in judge.cases if case.setting_context is not None]
    assert len(contextual) == 1
    assert contextual[0].setting_context.scope_name is None
    assert contextual[0].setting_context.actual_scope_name == "게임 규칙"
    case = report["scenarios"][0]["stage2"][0]
    assert case["settingNameMatch"] == {"status": "MATCH", "method": "SEMANTIC"}
    assert case["fields"]["proposedPath"] == (
        "PENDING" if scope_equivalent is None else "MISMATCH"
    )


def test_missing_name_judge_stays_pending_without_granting_a_true_positive() -> None:
    gold, bundle = _fixture()

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    stage1 = report["stages"]["world"]["stage1"]
    case = report["scenarios"][0]["stage1"]["WORLD"]["cases"][0]
    assert stage1["counts"]["identityTruePositive"] == 0
    assert stage1["metrics"]["candidateF1"] is None
    assert case["settingNameMatch"] == {"status": "PENDING", "method": "UNRESOLVED"}
    assert report["stages"]["world"]["stage2"]["counts"]["upstreamReached"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] is None
    assert report["scenarios"][0]["stage2"][0]["result"] == "SEMANTIC_PENDING"
    assert report["failureCauses"].get("EXTRACTION_MISS", 0) == 0


def test_synonymous_duplicate_predictions_are_still_counted_separately() -> None:
    gold, bundle = _fixture()
    second_prediction = (
        bundle.scenarios[0]
        .stage1[0]
        .model_copy(update={"candidate_id": "P2", "setting_name": "전투 판정", "sort_order": 2})
    )
    second_decision = (
        bundle.scenarios[0]
        .stage2[0]
        .model_copy(
            update={
                "source_candidate_id": "P2",
                "proposed_setting_name": "전투 판정",
                "target_ref": world_subject_ref("WORLD_RULE_HISTORY", "던전 앤 스톤"),
            }
        )
    )
    bundle.scenarios[0].stage1.append(second_prediction)
    bundle.scenarios[0].stage2.append(second_decision)

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=_Judge()))

    stage1 = report["stages"]["world"]["stage1"]
    assert stage1["counts"]["predictions"] == 2
    assert stage1["counts"]["identityTruePositive"] == 1
    assert stage1["counts"]["extra"] == 1
    assert stage1["metrics"]["candidatePrecision"] == 0.5
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 2
    assert report["endToEnd"]["domains"]["WORLD"]["afterStatePrecision"] == 0.5


def test_oracle_world_name_semantics_align_stage2_and_state() -> None:
    gold, bundle = _fixture(extraction_name=CANONICAL_NAME)
    bundle = bundle.model_copy(update={"mode": "ORACLE"})
    bundle.scenarios[0].stage1 = []
    bundle.scenarios[0].stage2[0] = (
        bundle.scenarios[0].stage2[0].model_copy(update={"source_candidate_id": "W1"})
    )
    judge = _Judge()

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    assert report["stages"]["world"]["stage1"]["evaluated"] is False
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert not any(case.case_id.startswith("stage1-name:") for case in judge.cases)


@pytest.mark.parametrize("reviewed_alias", [False, True])
def test_update_must_preserve_stored_path_even_when_names_are_equivalent(
    reviewed_alias: bool,
) -> None:
    gold, bundle = _fixture(
        extraction_name=CANONICAL_NAME,
        aliases=[ALTERNATE_NAME] if reviewed_alias else [],
        operation="UPDATE",
    )
    gold_before = gold.model_dump(mode="json")
    prediction_before = bundle.model_dump(mode="json")

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=_Judge()))

    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 0
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 0
    assert report["endToEnd"]["counts"]["expectedTransitions"] == 1
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 0
    assert gold.model_dump(mode="json") == gold_before
    assert bundle.model_dump(mode="json") == prediction_before
    state_row = report["endToEnd"]["scenarios"][0]
    assert state_row["expectedStateHash"] != state_row["predictedStateHash"]


@pytest.mark.parametrize("operation", ["ADD", "UPDATE"])
def test_name_judge_does_not_change_raw_state_hash_or_application_errors(operation: str) -> None:
    gold, bundle = _fixture(operation=operation)
    reports = [
        asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))
        for judge in (None, _Judge())
    ]

    without, with_judge = reports
    assert (
        without["endToEnd"]["scenarios"][0]["predictedStateHash"]
        == (with_judge["endToEnd"]["scenarios"][0]["predictedStateHash"])
    )
    assert (
        without["endToEnd"]["counts"]["stateApplicationErrors"]
        == (with_judge["endToEnd"]["counts"]["stateApplicationErrors"])
    )


def test_later_grouped_gold_alias_applies_to_both_stages_and_state() -> None:
    gold, bundle = _fixture()
    second = gold.stage1[0].model_copy(
        update={"gold_id": "W2", "sort_order": 2, "accepted_setting_name_aliases": [ALTERNATE_NAME]}
    )
    gold.stage1.append(second)
    gold.stage2[0] = gold.stage2[0].model_copy(update={"source_gold_ids": ["W1", "W2"]})
    gold = gold.with_fixture_hash()
    bundle = bundle.model_copy(update={"fixture_hash": gold.fixture_hash})
    judge = _Judge()

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    _assert_full_world_result(report)
    assert not any(case.setting_context is not None for case in judge.cases)
    case = report["scenarios"][0]["stage1"]["WORLD"]["cases"][0]
    assert case["goldIds"] == ["W1", "W2"]
    assert case["settingNameMatch"] == {"status": "MATCH", "method": "ALIAS"}


def test_normalized_subject_display_keeps_semantic_name_scoring_and_raw_state_separate() -> None:
    gold, bundle = _fixture()
    gold.stage1[0] = gold.stage1[0].model_copy(update={"subject_name": "Dungeon"})
    gold = gold.with_fixture_hash()
    bundle = bundle.model_copy(update={"fixture_hash": gold.fixture_hash})
    bundle.scenarios[0].stage1[0] = bundle.scenarios[0].stage1[0].model_copy(
        update={"subject_name": " dungeon "}
    )

    without = asyncio.run(evaluate_multi_stage(gold, bundle))
    with_judge = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=_Judge()))

    _assert_full_world_result(with_judge)
    assert without["endToEnd"]["scenarios"][0]["predictedStateHash"] == (
        with_judge["endToEnd"]["scenarios"][0]["predictedStateHash"]
    )
    assert without["endToEnd"]["counts"]["stateApplicationErrors"] == 0


def test_rolling_selected_child_preserves_prior_approved_name_without_scoring_parent() -> None:
    gold, bundle = _fixture(aliases=[ALTERNATE_NAME])
    gold.scenarios.append(
        ScenarioGold(
            scenario_id="S2",
            episode_no=2,
            source_identifier="02화.txt",
            source_text="다음 회차",
            target_domains={"WORLD"},
            gold_version="v3",
            start_state_mode="PREVIOUS_GOLD",
            previous_scenario_id="S1",
            cumulative_through_episode=1,
            candidate_free=True,
            review_status="FINAL",
        )
    )
    gold = gold.with_fixture_hash()
    bundle = PredictionBundleV3.model_validate(
        bundle.model_dump()
        | {
            "fixture_hash": gold.fixture_hash,
            "mode": "ROLLING",
            "state_application_policy": None,
            "evaluation_scenario_ids": ["S2"],
        }
    )
    bundle.scenarios.append(ScenarioPrediction(scenario_id="S2"))

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=_Judge()))

    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1
    assert report["stages"]["world"]["stage2"]["counts"]["gold"] == 0
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert [row["scenarioId"] for row in report["endToEnd"]["scenarios"]] == ["S2"]


def test_name_approval_does_not_cross_distinct_stable_subject_ids_with_same_display_name() -> None:
    gold, bundle = _fixture()

    def entry(subject_id: str, setting: str) -> WorldStateEntry:
        return WorldStateEntry(
            ref=world_state_ref(
                "WORLD_RULE_HISTORY", "던전 앤 스톤", None, setting, subject_ref=subject_id
            ),
            subject_ref=subject_id,
            category="WORLD_RULE_HISTORY",
            subject_name="던전 앤 스톤",
            setting_name=setting,
            value=VALUE,
        )

    expected_entries = [entry(subject_id, CANONICAL_NAME) for subject_id in ("A", "B")]
    actual_entries = [entry(subject_id, ALTERNATE_NAME) for subject_id in ("A", "B")]

    def transition(entries) -> ScenarioStateTransition:
        return ScenarioStateTransition(
            scenario_id="S1",
            before_state=EvaluationState(),
            after_state=EvaluationState(world_facts=entries),
            applied_decision_ids=("D1",),
            held_decision_ids=(),
        )

    target_ref = world_subject_ref("WORLD_RULE_HISTORY", "던전 앤 스톤", subject_ref="A")
    case = Stage2Case(
        scenario_id="S1",
        gold=gold.stage2[0].model_copy(update={"target_ref": target_ref}),
        prediction=bundle.scenarios[0].stage2[0].model_copy(update={"target_ref": target_ref}),
        upstream_outcome="REACHED",
        failure_cause=None,
        target_matched=True,
        proposed_path_matched=True,
    )

    mapping = _world_scoring_ref_maps(
        gold,
        [case],
        {"S1": transition(expected_entries)},
        {"S1": transition(actual_entries)},
        {"S1"},
    )["S1"].after

    assert mapping == {f"fact:{actual_entries[0].ref}": f"fact:{expected_entries[0].ref}"}


def _assert_full_world_result(report) -> None:
    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage1"]["metrics"]["valueAccuracy"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0


class _Judge:
    def __init__(
        self, *, same_setting: bool | None = True, scope_equivalent: bool | None = True,
    ) -> None:
        self.same_setting = same_setting
        self.scope_equivalent = scope_equivalent
        self.cases = []

    async def judge_many(self, cases):
        self.cases.extend(cases)
        return SemanticOutcomeBatchResult(
            decisions=tuple(
                SemanticOutcomeDecision(
                    caseId=case.case_id,
                    coreMeaningCovered=case.actual_value not in {OPPOSITE_VALUE, BEFORE_VALUE},
                    requiredFactsCovered=case.actual_value not in {OPPOSITE_VALUE, BEFORE_VALUE},
                    forbiddenFactsAbsent=True,
                    contradiction=case.actual_value in {OPPOSITE_VALUE, BEFORE_VALUE},
                    unsupportedDetail=False,
                    reason="meaning checked independently",
                    sameSetting=self.same_setting if case.setting_context else None,
                    settingReason="same property checked in context"
                    if case.setting_context
                    else None,
                    scopeEquivalent=self.scope_equivalent if case.setting_context else None,
                    scopeReason="scope checked independently" if case.setting_context else None,
                )
                for case in cases
            )
        )


def _fixture(
    *,
    expected_name: str = CANONICAL_NAME,
    extraction_name: str = ALTERNATE_NAME,
    proposed_name: str = ALTERNATE_NAME,
    expected_value: str = VALUE,
    prediction_value: str = VALUE,
    aliases: list[str] | None = None,
    operation: str = "ADD",
) -> tuple[GoldSnapshotV3, PredictionBundleV3]:
    target_ref = world_state_ref("WORLD_RULE_HISTORY", "던전 앤 스톤", None, expected_name)
    before_value = BEFORE_VALUE
    seed_state = None
    if operation == "UPDATE":
        seed_state = EvaluationState(
            world_facts=[
                WorldStateEntry(
                    ref=target_ref,
                    category="WORLD_RULE_HISTORY",
                    subject_name="던전 앤 스톤",
                    setting_name=expected_name,
                    value=before_value,
                )
            ]
        )
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text=expected_value,
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="SEED" if operation == "UPDATE" else "EMPTY",
        seed_state=seed_state,
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    source = WorldStage1Gold(
        gold_id="W1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=[expected_value],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="WORLD_RULE_HISTORY",
        subject_name="던전 앤 스톤",
        setting_name=expected_name,
        accepted_setting_name_aliases=aliases or [],
        source_values=[expected_value],
    )
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation=operation,
        consolidation_status="SINGLE",
        proposed_setting_name=expected_name,
        proposed_value=expected_value,
        review_status="FINAL",
        target_ref=target_ref if operation == "UPDATE" else None,
        matched_property_name=expected_name if operation == "UPDATE" else None,
        before_value=before_value if operation == "UPDATE" else None,
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="issue-62-world-name-evaluation",
        scenarios=[scenario],
        stage1=[source],
        stage2=[decision],
    ).with_fixture_hash()
    prediction = WorldStage1Prediction(
        candidate_id="P1",
        domain="WORLD",
        category="WORLD_RULE_HISTORY",
        subject_name="던전 앤 스톤",
        setting_name=extraction_name,
        source_values=[prediction_value],
        evidence_spans=[{"quote": expected_value}],
    )
    prediction_decision = WorldStage2Prediction(
        source_candidate_id="P1",
        domain="WORLD",
        operation=operation,
        consolidation_status="SINGLE",
        proposed_setting_name=proposed_name,
        proposed_value=prediction_value,
        target_ref=target_ref if operation == "UPDATE" else None,
        matched_property_name=expected_name if operation == "UPDATE" else None,
    )
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[prediction],
                stage2=[prediction_decision],
            )
        ],
    )
    return gold, bundle
