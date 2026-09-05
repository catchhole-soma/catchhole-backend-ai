import asyncio

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStateEntry,
    CharacterStage2Gold,
    CharacterStage2Prediction,
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
    character_state_ref,
    world_subject_ref,
    world_state_ref,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeBatchResult,
    SemanticOutcomeDecision,
)
from evals.multi_stage_setting.state_effects import build_gold_state_chain


def test_fixed_evaluator_scores_character_and_world_stages_separately() -> None:
    gold = _basic_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        analysis_model="test-extractor",
        comparison_model="test-comparator",
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    CharacterStage1Prediction(
                        candidate_id="pc1",
                        domain="CHARACTER",
                        candidate_kind="SETTING",
                        entity_name="비요른",
                        matched_character_name="비요른",
                        match_status="MATCHED",
                        fact_type="PROFILE",
                        fact_key="profile.species",
                        value_type="STRING",
                        display_value="바바리안",
                        value_json={"value": "바바리안"},
                        evidence_spans=[{"quote": "나는 바바리안이다."}],
                    ),
                    WorldStage1Prediction(
                        candidate_id="pw1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="바바리안",
                        setting_name="전투 특성",
                        source_values=["근접 전투에 강하다."],
                        evidence_spans=[{"quote": "바바리안은 근접 전투에 강하다."}],
                    ),
                ],
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="pc1",
                        domain="CHARACTER",
                        operation="ADD",
                        resolved_canonical_fact_key="profile.species",
                        temporal_scope="PRESENT",
                        proposed_value="바바리안",
                        proposed_value_json={"value": "바바리안"},
                    ),
                    WorldStage2Prediction(
                        source_candidate_id="pw1",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="ADD",
                        proposed_setting_name="전투 특성",
                        proposed_value="근접 전투에 강하다.",
                    ),
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["character"]["stage2"]["metrics"][
        "liveConditionalAccuracy"
    ] == 1
    assert report["stages"]["world"]["stage2"]["metrics"][
        "liveConditionalAccuracy"
    ] == 1
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 1


def test_world_add_does_not_double_penalize_a_projected_subject_ref() -> None:
    gold = _basic_gold()
    projected_subject_ref = world_subject_ref("RACE", "바바리안")
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="pw-extra",
                        sort_order=1,
                        domain="WORLD",
                        category="RACE",
                        subject_name="바바리안",
                        setting_name="직역 의미",
                        source_values=["야만인"],
                        evidence_spans=[{"quote": "나는 바바리안이다."}],
                    ),
                    WorldStage1Prediction(
                        candidate_id="pw-match",
                        sort_order=2,
                        domain="WORLD",
                        category="RACE",
                        subject_name="바바리안",
                        setting_name="전투 특성",
                        source_values=["근접 전투에 강하다."],
                        evidence_spans=[{"quote": "바바리안은 근접 전투에 강하다."}],
                    ),
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="pw-extra",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="ADD",
                        proposed_setting_name="직역 의미",
                        proposed_value="야만인",
                    ),
                    WorldStage2Prediction(
                        source_candidate_id="pw-match",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="ADD",
                        target_ref=projected_subject_ref,
                        proposed_setting_name="전투 특성",
                        proposed_value="근접 전투에 강하다.",
                    ),
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["world"]["stage1"]["counts"]["extra"] == 1
    stage2 = report["stages"]["world"]["stage2"]
    assert stage2["metrics"]["targetAccuracy"] == 1
    assert stage2["metrics"]["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0


def test_upstream_missing_is_excluded_from_conditional_but_fails_end_to_end() -> None:
    gold = _basic_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        scenarios=[ScenarioPrediction(scenario_id="S1")],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    character_stage2 = report["stages"]["character"]["stage2"]
    assert character_stage2["metrics"]["liveConditionalAccuracy"] is None
    assert character_stage2["counts"]["upstreamOutcomes"] == {
        "UPSTREAM_MISSING": 1
    }
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 0


def test_character_remove_scores_the_explicit_snapshot_set_and_reports_it() -> None:
    injury_ref = character_state_ref(
        "character:bjorn", "STATUS", "status.right_foot_injury"
    )
    poison_ref = character_state_ref(
        "character:bjorn", "STATUS", "status.paralysis_poison"
    )
    scenario = ScenarioGold(
        scenario_id="S5",
        episode_no=5,
        source_identifier="05화.txt",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=4,
        seed_state=EvaluationState(
            character_facts=[
                CharacterStateEntry(
                    ref=injury_ref,
                    entity_ref="character:bjorn",
                    entity_name="비요른",
                    fact_type="STATUS",
                    fact_key="status.right_foot_injury",
                    value_type="STRING",
                    value="오른발을 사용할 수 없음",
                    value_json={"value": "오른발을 사용할 수 없음"},
                ),
                CharacterStateEntry(
                    ref=poison_ref,
                    entity_ref="character:bjorn",
                    entity_name="비요른",
                    fact_type="STATUS",
                    fact_key="status.paralysis_poison",
                    value_type="STRING",
                    value="마비독에 중독됨",
                    value_json={"value": "마비독에 중독됨"},
                ),
            ]
        ),
        review_status="FINAL",
    )
    source = CharacterStage1Gold(
        gold_id="C-RECOVERY",
        scenario_id="S5",
        episode_no=5,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["회복 효과로 신체가 빠르게 재생되었다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="STATUS",
        fact_key="status.recovered",
        value_type="STRING",
        display_value="신체가 회복됨",
        value_json={"value": "신체가 회복됨"},
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )
    decision = CharacterStage2Gold(
        decision_id="D-RECOVERY",
        scenario_id="S5",
        episode_no=5,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="REMOVE",
        removed_snapshot_refs=[injury_ref, poison_ref],
        temporal_scope="PRESENT",
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="status removal scoring",
        scenarios=[scenario],
        stage1=[source],
        stage2=[decision],
    ).with_fixture_hash()

    def prediction(removed_refs: list[str]) -> PredictionBundleV3:
        return PredictionBundleV3(
            fixture_hash=gold.fixture_hash,
            mode="ORACLE",
            scenarios=[
                ScenarioPrediction(
                    scenario_id="S5",
                    stage2=[
                        CharacterStage2Prediction(
                            source_candidate_id=source.gold_id,
                            domain="CHARACTER",
                            operation="REMOVE",
                            resolved_canonical_fact_key="status.recovered",
                            removed_snapshot_refs=removed_refs,
                            temporal_scope="PRESENT",
                        )
                    ],
                )
            ],
        )

    matched = asyncio.run(evaluate_multi_stage(gold, prediction([poison_ref, injury_ref])))
    missing = asyncio.run(evaluate_multi_stage(gold, prediction([injury_ref])))

    matched_stage2 = matched["stages"]["character"]["stage2"]["metrics"]
    assert matched_stage2["removedSnapshotSetAccuracy"] == 1
    assert matched_stage2["fullDecisionAccuracy"] == 1
    assert matched["scenarios"][0]["stage2"][0]["removedSnapshotSetMatched"] is True

    missing_stage2 = missing["stages"]["character"]["stage2"]["metrics"]
    assert missing_stage2["removedSnapshotSetAccuracy"] == 0
    assert missing_stage2["fullDecisionAccuracy"] == 0
    assert missing["scenarios"][0]["stage2"][0]["failureCause"] == "COMPARISON_ERROR"


def test_semantic_merge_can_match_paraphrase_without_hiding_operation_score() -> None:
    target_ref = world_state_ref("RACE", "고블린", None, "체격")
    scenario = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=1,
        seed_state=EvaluationState(
            world_facts=[
                WorldStateEntry(
                    ref=target_ref,
                    category="RACE",
                    subject_name="고블린",
                    setting_name="체격",
                    value="평균 키는 140cm다.",
                )
            ]
        ),
        review_status="FINAL",
    )
    source = WorldStage1Gold(
        gold_id="W2",
        scenario_id="S2",
        episode_no=2,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["큰 변종은 190cm도 아주 희귀하게 보인다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name="고블린",
        setting_name="체격",
        source_values=["큰 변종은 190cm도 아주 희귀하게 보인다."],
    )
    decision = WorldStage2Gold(
        decision_id="D2",
        scenario_id="S2",
        episode_no=2,
        sort_order=1,
        source_gold_ids=["W2"],
        domain="WORLD",
        operation="MERGE",
        consolidation_status="SINGLE",
        target_ref=target_ref,
        before_value="평균 키는 140cm다.",
        matched_property_name="체격",
        proposed_setting_name="체격",
        proposed_value="평균은 140cm이며 아주 드문 큰 변종은 190cm다.",
        required_facts=["고블린 | 평균 키 | 140cm", "고블린 큰 변종 | 키 | 190cm"],
        forbidden_facts=["모든 고블린 | 키 | 190cm"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="merge",
        scenarios=[scenario],
        stage1=[source],
        stage2=[decision],
    ).with_fixture_hash()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        scenarios=[
            ScenarioPrediction(
                scenario_id="S2",
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="W2",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="UPDATE",
                        target_ref=target_ref,
                        matched_property_name="체격",
                        proposed_setting_name="체격",
                        proposed_value=(
                            "보통 140cm 정도지만 190cm에 이르는 대형 개체는 매우 드물다."
                        ),
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=_AlwaysMatch()))

    world_stage2 = report["stages"]["world"]["stage2"]["metrics"]
    assert world_stage2["operationAccuracy"] == 0
    assert world_stage2["proposedValueAccuracy"] == 1
    assert world_stage2["fullDecisionAccuracy"] == 0
    # reference reducer의 기계 동작은 UPDATE와 MERGE가 같으므로 상태 결과는 맞는다.
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1


def test_world_conflict_is_held_and_does_not_mutate_snapshot() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="EMPTY",
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
        evidence_quotes=["300m다.", "3km다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="POWER_SYSTEM",
        subject_name="통신석",
        setting_name="반경",
        source_values=["약 300m다.", "약 3km다."],
    )
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="ADD",
        consolidation_status="CONFLICT",
        proposed_setting_name="반경",
        proposed_value="약 300m다.\n약 3km다.",
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="conflict",
        scenarios=[scenario],
        stage1=[source],
        stage2=[decision],
    )

    transition = build_gold_state_chain(gold)["S1"]

    assert transition.after_state.world_facts == []
    assert transition.held_decision_ids == ("D1",)
    assert transition.after_state.held_world_conflicts[0].source_values == [
        "약 300m다.",
        "약 3km다.",
    ]


def test_domain_selection_does_not_score_unselected_domain_as_missing() -> None:
    gold = _basic_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="pw1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="바바리안",
                        setting_name="전투 특성",
                        source_values=["근접 전투에 강하다."],
                        evidence_spans=[{"quote": "바바리안은 근접 전투에 강하다."}],
                    )
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="pw1",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="ADD",
                        proposed_setting_name="전투 특성",
                        proposed_value="근접 전투에 강하다.",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage1"] == {
        "evaluated": False,
        "reason": "Domain not selected.",
    }
    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["failureCauses"].get("EXTRACTION_MISS", 0) == 0


def test_explicitly_ambiguous_character_handoff_is_reported_as_subject_blocked() -> None:
    gold = _basic_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    CharacterStage1Prediction(
                        candidate_id="pc1",
                        domain="CHARACTER",
                        candidate_kind="SETTING",
                        entity_name="비요른",
                        match_status="AMBIGUOUS",
                        fact_type="PROFILE",
                        fact_key="profile.species",
                        value_type="STRING",
                        display_value="바바리안",
                        value_json={"value": "바바리안"},
                        evidence_spans=[{"quote": "나는 바바리안이다."}],
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage2"]["counts"][
        "upstreamOutcomes"
    ] == {"UPSTREAM_BLOCKED_SUBJECT": 1}


def test_failed_stage1_semantic_value_is_removed_from_stage2_conditional_denominator() -> None:
    gold = _basic_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="pw1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="바바리안",
                        setting_name="전투 특성",
                        source_values=["마법만 사용할 수 있다."],
                        evidence_spans=[{"quote": "바바리안은 근접 전투에 강하다."}],
                    )
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="pw1",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="ADD",
                        proposed_setting_name="전투 특성",
                        proposed_value="근접 전투에 강하다.",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(
        evaluate_multi_stage(gold, bundle, semantic_judge=_AlwaysMismatch())
    )

    world_stage2 = report["stages"]["world"]["stage2"]
    assert world_stage2["metrics"]["liveConditionalAccuracy"] is None
    assert world_stage2["counts"]["upstreamOutcomes"] == {
        "UPSTREAM_VALUE_ERROR": 1
    }


class _AlwaysMatch:
    async def judge_many(self, cases):
        return SemanticOutcomeBatchResult(
            decisions=tuple(
                SemanticOutcomeDecision(
                    caseId=case.case_id,
                    coreMeaningCovered=True,
                    requiredFactsCovered=True,
                    forbiddenFactsAbsent=True,
                    contradiction=False,
                    unsupportedDetail=False,
                    reason="equivalent",
                )
                for case in cases
            )
        )


class _AlwaysMismatch:
    async def judge_many(self, cases):
        return SemanticOutcomeBatchResult(
            decisions=tuple(
                SemanticOutcomeDecision(
                    caseId=case.case_id,
                    coreMeaningCovered=False,
                    requiredFactsCovered=False,
                    forbiddenFactsAbsent=True,
                    contradiction=True,
                    unsupportedDetail=False,
                    reason="different",
                )
                for case in cases
            )
        )


def _basic_gold() -> GoldSnapshotV3:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="나는 바바리안이다. 바바리안은 근접 전투에 강하다.",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    character = CharacterStage1Gold(
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
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.species",
        value_type="STRING",
        display_value="바바리안",
        value_json={"value": "바바리안"},
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )
    world = WorldStage1Gold(
        gold_id="W1",
        scenario_id="S1",
        episode_no=1,
        sort_order=2,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["바바리안은 근접 전투에 강하다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name="바바리안",
        setting_name="전투 특성",
        source_values=["근접 전투에 강하다."],
    )
    character_decision = CharacterStage2Gold(
        decision_id="DC1",
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
    world_decision = WorldStage2Gold(
        decision_id="DW1",
        scenario_id="S1",
        episode_no=1,
        sort_order=2,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="ADD",
        consolidation_status="SINGLE",
        proposed_setting_name="전투 특성",
        proposed_value="근접 전투에 강하다.",
        review_status="FINAL",
    )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="basic",
        scenarios=[scenario],
        stage1=[character, world],
        stage2=[character_decision, world_decision],
    ).with_fixture_hash()
