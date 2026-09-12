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
    _pending_refs,
    _world_extraction_paths,
    evaluate_multi_stage,
)
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeBatchResult,
    SemanticOutcomeDecision,
)
from evals.multi_stage_setting.world_name_state import world_setting_ref_mapping
from tests.test_multi_stage_setting_name_evaluation import _fixture as _single_fixture

CATEGORY = "WORLD_RULE_HISTORY"
SUBJECT = "던전 앤 스톤"
SCOPE = "게임 규칙"
SUBJECT_REF = world_subject_ref(CATEGORY, SUBJECT)
ROWS = (
    (
        "GB-EP01-W-002",
        "GB-EP01-D-003",
        "캐릭터 사망 규칙",
        "사망 규칙",
        "캐릭터가 사망하면 처음부터 다시 육성해야 한다.",
        "캐릭터가 사망하면 처음부터 다시 키워야 한다.",
    ),
    (
        "GB-EP01-W-003",
        "GB-EP01-D-004",
        "동료 요구 조건",
        "진행 조건",
        "게임 진행에 NPC 동료가 필수다.",
        "게임 진행에는 NPC 동료가 필수다.",
    ),
    (
        "GB-EP01-W-006",
        "GB-EP01-D-007",
        "전투 위험성",
        "전투 난이도 규칙",
        "전투는 HP/MP만으로 결정되지 않으며 체력이 가득해도 판단 실수로 사망할 수 있다.",
        "HP/MP가 전투의 전부는 아니며 체력이 가득해도 판단 실수 한 번으로 캐릭터를 잃는다.",
    ),
)
NAME_FAMILIES = [{row[2], row[3]} for row in ROWS]
NAME_FAMILIES[0].add("사망 시 재시작")


def test_related_extraction_context_excludes_other_episodes() -> None:
    gold, bundle = _fixture()
    source = gold.stage1[0]
    future = source.model_copy(
        update={"scenario_id": "future", "source_values": ["미래 회차의 규칙"]}
    )
    context = _world_extraction_paths(source, [*gold.stage1, future], bundle.scenarios[0].stage1)
    assert not any("미래 회차의 규칙" in row["values"] for row in context)
    assert any(row["values"] == source.source_values for row in context)


@pytest.mark.parametrize("mode,stage1_scope", [("FIXED", None), ("FIXED", SCOPE), ("ORACLE", None)])
def test_episode_one_grouping_can_match_all_three_items_without_rewriting_raw_state(
    mode: str,
    stage1_scope: str | None,
) -> None:
    gold, bundle = _fixture(mode=mode, stage1_scope=stage1_scope)
    gold_before = gold.model_dump(mode="json")
    prediction_before = bundle.model_dump(mode="json")
    baseline = _evaluate(gold, bundle)
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    if mode == "FIXED":
        assert report["stages"]["world"]["stage1"]["counts"]["identityTruePositive"] == 3
        assert report["stages"]["world"]["stage1"]["metrics"]["valueAccuracy"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert all(
        case["fields"]["proposedPath"] == "MATCH" for case in report["scenarios"][0]["stage2"]
    )
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    contexts = [case.setting_context for case in judge.cases if case.setting_context]
    scoped = [context for context in contexts if context.actual_scope_name == SCOPE]
    assert {context.expected_setting_name for context in scoped} == {row[2] for row in ROWS}
    assert all(context.scope_name is None for context in scoped)
    assert gold.model_dump(mode="json") == gold_before
    assert bundle.model_dump(mode="json") == prediction_before
    _assert_raw_state_unchanged(baseline, report)
    state = report["endToEnd"]["scenarios"][0]
    assert state["expectedStateHash"] != state["predictedStateHash"]


@pytest.mark.parametrize("axis", ["item", "scope", "value"])
def test_each_semantic_axis_can_reject_an_otherwise_correct_stage_two(axis: str) -> None:
    gold, bundle = _fixture(mode="ORACLE")
    report = _evaluate(gold, bundle, _Judge(**{axis: False}))

    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0
    for case in report["scenarios"][0]["stage2"]:
        assert case["fields"]["proposedPath"] == ("MATCH" if axis == "value" else "MISMATCH")
        assert case["fields"]["value"] == ("MISMATCH" if axis == "value" else "MATCH")
        assert case["settingNameMatch"]["status"] == ("MISMATCH" if axis == "item" else "MATCH")
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] < 1
    assert report["endToEnd"]["metrics"]["transitionF1"] < 1


@pytest.mark.parametrize(
    "pending_axis,rejected_axis",
    [
        (pending, rejected)
        for pending in ("item", "scope", "value")
        for rejected in ("item", "scope", "value")
        if pending != rejected
    ],
)
def test_known_semantic_failure_is_not_hidden_by_another_unknown_axis(
    pending_axis: str,
    rejected_axis: str,
) -> None:
    gold, bundle = _fixture(mode="ORACLE")
    report = _evaluate(gold, bundle, _Judge(**{pending_axis: None, rejected_axis: False}))

    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0
    world = report["endToEnd"]["domains"]["WORLD"]
    assert world["afterStateF1"] == 0.25  # Only the unchanged seed fact is correct.
    assert world["resolvedAfterStateF1"] == 0.25
    assert world["semanticPending"] == 0
    assert report["endToEnd"]["metrics"]["transitionF1"] == 0


@pytest.mark.parametrize("axis", ["item", "scope", "value"])
@pytest.mark.parametrize("mode", ["FIXED", "ORACLE"])
def test_unknown_semantics_stay_pending_through_stage_and_end_to_end(
    axis: str,
    mode: str,
) -> None:
    gold, bundle = _fixture(mode=mode, stage1_scope=SCOPE)
    report = _evaluate(gold, bundle, _Judge(**{axis: None}))

    if mode == "FIXED":
        stage1 = report["scenarios"][0]["stage1"]["WORLD"]["cases"]
        field = "value" if axis == "value" else "path"
        assert all(case["fields"][field] == "PENDING" for case in stage1)
        assert all(case["upstreamOutcome"] != "UPSTREAM_VALUE_ERROR" for case in stage1)
    else:
        stage2 = report["scenarios"][0]["stage2"]
        field = "value" if axis == "value" else "proposedPath"
        assert all(case["fields"][field] == "PENDING" for case in stage2)
        assert all(case["result"] == "SEMANTIC_PENDING" for case in stage2)
        assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] is None
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] is None
    assert report["endToEnd"]["metrics"]["transitionF1"] is None
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0


@pytest.mark.parametrize("axis", ["item", "scope", "value"])
def test_stage_one_does_not_conflate_item_scope_and_value(axis: str) -> None:
    gold, bundle = _fixture(stage1_scope=SCOPE)
    report = _evaluate(gold, bundle, _Judge(**{axis: False}))

    for case in report["scenarios"][0]["stage1"]["WORLD"]["cases"]:
        assert case["fields"]["path"] == ("MATCH" if axis == "value" else "MISMATCH")
        assert case["fields"]["value"] == ("MISMATCH" if axis == "value" else "MATCH")
        assert case["settingNameMatch"]["status"] == ("MISMATCH" if axis == "item" else "MATCH")


@pytest.mark.parametrize("reviewed", ["EXACT", "ALIAS"])
def test_reviewed_names_and_equal_scopes_need_no_identity_judge(reviewed: str) -> None:
    gold, bundle = _fixture(proposed_scope=None, reviewed=reviewed, equal_values=True)
    judge = _Judge(item=False, scope=False, value=False)
    report = _evaluate(gold, bundle, judge)

    assert not any(
        case.setting_context is not None
        and any(
            case.setting_context.expected_setting_name in family
            and case.setting_context.actual_setting_name in family
            for family in NAME_FAMILIES
        )
        for case in judge.cases
    )
    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1


@pytest.mark.parametrize("reviewed", ["EXACT", "ALIAS"])
def test_reviewed_item_names_still_require_scope_judgment(reviewed: str) -> None:
    gold, bundle = _fixture(stage1_scope=SCOPE, reviewed=reviewed, equal_values=True)
    judge = _Judge(item=False)
    report = _evaluate(gold, bundle, judge)

    assert any(case.setting_context for case in judge.cases)
    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1
    for case in report["scenarios"][0]["stage2"]:
        assert case["settingNameMatch"] == {"status": "MATCH", "method": reviewed}
    rejected = _evaluate(gold, bundle, _Judge(scope=False))
    assert rejected["stages"]["world"]["stage1"]["counts"]["identityTruePositive"] == 0


@pytest.mark.parametrize(
    "changed_context", [{"category": "POWER_SYSTEM"}, {"subject_name": "다른 게임"}]
)
def test_scope_semantics_cannot_cross_category_or_subject(changed_context: dict[str, str]) -> None:
    gold, bundle = _fixture(stage1_scope=SCOPE)
    bundle.scenarios[0].stage1 = [
        WorldStage1Prediction.model_validate(row.model_dump() | changed_context)
        for row in bundle.scenarios[0].stage1
    ]
    judge = _Judge()
    report = _evaluate(gold, bundle, judge)

    assert report["stages"]["world"]["stage1"]["counts"]["identityTruePositive"] == 0
    assert not any(case.case_id.startswith("stage1-name:") for case in judge.cases)


def test_semantic_scope_mapping_keeps_duplicate_predictions_one_to_one() -> None:
    gold, bundle = _fixture(stage1_scope=SCOPE)
    extra = (
        bundle.scenarios[0]
        .stage1[0]
        .model_copy(
            update={"candidate_id": "P-extra", "setting_name": "사망 시 재시작", "sort_order": 4}
        )
    )
    decision = (
        bundle.scenarios[0]
        .stage2[0]
        .model_copy(
            update={"source_candidate_id": "P-extra", "proposed_setting_name": "사망 시 재시작"}
        )
    )
    bundle.scenarios[0].stage1.append(extra)
    bundle.scenarios[0].stage2.append(decision)

    report = _evaluate(gold, bundle, _Judge())

    stage1 = report["stages"]["world"]["stage1"]
    assert stage1["counts"]["identityTruePositive"] == 3
    assert stage1["counts"]["extra"] == 1
    assert stage1["metrics"]["candidatePrecision"] == 0.75
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 4
    assert report["endToEnd"]["domains"]["WORLD"]["afterStatePrecision"] < 1


def test_pending_scope_edge_cannot_take_an_exact_ref_or_hide_an_extra_fact() -> None:
    expected = "fact:" + world_state_ref(CATEGORY, SUBJECT, None, ROWS[0][2])
    alternate = "fact:" + world_state_ref(CATEGORY, SUBJECT, SCOPE, ROWS[0][3])
    pending_pairs = [(expected, alternate)]
    mapping = world_setting_ref_mapping({expected}, {expected, alternate}, pending_pairs)

    assert mapping == {expected: expected}
    assert _pending_refs(pending_pairs, {expected}, {expected, alternate}, mapping) == frozenset()


def test_rolling_exact_add_resolves_current_identity_without_inventing_transitions() -> None:
    gold, bundle = _fixture()
    gold.scenarios.append(
        ScenarioGold(
            scenario_id="S2",
            episode_no=2,
            source_identifier="02화.txt",
            source_text="새 설정 없음",
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
    exact = (
        bundle.scenarios[0]
        .stage1[0]
        .model_copy(
            update={
                "candidate_id": "P-exact",
                "scope_name": None,
                "setting_name": ROWS[0][2],
                "source_values": [ROWS[0][4]],
            }
        )
    )
    added = (
        bundle.scenarios[0]
        .stage2[0]
        .model_copy(
            update={
                "source_candidate_id": "P-exact",
                "proposed_scope_name": None,
                "proposed_setting_name": ROWS[0][2],
                "proposed_value": ROWS[0][4],
            }
        )
    )
    bundle = PredictionBundleV3.model_validate(
        bundle.model_dump()
        | {
            "fixture_hash": gold.fixture_hash,
            "mode": "ROLLING",
            "state_application_policy": None,
            "evaluation_scenario_ids": ["S2"],
        }
    )
    bundle.scenarios.append(ScenarioPrediction(scenario_id="S2", stage1=[exact], stage2=[added]))

    report = _evaluate(gold, bundle, _Judge(scope=None))

    world = report["endToEnd"]["domains"]["WORLD"]
    assert world["semanticPending"] == 2
    assert world["resolvedAfterStatePrecision"] == pytest.approx(2 / 3)
    assert world["resolvedAfterStateRecall"] == 1
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert report["endToEnd"]["counts"]["expectedTransitions"] == 0
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 1
    assert report["endToEnd"]["counts"]["matchedTransitions"] == 0


@pytest.mark.parametrize(
    "change,field",
    [
        ({"operation": "EXCLUDE"}, "operation"),
        ({"existing_root_property_names_to_move": ["게임 장르"]}, "rootMoveSet"),
    ],
)
def test_semantic_scope_approval_does_not_override_operation_or_root_moves(
    change: dict,
    field: str,
) -> None:
    gold, bundle = _fixture(mode="ORACLE")
    bundle.scenarios[0].stage2[0] = WorldStage2Prediction.model_validate(
        bundle.scenarios[0].stage2[0].model_dump() | change
    )
    report = _evaluate(gold, bundle, _Judge())

    case = report["scenarios"][0]["stage2"][0]
    assert case["fields"][field] == "MISMATCH"
    assert case["result"] == "DECISION_MISMATCH"
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] < 1


@pytest.mark.parametrize(
    "change,field",
    [
        ({"proposed_scope_name": SCOPE}, "pathPreservation"),
        ({"target_ref": "gold:world:missing-target"}, "target"),
    ],
)
def test_update_scope_judgment_cannot_repair_existing_target_or_raw_path(
    change: dict,
    field: str,
) -> None:
    gold, bundle = _single_fixture(
        expected_name=ROWS[0][2],
        extraction_name=ROWS[0][2],
        proposed_name=ROWS[0][2],
        operation="UPDATE",
    )
    bundle.scenarios[0].stage2[0] = bundle.scenarios[0].stage2[0].model_copy(update=change)
    before = bundle.model_dump(mode="json")
    baseline = _evaluate(gold, bundle)
    report = _evaluate(gold, bundle, _Judge())

    assert report["scenarios"][0]["stage2"][0]["fields"][field] == "MISMATCH"
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 1
    assert bundle.model_dump(mode="json") == before
    _assert_raw_state_unchanged(baseline, report)


@pytest.mark.parametrize("reviewed_alias", [False, True])
def test_update_keeps_item_identity_independent_of_a_forbidden_scope_change(
    reviewed_alias: bool,
) -> None:
    name, alternate = ROWS[0][2:4]
    gold, bundle = _single_fixture(
        expected_name=name,
        extraction_name=name,
        proposed_name=alternate,
        aliases=[alternate] if reviewed_alias else [],
        operation="UPDATE",
    )
    bundle.scenarios[0].stage2[0] = (
        bundle.scenarios[0].stage2[0].model_copy(update={"proposed_scope_name": SCOPE})
    )
    judge = _Judge()

    report = _evaluate(gold, bundle, judge)

    case = report["scenarios"][0]["stage2"][0]
    assert case["settingNameMatch"] == {
        "status": "MATCH",
        "method": "ALIAS" if reviewed_alias else "SEMANTIC",
    }
    assert case["fields"]["proposedPath"] == "MISMATCH"
    assert case["fields"]["pathPreservation"] == "MISMATCH"
    assert case["fields"]["stateApplication"] == "MISMATCH"
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 1


def _evaluate(gold, bundle, judge=None):
    return asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))


def _assert_raw_state_unchanged(before, after) -> None:
    assert (
        before["endToEnd"]["scenarios"][0]["predictedStateHash"]
        == after["endToEnd"]["scenarios"][0]["predictedStateHash"]
    )
    assert (
        before["endToEnd"]["counts"]["stateApplicationErrors"]
        == after["endToEnd"]["counts"]["stateApplicationErrors"]
    )


class _Judge:
    def __init__(self, *, item=True, scope=True, value=True) -> None:
        self.item = item
        self.scope = scope
        self.value = value
        self.cases = []

    async def judge_many(self, cases):
        self.cases.extend(cases)
        decisions = []
        for case in cases:
            context = case.setting_context
            same_family = context is None or any(
                context.expected_setting_name in family and context.actual_setting_name in family
                for family in NAME_FAMILIES
            )
            decisions.append(
                SemanticOutcomeDecision(
                    caseId=case.case_id,
                    coreMeaningCovered=self.value is not False,
                    requiredFactsCovered=self.value is not False,
                    forbiddenFactsAbsent=True,
                    contradiction=self.value is False,
                    unsupportedDetail=False,
                    valueResolved=self.value is not None,
                    reason="Value judged independently of item and scope.",
                    sameSetting=(self.item if same_family else False) if context else None,
                    settingReason="Independent item judgment." if context else None,
                    scopeEquivalent=self.scope if context else None,
                    scopeReason="Independent scope judgment." if context else None,
                )
            )
        return SemanticOutcomeBatchResult(decisions=tuple(decisions))


def _fixture(
    *,
    mode="FIXED",
    stage1_scope=None,
    proposed_scope=SCOPE,
    reviewed=None,
    equal_values=False,
):
    seed = WorldStateEntry(
        ref=world_state_ref(CATEGORY, SUBJECT, None, "게임 장르"),
        category=CATEGORY,
        subject_name=SUBJECT,
        setting_name="게임 장르",
        value="로그라이크 게임이다.",
    )
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="\n".join(row[4] for row in ROWS),
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        seed_state=EvaluationState(world_facts=[seed]),
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    sources, decisions, predictions, proposed = [], [], [], []
    for order, (gold_id, decision_id, name, alternate, value, paraphrase) in enumerate(ROWS, 1):
        actual_name = name if reviewed == "EXACT" else alternate
        actual_value = value if equal_values else paraphrase
        sources.append(
            WorldStage1Gold(
                gold_id=gold_id,
                scenario_id="S1",
                episode_no=1,
                sort_order=order,
                decision="EXTRACT",
                importance="MUST",
                evidence_quotes=[value],
                review_status="FINAL",
                domain="WORLD",
                candidate_kind="WORLD_SETTING",
                category=CATEGORY,
                subject_name=SUBJECT,
                setting_name=name,
                source_values=[value],
                accepted_setting_name_aliases=[alternate] if reviewed == "ALIAS" else [],
            )
        )
        decisions.append(
            WorldStage2Gold(
                decision_id=decision_id,
                scenario_id="S1",
                episode_no=1,
                sort_order=order,
                source_gold_ids=[gold_id],
                domain="WORLD",
                operation="ADD",
                consolidation_status="SINGLE",
                target_ref=SUBJECT_REF,
                proposed_setting_name=name,
                proposed_value=value,
                review_status="FINAL",
            )
        )
        predictions.append(
            WorldStage1Prediction(
                candidate_id=f"P{order}",
                sort_order=order,
                domain="WORLD",
                category=CATEGORY,
                subject_name=SUBJECT,
                scope_name=stage1_scope,
                setting_name=actual_name,
                source_values=[actual_value],
                evidence_spans=[{"quote": value}],
            )
        )
        proposed.append(
            WorldStage2Prediction(
                source_candidate_id=gold_id if mode == "ORACLE" else f"P{order}",
                domain="WORLD",
                operation="ADD",
                consolidation_status="SINGLE",
                target_ref=SUBJECT_REF,
                proposed_scope_name=proposed_scope,
                proposed_setting_name=actual_name,
                proposed_value=actual_value,
            )
        )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="world-scope-semantics",
        scenarios=[scenario],
        stage1=sources,
        stage2=decisions,
    ).with_fixture_hash()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode=mode,
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=predictions if mode == "FIXED" else [],
                stage2=proposed,
            )
        ],
    )
    return gold, bundle
