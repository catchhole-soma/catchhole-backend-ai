import asyncio
from types import SimpleNamespace

import pytest

from evals.multi_stage_setting.contracts import (
    EvaluationDomain,
    EvaluationState,
    FailureCause,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStage2Prediction,
    WorldStateEntry,
    world_state_ref,
)
from evals.multi_stage_setting.evaluator import (
    _apply_semantic_results,
    _evaluate_stage2_cases,
    _score_stage2_case,
    _stage2_semantic_cases,
    evaluate_multi_stage,
)
from evals.multi_stage_setting.state_effects import build_gold_state_chain

VALUE = "게임 진행에 NPC 동료가 필수다."
NAME = "동료 요구 조건"
ALIAS = "NPC 동료 필요성"


def _source(**changes) -> WorldStage1Gold:
    return WorldStage1Gold(
        **{
            "gold_id": "W1",
            "scenario_id": "S1",
            "episode_no": 1,
            "sort_order": 1,
            "decision": "EXTRACT",
            "importance": "MUST",
            "review_status": "FINAL",
            "evidence_quotes": ["동료 없이 진행할 수 없다."],
            "domain": "WORLD",
            "candidate_kind": "WORLD_SETTING",
            "category": "WORLD_RULE_HISTORY",
            "subject_name": "던전 앤 스톤",
            "setting_name": NAME,
            "source_values": [VALUE],
            **changes,
        }
    )


def _gold(**changes) -> WorldStage2Gold:
    return WorldStage2Gold(
        **{
            "decision_id": "D1",
            "scenario_id": "S1",
            "episode_no": 1,
            "sort_order": 1,
            "source_gold_ids": ["W1"],
            "domain": "WORLD",
            "operation": "ADD",
            "consolidation_status": "SINGLE",
            "proposed_setting_name": NAME,
            "proposed_value": VALUE,
            "review_status": "FINAL",
            **changes,
        }
    )


def _prediction(**changes) -> WorldStage2Prediction:
    return WorldStage2Prediction(
        **{
            "source_candidate_id": "W1",
            "domain": "WORLD",
            "operation": "ADD",
            "consolidation_status": "SINGLE",
            "proposed_setting_name": NAME,
            "proposed_value": VALUE,
            **changes,
        }
    )


def _semantic(*, same_setting=True, matched=True):
    return SimpleNamespace(same_setting=same_setting, matched=matched)


@pytest.mark.parametrize("actual_name,method", [(NAME, "EXACT"), (ALIAS, "ALIAS")])
def test_stage2_accepts_exact_name_and_reviewed_source_alias_without_semantic_call(
    actual_name: str, method: str
) -> None:
    source = _source(accepted_setting_name_aliases=[ALIAS])
    case = _score_stage2_case(
        _gold(), _prediction(proposed_setting_name=actual_name), expected_world_source=source
    )

    assert case.full_decision_matched is True
    assert case.setting_name_match == {"status": "MATCH", "method": method}
    assert _stage2_semantic_cases(case, [source]) == []


@pytest.mark.parametrize("gold_scope,gold_name", [(None, "입장 조건"), ("진행", NAME)])
def test_stage2_does_not_inherit_source_alias_after_canonical_path_change(
    gold_scope: str | None, gold_name: str
) -> None:
    source = _source(accepted_setting_name_aliases=[ALIAS])
    case = _score_stage2_case(
        _gold(proposed_scope_name=gold_scope, proposed_setting_name=gold_name),
        _prediction(proposed_scope_name=gold_scope, proposed_setting_name=ALIAS),
        expected_world_source=source,
    )

    assert case.proposed_path_matched is None
    assert case.setting_name_match == {"status": "PENDING", "method": "UNRESOLVED"}
    assert _stage2_semantic_cases(case, [source])[0].setting_context.expected_setting_name == (
        gold_name
    )
    _apply_semantic_results(
        [case], [], {case.semantic_case_id: _semantic(same_setting=False)}
    )
    assert case.proposed_path_matched is False


def test_stage2_pending_name_gets_full_context_even_when_value_is_exact() -> None:
    source = _source()
    case = _score_stage2_case(
        _gold(required_facts=["NPC 동료가 필요하다."], forbidden_facts=["혼자 진행 가능"]),
        _prediction(proposed_setting_name="특징"),
        expected_world_source=source,
    )
    semantic_cases = _stage2_semantic_cases(case, [source])

    assert case.full_decision_matched is None
    assert case.value_matched is True
    assert len(semantic_cases) == 1
    semantic = semantic_cases[0]
    assert semantic.case_id == case.proposed_setting_semantic_case_id
    assert semantic.setting_context.category == "WORLD_RULE_HISTORY"
    assert semantic.setting_context.subject_name == "던전 앤 스톤"
    assert semantic.setting_context.scope_name is None
    assert semantic.setting_context.expected_setting_name == NAME
    assert semantic.setting_context.actual_setting_name == "특징"
    assert semantic.source_values == (VALUE,)
    assert semantic.evidence_quotes == ("동료 없이 진행할 수 없다.",)
    assert semantic.required_facts == ("NPC 동료가 필요하다.",)
    assert semantic.forbidden_facts == ("혼자 진행 가능",)
    _apply_semantic_results([case], [], {})
    assert case.full_decision_matched is None


def test_stage2_name_match_preserves_deterministic_value_and_clears_failure() -> None:
    case = _score_stage2_case(
        _gold(), _prediction(proposed_setting_name=ALIAS), expected_world_source=_source()
    )
    case.failure_cause = FailureCause.COMPARISON_ERROR

    _apply_semantic_results([case], [], {case.semantic_case_id: _semantic(matched=False)})

    assert case.value_matched is True
    assert case.proposed_path_matched is True
    assert case.full_decision_matched is True
    assert case.failure_cause is None
    assert case.setting_name_match == {"status": "MATCH", "method": "SEMANTIC"}


@pytest.mark.parametrize("same_setting,value_matched", [(False, True), (True, False)])
def test_stage2_name_and_value_mismatches_are_independent(
    same_setting: bool, value_matched: bool
) -> None:
    case = _score_stage2_case(
        _gold(),
        _prediction(proposed_setting_name=ALIAS, proposed_value="NPC 동료는 필요하지 않다."),
        expected_world_source=_source(),
    )

    _apply_semantic_results(
        [case],
        [],
        {case.semantic_case_id: _semantic(same_setting=same_setting, matched=value_matched)},
    )

    assert case.proposed_path_matched is same_setting
    assert case.value_matched is value_matched
    assert case.full_decision_matched is False
    assert case.failure_cause == FailureCause.COMPARISON_ERROR


def test_stage2_scope_difference_keeps_alias_and_requires_scope_semantics() -> None:
    source = _source(accepted_setting_name_aliases=[ALIAS])
    case = _score_stage2_case(
        _gold(),
        _prediction(proposed_scope_name="입장", proposed_setting_name=ALIAS),
        expected_world_source=source,
    )

    assert case.proposed_path_matched is None
    assert case.setting_name_match == {"status": "MATCH", "method": "ALIAS"}
    assert case.proposed_setting_semantic_case_id is None
    assert case.proposed_scope_semantic_case_id is not None
    assert _stage2_semantic_cases(case, [source])[0].setting_context.actual_scope_name == "입장"
    _apply_semantic_results([case], [], {"unexpected": _semantic()})
    assert case.full_decision_matched is None


def _update_gold(**changes) -> WorldStage2Gold:
    return _gold(
        operation="UPDATE",
        target_ref="gold:world:existing",
        matched_property_name=NAME,
        **changes,
    )


def _update_prediction(**changes) -> WorldStage2Prediction:
    return _prediction(
        **{
            "operation": "UPDATE",
            "target_ref": "gold:world:existing",
            "matched_property_name": ALIAS,
            "proposed_setting_name": ALIAS,
            **changes,
        }
    )


def test_stage2_matched_property_and_proposed_name_have_separate_semantic_decisions() -> None:
    source = _source()
    case = _score_stage2_case(
        _update_gold(), _update_prediction(), expected_world_source=source
    )
    semantic_cases = _stage2_semantic_cases(case, [source])

    assert len(semantic_cases) == 2
    assert len({item.case_id for item in semantic_cases}) == 2
    assert case.target_matched is None
    assert case.proposed_path_matched is None
    _apply_semantic_results(
        [case],
        [],
        {
            case.proposed_setting_semantic_case_id: _semantic(),
            case.matched_property_semantic_case_id: _semantic(same_setting=False),
        },
    )
    assert case.proposed_path_matched is True
    assert case.target_matched is False
    assert case.failure_cause == FailureCause.RETRIEVAL_MISS
    assert case.full_decision_matched is False


def test_stage2_matched_property_accepts_the_same_reviewed_alias() -> None:
    source = _source(accepted_setting_name_aliases=[ALIAS])
    case = _score_stage2_case(
        _update_gold(), _update_prediction(), expected_world_source=source
    )

    assert case.target_matched is True
    assert case.matched_property_name_match == {"status": "MATCH", "method": "ALIAS"}
    assert case.proposed_path_matched is True
    assert case.full_decision_matched is True
    assert _stage2_semantic_cases(case, [source]) == []


@pytest.mark.parametrize("operation", ["UPDATE", "MERGE"])
@pytest.mark.parametrize("method", ["ALIAS", "SEMANTIC"])
def test_stage2_name_equivalence_cannot_hide_an_update_or_merge_path_change(
    operation: str, method: str
) -> None:
    source = _source(accepted_setting_name_aliases=[ALIAS] if method == "ALIAS" else [])
    case = _score_stage2_case(
        _gold(operation=operation, target_ref="gold:world:existing", matched_property_name=NAME),
        _update_prediction(operation=operation, matched_property_name=NAME),
        expected_world_source=source,
    )

    assert case.world_path_preserved_matched is False
    assert case.full_decision_matched is False
    _apply_semantic_results(
        [case], [], {case.semantic_case_id: _semantic()} if case.semantic_case_id else {}
    )

    assert case.setting_name_match == {"status": "MATCH", "method": method}
    assert case.matched_property_name_match == {"status": "MATCH", "method": "EXACT"}
    assert case.proposed_path_matched is True
    assert case.target_matched is True
    assert case.world_path_preserved_matched is False
    assert case.full_decision_matched is False
    assert case.failure_cause == FailureCause.COMPARISON_ERROR


def test_stage2_path_preservation_allows_existing_name_normalization() -> None:
    case = _score_stage2_case(
        _update_gold(),
        _update_prediction(matched_property_name=NAME, proposed_setting_name=f" {NAME} "),
        expected_world_source=_source(),
    )

    assert case.world_path_preserved_matched is True
    assert case.full_decision_matched is True


def test_stage2_add_does_not_require_an_existing_matched_path() -> None:
    case = _score_stage2_case(_gold(), _prediction(), expected_world_source=_source())

    assert case.world_path_preserved_matched is None
    assert case.full_decision_matched is True


@pytest.mark.parametrize(
    "changes", [{"target_ref": "gold:world:other"}, {"matched_scope_name": "다른 범위"}]
)
def test_stage2_matched_name_cannot_relax_target_reference_or_scope(changes: dict) -> None:
    source = _source()
    case = _score_stage2_case(
        _update_gold(), _update_prediction(**changes), expected_world_source=source
    )

    assert case.target_matched is False
    assert case.matched_property_semantic_case_id is None
    assert len(_stage2_semantic_cases(case, [source])) == 1
    _apply_semantic_results([case], [], {case.semantic_case_id: _semantic()})
    assert case.proposed_path_matched is True
    assert case.target_matched is False
    assert case.full_decision_matched is False
    assert case.failure_cause == FailureCause.RETRIEVAL_MISS


@pytest.mark.parametrize("same_setting", [None, "true", 1])
def test_stage2_missing_or_nonboolean_setting_decision_stays_pending(same_setting) -> None:
    case = _score_stage2_case(
        _gold(), _prediction(proposed_setting_name=ALIAS), expected_world_source=_source()
    )

    _apply_semantic_results(
        [case], [], {case.semantic_case_id: _semantic(same_setting=same_setting)}
    )

    assert case.setting_name_match == {"status": "PENDING", "method": "UNRESOLVED"}
    assert case.proposed_path_matched is None
    assert case.full_decision_matched is None


def _oracle_cases(
    sources: list[WorldStage1Gold],
    actual_name: str = ALIAS,
    state_errors: list[dict[str, str]] | None = None,
):
    snapshot = GoldSnapshotV3(
        dataset_version="v3",
        name="test",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01.txt",
                target_domains={"WORLD"},
                gold_version="v3",
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
        stage1=sources,
        stage2=[_gold(source_gold_ids=[source.gold_id for source in sources])],
    ).with_fixture_hash()
    scenario_prediction = ScenarioPrediction(
        scenario_id="S1", stage2=[_prediction(proposed_setting_name=actual_name)]
    )
    predictions = PredictionBundleV3(
        fixture_hash=snapshot.fixture_hash,
        mode="ORACLE",
        analysis_model="test",
        comparison_model="test",
        scenarios=[scenario_prediction],
    )
    semantic_cases = []
    gold_chain = build_gold_state_chain(snapshot)

    cases = _evaluate_stage2_cases(
        snapshot,
        predictions,
        {},
        {"S1": scenario_prediction},
        {"S1"},
        semantic_cases,
        {EvaluationDomain.WORLD},
        state_errors or [],
        gold_chain,
        gold_chain,  # ORACLE uses the same Gold state before the decision.
    )

    return cases, semantic_cases


def test_stage2_oracle_builder_queues_name_context() -> None:
    cases, semantic_cases = _oracle_cases([_source()])

    assert len(cases) == 1
    assert cases[0].proposed_path_matched is None
    assert semantic_cases[0].setting_context.actual_setting_name == ALIAS


@pytest.mark.parametrize("actual_name", ["동료 필수 조건", ALIAS])
def test_stage2_collects_aliases_from_all_sources_on_the_proposed_canonical_path(
    actual_name: str,
) -> None:
    sources = [
        _source(setting_name="입장 조건", accepted_setting_name_aliases=["입장 요건"]),
        _source(gold_id="W2", sort_order=2, accepted_setting_name_aliases=["동료 필수 조건"]),
        _source(gold_id="W3", sort_order=3, accepted_setting_name_aliases=[ALIAS]),
    ]

    cases, semantic_cases = _oracle_cases(sources, actual_name)

    assert cases[0].setting_name_match == {"status": "MATCH", "method": "ALIAS"}
    assert cases[0].full_decision_matched is True
    assert semantic_cases == []
    assert sources[1].accepted_setting_name_aliases == ["동료 필수 조건"]


def test_stage2_does_not_collect_aliases_from_a_different_source_property() -> None:
    sources = [
        _source(setting_name="입장 조건", accepted_setting_name_aliases=["입장 요건"]),
        _source(gold_id="W2", sort_order=2, accepted_setting_name_aliases=[ALIAS]),
    ]

    cases, semantic_cases = _oracle_cases(sources, "입장 요건")

    assert cases[0].setting_name_match == {"status": "PENDING", "method": "UNRESOLVED"}
    assert cases[0].proposed_path_matched is None
    assert semantic_cases[0].setting_context.expected_setting_name == NAME


@pytest.mark.parametrize(
    "scenario_id,candidate_id,matched",
    [("S1", "W1", False), ("S2", "W1", True), ("S1", "W2", True)],
)
def test_stage2_application_axis_only_uses_errors_for_its_own_candidate(
    scenario_id: str, candidate_id: str, matched: bool
) -> None:
    cases, _ = _oracle_cases(
        [_source(accepted_setting_name_aliases=[ALIAS])],
        state_errors=[{"scenarioId": scenario_id, "sourceCandidateId": candidate_id}],
    )

    assert cases[0].world_application_matched is matched
    assert cases[0].full_decision_matched is matched
    _apply_semantic_results(cases, [], {})
    assert cases[0].full_decision_matched is matched


def test_stage2_aliases_cannot_hide_an_existing_target_application_failure() -> None:
    source = _source(accepted_setting_name_aliases=[ALIAS])
    target_ref = world_state_ref(source.category, source.subject_name, None, NAME)
    before_value = "동료 없이 진행할 수 있다."
    snapshot = GoldSnapshotV3(
        dataset_version="v3",
        name="world-application-regression",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01.txt",
                target_domains={"WORLD"},
                gold_version="v3",
                start_state_mode="SEED",
                cumulative_through_episode=0,
                review_status="FINAL",
                seed_state=EvaluationState(
                    world_facts=[
                        WorldStateEntry(
                            ref=target_ref,
                            category=source.category,
                            subject_name=source.subject_name,
                            setting_name=NAME,
                            value=before_value,
                        )
                    ]
                ),
            )
        ],
        stage1=[source],
        stage2=[
            _gold(
                operation="UPDATE",
                target_ref=target_ref,
                matched_property_name=NAME,
                before_value=before_value,
            )
        ],
    ).with_fixture_hash()
    predictions = PredictionBundleV3(
        fixture_hash=snapshot.fixture_hash,
        mode="ORACLE",
        analysis_model="test",
        comparison_model="test",
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1", stage2=[_update_prediction(target_ref=target_ref)]
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(snapshot, predictions))

    case = report["scenarios"][0]["stage2"][0]
    assert case["settingNameMatch"] == {"status": "MATCH", "method": "ALIAS"}
    assert case["matchedPropertyNameMatch"] == {"status": "MATCH", "method": "ALIAS"}
    assert case["fields"]["pathPreservation"] == "MATCH"
    assert case["fields"]["stateApplication"] == "MISMATCH"
    assert case["result"] == "DECISION_MISMATCH"
    assert report["stages"]["world"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 1
