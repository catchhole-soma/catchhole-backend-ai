import asyncio

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
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStage2Prediction,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.semantic_outcome import (
    SemanticOutcomeBatchResult,
    SemanticOutcomeDecision,
)


def test_stage2_comparator_silence_is_an_error_in_conditional_denominator() -> None:
    gold = _character_add_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[_character_prediction("P1")],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    stage2 = report["stages"]["character"]["stage2"]
    assert stage2["counts"]["upstreamReached"] == 1
    assert stage2["counts"]["reachedAndCompared"] == 0
    assert stage2["metrics"]["liveConditionalAccuracy"] == 0


def test_history_only_miss_is_visible_in_end_to_end_state() -> None:
    scenario = _scenario({"CHARACTER"})
    source = _character_source()
    decision = CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="HISTORY_ONLY",
        temporal_scope="PAST",
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [source], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[ScenarioPrediction(scenario_id="S1")],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 0


def test_present_history_only_is_scored_without_creating_current_snapshot() -> None:
    scenario = _scenario({"CHARACTER"})
    source = CharacterStage1Gold.model_validate(
        _character_source().model_dump()
        | {
            "fact_type": "ITEM",
            "fact_key": "item.potion",
            "display_value": "포션을 획득 직후 모두 사용함",
            "value_json": {"value": "포션을 획득 직후 모두 사용함"},
            "evidence_quotes": ["포션 절반을 바르고 나머지를 마셨다."],
        }
    )
    decision = CharacterStage2Gold(
        decision_id="D-POTION",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="HISTORY_ONLY",
        temporal_scope="PRESENT",
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [source], [decision])
    stage1 = CharacterStage1Prediction.model_validate(
        _character_prediction("P1").model_dump()
        | {
            "fact_type": "ITEM",
            "fact_key": "item.potion",
            "display_value": "포션을 획득 직후 모두 사용함",
            "value_json": {"value": "포션을 획득 직후 모두 사용함"},
            "evidence_spans": [{"quote": "포션 절반을 바르고 나머지를 마셨다."}],
        }
    )
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[stage1],
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="P1",
                        domain="CHARACTER",
                        operation="HISTORY_ONLY",
                        resolved_canonical_fact_key="item.potion",
                        temporal_scope="PRESENT",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage2"]["metrics"][
        "fullDecisionAccuracy"
    ] == 1
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1
    scenario_detail = report["scenarios"][0]
    assert scenario_detail["beforeStateHash"] != scenario_detail["expectedAfterStateHash"]


def test_world_conflict_hold_miss_is_visible_in_end_to_end_state() -> None:
    scenario = _scenario({"WORLD"})
    source = _world_source(
        values=["약 300m다.", "약 3km다."],
        category="POWER_SYSTEM",
        subject="통신석",
        setting="반경",
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
    gold = _snapshot(scenario, [source], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[ScenarioPrediction(scenario_id="S1")],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 0


def test_oracle_multi_source_world_conflict_applies_the_grouped_handoff() -> None:
    scenario = _scenario({"WORLD"})
    first = _world_source(values=["약 300m다."], category="POWER_SYSTEM")
    second = _world_source(
        gold_id="W2",
        sort_order=2,
        values=["약 3km다."],
        category="POWER_SYSTEM",
    )
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1", "W2"],
        domain="WORLD",
        operation="ADD",
        consolidation_status="CONFLICT",
        proposed_setting_name="체격",
        proposed_value="약 300m다.\n약 3km다.",
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [first, second], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="W1",
                        domain="WORLD",
                        consolidation_status="CONFLICT",
                        operation="ADD",
                        proposed_setting_name="체격",
                        proposed_value="약 300m다.\n약 3km다.",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1


def test_oracle_character_discovery_preserves_the_gold_entity_ref() -> None:
    scenario = _scenario({"CHARACTER"})
    discovery = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["그의 이름은 비요른 얀델이었다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="CHARACTER_DISCOVERY",
        entity_ref="character:bjorn",
        entity_name="비요른 얀델",
    )
    gold = _snapshot(scenario, [discovery], [])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    CharacterStage1Prediction(
                        candidate_id="C1",
                        domain="CHARACTER",
                        candidate_kind="CHARACTER_DISCOVERY",
                        entity_ref="character:bjorn",
                        entity_name="비요른 얀델",
                        evidence_spans=[
                            {"quote": "그의 이름은 비요른 얀델이었다."}
                        ],
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1


def test_semantic_pending_is_not_silently_scored_as_wrong() -> None:
    gold, target_ref = _world_merge_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="W1",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="MERGE",
                        target_ref=target_ref,
                        matched_property_name="체격",
                        proposed_setting_name="체격",
                        proposed_value=(
                            "보통은 140cm이고 190cm짜리 변종은 극히 드물다."
                        ),
                    )
                ],
            )
        ],
    )

    pending = asyncio.run(evaluate_multi_stage(gold, bundle))
    matched = asyncio.run(
        evaluate_multi_stage(gold, bundle, semantic_judge=_AlwaysMatch())
    )

    pending_stage2 = pending["stages"]["world"]["stage2"]["metrics"]
    assert pending_stage2["proposedValueAccuracy"] is None
    assert pending_stage2["fullDecisionAccuracy"] is None
    assert pending_stage2["fullDecisionLowerBoundAccuracy"] == 0
    assert pending_stage2["semanticCoverage"] == 0
    assert pending["endToEnd"]["metrics"]["afterStateF1"] is None
    assert pending["endToEnd"]["metrics"]["afterStateLowerBoundF1"] == 0
    assert pending["endToEnd"]["metrics"]["transitionF1"] is None
    assert matched["endToEnd"]["metrics"]["afterStateF1"] == 1
    assert matched["endToEnd"]["metrics"]["transitionF1"] == 1


def test_end_to_end_semantic_case_keeps_reviewed_merge_constraints() -> None:
    gold, target_ref = _world_merge_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="W1",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="MERGE",
                        target_ref=target_ref,
                        matched_property_name="체격",
                        proposed_setting_name="체격",
                        proposed_value=(
                            "보통은 140cm이고 190cm짜리 변종은 극히 드물다."
                        ),
                    )
                ],
            )
        ],
    )
    judge = _CaptureMatch()

    asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    state_case = next(case for case in judge.cases if case.case_id.startswith("state:"))
    assert state_case.required_facts == ("평균 키 140cm", "희귀 변종 190cm")
    assert state_case.forbidden_facts == ("모든 고블린 190cm",)
    assert state_case.before_value == "평균은 140cm다."
    assert state_case.source_values == ("큰 변종은 190cm도 아주 희귀하게 보인다.",)


def test_stage1_pending_value_does_not_inflate_primary_accuracy() -> None:
    gold, target_ref = _world_merge_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="P1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="고블린",
                        setting_name="체격",
                        source_values=["190cm인 큰 변종은 매우 드물다."],
                        evidence_spans=[{"quote": "큰 변종은 190cm다."}],
                    )
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="P1",
                        domain="WORLD",
                        consolidation_status="SINGLE",
                        operation="MERGE",
                        target_ref=target_ref,
                        matched_property_name="체격",
                        proposed_setting_name="체격",
                        proposed_value="평균은 140cm이며 아주 드문 큰 변종은 190cm다.",
                    )
                ],
            )
        ],
    )

    pending = asyncio.run(evaluate_multi_stage(gold, bundle))
    matched = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=_AlwaysMatch()))

    pending_metrics = pending["stages"]["world"]["stage1"]["metrics"]
    assert pending_metrics["valueAccuracy"] is None
    assert pending_metrics["valueLowerBoundAccuracy"] == 0
    assert pending_metrics["valueSemanticCoverage"] == 0
    assert matched["stages"]["world"]["stage1"]["metrics"]["valueAccuracy"] == 1


def test_character_stage2_json_mismatch_fails_decision_and_end_to_end_state() -> None:
    gold = _character_add_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="C1",
                        domain="CHARACTER",
                        operation="ADD",
                        resolved_canonical_fact_key="profile.species",
                        temporal_scope="PRESENT",
                        proposed_value="바바리안",
                        proposed_value_json={"value": "엘프"},
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    stage2 = report["stages"]["character"]["stage2"]["metrics"]
    assert stage2["proposedValueAccuracy"] == 1
    assert stage2["proposedValueJsonAccuracy"] == 0
    assert stage2["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] < 1
    assert report["endToEnd"]["metrics"]["transitionF1"] < 1


def test_character_json_subset_allows_additional_structured_fields_end_to_end() -> None:
    gold = _character_add_gold()
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="C1",
                        domain="CHARACTER",
                        operation="ADD",
                        resolved_canonical_fact_key="profile.species",
                        temporal_scope="PRESENT",
                        proposed_value="바바리안",
                        proposed_value_json={
                            "value": "바바리안",
                            "confidenceLabel": "confirmed",
                        },
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    stage2 = report["stages"]["character"]["stage2"]["metrics"]
    assert stage2["proposedValueJsonAccuracy"] == 1
    assert stage2["fullDecisionAccuracy"] == 1
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1


def test_character_json_subset_ignores_extra_only_transition() -> None:
    fact_ref = character_state_ref(
        "character:bjorn",
        "PROFILE",
        "profile.height",
    )
    scenario = _scenario(
        {"CHARACTER"},
        start_state_mode="SEED",
        seed_state=EvaluationState(
            character_facts=[
                CharacterStateEntry(
                    ref=fact_ref,
                    entity_ref="character:bjorn",
                    entity_name="비요른",
                    fact_type="PROFILE",
                    fact_key="profile.height",
                    value_type="JSON",
                    value="키는 170cm다.",
                    value_json={"unit": "cm"},
                )
            ]
        ),
    )
    source = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["키는 170cm다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.height",
        value_type="JSON",
        display_value="키는 170cm다.",
        value_json={"unit": "cm"},
    )
    decision = CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="UPDATE",
        temporal_scope="PRESENT",
        target_ref=fact_ref,
        before_value="키는 170cm다.",
        before_value_json={"unit": "cm"},
        proposed_value="키는 170cm다.",
        proposed_value_json={"unit": "cm"},
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [source], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="ORACLE",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="C1",
                        domain="CHARACTER",
                        operation="UPDATE",
                        resolved_canonical_fact_key="profile.height",
                        temporal_scope="PRESENT",
                        target_ref=fact_ref,
                        proposed_value="키는 170cm다.",
                        proposed_value_json={"unit": "cm", "max": 190},
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage2"]["metrics"][
        "fullDecisionAccuracy"
    ] == 1
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1
    assert report["endToEnd"]["metrics"]["transitionF1"] == 1
    assert report["endToEnd"]["counts"]["expectedTransitions"] == report[
        "endToEnd"
    ]["counts"]["predictedTransitions"]


def test_world_normalized_identity_uses_gold_canonical_state_path() -> None:
    scenario = _scenario({"WORLD"})
    source = _world_source(values=["평균 140cm다."], subject="Goblin")
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="ADD",
        consolidation_status="SINGLE",
        proposed_setting_name="체격",
        proposed_value="평균 140cm다.",
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [source], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="P1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="goblin",
                        setting_name="체격",
                        source_values=["평균 140cm다."],
                    )
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="P1",
                        domain="WORLD",
                        operation="ADD",
                        consolidation_status="SINGLE",
                        proposed_setting_name="체격",
                        proposed_value="평균 140cm다.",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"][
        "fullDecisionAccuracy"
    ] == 1
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1


def test_world_setting_name_alias_matches_stage1_and_uses_gold_canonical_path() -> None:
    scenario = _scenario({"WORLD"})
    source = _world_source(
        values=["고블린은 함정을 설치한다."],
        setting="함정 사용",
    ).model_copy(
        update={"accepted_setting_name_aliases": ["함정 습성", "함정 활용"]}
    )
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="ADD",
        consolidation_status="SINGLE",
        proposed_setting_name="함정 사용",
        proposed_value="고블린은 함정을 설치한다.",
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [source], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="P1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="고블린",
                        setting_name="함정 습성",
                        source_values=["고블린은 함정을 설치한다."],
                    )
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="P1",
                        domain="WORLD",
                        operation="ADD",
                        consolidation_status="SINGLE",
                        proposed_setting_name="함정 사용",
                        proposed_value="고블린은 함정을 설치한다.",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage1"]["metrics"][
        "pathOrFactAccuracy"
    ] == 1
    assert report["stages"]["world"]["stage2"]["metrics"][
        "fullDecisionAccuracy"
    ] == 1
    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] == 1


def test_character_accepted_alias_uses_gold_canonical_fact_slot() -> None:
    source = _character_source().model_copy(
        update={"accepted_fact_key_aliases": ["species"]}
    )
    gold = _snapshot(_scenario({"CHARACTER"}), [source], [_character_add_decision()])
    prediction = _character_prediction("P1").model_copy(update={"fact_key": "species"})
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[prediction],
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
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == 1
    stage2_metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert stage2_metrics["characterCanonicalFactKeyResolutionAccuracy"] == 1
    assert stage2_metrics["removedSnapshotSetAccuracy"] is None
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 1


def test_wrong_character_canonical_key_resolution_is_not_masked_by_gold_match() -> None:
    source = _character_source().model_copy(
        update={"accepted_fact_key_aliases": ["species"]}
    )
    gold = _snapshot(_scenario({"CHARACTER"}), [source], [_character_add_decision()])
    prediction = _character_prediction("P1").model_copy(update={"fact_key": "species"})
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[prediction],
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="P1",
                        domain="CHARACTER",
                        operation="ADD",
                        resolved_canonical_fact_key="profile.race",
                        temporal_scope="PRESENT",
                        proposed_value="바바리안",
                        proposed_value_json={"value": "바바리안"},
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    metrics = report["stages"]["character"]["stage2"]["metrics"]
    assert metrics["characterCanonicalFactKeyResolutionAccuracy"] == 0
    assert metrics["fullDecisionAccuracy"] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] < 1


def test_wrong_character_ref_cannot_borrow_gold_selector_for_end_to_end() -> None:
    gold = _character_add_gold()
    # Gold ID와 우연히 같은 runtime candidate ID여도 identity가 틀리면
    # Gold selector/ledger ID를 빌리면 안 된다.
    prediction = _character_prediction("C1")
    prediction = prediction.model_copy(update={"entity_ref": "character:thor"})
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[prediction],
                stage2=[
                    CharacterStage2Prediction(
                        source_candidate_id="C1",
                        domain="CHARACTER",
                        operation="ADD",
                        resolved_canonical_fact_key="profile.species",
                        temporal_scope="PRESENT",
                        proposed_value="바바리안",
                        proposed_value_json={"value": "바바리안"},
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage1"]["metrics"][
        "entityOrSubjectAccuracy"
    ] == 0
    assert report["endToEnd"]["domains"]["CHARACTER"]["afterStateF1"] == 0


def test_non_target_inherited_domain_is_not_recounted() -> None:
    inherited_ref = world_state_ref("RACE", "고블린", None, "체격")
    scenario = _scenario(
        {"CHARACTER"},
        start_state_mode="SEED",
        seed_state=EvaluationState(
            world_facts=[
                WorldStateEntry(
                    ref=inherited_ref,
                    category="RACE",
                    subject_name="고블린",
                    setting_name="체격",
                    value="평균 140cm",
                )
            ]
        ),
    )
    source = _character_source()
    decision = _character_add_decision()
    gold = _snapshot(scenario, [source], [decision])
    prediction = _character_prediction("P1")
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"CHARACTER", "WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[prediction],
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
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["endToEnd"]["domains"]["WORLD"]["afterStateF1"] is None
    # display state 3개 + 별도 structured JSON state 2개만 CHARACTER로 집계된다.
    assert report["endToEnd"]["counts"]["expectedTransitions"] == 5
    assert report["endToEnd"]["counts"]["matchedTransitions"] == 5


def test_world_rows_on_same_path_are_one_stage1_and_stage2_case() -> None:
    scenario = _scenario({"WORLD"})
    first = _world_source(values=["평균은 140cm다."])
    second = _world_source(
        gold_id="W2",
        sort_order=2,
        values=["큰 변종은 190cm다."],
    )
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1", "W2"],
        domain="WORLD",
        operation="ADD",
        consolidation_status="MERGED",
        proposed_setting_name="체격",
        proposed_value="평균은 140cm이며 큰 변종은 190cm다.",
        review_status="FINAL",
    )
    gold = _snapshot(scenario, [first, second], [decision])
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        evaluation_domains={"WORLD"},
        scenarios=[
            ScenarioPrediction(
                scenario_id="S1",
                stage1=[
                    WorldStage1Prediction(
                        candidate_id="P1",
                        domain="WORLD",
                        category="RACE",
                        subject_name="고블린",
                        setting_name="체격",
                        source_values=["평균은 140cm다.", "큰 변종은 190cm다."],
                        evidence_spans=[
                            {"quote": "평균은 140cm다."},
                            {"quote": "큰 변종은 190cm다."},
                        ],
                    )
                ],
                stage2=[
                    WorldStage2Prediction(
                        source_candidate_id="P1",
                        domain="WORLD",
                        consolidation_status="MERGED",
                        operation="ADD",
                        proposed_setting_name="체격",
                        proposed_value="평균은 140cm이며 큰 변종은 190cm다.",
                    )
                ],
            )
        ],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["world"]["stage1"]["counts"]["gold"] == 1
    assert report["stages"]["world"]["stage1"]["metrics"]["candidateF1"] == 1
    assert report["stages"]["world"]["stage2"]["counts"]["gold"] == 1
    assert report["stages"]["world"]["stage2"]["metrics"][
        "liveConditionalAccuracy"
    ] == 1


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


class _CaptureMatch(_AlwaysMatch):
    def __init__(self) -> None:
        self.cases = ()

    async def judge_many(self, cases):
        self.cases = tuple(cases)
        return await super().judge_many(cases)


def _scenario(target_domains, **updates) -> ScenarioGold:
    values = {
        "scenario_id": "S1",
        "episode_no": 1,
        "source_identifier": "01화.txt",
        "source_text": "평가 원문",
        "target_domains": target_domains,
        "gold_version": "v3",
        "start_state_mode": "EMPTY",
        "cumulative_through_episode": 0,
        "review_status": "FINAL",
    }
    values.update(updates)
    return ScenarioGold(**values)


def _character_source() -> CharacterStage1Gold:
    return CharacterStage1Gold(
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
    )


def _character_add_decision() -> CharacterStage2Gold:
    return CharacterStage2Gold(
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


def _character_add_gold() -> GoldSnapshotV3:
    return _snapshot(
        _scenario({"CHARACTER"}),
        [_character_source()],
        [_character_add_decision()],
    )


def _character_prediction(candidate_id: str) -> CharacterStage1Prediction:
    return CharacterStage1Prediction(
        candidate_id=candidate_id,
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
    )


def _world_source(
    *,
    gold_id: str = "W1",
    sort_order: int = 1,
    values: list[str],
    category: str = "RACE",
    subject: str = "고블린",
    setting: str = "체격",
) -> WorldStage1Gold:
    return WorldStage1Gold(
        gold_id=gold_id,
        scenario_id="S1",
        episode_no=1,
        sort_order=sort_order,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=values,
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category=category,
        subject_name=subject,
        setting_name=setting,
        source_values=values,
    )


def _world_merge_gold() -> tuple[GoldSnapshotV3, str]:
    target_ref = world_state_ref("RACE", "고블린", None, "체격")
    scenario = _scenario(
        {"WORLD"},
        start_state_mode="SEED",
        seed_state=EvaluationState(
            world_facts=[
                WorldStateEntry(
                    ref=target_ref,
                    category="RACE",
                    subject_name="고블린",
                    setting_name="체격",
                    value="평균은 140cm다.",
                )
            ]
        ),
    )
    source = _world_source(values=["큰 변종은 190cm도 아주 희귀하게 보인다."])
    decision = WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="MERGE",
        consolidation_status="SINGLE",
        target_ref=target_ref,
        before_value="평균은 140cm다.",
        matched_property_name="체격",
        proposed_setting_name="체격",
        proposed_value="평균은 140cm이며 아주 드문 큰 변종은 190cm다.",
        required_facts=["평균 키 140cm", "희귀 변종 190cm"],
        forbidden_facts=["모든 고블린 190cm"],
        review_status="FINAL",
    )
    return _snapshot(scenario, [source], [decision]), target_ref


def _snapshot(scenario, stage1, stage2) -> GoldSnapshotV3:
    return GoldSnapshotV3(
        dataset_version="v3",
        name="semantic-regression",
        scenarios=[scenario],
        stage1=stage1,
        stage2=stage2,
    ).with_fixture_hash()
