import pytest
from pydantic import ValidationError

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    ScenarioGold,
    WorldStage1Gold,
    WorldStage1Prediction,
    WorldStage2Gold,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
    world_subject_ref,
)
from evals.multi_stage_setting.matching import (
    consolidate_world_predictions,
    match_stage1,
)
from evals.multi_stage_setting.state_effects import (
    StateApplicationError,
    apply_gold_decision,
)


def test_world_source_value_uniqueness_uses_production_normalization() -> None:
    gold = _world_gold(values=["큰  변종", "큰 변종"])

    assert gold.source_values == ["큰  변종", "큰 변종"]

    with pytest.raises(ValidationError, match="unique after normalization"):
        _world_gold(values=["Goblin", "goblin"])


def test_world_prediction_consolidation_uses_production_value_identity() -> None:
    predictions = [
        _world_prediction(candidate_id="P1", values=["큰  변종"]),
        _world_prediction(candidate_id="P2", values=["큰 변종"]),
    ]

    consolidated = consolidate_world_predictions(predictions)

    assert len(consolidated) == 1
    assert consolidated[0].source_values == ["큰  변종", "큰 변종"]

    with pytest.raises(ValidationError, match="unique after normalization"):
        _world_prediction(values=["Goblin", "goblin"])


@pytest.mark.parametrize(
    ("gold_subject", "prediction_subject", "gold_scope", "prediction_scope", "subject", "path"),
    [
        ("고블  린", "고블 린", None, None, False, True),
        ("고블린", "고블린", "지하  1층", "지하 1층", True, False),
    ],
)
def test_world_matching_uses_production_identity_but_semantic_value_comparison(
    gold_subject: str,
    prediction_subject: str,
    gold_scope: str | None,
    prediction_scope: str | None,
    subject: bool,
    path: bool,
) -> None:
    gold = _world_gold(subject=gold_subject, scope=gold_scope)
    prediction = _world_prediction(subject=prediction_subject, scope=prediction_scope)

    result = match_stage1(
        [gold],
        [prediction],
        domain="WORLD",
        source_text=None,
    )

    assert len(result.matches) == 1
    assert result.matches[0].entity_or_subject_matched is subject
    assert result.matches[0].path_or_fact_matched is path
    # Value comparison intentionally remains semantic and collapses whitespace.
    assert result.matches[0].value_status == "MATCH"


def test_targetless_world_add_rejects_an_existing_canonical_subject() -> None:
    state = EvaluationState(world_facts=[_world_entry(setting="체격")])
    source = _world_gold(subject=" 고블린 ", setting="야간 시야", values=["밝게 본다."])
    decision = _world_add(source, target_ref=None)

    with pytest.raises(
        StateApplicationError,
        match="existing canonical subject requires a subject target ref",
    ):
        apply_gold_decision(state, _scenario(state), [source], decision)


def test_evaluation_state_rejects_property_scope_tree_shape_collision() -> None:
    root_property = _world_entry(setting="체격")
    scoped_property = _world_entry(scope=" 체격 ", setting="변종")

    with pytest.raises(ValidationError, match="both a property and a scope"):
        EvaluationState(world_facts=[root_property, scoped_property])


def test_world_reducer_rejects_property_scope_tree_shape_collision() -> None:
    state = EvaluationState(world_facts=[_world_entry(setting="체격")])
    source = _world_gold(scope="체격", setting="변종", values=["190cm다."])
    decision = _world_add(
        source,
        target_ref=world_subject_ref("RACE", "고블린"),
    )

    with pytest.raises(StateApplicationError, match="both a property and a scope"):
        apply_gold_decision(state, _scenario(state), [source], decision)


def test_world_gold_rejects_scope_mismatch_the_production_comparator_cannot_emit() -> None:
    target_ref = world_state_ref("RACE", "고블린", "일반", "체격")
    state = EvaluationState(
        world_facts=[_world_entry(scope="일반", setting="체격", value="평균 140cm")]
    )
    source = _world_gold(scope="변종", values=["희귀하게 190cm다."])
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
        matched_scope_name="일반",
        matched_property_name="체격",
        proposed_scope_name="일반",
        proposed_setting_name="체격",
        proposed_value="평균 140cm이며 변종은 희귀하게 190cm다.",
        review_status="FINAL",
    )

    with pytest.raises(ValidationError, match="extracted scope as matchedScopeName"):
        GoldSnapshotV3(
            dataset_version="v3",
            name="invalid-world-scope",
            scenarios=[_scenario(state)],
            stage1=[source],
            stage2=[decision],
        )


def test_same_world_path_in_later_episode_uses_its_own_stage2_decision() -> None:
    scenario1 = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    scenario2 = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="PREVIOUS_GOLD",
        previous_scenario_id="S1",
        cumulative_through_episode=1,
        review_status="FINAL",
    )
    source1 = _world_gold()
    source2 = _world_gold().model_copy(
        update={
            "gold_id": "W2",
            "scenario_id": "S2",
            "episode_no": 2,
            "source_values": ["변종은 희귀하게 190cm다."],
        }
    )
    decision1 = _world_add(source1, target_ref=None)
    decision2 = _world_add(source2, target_ref=None).model_copy(
        update={
            "decision_id": "D2",
            "scenario_id": "S2",
            "episode_no": 2,
            "source_gold_ids": ["W2"],
            "proposed_value": "변종은 희귀하게 190cm다.",
        }
    )

    snapshot = GoldSnapshotV3(
        dataset_version="v3",
        name="same world path across episodes",
        scenarios=[scenario1, scenario2],
        stage1=[source1, source2],
        stage2=[decision1, decision2],
    )

    assert [item.decision_id for item in snapshot.stage2] == ["D1", "D2"]


def test_character_fact_type_is_limited_to_java_enum() -> None:
    with pytest.raises(ValidationError):
        CharacterStateEntry(
            ref=character_state_ref("character:bjorn", "STATS", "stats.strength"),
            entity_ref="character:bjorn",
            entity_name="비요른",
            fact_type="STATS",
            fact_key="stats.strength",
            value="10",
        )


@pytest.mark.parametrize(
    ("fact_type", "fact_key"),
    [
        ("PROFILE", "profile.species"),
        ("AGE", "age"),
        ("LEVEL", "level"),
        ("STAT", "stats.strength"),
        ("SKILL", "skill.검술"),
        ("ITEM", "item.장검"),
        ("STATUS", "status.출혈"),
        ("STATUS", "statuses.condition"),
        ("TIME", "time.flashback"),
    ],
)
def test_character_built_in_fact_key_accepts_matching_java_type(
    fact_type: str,
    fact_key: str,
) -> None:
    row = _character_gold(fact_type=fact_type, fact_key=fact_key)

    assert row.fact_type == fact_type


def test_character_built_in_fact_key_rejects_mismatched_type() -> None:
    with pytest.raises(ValidationError, match="does not match built-in factKey"):
        _character_gold(fact_type="PROFILE", fact_key="status.bleeding")

    with pytest.raises(ValidationError, match="does not match built-in factKey"):
        CharacterStage1Prediction(
            candidate_id="C1",
            domain="CHARACTER",
            candidate_kind="SETTING",
            entity_name="비요른",
            fact_type="ITEM",
            fact_key="skill.검술",
        )


def test_character_custom_fact_key_keeps_its_declared_type() -> None:
    row = _character_gold(fact_type="PROFILE", fact_key="lore.reputation")

    assert row.fact_type == "PROFILE"


def _world_entry(
    *,
    subject: str = "고블린",
    scope: str | None = None,
    setting: str,
    value: str = "값",
) -> WorldStateEntry:
    return WorldStateEntry(
        ref=world_state_ref("RACE", subject, scope, setting),
        category="RACE",
        subject_name=subject,
        scope_name=scope,
        setting_name=setting,
        value=value,
    )


def _world_gold(
    *,
    subject: str = "고블린",
    scope: str | None = None,
    setting: str = "체격",
    values: list[str] | None = None,
) -> WorldStage1Gold:
    return WorldStage1Gold(
        gold_id="W1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["원문 근거"],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name=subject,
        scope_name=scope,
        setting_name=setting,
        source_values=values or ["평균 140cm다."],
    )


def _world_prediction(
    *,
    candidate_id: str = "P1",
    subject: str = "고블린",
    scope: str | None = None,
    setting: str = "체격",
    values: list[str] | None = None,
) -> WorldStage1Prediction:
    return WorldStage1Prediction(
        candidate_id=candidate_id,
        domain="WORLD",
        category="RACE",
        subject_name=subject,
        scope_name=scope,
        setting_name=setting,
        source_values=values or ["평균  140cm다."],
    )


def _world_add(source: WorldStage1Gold, *, target_ref: str | None) -> WorldStage2Gold:
    return WorldStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="WORLD",
        operation="ADD",
        consolidation_status="SINGLE",
        target_ref=target_ref,
        proposed_scope_name=source.scope_name,
        proposed_setting_name=source.setting_name,
        proposed_value=source.display_value,
        review_status="FINAL",
    )


def _scenario(state: EvaluationState) -> ScenarioGold:
    return ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=state,
        review_status="FINAL",
    )


def _character_gold(*, fact_type: str, fact_key: str) -> CharacterStage1Gold:
    return CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["원문 근거"],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type=fact_type,
        fact_key=fact_key,
        value_type="STRING",
        display_value="값",
    )
