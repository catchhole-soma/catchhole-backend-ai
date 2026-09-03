import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    ScenarioGold,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
    world_subject_ref,
)
from evals.multi_stage_setting.state_effects import (
    apply_gold_decision,
    build_gold_state_chain,
)


@pytest.mark.parametrize(
    ("operation", "fact_key", "target", "proposal", "temporal", "expected_value", "history"),
    [
        ("ADD", "profile.job", None, "전사", "PRESENT", "전사", 1),
        ("UPDATE", "profile.height", "HEIGHT", "180cm", "PRESENT", "180cm", 1),
        (
            "MERGE",
            "profile.height",
            "HEIGHT",
            "평소 170cm, 변신 시 180cm",
            "PRESENT",
            "평소 170cm, 변신 시 180cm",
            1,
        ),
        ("REMOVE", "status.bleeding", "BLEEDING", None, "PRESENT", None, 1),
        ("HISTORY_ONLY", "profile.height", None, None, "PAST", "170cm", 1),
        ("EXCLUDE", "profile.height", None, None, "PRESENT", "170cm", 0),
        ("REVIEW_REQUIRED", "profile.height", None, None, "UNKNOWN", "170cm", 0),
    ],
)
def test_character_reference_reducer_operation_matrix(
    operation,
    fact_key,
    target,
    proposal,
    temporal,
    expected_value,
    history,
) -> None:
    scenario = _scenario({"CHARACTER"})
    state = _state()
    source = _character_source(fact_key)
    target_ref = {
        "HEIGHT": character_state_ref("character:bjorn", "PROFILE", "profile.height"),
        "BLEEDING": character_state_ref("character:bjorn", "STATUS", "status.bleeding"),
    }.get(target)
    decision = CharacterStage2Gold(
        decision_id=f"D-{operation}",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation=operation,
        target_ref=None if operation == "REMOVE" else target_ref,
        removed_snapshot_refs=[target_ref] if operation == "REMOVE" else [],
        proposed_value=proposal,
        proposed_value_json={"value": proposal} if proposal is not None else None,
        temporal_scope=temporal,
        review_status="FINAL",
    )

    after, held = apply_gold_decision(state, scenario, [source], decision)

    assert held is False
    ref = character_state_ref("character:bjorn", source.fact_type, fact_key)
    actual = next((item.value for item in after.character_facts if item.ref == ref), None)
    assert actual == expected_value
    assert len(after.character_history) == history


def test_character_present_status_add_can_remove_superseded_status() -> None:
    scenario = _scenario({"CHARACTER"})
    state = _state()
    source = _character_source("status.recovered")
    bleeding_ref = character_state_ref(
        "character:bjorn", "STATUS", "status.bleeding"
    )
    decision = CharacterStage2Gold(
        decision_id="D-STATUS",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="ADD",
        removed_snapshot_refs=[bleeding_ref],
        proposed_value="회복됨",
        proposed_value_json={"value": "회복됨"},
        temporal_scope="PRESENT",
        review_status="FINAL",
    )

    after, _ = apply_gold_decision(state, scenario, [source], decision)

    refs = {item.ref for item in after.character_facts}
    assert bleeding_ref not in refs
    assert character_state_ref("character:bjorn", "STATUS", "status.recovered") in refs


def test_character_present_history_only_records_event_without_mutating_snapshot() -> None:
    scenario = _scenario({"CHARACTER"})
    state = _state()
    source = _character_source("profile.potion_used").model_copy(
        update={
            "fact_type": "ITEM",
            "fact_key": "item.potion",
            "display_value": "포션을 획득 직후 모두 사용함",
            "value_json": {"value": "포션을 획득 직후 모두 사용함"},
        }
    )
    decision = CharacterStage2Gold(
        decision_id="D-POTION-CONSUMED",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="HISTORY_ONLY",
        temporal_scope="PRESENT",
        review_status="FINAL",
    )

    after, held = apply_gold_decision(state, scenario, [source], decision)

    assert held is False
    assert after.character_facts == state.character_facts
    assert after.character_history[-1].fact_key == "item.potion"
    assert after.character_history[-1].value == "포션을 획득 직후 모두 사용함"
    assert after.character_history[-1].operation == "HISTORY_ONLY"
    assert after.character_history[-1].temporal_scope == "PRESENT"


def test_character_remove_requires_explicit_snapshot_refs() -> None:
    source = _character_source("status.recovered")

    with pytest.raises(ValueError, match="REMOVE requires at least one removedSnapshotRef"):
        CharacterStage2Gold(
            decision_id="D-REMOVE-WITHOUT-REF",
            scenario_id="S1",
            episode_no=1,
            sort_order=1,
            source_gold_ids=[source.gold_id],
            domain="CHARACTER",
            operation="REMOVE",
            temporal_scope="PRESENT",
            review_status="FINAL",
        )


def test_character_remove_forbids_target_ref() -> None:
    source = _character_source("status.recovered")
    bleeding_ref = character_state_ref(
        "character:bjorn", "STATUS", "status.bleeding"
    )

    with pytest.raises(ValueError, match="other operations forbid it"):
        CharacterStage2Gold(
            decision_id="D-REMOVE-WITH-TARGET",
            scenario_id="S1",
            episode_no=1,
            sort_order=1,
            source_gold_ids=[source.gold_id],
            domain="CHARACTER",
            operation="REMOVE",
            target_ref=bleeding_ref,
            removed_snapshot_refs=[bleeding_ref],
            temporal_scope="PRESENT",
            review_status="FINAL",
        )


def test_same_slot_decisions_compare_and_apply_against_projected_state() -> None:
    scenario = _scenario({"CHARACTER"})
    target_ref = character_state_ref(
        "character:bjorn", "PROFILE", "profile.height"
    )
    first_source = _character_source("profile.height").model_copy(
        update={"gold_id": "C-HEIGHT-1", "sort_order": 1}
    )
    second_source = _character_source("profile.height").model_copy(
        update={"gold_id": "C-HEIGHT-2", "sort_order": 2}
    )
    decisions = [
        CharacterStage2Gold(
            decision_id="D-HEIGHT-1",
            scenario_id="S1",
            episode_no=1,
            sort_order=1,
            source_gold_ids=[first_source.gold_id],
            domain="CHARACTER",
            operation="UPDATE",
            target_ref=target_ref,
            before_value="170cm",
            before_value_json={"value": "170cm"},
            proposed_value="180cm",
            proposed_value_json={"value": "180cm"},
            temporal_scope="PRESENT",
            review_status="FINAL",
        ),
        CharacterStage2Gold(
            decision_id="D-HEIGHT-2",
            scenario_id="S1",
            episode_no=1,
            sort_order=2,
            source_gold_ids=[second_source.gold_id],
            domain="CHARACTER",
            operation="UPDATE",
            target_ref=target_ref,
            before_value="180cm",
            before_value_json={"value": "180cm"},
            proposed_value="190cm",
            proposed_value_json={"value": "190cm"},
            temporal_scope="PRESENT",
            review_status="FINAL",
        ),
    ]
    snapshot = GoldSnapshotV3(
        dataset_version="v3",
        name="same slot",
        scenarios=[scenario],
        stage1=[first_source, second_source],
        stage2=decisions,
    )

    transition = build_gold_state_chain(snapshot)["S1"]

    assert [item.value for item in transition.resolved_decision_befores] == [
        "170cm",
        "180cm",
    ]
    final_entry = next(
        item for item in transition.after_state.character_facts if item.ref == target_ref
    )
    assert final_entry.value == "190cm"


@pytest.mark.parametrize(
    ("operation", "target", "proposal", "expected"),
    [
        ("ADD", "SUBJECT", "야간 시야가 좋다.", "야간 시야가 좋다."),
        ("UPDATE", "TARGET", "평균 150cm다.", "평균 150cm다."),
        (
            "MERGE",
            "TARGET",
            "평균 140cm이며 큰 변종은 드물게 190cm다.",
            "평균 140cm이며 큰 변종은 드물게 190cm다.",
        ),
        ("EXCLUDE", None, None, "평균 140cm다."),
    ],
)
def test_world_reference_reducer_operation_matrix(operation, target, proposal, expected) -> None:
    scenario = _scenario({"WORLD"})
    state = _state()
    is_add = operation == "ADD"
    source = _world_source("야간 시야" if is_add else "체격", "야간 시야가 좋다.")
    target_ref = {
        "SUBJECT": world_subject_ref("RACE", "고블린"),
        "TARGET": world_state_ref("RACE", "고블린", None, "체격"),
    }.get(target)
    decision = WorldStage2Gold(
        decision_id=f"DW-{operation}",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="WORLD",
        operation=operation,
        consolidation_status="SINGLE",
        target_ref=target_ref,
        matched_property_name="체격" if target == "TARGET" else None,
        proposed_setting_name=("야간 시야" if is_add else "체격"),
        proposed_value=(proposal or source.display_value),
        review_status="FINAL",
    )

    after, held = apply_gold_decision(state, scenario, [source], decision)

    assert held is False
    expected_ref = world_state_ref(
        "RACE", "고블린", None, "야간 시야" if is_add else "체격"
    )
    assert next(item.value for item in after.world_facts if item.ref == expected_ref) == expected


def test_world_conflict_is_held_regardless_of_suggested_mutating_operation() -> None:
    scenario = _scenario({"WORLD"})
    state = _state()
    source = _world_source("번식 주기", "한 달이다.\n일 년이다.", values=["한 달", "일 년"])
    decision = WorldStage2Gold(
        decision_id="DW-CONFLICT",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="WORLD",
        operation="ADD",
        consolidation_status="CONFLICT",
        target_ref=world_subject_ref("RACE", "고블린"),
        proposed_setting_name="번식 주기",
        proposed_value="한 달이다.\n일 년이다.",
        review_status="FINAL",
    )

    after, held = apply_gold_decision(state, scenario, [source], decision)

    assert held is True
    assert after.world_facts == state.world_facts
    assert after.held_world_conflicts[0].source_values == ["한 달", "일 년"]


def test_world_add_can_target_an_existing_subject_without_targeting_a_property() -> None:
    scenario = _scenario({"WORLD"})
    state = _state()
    source = _world_source("야간 시야", "야간 시야가 좋다.")
    decision = WorldStage2Gold(
        decision_id="DW-SUBJECT-ADD",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="WORLD",
        operation="ADD",
        consolidation_status="SINGLE",
        target_ref=world_subject_ref("RACE", "고블린"),
        proposed_setting_name="야간 시야",
        proposed_value="야간 시야가 좋다.",
        review_status="FINAL",
    )

    after, held = apply_gold_decision(state, scenario, [source], decision)

    assert held is False
    assert any(item.setting_name == "야간 시야" for item in after.world_facts)


def test_gold_contract_rejects_upsert_removing_the_new_character_candidate_slot() -> None:
    scenario = _scenario({"CHARACTER"})
    source = _character_source("status.recovered")
    exact_ref = character_state_ref(
        source.entity_ref,
        source.fact_type or "",
        source.fact_key or "",
    )
    decision = CharacterStage2Gold(
        decision_id="D-OVERLAP",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="ADD",
        removed_snapshot_refs=[exact_ref],
        proposed_value="회복됨",
        proposed_value_json={"value": "회복됨"},
        temporal_scope="PRESENT",
        review_status="FINAL",
    )

    with pytest.raises(ValueError, match="must not remove the candidate's exact slot"):
        GoldSnapshotV3(
            dataset_version="v3",
            name="removed overlap",
            scenarios=[scenario],
            stage1=[source],
            stage2=[decision],
        )


def test_character_remove_uses_explicit_cross_key_snapshot_refs_and_keeps_history() -> None:
    scenario = _scenario({"CHARACTER"})
    bleeding_ref = character_state_ref(
        "character:bjorn", "STATUS", "status.bleeding"
    )
    poison_ref = character_state_ref(
        "character:bjorn", "STATUS", "status.poisoned"
    )
    state = _state().model_copy(
        update={
            "character_facts": [
                *_state().character_facts,
                CharacterStateEntry(
                    ref=poison_ref,
                    entity_ref="character:bjorn",
                    entity_name="비요른",
                    fact_type="STATUS",
                    fact_key="status.poisoned",
                    value_type="STRING",
                    value="중독됨",
                    value_json={"value": "중독됨"},
                ),
            ]
        }
    )
    source = _character_source("status.recovered")
    decision = CharacterStage2Gold(
        decision_id="D-RECOVERED",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="REMOVE",
        removed_snapshot_refs=[bleeding_ref, poison_ref],
        temporal_scope="PRESENT",
        review_status="FINAL",
    )

    after, held = apply_gold_decision(state, scenario, [source], decision)

    assert held is False
    assert bleeding_ref not in {item.ref for item in after.character_facts}
    assert poison_ref not in {item.ref for item in after.character_facts}
    assert after.character_history[-1].fact_key == "status.recovered"
    assert after.character_history[-1].operation == "REMOVE"


def test_projected_status_added_then_removed_in_same_scenario() -> None:
    scenario = _scenario({"CHARACTER"})
    source = _character_source("status.restrained")
    source_ref = character_state_ref(
        source.entity_ref,
        source.fact_type or "",
        source.fact_key or "",
    )
    release_source = source.model_copy(
        update={
            "gold_id": "C-RELEASED",
            "sort_order": 2,
            "display_value": "구속에서 풀려남",
            "value_json": {"value": "구속에서 풀려남"},
        }
    )
    decisions = [
        CharacterStage2Gold(
            decision_id="D-RESTRAINED",
            scenario_id="S1",
            episode_no=1,
            sort_order=1,
            source_gold_ids=[source.gold_id],
            domain="CHARACTER",
            operation="ADD",
            proposed_value="구속됨",
            proposed_value_json={"value": "구속됨"},
            temporal_scope="PRESENT",
            review_status="FINAL",
        ),
        CharacterStage2Gold(
            decision_id="D-RELEASED",
            scenario_id="S1",
            episode_no=1,
            sort_order=2,
            source_gold_ids=[release_source.gold_id],
            domain="CHARACTER",
            operation="REMOVE",
            removed_snapshot_refs=[source_ref],
            before_value="구속됨",
            before_value_json={"value": "구속됨"},
            temporal_scope="PRESENT",
            review_status="FINAL",
        ),
    ]
    snapshot = GoldSnapshotV3(
        dataset_version="v3",
        name="projected status lifecycle",
        scenarios=[scenario],
        stage1=[source, release_source],
        stage2=decisions,
    )

    transition = build_gold_state_chain(snapshot)["S1"]

    assert source_ref not in {item.ref for item in transition.after_state.character_facts}
    operations_by_source = {
        item.source_gold_id: item.operation
        for item in transition.after_state.character_history
        if item.source_gold_id in {source.gold_id, release_source.gold_id}
    }
    assert operations_by_source == {
        source.gold_id: "ADD",
        release_source.gold_id: "REMOVE",
    }
    assert [item.value for item in transition.resolved_decision_befores] == [
        None,
        "구속됨",
    ]


def test_character_status_side_effect_cannot_remove_another_character_status() -> None:
    scenario = _scenario({"CHARACTER"})
    source = _character_source("status.recovered")
    other_ref = character_state_ref("character:ainar", "STATUS", "status.bleeding")
    state = _state().model_copy(
        update={
            "character_facts": [
                *_state().character_facts,
                CharacterStateEntry(
                    ref=other_ref,
                    entity_ref="character:ainar",
                    entity_name="아이날",
                    fact_type="STATUS",
                    fact_key="status.bleeding",
                    value_type="STRING",
                    value="출혈 중",
                    value_json={"value": "출혈 중"},
                ),
            ]
        }
    )
    decision = CharacterStage2Gold(
        decision_id="D-CROSS-CHARACTER",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="CHARACTER",
        operation="ADD",
        removed_snapshot_refs=[other_ref],
        proposed_value="회복됨",
        proposed_value_json={"value": "회복됨"},
        temporal_scope="PRESENT",
        review_status="FINAL",
    )

    with pytest.raises(ValueError, match="must belong to the source character"):
        apply_gold_decision(state, scenario, [source], decision)


def test_world_update_contract_rejects_property_rename() -> None:
    target_ref = world_state_ref("RACE", "고블린", None, "체격")

    with pytest.raises(ValueError, match="preserve the matched property path"):
        WorldStage2Gold(
            decision_id="DW-RENAME",
            scenario_id="S1",
            episode_no=1,
            sort_order=1,
            source_gold_ids=["W-체격"],
            domain="WORLD",
            operation="UPDATE",
            consolidation_status="SINGLE",
            target_ref=target_ref,
            matched_property_name="체격",
            proposed_setting_name="신장",
            proposed_value="평균 150cm다.",
            review_status="FINAL",
        )


def test_world_add_rejects_property_level_target() -> None:
    scenario = _scenario({"WORLD"})
    source = _world_source("야간 시야", "야간 시야가 좋다.")
    decision = WorldStage2Gold(
        decision_id="DW-BAD-ADD-TARGET",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="WORLD",
        operation="ADD",
        consolidation_status="SINGLE",
        target_ref=world_state_ref("RACE", "고블린", None, "체격"),
        proposed_setting_name="야간 시야",
        proposed_value="야간 시야가 좋다.",
        review_status="FINAL",
    )

    with pytest.raises(ValueError, match="ADD may target only an existing subject ref"):
        apply_gold_decision(_state(), scenario, [source], decision)


def test_world_exclude_rejects_mismatched_target_path() -> None:
    scenario = _scenario({"WORLD"})
    source = _world_source("체격", "평균 140cm다.")
    decision = WorldStage2Gold(
        decision_id="DW-BAD-EXCLUDE",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=[source.gold_id],
        domain="WORLD",
        operation="EXCLUDE",
        consolidation_status="SINGLE",
        target_ref=world_state_ref("RACE", "고블린", None, "체격"),
        matched_property_name="나이",
        proposed_setting_name="체격",
        proposed_value="평균 140cm다.",
        review_status="FINAL",
    )

    with pytest.raises(ValueError, match="matched path differs from target state"):
        apply_gold_decision(_state(), scenario, [source], decision)


def _scenario(domains: set[str]) -> ScenarioGold:
    return ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains=domains,
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=_state(),
        review_status="FINAL",
    )


def _state() -> EvaluationState:
    return EvaluationState(
        character_facts=[
            CharacterStateEntry(
                ref=character_state_ref(
                    "character:bjorn", "PROFILE", "profile.height"
                ),
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="PROFILE",
                fact_key="profile.height",
                value_type="STRING",
                value="170cm",
                value_json={"value": "170cm"},
            ),
            CharacterStateEntry(
                ref=character_state_ref(
                    "character:bjorn", "STATUS", "status.bleeding"
                ),
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="STATUS",
                fact_key="status.bleeding",
                value_type="STRING",
                value="출혈 중",
                value_json={"value": "출혈 중"},
            ),
        ],
        world_facts=[
            WorldStateEntry(
                ref=world_state_ref("RACE", "고블린", None, "체격"),
                category="RACE",
                subject_name="고블린",
                setting_name="체격",
                value="평균 140cm다.",
            )
        ],
    )


def _character_source(fact_key: str) -> CharacterStage1Gold:
    fact_type = "STATUS" if fact_key.startswith("status.") else "PROFILE"
    return CharacterStage1Gold(
        gold_id=f"C-{fact_key}",
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
        display_value="후보 값",
        value_json={"value": "후보 값"},
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )


def _world_source(
    setting_name: str,
    display: str,
    *,
    values: list[str] | None = None,
) -> WorldStage1Gold:
    return WorldStage1Gold(
        gold_id=f"W-{setting_name}",
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
        subject_name="고블린",
        setting_name=setting_name,
        source_values=values or [display],
    )
