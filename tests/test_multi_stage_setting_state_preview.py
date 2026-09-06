from evals.multi_stage_setting.contracts import (
    CharacterHistoryEntry,
    CharacterStateEntry,
    EvaluationState,
    HeldWorldConflict,
    KnownCharacter,
    ScenarioGold,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.state_preview import render_notion_before_state


def test_before_state_preview_hides_machine_fields_and_groups_human_rows() -> None:
    state = EvaluationState(
        known_characters=[
            KnownCharacter(
                entity_ref="character:bjorn",
                name="비요른",
                creation_order=1,
            ),
            KnownCharacter(
                entity_ref="character:bulkan",
                name="불칸",
                creation_order=2,
            ),
        ],
        character_facts=[
            CharacterStateEntry(
                ref=character_state_ref(
                    "character:bjorn", "PROFILE", "profile.species"
                ),
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="PROFILE",
                fact_key="profile.species",
                value_type="STRING",
                value="바바리안",
                value_json={"value": "바바리안", "internal": "hidden"},
                source_episode_no=1,
                source_sort_order=1,
            ),
            CharacterStateEntry(
                ref=character_state_ref(
                    "character:bjorn", "PROFILE", "profile.gender"
                ),
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="PROFILE",
                fact_key="profile.gender",
                value_type="STRING",
                value="남성",
                value_json={"value": "남성"},
                source_episode_no=1,
                source_sort_order=2,
            ),
        ],
        character_history=[
            CharacterHistoryEntry(
                scenario_id="S1",
                source_gold_id="G1",
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="STATUS",
                fact_key="status.과거_부상",
                value="과거에 팔을 다침",
                operation="HISTORY_ONLY",
                temporal_scope="PAST",
            )
        ],
        world_facts=[
            WorldStateEntry(
                ref=world_state_ref("LOCATION", "미궁", "1층", "명칭"),
                category="LOCATION",
                subject_name="미궁",
                scope_name="1층",
                setting_name="명칭",
                value="수정 동굴",
            )
        ],
        held_world_conflicts=[
            HeldWorldConflict(
                scenario_id="S1",
                decision_id="D1",
                category="RACE",
                subject_name="고블린",
                setting_name="키",
                source_values=["평균 140cm", "항상 190cm"],
            )
        ],
    )

    preview = render_notion_before_state(_scenario(), state)

    assert "평가 시작 전 누적 상태 · 자동 생성" in preview
    assert "비요른" in preview
    assert "종족" in preview
    assert "성별" in preview
    assert "바바리안" in preview
    assert "불칸" in preview
    assert "이름만 확인됨" in preview
    assert "미궁" in preview
    assert "1층" in preview
    assert "수정 동굴" in preview
    assert "현재 상태에 반영되지 않은 과거 기록" in preview
    assert "과거에 팔을 다침" in preview
    assert "검수 대기 중인 세계관 충돌" in preview
    assert "평균 140cm / 항상 190cm" in preview
    assert "character:bjorn" not in preview
    assert '"internal"' not in preview


def test_before_state_preview_labels_duplicate_names_without_exposing_refs() -> None:
    state = EvaluationState(
        known_characters=[
            KnownCharacter(entity_ref="character:a", name="비요른", creation_order=1),
            KnownCharacter(entity_ref="character:b", name="비요른", creation_order=2),
        ]
    )

    preview = render_notion_before_state(_scenario(), state)

    assert "비요른 (동명이인 1)" in preview
    assert "비요른 (동명이인 2)" in preview
    assert "character:a" not in preview
    assert "character:b" not in preview


def test_empty_before_state_preview_explains_first_episode() -> None:
    scenario = _scenario().model_copy(
        update={"start_state_mode": "EMPTY", "cumulative_through_episode": 0}
    )

    preview = render_notion_before_state(scenario, EvaluationState())

    assert "빈 시작 상태" in preview
    assert "캐릭터와 세계관 누적 상태가 없습니다." in preview
    assert "첫 회차는 빈 상태에서 평가를 시작합니다." in preview


def _scenario() -> ScenarioGold:
    return ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="draft",
        start_state_mode="SEED",
        cumulative_through_episode=1,
        seed_state=EvaluationState(),
        review_status="DRAFT",
    )
