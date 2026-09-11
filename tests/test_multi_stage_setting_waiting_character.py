import asyncio

import pytest

from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
)
from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    ExtractedCharacterSettingCandidate,
    ExtractedEvidenceSpan,
)
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    ScenarioGold,
    character_state_ref,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.runtime_adapter import (
    RuntimeComponents,
    _apply_runtime_scenario,
    _runtime_known_characters,
    run_multi_stage_predictions,
)
from evals.multi_stage_setting.state_effects import build_gold_state_chain

CHARACTER_REF = "character:bjorn-yandel"
REVEALED_NAME = "비요른 얀델"


def test_waiting_gold_keeps_unknown_identity_out_of_state() -> None:
    gold = _gold()

    chain = build_gold_state_chain(gold)

    assert gold.stage1[0].entity_ref == gold.stage1[1].entity_ref == CHARACTER_REF
    assert gold.stage1[0].entity_name == "미상"
    assert gold.stage1[1].entity_name == REVEALED_NAME
    assert chain["S1"].after_state.known_characters == []
    assert chain["S1"].after_state.character_facts == []
    assert chain["S1"].applied_decision_ids == ()
    assert [(item.entity_ref, item.name) for item in chain["S2"].before_state.known_characters] == [
        (CHARACTER_REF, REVEALED_NAME)
    ]
    assert chain["S2"].after_state.character_facts[0].entity_ref == CHARACTER_REF
    assert chain["S2"].after_state.known_characters[0].name == REVEALED_NAME


def test_waiting_character_compares_after_known_name_is_provided() -> None:
    gold = _gold()
    extractor = _Extractor()
    comparator = _Comparator()
    bundle = _run(gold, "FIXED", extractor, comparator)

    first, second = bundle.scenarios
    assert first.stage1[0].entity_name == "미상"
    assert first.stage1[0].entity_ref is None
    assert first.stage1[0].match_status in {"AMBIGUOUS", "UNRESOLVED"}
    assert first.stage2 == []
    assert second.stage1[0].entity_name == REVEALED_NAME
    assert second.stage1[0].entity_ref == CHARACTER_REF
    assert second.stage1[0].match_status == "MATCHED"
    assert len(second.stage2) == 1
    assert second.stage2[0].operation == "ADD"
    assert comparator.names == [REVEALED_NAME]
    assert extractor.known_names == {1: (), 2: (REVEALED_NAME,)}
    assert not first.failures and not second.failures
    assert first.processing[0].status == "AMBIGUOUS_CHARACTER"
    assert first.processing[0].comparison_forwarded is False

    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert report["scenarios"][0]["stage1"]["CHARACTER"]["cases"][0]["result"] == "FULL_MATCH"
    assert report["stages"]["character"]["stage2"]["counts"]["gold"] == 1
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1

    before_second = build_gold_state_chain(gold)["S2"].before_state
    actual = _apply_runtime_scenario(gold.scenarios[1], before_second, second)
    assert len(actual.character_facts) == 1
    fact = actual.character_facts[0]
    assert fact.ref == character_state_ref(CHARACTER_REF, "PROFILE", "profile.species")
    assert fact.entity_name == REVEALED_NAME
    assert fact.value == "바바리안"


def test_second_episode_discovery_registers_name_on_original_canonical_id() -> None:
    base = _gold()
    second = ScenarioGold.model_validate(
        base.scenarios[1].model_dump()
        | {
            "start_state_mode": "PREVIOUS_GOLD",
            "previous_scenario_id": "S1",
            "seed_state": None,
            "known_character_names": [],
        }
    )
    discovery = CharacterStage1Gold(
        gold_id="C013",
        scenario_id="S2",
        episode_no=2,
        sort_order=13,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["내 이름은 비요른 얀델이다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="CHARACTER_DISCOVERY",
        entity_ref=CHARACTER_REF,
        entity_name=REVEALED_NAME,
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="unknown-then-discovered-character",
        scenarios=[base.scenarios[0], second],
        stage1=[base.stage1[0], discovery],
        stage2=[],
    ).with_fixture_hash()

    chain = build_gold_state_chain(gold)

    assert chain["S1"].after_state.known_characters == []
    assert chain["S2"].before_state.known_characters == []
    assert chain["S2"].after_state.character_facts == []
    assert [(item.entity_ref, item.name) for item in chain["S2"].after_state.known_characters] == [
        (CHARACTER_REF, REVEALED_NAME)
    ]
    runtime_characters, canonical_ref_by_id = _runtime_known_characters(chain["S2"].after_state)
    assert len(runtime_characters) == 1
    assert runtime_characters[0].name == REVEALED_NAME
    assert canonical_ref_by_id[runtime_characters[0].character_id] == CHARACTER_REF


def test_oracle_skips_waiting_row_but_still_compares_required_row_with_same_canonical_id() -> None:
    gold = _gold()
    extractor = _Extractor()
    comparator = _Comparator()

    bundle = _run(gold, "ORACLE", extractor, comparator)

    first, second = bundle.scenarios
    assert [item.candidate_id for item in first.stage1] == ["C1"]
    assert first.stage2 == []
    assert [item.source_candidate_id for item in second.stage2] == ["C2"]
    assert comparator.names == [REVEALED_NAME]
    assert extractor.known_names == {}
    assert gold.stage1[0].entity_ref == gold.stage1[1].entity_ref == CHARACTER_REF
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert report["stages"]["character"]["stage2"]["counts"]["gold"] == 1
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING"])
def test_model_name_compares_without_registered_context_or_metadata_injection(mode: str) -> None:
    gold = _gold(second_has_known_context=False)
    extractor = _Extractor()
    comparator = _Comparator()

    chain = build_gold_state_chain(gold)
    bundle = _run(gold, mode, extractor, comparator)

    assert gold.scenarios[1].known_character_names == [REVEALED_NAME]
    assert chain["S1"].after_state.known_characters == []
    assert chain["S2"].before_state.known_characters == []
    assert extractor.known_names == {1: (), 2: ()}
    assert bundle.scenarios[1].stage1[0].entity_name == REVEALED_NAME
    assert bundle.scenarios[1].stage1[0].entity_ref == f"prediction-character:{REVEALED_NAME}"
    assert bundle.scenarios[1].stage1[0].match_status == "EVALUATION_NEW_CHARACTER"
    assert bundle.scenarios[0].stage2 == []
    assert len(bundle.scenarios[1].stage2) == 1
    assert comparator.names == [REVEALED_NAME]
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert report["stages"]["character"]["stage2"]["counts"]["reachedAndCompared"] == 1
    assert report["stages"]["character"]["stage2"]["metrics"]["fullDecisionAccuracy"] == 1


def _run(gold, mode, extractor, comparator):
    return asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode=mode,
            domains={"CHARACTER"},
            components=RuntimeComponents(
                character_extractor=extractor,
                character_subject_resolver=_SubjectResolver(),
                character_comparator=comparator,
                world_comparator=_UnusedWorldComparator(),
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


class _Extractor:
    def __init__(self) -> None:
        self.known_names = {}

    async def extract_from_chunk(self, source_chunk_id, episode_no, known_characters, **kwargs):
        self.known_names[episode_no] = tuple(item.name for item in known_characters)
        name = "미상" if episode_no == 1 else REVEALED_NAME
        return CharacterSettingExtractionResult(
            candidates=[
                ExtractedCharacterSettingCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="SETTING",
                    entity_name=name,
                    raw_entity_mention="나" if episode_no == 1 else REVEALED_NAME,
                    attribute_name="profile.species",
                    attribute_value="바바리안",
                    value_type="STRING",
                    value_json={"value": "바바리안"},
                    evidence_spans=[ExtractedEvidenceSpan(quote="나는 바바리안이다.")],
                    confidence=0.9,
                )
            ]
        )


class _SubjectResolver:
    async def reconcile_episode_names(self, *, candidates, **kwargs):
        return SubjectResolutionResult(candidates=candidates)

    async def resolve_candidates(self, *, candidates, **kwargs):
        return SubjectResolutionResult(candidates=candidates)


class _Comparator:
    def __init__(self) -> None:
        self.names = []

    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(self, *, matched_character_name, candidates, **kwargs):
        self.names.append(matched_character_name)
        result = CharacterFactComparisonBatchResult(
            decisions=[
                CharacterFactComparisonBatchDecision(
                    candidate_ref=candidate.candidate_ref,
                    operation="ADD",
                    resolved_canonical_fact_key=candidate.initial_canonical_fact_key,
                    target_ref=None,
                    removed_snapshot_refs=[],
                    proposed_fact_value=candidate.attribute_value,
                    proposed_value_json=candidate.value_json,
                    temporal_scope="PRESENT",
                    comparison_reason="이름이 확인된 인물의 종족을 추가합니다.",
                )
                for candidate in candidates
            ]
        )
        return result, result.model_dump(mode="json")


class _UnusedWorldComparator:
    async def compare_batch(self, **kwargs):
        raise AssertionError("Character-only evaluation must not call the world comparator.")


def _gold(*, second_has_known_context: bool = True) -> GoldSnapshotV3:
    first = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        source_text="나는 바바리안이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    second = ScenarioGold(
        scenario_id="S2",
        episode_no=2,
        source_identifier="02화.txt",
        source_text="나는 바바리안이다. 내 이름은 비요른 얀델이다.",
        target_domains={"CHARACTER"},
        gold_version="v3",
        start_state_mode="SEED" if second_has_known_context else "PREVIOUS_GOLD",
        previous_scenario_id=None if second_has_known_context else "S1",
        seed_state=EvaluationState(
            known_characters=[
                KnownCharacter(
                    entity_ref=CHARACTER_REF,
                    name=REVEALED_NAME,
                )
            ]
        )
        if second_has_known_context
        else None,
        known_character_names=[REVEALED_NAME],
        cumulative_through_episode=1,
        review_status="FINAL",
    )
    sources = [
        CharacterStage1Gold(
            gold_id=f"C{episode}",
            scenario_id=f"S{episode}",
            episode_no=episode,
            sort_order=1,
            decision="EXTRACT",
            importance="MUST",
            evidence_quotes=["나는 바바리안이다."],
            review_status="FINAL",
            domain="CHARACTER",
            candidate_kind="SETTING",
            entity_ref=CHARACTER_REF,
            entity_name="미상" if episode == 1 else REVEALED_NAME,
            fact_type="PROFILE",
            fact_key="profile.species",
            value_type="STRING",
            display_value="바바리안",
            value_json={"value": "바바리안"},
            stage2_policy="WAIT_FOR_CHARACTER_MATCH" if episode == 1 else "REQUIRED",
        )
        for episode in (1, 2)
    ]
    decision = CharacterStage2Gold(
        decision_id="DC2",
        scenario_id="S2",
        episode_no=2,
        sort_order=1,
        source_gold_ids=["C2"],
        domain="CHARACTER",
        operation="ADD",
        temporal_scope="PRESENT",
        proposed_value="바바리안",
        proposed_value_json={"value": "바바리안"},
        review_status="FINAL",
    )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="waiting-character-then-known-name",
        scenarios=[first, second],
        stage1=sources,
        stage2=[decision],
    ).with_fixture_hash()
