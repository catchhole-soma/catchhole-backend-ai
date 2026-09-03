import asyncio
from decimal import Decimal

import httpx
import pytest

from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonDecision
from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    ExtractedEvidenceSpan,
    ExtractedSettingCandidate,
)
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from app.analysis.world_setting_schemas import (
    ExtractedWorldSettingCandidate,
    WorldSettingComparisonDecision,
    WorldSettingExtractionResult,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.exceptions import LlmResponseValidationError
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerCharacterPriorFactCandidate
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
    _bounded_prior_candidates,
    _character_schema_hash,
    _character_snapshots,
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
            known_characters=[KnownCharacter(entity_ref="character:bjorn", name="비요른")]
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
            ]
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


def test_character_comparison_reuses_same_batch_priors_and_caps_context() -> None:
    comparator = _PriorCapturingCharacterComparator()
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

    assert comparator.prior_counts == [0, 1]
    assert bundle.state_application_policy == "SCENARIO_LOCAL"


def test_character_prior_context_keeps_only_the_latest_thirty() -> None:
    priors = [
        WorkerCharacterPriorFactCandidate(
            source_episode_no=episode_no,
            attribute_name="profile.species",
            comparison_status="COMPLETED",
        )
        for episode_no in range(1, 32)
    ]

    bounded = _bounded_prior_candidates(priors)

    assert len(bounded) == 30
    assert bounded[0].source_episode_no == 2
    assert bounded[-1].source_episode_no == 31


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


def test_character_snapshot_context_matches_spring_slot_selection() -> None:
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

    profile_context, _ = _character_snapshots(
        state,
        "character:bjorn",
        "PROFILE",
        "profile.height",
    )
    status_context, _ = _character_snapshots(
        state,
        "character:bjorn",
        "STATUS",
        "status.부상",
    )

    assert [item.fact_key for item in profile_context] == ["profile.height"]
    assert [item.fact_key for item in status_context] == [
        "status.부상",
        "status.피로",
        "status.중독",
    ]


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

    async def compare(self, candidate, snapshot_entries, prior_candidates=None):
        self.snapshot_fact_keys = [entry.fact_key for entry in snapshot_entries]
        decision = CharacterFactComparisonDecision(
            operation="UPDATE",
            target_ref="P1",
            proposed_fact_value="180",
            proposed_value_json={"value": 180},
            temporal_scope="PRESENT",
            comparison_reason="기존 키 정보를 새 수치로 바꾼다.",
        )
        return decision, decision.model_dump(mode="json")


class _HttpStatusFailingCharacterComparator:
    async def compare(self, candidate, snapshot_entries, prior_candidates=None):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(
            "provider rejected request",
            request=request,
            response=response,
        )


class _OracleWorldComparator:
    target_property_names = None

    async def compare(self, candidate, targets):
        self.target_property_names = [
            property.setting_name for target in targets for property in target.properties
        ]
        decision = WorldSettingComparisonDecision(
            consolidation_status="SINGLE",
            operation="MERGE",
            target_ref="T1",
            matched_property_name="체격",
            proposed_setting_name="체격",
            proposed_value="평균 140cm이며 큰 변종은 드물게 190cm다.",
            comparison_reason="기존 평균과 변종 정보를 합친다.",
        )
        return decision, decision.model_dump(mode="json")


class _LiveCharacterExtractor:
    async def extract_from_chunk(self, source_chunk_id, **kwargs):
        candidate = ExtractedSettingCandidate(
            source_chunk_id=source_chunk_id,
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
                ExtractedSettingCandidate(
                    source_chunk_id=source_chunk_id,
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
                ExtractedSettingCandidate(
                    source_chunk_id=source_chunk_id,
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
                ExtractedSettingCandidate(
                    source_chunk_id=source_chunk_id,
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
    known_character_names = None

    async def extract_from_chunk(self, **kwargs):
        self.known_character_names = [character.name for character in kwargs["known_characters"]]
        return CharacterSettingExtractionResult(candidates=[])


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
    async def compare(self, candidate, snapshot_entries, prior_candidates=None):
        decision = CharacterFactComparisonDecision(
            operation="ADD",
            proposed_fact_value="바바리안",
            proposed_value_json={"value": "바바리안"},
            temporal_scope="PRESENT",
            comparison_reason="새로 확인된 종족을 반영한다.",
        )
        return decision, decision.model_dump(mode="json")


class _CanonicalKeyCapturingComparator(_AddCharacterComparator):
    canonical_fact_key = None
    canonical_fact_type = None

    async def compare(self, candidate, snapshot_entries, prior_candidates=None):
        self.canonical_fact_key = candidate.canonical_fact_key
        self.canonical_fact_type = candidate.canonical_fact_type
        return await super().compare(candidate, snapshot_entries, prior_candidates)


class _PriorCapturingCharacterComparator(_AddCharacterComparator):
    def __init__(self) -> None:
        self.prior_counts: list[int] = []

    async def compare(self, candidate, snapshot_entries, prior_candidates=None):
        self.prior_counts.append(len(prior_candidates or []))
        return await super().compare(candidate, snapshot_entries, prior_candidates)


class _AddWorldComparator:
    async def compare(self, candidate, targets):
        decision = WorldSettingComparisonDecision(
            consolidation_status="MERGED",
            operation="ADD",
            proposed_setting_name="체격",
            proposed_value="평균 140cm이며 큰 변종은 드물게 190cm다.",
            comparison_reason="두 원문 정보를 하나의 설정으로 합친다.",
        )
        return decision, decision.model_dump(mode="json")


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


def _character_schema_hint() -> CharacterSettingSchemaHint:
    return CharacterSettingSchemaHint(
        schema_key="profile.species",
        display_name="종족",
        attribute_pattern=None,
        aliases=("종족",),
        value_type="STRING",
    )


def _character_state(
    fact_type: str,
    fact_key: str,
    value: str,
    *,
    source_episode_no: int | None = None,
    source_sort_order: int | None = None,
) -> CharacterStateEntry:
    return CharacterStateEntry(
        ref=character_state_ref("character:bjorn", fact_type, fact_key),
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type=fact_type,
        fact_key=fact_key,
        value_type="STRING",
        value=value,
        value_json={"value": value},
        source_episode_no=source_episode_no,
        source_sort_order=source_sort_order,
    )
