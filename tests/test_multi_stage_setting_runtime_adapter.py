import asyncio
from decimal import Decimal

import httpx
import pytest

import evals.multi_stage_setting.runtime_adapter as runtime_adapter_module
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
)
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
)
from app.analysis.character_name_resolver import ActiveCharacterStatus
from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    ExtractedEvidenceSpan,
    ExtractedCharacterSettingCandidate,
)
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from app.analysis.world_setting_schemas import (
    ExtractedWorldSettingCandidate,
    WorldSettingComparisonBatchDecision,
    WorldSettingComparisonBatchResult,
    WorldSettingExtractionResult,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.exceptions import LlmIncompleteResponseError, LlmResponseValidationError
from app.llm.responses import LlmTextResponse
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStateEntry,
    CharacterStage2Gold,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    ScenarioGold,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.runtime_adapter import (
    RuntimeComponents,
    RuntimePricing,
    RuntimeUsageCounter,
    UsageRecordingTextGenerationClient,
    _character_batch_snapshots,
    _character_schema_hash,
    create_default_runtime_components,
    run_multi_stage_predictions,
)


def test_oracle_runtime_maps_request_local_refs_back_to_stable_state_refs() -> None:
    character_ref = character_state_ref("character:bjorn", "PROFILE", "profile.height")
    world_ref = world_state_ref("RACE", "고블린", None, "체격")
    scenario = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=1,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")],
            character_facts=[
                CharacterStateEntry(
                    ref=character_ref,
                    entity_ref="character:bjorn",
                    entity_name="비요른",
                    fact_type="PROFILE",
                    fact_key="profile.height",
                    value_type="NUMBER",
                    value="170",
                    value_json={"value": 170},
                )
            ],
            world_facts=[
                WorldStateEntry(
                    ref=world_ref,
                    category="RACE",
                    subject_name="고블린",
                    setting_name="체격",
                    value="평균 140cm다.",
                )
            ],
        ),
        review_status="FINAL",
    )
    character = CharacterStage1Gold(
        gold_id="C2",
        scenario_id="S2",
        episode_no=2,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["키는 180cm였다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.height",
        value_type="NUMBER",
        display_value="180",
        value_json={"value": 180},
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )
    world = WorldStage1Gold(
        gold_id="W2",
        scenario_id="S2",
        episode_no=2,
        sort_order=2,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["큰 변종은 190cm다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name="고블린",
        setting_name="체격",
        source_values=["큰 변종은 드물게 190cm다."],
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="oracle refs",
        scenarios=[scenario],
        stage1=[character, world],
        stage2=[
            CharacterStage2Gold(
                decision_id="DC2",
                scenario_id="S2",
                episode_no=2,
                sort_order=1,
                source_gold_ids=["C2"],
                domain="CHARACTER",
                operation="UPDATE",
                target_ref=character_ref,
                proposed_value="180",
                proposed_value_json={"value": 180},
                temporal_scope="PRESENT",
                review_status="FINAL",
            ),
            WorldStage2Gold(
                decision_id="DW2",
                scenario_id="S2",
                episode_no=2,
                sort_order=2,
                source_gold_ids=["W2"],
                domain="WORLD",
                operation="MERGE",
                consolidation_status="SINGLE",
                target_ref=world_ref,
                matched_property_name="체격",
                proposed_setting_name="체격",
                proposed_value="평균 140cm이며 큰 변종은 드물게 190cm다.",
                review_status="FINAL",
            ),
        ],
    ).with_fixture_hash()
    character_comparator = _OracleCharacterComparator()
    world_comparator = _OracleWorldComparator()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="ORACLE",
            components=RuntimeComponents(
                character_comparator=character_comparator,
                world_comparator=world_comparator,
            ),
        )
    )

    assert character_comparator.snapshot_fact_keys == ["profile.height"]
    assert world_comparator.target_property_names == ["체격"]
    assert bundle.scenarios[0].stage2[0].target_ref == character_ref
    assert bundle.scenarios[0].stage2[1].target_ref == world_ref
    serialized = bundle.model_dump_json(by_alias=True)
    assert '"targetRef":"P1"' not in serialized
    assert '"targetRef":"T1"' not in serialized


def test_oracle_character_batch_maps_projected_q_removal_to_stable_ref() -> None:
    injury_ref = character_state_ref(
        "character:bjorn",
        "STATUS",
        "status.오른발_부상",
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
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")],
            character_facts=[
                _character_state(
                    "STATUS",
                    "status.오른발_부상",
                    "오른발을 심하게 다침",
                    value_type="JSON",
                    value_json={"active": True},
                )
            ],
        ),
        review_status="FINAL",
    )
    worsening = _character_gold(
        "C1",
        "status.오른발_부상",
        "오른발 부상이 악화됨",
        {"name": "오른발 부상", "active": True},
        sort_order=1,
    )
    recovery = _character_gold(
        "C2",
        "status.회복",
        "오른발 기능이 회복됨",
        {"name": "회복", "active": False},
        sort_order=2,
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="oracle projected status",
        scenarios=[scenario],
        stage1=[worsening, recovery],
        stage2=[
            CharacterStage2Gold(
                decision_id="D1",
                scenario_id="S5",
                episode_no=5,
                sort_order=1,
                source_gold_ids=["C1"],
                domain="CHARACTER",
                operation="UPDATE",
                target_ref=injury_ref,
                proposed_value="오른발 부상이 악화됨",
                proposed_value_json={"name": "오른발 부상", "active": True},
                temporal_scope="PRESENT",
                review_status="FINAL",
            ),
            CharacterStage2Gold(
                decision_id="D2",
                scenario_id="S5",
                episode_no=5,
                sort_order=2,
                source_gold_ids=["C2"],
                domain="CHARACTER",
                operation="REMOVE",
                removed_snapshot_refs=[injury_ref],
                temporal_scope="PRESENT",
                review_status="FINAL",
            ),
        ],
    ).with_fixture_hash()
    comparator = _ProjectedStatusComparator()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="ORACLE",
            components=RuntimeComponents(
                character_comparator=comparator,
                world_comparator=_AddWorldComparator(),
            ),
            domains={"CHARACTER"},
        )
    )

    assert comparator.calls == [(["C1", "C2"], ["P1"])]
    first, second = bundle.scenarios[0].stage2
    assert first.target_ref == injury_ref
    assert second.removed_snapshot_refs == [injury_ref]
    serialized = bundle.model_dump_json(by_alias=True)
    assert '"targetRef":"Q1"' not in serialized
    assert '"removedSnapshotRefs":["Q1"]' not in serialized


def test_fixed_runtime_reuses_character_dedupe_and_world_consolidation() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 바바리안이다. 고블린은 작고 큰 변종도 있다.",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        known_character_names=["비요른"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="live boundaries",
        scenarios=[scenario],
    ).with_fixture_hash()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_LiveCharacterExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                world_extractor=_LiveWorldExtractor(),
                character_comparator=_AddCharacterComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(
                CharacterSettingSchemaHint(
                    schema_key="profile.species",
                    display_name="종족",
                    attribute_pattern=None,
                    aliases=("종족",),
                    value_type="STRING",
                ),
            ),
            analysis_model="analysis-model",
            subject_resolution_model="subject-model",
            comparison_model="comparison-model",
        )
    )

    prediction = bundle.scenarios[0]
    assert len(prediction.raw_stage1) == 4
    assert len(prediction.stage1) == 2
    character = next(item for item in prediction.stage1 if item.domain == "CHARACTER")
    world = next(item for item in prediction.stage1 if item.domain == "WORLD")
    assert character.match_status == "MATCHED"
    assert character.entity_ref == "character:bjorn"
    assert world.source_values == ["평균 140cm다.", "큰 변종은 드물게 190cm다."]
    assert len(prediction.stage2) == 2
    assert prediction.failures == []
    assert bundle.analysis_model == "analysis-model"
    assert bundle.subject_resolution_model == "subject-model"
    assert bundle.comparison_model == "comparison-model"
    assert bundle.character_schema_hash is not None
    assert bundle.prompt_versions["characterExtraction"] == "setting-extraction:v10"
    assert (
        bundle.prompt_versions["characterComparison"]
        == "character-fact-comparison-batch:v2"
    )


def test_fixed_runtime_compares_related_world_properties_in_one_batch() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="고블린은 함정을 설치하고 주변에 매복한다.",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        candidate_free=True,
        review_status="FINAL",
    )
    comparator = _BatchCapturingWorldComparator()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            GoldSnapshotV3(
                dataset_version="v3",
                name="world comparison batch",
                scenarios=[scenario],
            ).with_fixture_hash(),
            mode="FIXED",
            components=RuntimeComponents(
                character_comparator=_AddCharacterComparator(),
                world_extractor=_IndependentWorldPropertiesExtractor(),
                world_comparator=comparator,
            ),
            domains={"WORLD"},
        )
    )

    assert comparator.calls == [["함정 사용", "매복 습성"]]
    assert len(bundle.scenarios[0].stage2) == 2


def test_fixed_runtime_reraises_incomplete_world_batch_response() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="고블린은 함정을 설치한다.",
        target_domains={"WORLD"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        candidate_free=True,
        review_status="FINAL",
    )

    with pytest.raises(LlmIncompleteResponseError, match="provider incomplete"):
        asyncio.run(
            run_multi_stage_predictions(
                GoldSnapshotV3(
                    dataset_version="v3",
                    name="incomplete world comparison",
                    scenarios=[scenario],
                ).with_fixture_hash(),
                    mode="FIXED",
                    components=RuntimeComponents(
                        character_comparator=_AddCharacterComparator(),
                        world_extractor=_IndependentWorldPropertiesExtractor(),
                        world_comparator=_IncompleteWorldComparator(),
                ),
                domains={"WORLD"},
            )
        )


def test_oracle_episode_selection_uses_gold_dependencies_without_running_them() -> None:
    first = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        candidate_free=True,
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        review_status="FINAL",
    )
    second = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        candidate_free=True,
        start_state_mode="PREVIOUS_GOLD",
        previous_scenario_id="S1",
        cumulative_through_episode=1,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="episode filter",
        scenarios=[first, second],
    ).with_fixture_hash()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="ORACLE",
            components=RuntimeComponents(
                character_comparator=_OracleCharacterComparator(),
                world_comparator=_OracleWorldComparator(),
            ),
            domains={"WORLD"},
            episode_numbers={2},
        )
    )

    assert [item.scenario_id for item in bundle.scenarios] == ["S2"]
    assert bundle.evaluation_scenario_ids == ["S2"]


def test_runtime_rejects_domain_outside_selected_episode_targets() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"WORLD"},
        gold_version="v3",
        candidate_free=True,
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="domain intersection",
        scenarios=[scenario],
    ).with_fixture_hash()

    with pytest.raises(
        ValueError,
        match="Requested domains are not enabled by the selected scenarios: CHARACTER",
    ):
        asyncio.run(
            run_multi_stage_predictions(
                gold,
                mode="ORACLE",
                components=RuntimeComponents(
                    character_comparator=_OracleCharacterComparator(),
                    world_comparator=_OracleWorldComparator(),
                ),
                domains={"CHARACTER"},
                episode_numbers={1},
            )
        )


def test_fixed_runtime_generates_known_character_context_from_before_state() -> None:
    extractor = _KnownCharacterCapturingExtractor()
    scenario = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        source_text="새로운 장면이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=1,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")],
            character_facts=[
                _character_state(
                    "STATUS",
                    "status.오른발_부상",
                    "오른발이 심하게 다쳐 걷기 어려움",
                    value_type="JSON",
                    value_json={"active": True},
                ),
                _character_state(
                    "STATUS",
                    "status.마비독",
                    "마비독에 중독됨",
                    value_type="JSON",
                    value_json={"active": True},
                ),
                _character_state(
                    "STATUS",
                    "status.종료됨",
                    "이미 종료된 상태",
                    value_type="JSON",
                    value_json={"active": False},
                ),
                _character_state("PROFILE", "profile.species", "바바리안"),
            ],
        ),
        candidate_free=True,
        known_character_names=[],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="explicit empty fixed context",
        scenarios=[scenario],
    ).with_fixture_hash()

    asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=extractor,
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=_AddCharacterComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    assert extractor.known_character_names == ["비요른"]
    assert extractor.requests[0][0].active_statuses == (
        ActiveCharacterStatus(
            fact_key="status.마비독",
            fact_value="마비독에 중독됨",
        ),
        ActiveCharacterStatus(
            fact_key="status.오른발_부상",
            fact_value="오른발이 심하게 다쳐 걷기 어려움",
        ),
    )


def test_fixed_runtime_preserves_duplicate_names_and_latest_creation_order() -> None:
    extractor = _KnownCharacterCapturingExtractor()
    scenario = ScenarioGold(
        scenario_id="S3",
        episode_no=3,
        source_identifier="03화.txt",
        source_text="새로운 장면이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=2,
        seed_state=EvaluationState(
            known_characters=[
                KnownCharacter(
                    entity_ref="character:older-bjorn",
                    name="비요른",
                    creation_order=1,
                ),
                KnownCharacter(
                    entity_ref="character:newer-bjorn",
                    name="비요른",
                    creation_order=3,
                ),
                KnownCharacter(
                    entity_ref="character:archived",
                    name="은퇴자",
                    creation_order=4,
                    active=False,
                ),
                KnownCharacter(
                    entity_ref="character:misha",
                    name="미샤",
                    creation_order=2,
                ),
            ],
            character_facts=[
                _character_state(
                    "STATUS",
                    "status.오래된_부상",
                    "오래된 비요른의 부상",
                    entity_ref="character:older-bjorn",
                    entity_name="비요른",
                    value_type="JSON",
                    value_json={"active": True},
                ),
                _character_state(
                    "STATUS",
                    "status.새로운_부상",
                    "새로운 비요른의 부상",
                    entity_ref="character:newer-bjorn",
                    entity_name="비요른",
                    value_type="JSON",
                    value_json={"active": True},
                ),
                _character_state(
                    "STATUS",
                    "status.피로",
                    "미샤의 피로",
                    entity_ref="character:misha",
                    entity_name="미샤",
                    value_type="JSON",
                    value_json={"active": True},
                ),
                _character_state(
                    "STATUS",
                    "status.은퇴",
                    "보내면 안 되는 상태",
                    entity_ref="character:archived",
                    entity_name="은퇴자",
                    value_type="JSON",
                    value_json={"active": True},
                ),
            ],
        ),
        candidate_free=True,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="runtime known character ordering",
        scenarios=[scenario],
    ).with_fixture_hash()

    asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=extractor,
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=_AddCharacterComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    assert extractor.known_character_names == ["비요른", "미샤", "비요른"]
    assert [
        tuple(status.fact_key for status in character.active_statuses)
        for character in extractor.requests[0]
    ] == [
        ("status.새로운_부상",),
        ("status.피로",),
        ("status.오래된_부상",),
    ]


def test_rolling_runtime_passes_only_previous_predicted_active_statuses() -> None:
    injury_ref = character_state_ref(
        "character:bjorn",
        "STATUS",
        "status.오른발_부상",
    )
    extractor = _RollingStatusExtractor()
    first = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 부상이 악화되었지만 완전히 회복했다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")],
            character_facts=[
                _character_state(
                    "STATUS",
                    "status.오른발_부상",
                    "오른발이 심하게 다쳐 걷기 어려움",
                    value_type="JSON",
                    value_json={"active": True},
                )
            ],
        ),
        candidate_free=True,
        review_status="FINAL",
    )
    second = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        source_text="비요른은 길을 걸었다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="PREVIOUS_GOLD",
        previous_scenario_id="S1",
        cumulative_through_episode=1,
        candidate_free=True,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="rolling active status context",
        scenarios=[first, second],
    ).with_fixture_hash()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="ROLLING",
            components=RuntimeComponents(
                character_extractor=extractor,
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=_ProjectedStatusComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_status_schema_hint(),),
            domains={"CHARACTER"},
        )
    )

    assert extractor.status_keys_by_episode == {
        1: [("status.오른발_부상",)],
        2: [()],
    }
    assert bundle.scenarios[0].stage2[1].removed_snapshot_refs == [injury_ref]
    assert bundle.state_application_policy == "ACCEPT_ALL_PREDICTIONS"


def test_fixed_runtime_orders_batch_by_evidence_and_maps_q_refs() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 다쳤지만 곧 완전히 회복했다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        review_status="FINAL",
    )
    comparator = _AddThenRemoveProjectedStatusComparator()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            GoldSnapshotV3(
                dataset_version="v3",
                name="fixed projected batch",
                scenarios=[scenario],
            ).with_fixture_hash(),
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_SameChunkStatusExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=comparator,
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_status_schema_hint(),),
            domains={"CHARACTER"},
        )
    )

    assert comparator.calls == [(["C1", "C2"], [])]
    first, second = bundle.scenarios[0].stage2
    injury_ref = character_state_ref("character:bjorn", "STATUS", "status.부상")
    assert first.resolved_canonical_fact_key == "status.부상"
    assert second.removed_snapshot_refs == [injury_ref]
    stage1 = bundle.scenarios[0].stage1
    assert stage1[0].display_value == "완전히 회복함"
    assert [item.source_candidate_id for item in bundle.scenarios[0].stage2] == [
        stage1[1].candidate_id,
        stage1[0].candidate_id,
    ]


def test_fixed_runtime_tracks_remove_before_same_slot_add_as_absence_dependency(
    monkeypatch,
) -> None:
    status_ref = character_state_ref("character:bjorn", "STATUS", "status.부상")
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른의 부상이 회복되었지만 곧 다시 같은 부상을 입었다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")],
            character_facts=[
                _character_state(
                    "STATUS",
                    "status.부상",
                    "기존 부상",
                    value_type="JSON",
                    value_json={"name": "부상", "active": True},
                )
            ],
        ),
        candidate_free=True,
        review_status="FINAL",
    )
    comparator = _RemoveThenAddSameSlotComparator()
    executions = []
    production_runner = runtime_adapter_module.execute_character_fact_comparison_batch

    async def capture_execution(*args, **kwargs):
        execution = await production_runner(*args, **kwargs)
        executions.append(execution)
        return execution

    monkeypatch.setattr(
        runtime_adapter_module,
        "execute_character_fact_comparison_batch",
        capture_execution,
    )

    bundle = asyncio.run(
        run_multi_stage_predictions(
            GoldSnapshotV3(
                dataset_version="v3",
                name="same-slot absence dependency",
                scenarios=[scenario],
            ).with_fixture_hash(),
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_RemoveThenAddSameSlotExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=comparator,
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_status_schema_hint(),),
            domains={"CHARACTER"},
        )
    )

    assert [item.operation for item in bundle.scenarios[0].stage2] == ["REMOVE", "ADD"]
    assert bundle.scenarios[0].stage2[0].removed_snapshot_refs == [status_ref]
    assert executions[0].decisions[1].dependency_candidate_refs == ["C1"]

    # C2's ADD is valid only after C1 removed the persisted slot. Replaying C2
    # without its dependency must remain an invalid transition, not a silent overwrite.
    state_without_c1 = CharacterProjectionState(
        [
            CharacterProjectionEntry(
                reference="P1",
                fact_type="STATUS",
                fact_key="status.부상",
                fact_value="기존 부상",
                value_json={"name": "부상", "active": True},
            )
        ]
    )
    with pytest.raises(ValueError, match="ADD is invalid"):
        state_without_c1.apply(
            candidate_ref="C2",
            projected_snapshot_ref="Q2",
            fact_type="STATUS",
            resolved_fact_key="status.부상",
            value_type="JSON",
            candidate_value_json={"name": "부상", "active": True},
            decision=comparator.result.decisions[1],
        )


def test_character_batches_stay_scenario_local_when_evaluation_batch_matches() -> None:
    comparator = _BatchCapturingCharacterComparator()
    first = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 바바리안이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        evaluation_batch_id="upload-batch-1",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        review_status="FINAL",
    )
    second = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        source_text="비요른은 다시 바바리안으로 불렸다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        evaluation_batch_id="upload-batch-1",
        start_state_mode="PREVIOUS_GOLD",
        previous_scenario_id="S1",
        cumulative_through_episode=1,
        candidate_free=True,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="same batch prior chronology",
        scenarios=[first, second],
    ).with_fixture_hash()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_LiveCharacterExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=comparator,
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    assert comparator.calls == [(["C1"], []), (["C1"], [])]
    assert bundle.state_application_policy == "SCENARIO_LOCAL"


def test_live_runtime_keeps_raw_stage1_when_subject_resolution_fails() -> None:
    world_extractor = _CallCountingWorldExtractor()
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 바바리안이다.",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        known_character_names=["비요른"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="subject failure raw boundary",
        scenarios=[scenario],
    ).with_fixture_hash()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_LiveCharacterExtractor(),
                character_subject_resolver=_FailingSubjectResolver(),
                world_extractor=world_extractor,
                character_comparator=_AddCharacterComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    prediction = bundle.scenarios[0]
    assert len(prediction.raw_stage1) == 2
    assert prediction.stage1 == []
    assert prediction.stage2 == []
    assert len(prediction.failures) == 1
    assert prediction.failures[0].error_type == "LlmResponseValidationError"
    assert prediction.pipeline_status == "PIPELINE_FAILED"
    assert prediction.failed_stage == "CHARACTER_STAGE1"
    assert world_extractor.calls == 0


def test_live_runtime_canonicalizes_schema_alias_before_comparison() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 바바리안이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        known_character_names=["비요른"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="schema alias",
        scenarios=[scenario],
    ).with_fixture_hash()
    comparator = _CanonicalKeyCapturingComparator()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_AliasCharacterExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=comparator,
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    prediction = bundle.scenarios[0]
    assert prediction.raw_stage1[0].fact_key == "종족"
    assert prediction.stage1[0].fact_key == "profile.species"
    assert comparator.canonical_fact_key == "profile.species"
    assert comparator.raw_fact_key == "종족"
    assert comparator.canonical_key_resolution == "ALIAS"


def test_live_runtime_uses_explicit_fact_type_for_work_specific_schema() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른의 길드 계급은 철급이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        known_character_names=["비요른"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="work-specific schema fact type",
        scenarios=[scenario],
    ).with_fixture_hash()
    comparator = _CanonicalKeyCapturingComparator()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_CustomSchemaCharacterExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=comparator,
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(
                CharacterSettingSchemaHint(
                    schema_key="guild.rank",
                    display_name="길드 계급",
                    attribute_pattern=None,
                    aliases=("계급",),
                    value_type="STRING",
                    canonical_fact_type="PROFILE",
                ),
            ),
        )
    )

    assert bundle.scenarios[0].stage1[0].fact_key == "guild.rank"
    assert bundle.scenarios[0].stage1[0].fact_type == "PROFILE"
    assert comparator.canonical_fact_type == "PROFILE"


def test_live_runtime_passes_episode_title_to_both_extractors() -> None:
    character_extractor = _MetadataCapturingCharacterExtractor()
    world_extractor = _MetadataCapturingWorldExtractor()
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        episode_title="게임 속으로",
        source_identifier="01화.txt",
        source_text="첫 장면이다.",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        candidate_free=True,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="episode metadata parity",
        scenarios=[scenario],
    ).with_fixture_hash()

    asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=character_extractor,
                character_subject_resolver=_PassThroughSubjectResolver(),
                world_extractor=world_extractor,
                character_comparator=_AddCharacterComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    assert character_extractor.episode_title == "게임 속으로"
    assert world_extractor.episode_title == "게임 속으로"


def test_live_runtime_quarantines_candidate_that_does_not_match_schema() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른의 알 수 없는 설정이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        candidate_free=True,
        known_character_names=["비요른"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="schema quarantine",
        scenarios=[scenario],
    ).with_fixture_hash()

    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="FIXED",
            components=RuntimeComponents(
                character_extractor=_UnknownSchemaCharacterExtractor(),
                character_subject_resolver=_PassThroughSubjectResolver(),
                character_comparator=_AddCharacterComparator(),
                world_comparator=_AddWorldComparator(),
            ),
            character_schema_hints=(_character_schema_hint(),),
        )
    )

    prediction = bundle.scenarios[0]
    assert len(prediction.raw_stage1) == 1
    assert prediction.stage1 == []
    assert prediction.stage2 == []
    assert prediction.failures[0].error_type == "ValueError"


def test_character_batch_snapshot_context_matches_spring_slot_selection() -> None:
    profile_height = _character_state("PROFILE", "profile.height", "180")
    profile_species = _character_state("PROFILE", "profile.species", "바바리안")
    injured = _character_state(
        "STATUS",
        "status.부상",
        "오른팔 부상",
        source_episode_no=1,
        source_sort_order=1,
    )
    poisoned = _character_state(
        "STATUS",
        "status.중독",
        "독에 중독됨",
        source_episode_no=2,
        source_sort_order=1,
    )
    exhausted = _character_state(
        "STATUS",
        "status.피로",
        "심한 피로",
        source_episode_no=3,
        source_sort_order=1,
    )
    state = EvaluationState(
        character_facts=[profile_height, profile_species, injured, poisoned, exhausted]
    )

    profile_context, _ = _character_batch_snapshots(
        state,
        "character:bjorn",
        "PROFILE",
        ["profile.height"],
    )
    status_context, _ = _character_batch_snapshots(
        state,
        "character:bjorn",
        "STATUS",
        ["status.부상"],
    )

    assert [item.fact_key for item in profile_context] == [
        "profile.height",
        "profile.species",
    ]
    assert [item.fact_key for item in status_context] == [
        "status.부상",
        "status.중독",
        "status.피로",
    ]
    assert [item.snapshot_ref for item in status_context] == ["P1", "P2", "P3"]


def test_character_batch_snapshot_context_prioritizes_exact_slots_before_cap() -> None:
    state = EvaluationState(
        character_facts=[
            _character_state("PROFILE", f"profile.field_{index:02d}", str(index))
            for index in range(31)
        ]
    )

    context, refs = _character_batch_snapshots(
        state,
        "character:bjorn",
        "PROFILE",
        ["profile.field_30"],
    )

    assert len(context) == 30
    assert context[0].fact_key == "profile.field_30"
    assert "profile.field_29" not in {item.fact_key for item in context}
    assert refs["P1"] == character_state_ref(
        "character:bjorn",
        "PROFILE",
        "profile.field_30",
    )


def test_runtime_usage_wrapper_records_provider_token_counts() -> None:
    usage = RuntimeUsageCounter()
    client = UsageRecordingTextGenerationClient(_UsageClient(), usage)

    response = asyncio.run(client.create_text_response("system", "user"))

    assert response.text == "{}"
    assert usage.snapshot() == (12, 5, 3)


def test_runtime_usage_wrapper_records_token_counts_from_validation_errors() -> None:
    usage = RuntimeUsageCounter()
    client = UsageRecordingTextGenerationClient(_UsageValidationFailingClient(), usage)

    with pytest.raises(LlmResponseValidationError, match="invalid paid response"):
        asyncio.run(client.create_text_response("system", "user"))

    assert usage.snapshot() == (12, 5, 3)


def test_character_schema_hash_is_order_independent_and_includes_fact_type() -> None:
    first = CharacterSettingSchemaHint(
        schema_key="guild.rank",
        display_name="길드 계급",
        attribute_pattern=None,
        aliases=("계급", "등급"),
        value_type="STRING",
        canonical_fact_type="PROFILE",
    )
    second = _character_schema_hint()
    reordered_first = CharacterSettingSchemaHint(
        schema_key="guild.rank",
        display_name="길드 계급",
        attribute_pattern=None,
        aliases=("등급", "계급"),
        value_type="STRING",
        canonical_fact_type="PROFILE",
    )
    different_fact_type = CharacterSettingSchemaHint(
        schema_key="guild.rank",
        display_name="길드 계급",
        attribute_pattern=None,
        aliases=("계급", "등급"),
        value_type="STRING",
        canonical_fact_type="STATUS",
    )

    assert _character_schema_hash((first, second)) == _character_schema_hash(
        (second, reordered_first)
    )
    assert _character_schema_hash((first,)) != _character_schema_hash(
        (different_fact_type,)
    )


def test_oracle_runtime_reraises_provider_http_status_error() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    stage1 = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["비요른은 바바리안이다."],
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
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="fatal provider error",
        scenarios=[scenario],
        stage1=[stage1],
        stage2=[
            CharacterStage2Gold(
                decision_id="D1",
                scenario_id="S1",
                episode_no=1,
                sort_order=1,
                source_gold_ids=["C1"],
                domain="CHARACTER",
                operation="ADD",
                proposed_value="바바리안",
                proposed_value_json={"value": "바바리안"},
                temporal_scope="PRESENT",
                review_status="FINAL",
            )
        ],
    ).with_fixture_hash()

    with pytest.raises(httpx.HTTPStatusError, match="provider rejected request"):
        asyncio.run(
            run_multi_stage_predictions(
                gold,
                mode="ORACLE",
                components=RuntimeComponents(
                    character_comparator=_HttpStatusFailingCharacterComparator(),
                    world_comparator=_OracleWorldComparator(),
                ),
            )
        )


def test_live_runtime_reraises_provider_transport_error() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 바바리안이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        candidate_free=True,
        known_character_names=["비요른"],
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="fatal transport error",
        scenarios=[scenario],
    ).with_fixture_hash()

    with pytest.raises(httpx.ConnectError, match="provider connection failed"):
        asyncio.run(
            run_multi_stage_predictions(
                gold,
                mode="FIXED",
                components=RuntimeComponents(
                    character_extractor=_TransportFailingCharacterExtractor(),
                    character_subject_resolver=_PassThroughSubjectResolver(),
                    character_comparator=_AddCharacterComparator(),
                    world_comparator=_AddWorldComparator(),
                ),
                character_schema_hints=(_character_schema_hint(),),
            )
        )


def test_runtime_reraises_token_quota_exhaustion_from_character_batch() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="비요른은 바바리안이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=EvaluationState(
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
        ),
        candidate_free=True,
        review_status="FINAL",
    )

    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(
            run_multi_stage_predictions(
                GoldSnapshotV3(
                    dataset_version="v3",
                    name="fatal comparison quota",
                    scenarios=[scenario],
                ).with_fixture_hash(),
                mode="FIXED",
                components=RuntimeComponents(
                    character_extractor=_LiveCharacterExtractor(),
                    character_subject_resolver=_PassThroughSubjectResolver(),
                    character_comparator=_QuotaFailingCharacterComparator(),
                    world_comparator=_AddWorldComparator(),
                ),
                character_schema_hints=(_character_schema_hint(),),
            )
        )


def test_runtime_pricing_does_not_charge_cached_tokens_at_full_input_rate() -> None:
    pricing = RuntimePricing(
        input_usd_per_million=Decimal("2"),
        cached_input_usd_per_million=Decimal("0.5"),
        output_usd_per_million=Decimal("8"),
    )

    assert pricing.estimate((1_000_000, 400_000, 100_000)) == Decimal("2.2")


def test_default_runtime_routes_subject_resolution_model_independently(monkeypatch) -> None:
    monkeypatch.setattr(
        OpenAIResponsesClient,
        "from_settings",
        staticmethod(lambda: _UsageClient()),
    )

    components = create_default_runtime_components(
        analysis_model="analysis-model",
        subject_resolution_model="subject-model",
        comparison_model="comparison-model",
    )

    assert components.character_extractor.model == "analysis-model"
    assert components.world_extractor.model == "analysis-model"
    assert components.character_subject_resolver.model == "subject-model"
    assert components.world_subject_resolver.model == "subject-model"
    assert components.character_comparator.model == "comparison-model"
    assert components.world_comparator.model == "comparison-model"


class _OracleCharacterComparator:
    snapshot_fact_keys = None
    calls = None

    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, *, candidates, snapshot_entries, **kwargs):
        self.snapshot_fact_keys = [entry.fact_key for entry in snapshot_entries]
        self.calls = [candidate.candidate_ref for candidate in candidates]
        result = CharacterFactComparisonBatchResult(
            decisions=[
                _batch_decision(
                    candidate,
                    operation="UPDATE",
                    target_ref="P1",
                    value="180",
                    value_json={"value": 180},
                )
                for candidate in candidates
            ]
        )
        return result, result.model_dump(mode="json")


class _ProjectedStatusComparator:
    def __init__(self) -> None:
        self.calls = []

    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, *, candidates, snapshot_entries, **kwargs):
        assert [candidate.canonical_key_resolution for candidate in candidates] == [
            "PATTERN",
            "PATTERN",
        ]
        self.calls.append(
            (
                [candidate.candidate_ref for candidate in candidates],
                [entry.reference for entry in snapshot_entries],
            )
        )
        first, second = candidates
        result = CharacterFactComparisonBatchResult(
            decisions=[
                _batch_decision(
                    first,
                    operation="UPDATE",
                    target_ref="P1",
                    value="오른발 부상이 악화됨",
                    value_json={"name": "오른발 부상", "active": True},
                ),
                _batch_decision(
                    second,
                    operation="REMOVE",
                    removed_refs=["Q1"],
                    resolved_key="status.회복",
                ),
            ]
        )
        return result, result.model_dump(mode="json")


class _AddThenRemoveProjectedStatusComparator:
    def __init__(self) -> None:
        self.calls = []

    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, *, candidates, snapshot_entries, **kwargs):
        assert [candidate.canonical_key_resolution for candidate in candidates] == [
            "PATTERN",
            "PATTERN",
        ]
        self.calls.append(
            (
                [candidate.candidate_ref for candidate in candidates],
                [entry.reference for entry in snapshot_entries],
            )
        )
        first, second = candidates
        result = CharacterFactComparisonBatchResult(
            decisions=[
                _batch_decision(
                    first,
                    operation="ADD",
                    value="다리를 다침",
                    value_json={"name": "부상", "active": True},
                ),
                _batch_decision(
                    second,
                    operation="REMOVE",
                    removed_refs=["Q1"],
                ),
            ]
        )
        return result, result.model_dump(mode="json")


class _HttpStatusFailingCharacterComparator:
    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, **kwargs):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(
            "provider rejected request",
            request=request,
            response=response,
        )


class _QuotaFailingCharacterComparator:
    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, **kwargs):
        raise AiTokenQuotaExhaustedError()


class _OracleWorldComparator:
    target_property_names = None

    async def compare_batch(self, category, candidates, targets):
        self.target_property_names = [
            property.setting_name for target in targets for property in target.properties
        ]
        decision = WorldSettingComparisonBatchDecision(
            source_candidate_refs=[candidate.candidate_ref for candidate in candidates],
            consolidation_status="SINGLE",
            operation="MERGE",
            target_ref="T1",
            matched_property_name="체격",
            proposed_setting_name="체격",
            proposed_value="평균 140cm이며 큰 변종은 드물게 190cm다.",
            comparison_reason="기존 평균과 변종 정보를 합친다.",
        )
        result = WorldSettingComparisonBatchResult(decisions=[decision])
        return result, result.model_dump(mode="json")


class _LiveCharacterExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        candidate = ExtractedCharacterSettingCandidate(
            source_chunk_id=source_chunk_id,
            candidate_kind="SETTING",
            entity_name="비요른",
            raw_entity_mention="비요른은",
            attribute_name="profile.species",
            attribute_value="바바리안",
            value_type="STRING",
            value_json={"value": "바바리안"},
            evidence_spans=[ExtractedEvidenceSpan(quote="비요른은 바바리안이다.")],
            confidence=0.9,
        )
        return CharacterSettingExtractionResult(candidates=[candidate, candidate])


class _AliasCharacterExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name="비요른",
                    attribute_name="종족",
                    attribute_value="바바리안",
                    value_type="STRING",
                    value_json={"value": "바바리안"},
                    evidence_spans=[ExtractedEvidenceSpan(quote="비요른은 바바리안이다.")],
                )
            ]
        )


class _UnknownSchemaCharacterExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name="비요른",
                    attribute_name="unknown.fact",
                    attribute_value="알 수 없음",
                    value_type="STRING",
                    value_json={"value": "알 수 없음"},
                    evidence_spans=[ExtractedEvidenceSpan(quote="비요른의 알 수 없는 설정이다.")],
                )
            ]
        )


class _CustomSchemaCharacterExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name="비요른",
                    attribute_name="guild.rank",
                    attribute_value="철급",
                    value_type="STRING",
                    value_json={"value": "철급"},
                    evidence_spans=[ExtractedEvidenceSpan(quote="길드 계급은 철급이다.")],
                )
            ]
        )


class _MetadataCapturingCharacterExtractor:
    episode_title = None

    async def extract_from_chunk(self, **kwargs):
        self.episode_title = kwargs["episode_title"]
        return CharacterSettingExtractionResult(candidates=[])


class _TransportFailingCharacterExtractor:
    async def extract_from_chunk(self, **kwargs):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        raise httpx.ConnectError("provider connection failed", request=request)


class _PassThroughSubjectResolver:
    async def resolve_candidates(self, *, candidates, **kwargs):
        return SubjectResolutionResult(candidates=candidates)


class _FailingSubjectResolver:
    async def resolve_candidates(self, **kwargs):
        raise LlmResponseValidationError("subject resolver output is invalid")


class _KnownCharacterCapturingExtractor:
    def __init__(self) -> None:
        self.requests = []

    @property
    def known_character_names(self):
        return [character.name for character in self.requests[-1]]

    async def extract_from_chunk(self, **kwargs):
        self.requests.append(kwargs["known_characters"])
        return CharacterSettingExtractionResult(candidates=[])


class _RollingStatusExtractor:
    def __init__(self) -> None:
        self.status_keys_by_episode = {}

    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        episode_no = kwargs["episode_no"]
        self.status_keys_by_episode.setdefault(episode_no, []).append(
            tuple(
                status.fact_key
                for character in kwargs["known_characters"]
                for status in character.active_statuses
            )
        )
        if episode_no != 1:
            return CharacterSettingExtractionResult(candidates=[])
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name="비요른",
                    raw_entity_mention="비요른은",
                    attribute_name="status.오른발_부상",
                    attribute_value="오른발 부상이 악화됨",
                    value_type="JSON",
                    value_json={"name": "오른발 부상", "active": True},
                    evidence_spans=[
                        ExtractedEvidenceSpan(quote="부상이 악화되었지만")
                    ],
                    confidence=0.9,
                ),
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name="비요른",
                    raw_entity_mention="비요른은",
                    attribute_name="status.회복",
                    attribute_value="완전히 회복함",
                    value_type="JSON",
                    value_json={"name": "회복", "active": False},
                    evidence_spans=[ExtractedEvidenceSpan(quote="완전히 회복했다.")],
                    confidence=0.9,
                )
            ]
        )


class _SameChunkStatusExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        common = {
            "source_chunk_id": source_chunk_id,
            "candidate_kind": "SETTING",
            "entity_name": "비요른",
            "raw_entity_mention": "비요른은",
            "value_type": "JSON",
            "confidence": 0.9,
        }
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    **common,
                    attribute_name="status.회복",
                    attribute_value="완전히 회복함",
                    value_json={"name": "회복", "active": False},
                    evidence_spans=[ExtractedEvidenceSpan(quote="곧 완전히 회복했다.")],
                ),
                ExtractedCharacterSettingCandidate(
                    **common,
                    attribute_name="status.부상",
                    attribute_value="다리를 다침",
                    value_json={"name": "부상", "active": True},
                    evidence_spans=[ExtractedEvidenceSpan(quote="비요른은 다쳤지만")],
                ),
            ]
        )


class _RemoveThenAddSameSlotExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        common = {
            "source_chunk_id": source_chunk_id,
            "candidate_kind": "SETTING",
            "entity_name": "비요른",
            "raw_entity_mention": "비요른은",
            "value_type": "JSON",
            "confidence": 0.9,
        }
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    **common,
                    attribute_name="status.회복",
                    attribute_value="기존 부상이 회복됨",
                    value_json={"name": "회복", "active": False},
                    evidence_spans=[
                        ExtractedEvidenceSpan(quote="부상이 회복되었지만")
                    ],
                ),
                ExtractedCharacterSettingCandidate(
                    **common,
                    attribute_name="status.재부상",
                    attribute_value="같은 부상을 다시 입음",
                    value_json={"name": "부상", "active": True},
                    evidence_spans=[
                        ExtractedEvidenceSpan(quote="다시 같은 부상을 입었다.")
                    ],
                ),
            ]
        )


class _LiveWorldExtractor:
    async def extract_from_chunk(self, **kwargs):
        common = {
            "category": "RACE",
            "subject_name": "고블린",
            "scope_name": None,
            "setting_name": "체격",
            "confidence": 0.8,
        }
        return WorldSettingExtractionResult(
            candidates=[
                ExtractedWorldSettingCandidate(
                    **common,
                    extracted_value="평균 140cm다.",
                    evidence_spans=[ExtractedEvidenceSpan(quote="고블린은 작고")],
                ),
                ExtractedWorldSettingCandidate(
                    **common,
                    extracted_value="큰 변종은 드물게 190cm다.",
                    evidence_spans=[ExtractedEvidenceSpan(quote="큰 변종도 있다")],
                ),
            ]
        )


class _IndependentWorldPropertiesExtractor:
    async def extract_from_chunk(self, **kwargs):
        common = {
            "category": "MONSTER",
            "subject_name": "고블린",
            "scope_name": "전투 특성",
            "confidence": 0.8,
        }
        return WorldSettingExtractionResult(
            candidates=[
                ExtractedWorldSettingCandidate(
                    **common,
                    setting_name="함정 사용",
                    extracted_value="함정을 설치한다.",
                    evidence_spans=[ExtractedEvidenceSpan(quote="함정을 설치하고")],
                ),
                ExtractedWorldSettingCandidate(
                    **common,
                    setting_name="매복 습성",
                    extracted_value="함정 주변에 매복한다.",
                    evidence_spans=[ExtractedEvidenceSpan(quote="주변에 매복한다")],
                ),
            ]
        )


class _MetadataCapturingWorldExtractor:
    episode_title = None

    async def extract_from_chunk(self, **kwargs):
        self.episode_title = kwargs["episode_title"]
        return WorldSettingExtractionResult(candidates=[])


class _CallCountingWorldExtractor:
    calls = 0

    async def extract_from_chunk(self, **kwargs):
        self.calls += 1
        return WorldSettingExtractionResult(candidates=[])


class _AddCharacterComparator:
    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, *, candidates, **kwargs):
        result = CharacterFactComparisonBatchResult(
            decisions=[
                _batch_decision(
                    candidate,
                    operation="ADD",
                    value=candidate.attribute_value or "현재 설정",
                    value_json=candidate.value_json,
                )
                for candidate in candidates
            ]
        )
        return result, result.model_dump(mode="json")


class _CanonicalKeyCapturingComparator(_AddCharacterComparator):
    canonical_fact_key = None
    canonical_fact_type = None
    raw_fact_key = None
    canonical_key_resolution = None

    async def compare_batch(self, *, candidates, canonical_fact_type, **kwargs):
        self.canonical_fact_key = candidates[0].initial_canonical_fact_key
        self.canonical_fact_type = canonical_fact_type
        self.raw_fact_key = candidates[0].raw_fact_key
        self.canonical_key_resolution = candidates[0].canonical_key_resolution
        return await super().compare_batch(candidates=candidates, **kwargs)


class _BatchCapturingCharacterComparator(_AddCharacterComparator):
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []

    async def compare_batch(self, *, candidates, snapshot_entries, **kwargs):
        self.calls.append(
            (
                [candidate.candidate_ref for candidate in candidates],
                [entry.reference for entry in snapshot_entries],
            )
        )
        return await super().compare_batch(candidates=candidates, **kwargs)


class _RemoveThenAddSameSlotComparator:
    def __init__(self) -> None:
        self.result = None

    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, *, candidates, snapshot_entries, **kwargs):
        assert [candidate.candidate_ref for candidate in candidates] == ["C1", "C2"]
        assert [entry.reference for entry in snapshot_entries] == ["P1"]
        first, second = candidates
        self.result = CharacterFactComparisonBatchResult(
            decisions=[
                _batch_decision(
                    first,
                    operation="REMOVE",
                    removed_refs=["P1"],
                    resolved_key="status.부상",
                ),
                _batch_decision(
                    second,
                    operation="ADD",
                    value="같은 부상을 다시 입음",
                    value_json={"name": "부상", "active": True},
                    resolved_key="status.부상",
                ),
            ]
        )
        return self.result, self.result.model_dump(mode="json")


class _AddWorldComparator:
    async def compare_batch(self, category, candidates, targets):
        result = WorldSettingComparisonBatchResult(
            decisions=[
                WorldSettingComparisonBatchDecision(
                    source_candidate_refs=[candidate.candidate_ref],
                    consolidation_status="MERGED",
                    operation="ADD",
                    proposed_setting_name=candidate.setting_name,
                    proposed_value=candidate.extracted_value,
                    comparison_reason="원문 정보를 현재 세계관 설정으로 정리합니다.",
                )
                for candidate in candidates
            ]
        )
        return result, result.model_dump(mode="json")


class _BatchCapturingWorldComparator(_AddWorldComparator):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def compare_batch(self, category, candidates, targets):
        self.calls.append([candidate.setting_name for candidate in candidates])
        return await super().compare_batch(category, candidates, targets)


class _IncompleteWorldComparator:
    async def compare_batch(self, category, candidates, targets):
        raise LlmIncompleteResponseError("provider incomplete")


class _UsageClient:
    async def create_text_response(self, **kwargs):
        return LlmTextResponse(
            text="{}",
            input_token_count=12,
            cached_input_token_count=5,
            output_token_count=3,
        )


class _UsageValidationFailingClient:
    async def create_text_response(self, **kwargs):
        raise LlmResponseValidationError(
            "invalid paid response",
            input_token_count=12,
            cached_input_token_count=5,
            output_token_count=3,
        )


def _character_gold(
    gold_id: str,
    fact_key: str,
    value: str,
    value_json: dict,
    *,
    sort_order: int,
) -> CharacterStage1Gold:
    return CharacterStage1Gold(
        gold_id=gold_id,
        scenario_id="S5",
        episode_no=5,
        sort_order=sort_order,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=[value],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="STATUS",
        fact_key=fact_key,
        value_type="JSON",
        display_value=value,
        value_json=value_json,
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )


def _batch_decision(
    candidate,
    *,
    operation: str,
    target_ref: str | None = None,
    removed_refs: list[str] | None = None,
    value: str | None = None,
    value_json: dict | None = None,
    resolved_key: str | None = None,
) -> CharacterFactComparisonBatchDecision:
    return CharacterFactComparisonBatchDecision(
        candidate_ref=candidate.candidate_ref,
        operation=operation,
        resolved_canonical_fact_key=(
            resolved_key or candidate.initial_canonical_fact_key
        ),
        target_ref=target_ref,
        removed_snapshot_refs=removed_refs or [],
        proposed_fact_value=value,
        proposed_value_json=value_json,
        temporal_scope="PRESENT",
        comparison_reason="현재 사실을 기준으로 설정을 반영합니다.",
    )


def _character_schema_hint() -> CharacterSettingSchemaHint:
    return CharacterSettingSchemaHint(
        schema_key="profile.species",
        display_name="종족",
        attribute_pattern=None,
        aliases=("종족",),
        value_type="STRING",
    )


def _status_schema_hint() -> CharacterSettingSchemaHint:
    return CharacterSettingSchemaHint(
        schema_key="statuses.condition",
        display_name="상태",
        attribute_pattern="status.*",
        aliases=(),
        value_type="JSON",
        canonical_fact_type="STATUS",
    )


def _character_state(
    fact_type: str,
    fact_key: str,
    value: str,
    *,
    entity_ref: str = "character:bjorn",
    entity_name: str = "비요른",
    value_type: str = "STRING",
    value_json: dict | None = None,
    source_episode_no: int | None = None,
    source_sort_order: int | None = None,
) -> CharacterStateEntry:
    return CharacterStateEntry(
        ref=character_state_ref(entity_ref, fact_type, fact_key),
        entity_ref=entity_ref,
        entity_name=entity_name,
        fact_type=fact_type,
        fact_key=fact_key,
        value_type=value_type,
        value=value,
        value_json={"value": value} if value_json is None else value_json,
        source_episode_no=source_episode_no,
        source_sort_order=source_sort_order,
    )
