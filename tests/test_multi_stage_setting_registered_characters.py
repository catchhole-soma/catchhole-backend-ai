import asyncio
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    ExtractedCharacterSettingCandidate,
    ExtractedEvidenceSpan,
)
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    PredictionBundleV3,
    ScenarioGold,
    ScenarioPrediction,
    character_state_ref,
)
from evals.multi_stage_setting.evaluator import (
    _register_prediction_discoveries,
    evaluate_multi_stage,
)
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.runtime_adapter import (
    RuntimeComponents,
    _apply_runtime_scenario,
    run_multi_stage_predictions,
)
from evals.multi_stage_setting.state_effects import (
    StateApplicationError,
    apply_registered_characters_after_episode,
    build_gold_state_chain,
)

CHARACTER_REF = "character:bjorn-yandel"
CHARACTER_NAME = "비요른 얀델"


def test_episode_end_registration_keeps_original_waiting_gold_and_empty_before_state() -> None:
    gold = _gold()
    original_source = gold.stage1[0].model_dump()

    chain = build_gold_state_chain(gold)

    first = chain["S1"]
    assert first.before_state == EvaluationState()
    assert first.after_state.character_facts == []
    assert first.after_state.character_history == []
    assert first.applied_decision_ids == ()
    assert first.after_state.known_characters == [
        KnownCharacter(
            entity_ref=CHARACTER_REF, name=CHARACTER_NAME, creation_order=1_000_001
        )
    ]
    assert chain["S2"].before_state == first.after_state
    assert gold.scenarios[0].start_state_mode == "EMPTY"
    assert gold.stage1[0].model_dump() == original_source
    assert gold.stage1[0].entity_name == "미상"
    assert gold.stage1[0].stage2_policy == "WAIT_FOR_CHARACTER_MATCH"
    assert gold.stage2 == []


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING"])
def test_next_episode_receives_registered_name_and_runtime_evaluator_hashes_agree(mode) -> None:
    gold = _gold()
    extractor = _CapturingExtractor()
    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode=mode,
            components=RuntimeComponents(
                character_extractor=extractor,
                character_subject_resolver=_SubjectResolver(),
                character_comparator=_UnusedComparator(),
                world_comparator=_UnusedComparator(),
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
        )
    )
    raw_predictions = bundle.model_dump()

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert extractor.known_names == {1: (), 2: (CHARACTER_NAME,)}
    assert bundle.scenarios[0].stage1[0].entity_name == "미상"
    assert bundle.scenarios[0].stage1[0].entity_ref is None
    assert bundle.scenarios[0].stage2 == []
    assert not any(prediction.failures for prediction in bundle.scenarios)
    assert bundle.model_dump() == raw_predictions
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]["result"] == "FULL_MATCH"
    assert report["stages"]["character"]["stage2"]["counts"]["gold"] == 0
    assert report["endToEnd"]["counts"]["expectedTransitions"] == 0
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 0
    assert report["endToEnd"]["scenarios"][0]["afterStateF1"] is None
    assert report["endToEnd"]["scenarios"][1]["afterStateF1"] == 1
    runtime_state = EvaluationState()
    chain = build_gold_state_chain(gold)
    for scenario, prediction, row in zip(
        gold.scenarios, bundle.scenarios, report["endToEnd"]["scenarios"], strict=True
    ):
        before = runtime_state if mode == "ROLLING" else chain[scenario.scenario_id].before_state
        runtime_state = _apply_runtime_scenario(scenario, before, prediction)
        assert row["predictedStateHash"] == runtime_state.content_hash()
        assert row["expectedStateHash"] == row["predictedStateHash"]


def test_external_registration_without_any_prediction_is_not_a_model_success() -> None:
    gold = _gold(first_only=True)
    bundle = PredictionBundleV3(
        fixture_hash=gold.fixture_hash,
        mode="FIXED",
        scenarios=[ScenarioPrediction(scenario_id="S1")],
    )

    report = asyncio.run(evaluate_multi_stage(gold, bundle))

    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == 0
    assert report["endToEnd"]["metrics"]["afterStateF1"] is None
    assert report["endToEnd"]["counts"]["expectedTransitions"] == 0
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 0
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    row = report["endToEnd"]["scenarios"][0]
    assert row["expectedStateHash"] == row["predictedStateHash"]


def test_distinct_prediction_id_with_same_name_remains_a_scorable_extra() -> None:
    gold = _gold(first_only=True)
    prediction = ScenarioPrediction(
        scenario_id="S1",
        stage1=[
            CharacterStage1Prediction(
                candidate_id="raw-discovery",
                sort_order=7,
                domain="CHARACTER",
                candidate_kind="CHARACTER_DISCOVERY",
                entity_ref="prediction:distinct-character",
                entity_name=CHARACTER_NAME,
            )
        ],
    )
    runtime_state = _apply_runtime_scenario(gold.scenarios[0], EvaluationState(), prediction)

    report = asyncio.run(
        evaluate_multi_stage(
            gold,
            PredictionBundleV3(fixture_hash=gold.fixture_hash, mode="FIXED", scenarios=[prediction]),
        )
    )

    assert {item.entity_ref for item in runtime_state.known_characters} == {
        CHARACTER_REF, "prediction:distinct-character"
    }
    assert report["endToEnd"]["counts"]["expectedTransitions"] == 0
    assert report["endToEnd"]["counts"]["predictedTransitions"] == 1
    assert report["endToEnd"]["counts"]["matchedTransitions"] == 0
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 0
    assert report["endToEnd"]["scenarios"][0]["predictedStateHash"] == runtime_state.content_hash()
    assert {item.entity_ref: item.creation_order for item in runtime_state.known_characters} == {
        "prediction:distinct-character": 1_000_007,
        CHARACTER_REF: 1_000_008,
    }


@pytest.mark.parametrize("source_order", [35, None])
def test_fact_provenance_and_episode_end_registration_have_runtime_evaluator_hash_parity(
    source_order,
) -> None:
    seed = EvaluationState(character_facts=[_existing_fact(source_order)])
    base = _gold(first_only=True)
    scenario = ScenarioGold.model_validate(
        base.scenarios[0].model_dump() | {"start_state_mode": "SEED", "seed_state": seed}
    )
    gold = base.model_copy(update={"scenarios": [scenario]}).with_fixture_hash()
    prediction = ScenarioPrediction(scenario_id="S1")

    runtime_state = _apply_runtime_scenario(scenario, seed, prediction)
    report = asyncio.run(
        evaluate_multi_stage(
            gold,
            PredictionBundleV3(fixture_hash=gold.fixture_hash, mode="FIXED", scenarios=[prediction]),
        )
    )

    assert runtime_state.character_facts == seed.character_facts
    assert {item.entity_ref: item.creation_order for item in runtime_state.known_characters} == {
        "character:existing": None if source_order is None else 1_000_035,
        CHARACTER_REF: 1_000_001 if source_order is None else 1_000_036,
    }
    row = report["endToEnd"]["scenarios"][0]
    assert row["expectedStateHash"] == row["predictedStateHash"] == runtime_state.content_hash()


def test_without_episode_end_registration_evaluator_preserves_legacy_creation_metadata() -> None:
    state = EvaluationState(character_facts=[_existing_fact(35)])
    prediction = CharacterStage1Prediction(
        candidate_id="raw-discovery",
        sort_order=7,
        domain="CHARACTER",
        candidate_kind="CHARACTER_DISCOVERY",
        entity_ref="prediction:distinct-character",
        entity_name=CHARACTER_NAME,
    )

    after = _register_prediction_discoveries(state, _scenario(), [prediction], {})

    assert {item.entity_ref for item in after.known_characters} == {
        "character:existing", "prediction:distinct-character"
    }
    assert all(item.creation_order is None for item in after.known_characters)
    assert after.character_facts == state.character_facts


def test_registration_orders_after_existing_entries_and_preserves_same_identity() -> None:
    scenario = _scenario(
        registered_characters_after_episode=[
            {"entityRef": CHARACTER_REF, "name": CHARACTER_NAME},
            {"entityRef": "character:new", "name": "새 인물"},
        ]
    )
    existing = KnownCharacter(entity_ref=CHARACTER_REF, name=CHARACTER_NAME, creation_order=12)
    state = EvaluationState(
        known_characters=[
            existing,
            KnownCharacter(entity_ref="character:discovered", name="발견 인물", creation_order=1_000_035),
        ]
    )

    after = apply_registered_characters_after_episode(state, scenario)

    assert state.known_characters == after.known_characters[:2]
    assert after.known_characters[0] is existing
    assert after.known_characters[-1].creation_order == 1_000_036
    assert apply_registered_characters_after_episode(after, scenario) == after
    assert after.character_facts == state.character_facts


@pytest.mark.parametrize("name,active", [("다른 이름", True), (CHARACTER_NAME, False)])
def test_registration_rejects_conflicting_existing_identity(name, active) -> None:
    state = EvaluationState(
        known_characters=[KnownCharacter(entity_ref=CHARACTER_REF, name=name, active=active)]
    )

    with pytest.raises(StateApplicationError, match="registration conflicts"):
        apply_registered_characters_after_episode(state, _gold().scenarios[0])


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_empty_registration_metadata_preserves_legacy_fixture_hash(tmp_path, explicit_empty) -> None:
    scenario = _scenario(**({"registeredCharactersAfterEpisode": []} if explicit_empty else {}))
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="legacy registration fixture",
        scenarios=[scenario],
        stage1=[],
        stage2=[],
    ).with_fixture_hash()
    payload = gold.model_dump(mode="json", by_alias=True)

    assert "registeredCharactersAfterEpisode" not in payload["scenarios"][0]
    legacy_payload = {key: value for key, value in payload.items() if key != "fixtureHash"}
    assert gold.fixture_hash == hashlib.sha256(
        json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert load_gold_snapshot_v3(path).fixture_hash == gold.fixture_hash


@pytest.mark.parametrize(
    "registrations",
    [
        [{"entityRef": " ", "name": CHARACTER_NAME}],
        [{"entityRef": CHARACTER_REF, "name": "\t"}],
        [{"entityRef": CHARACTER_REF, "name": CHARACTER_NAME, "active": False}],
        [{"entityRef": CHARACTER_REF, "name": CHARACTER_NAME}] * 2,
    ],
)
def test_registration_metadata_rejects_invalid_entries(registrations) -> None:
    with pytest.raises(ValidationError, match="registeredCharactersAfterEpisode"):
        _scenario(registeredCharactersAfterEpisode=registrations)


def test_registration_metadata_serializes_with_camel_case_and_round_trips() -> None:
    scenario = _gold().scenarios[0]
    payload = scenario.model_dump(mode="json", by_alias=True)

    assert payload["registeredCharactersAfterEpisode"][0]["entityRef"] == CHARACTER_REF
    assert payload["registeredCharactersAfterEpisode"][0]["name"] == CHARACTER_NAME
    assert ScenarioGold.model_validate(payload).model_dump(mode="json", by_alias=True) == payload


def _existing_fact(source_order) -> CharacterStateEntry:
    return CharacterStateEntry(
        ref=character_state_ref("character:existing", "PROFILE", "profile.species"),
        entity_ref="character:existing",
        entity_name="기존 인물",
        fact_type="PROFILE",
        fact_key="profile.species",
        value_type="STRING",
        value="인간",
        source_episode_no=1 if source_order is not None else None,
        source_sort_order=source_order,
    )


def _scenario(**changes) -> ScenarioGold:
    return ScenarioGold.model_validate(
        {
            "scenario_id": "S1",
            "episode_no": 1,
            "source_identifier": "01화.txt",
            "source_text": "나는 바바리안이다.",
            "target_domains": {"CHARACTER"},
            "gold_version": "v3",
            "candidate_free": True,
            "start_state_mode": "EMPTY",
            "cumulative_through_episode": 0,
            "review_status": "FINAL",
        }
        | changes
    )


def _gold(*, first_only=False) -> GoldSnapshotV3:
    scenarios = [
        _scenario(
            candidate_free=False,
            registered_characters_after_episode=[
                {"entityRef": CHARACTER_REF, "name": CHARACTER_NAME}
            ],
        )
    ]
    if not first_only:
        scenarios.append(
            _scenario(
                scenario_id="S2",
                episode_no=2,
                source_identifier="02화.txt",
                start_state_mode="PREVIOUS_GOLD",
                previous_scenario_id="S1",
                cumulative_through_episode=1,
            )
        )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="episode-end registered character",
        scenarios=scenarios,
        stage1=[
            CharacterStage1Gold(
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
                entity_ref=CHARACTER_REF,
                entity_name="미상",
                fact_type="PROFILE",
                fact_key="profile.species",
                value_type="STRING",
                display_value="바바리안",
                value_json={"value": "바바리안"},
                stage2_policy="WAIT_FOR_CHARACTER_MATCH",
            )
        ],
        stage2=[],
    ).with_fixture_hash()


class _CapturingExtractor:
    def __init__(self):
        self.known_names = {}

    async def extract_from_chunk(self, source_chunk_id, episode_no, known_characters, **kwargs):
        self.known_names[episode_no] = tuple(item.name for item in known_characters)
        candidates = []
        if episode_no == 1:
            candidates.append(
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name="미상",
                    raw_entity_mention="나",
                    attribute_name="profile.species",
                    attribute_value="바바리안",
                    value_type="STRING",
                    value_json={"value": "바바리안"},
                    evidence_spans=[ExtractedEvidenceSpan(quote="나는 바바리안이다.")],
                    confidence=0.9,
                )
            )
        return CharacterSettingExtractionResult(candidates=candidates)


class _SubjectResolver:
    async def resolve_candidates(self, *, candidates, **kwargs):
        return SubjectResolutionResult(candidates=candidates)


class _UnusedComparator:
    def batch_fits(self, **kwargs):
        raise AssertionError("An unresolved waiting character must not reach Stage2.")
