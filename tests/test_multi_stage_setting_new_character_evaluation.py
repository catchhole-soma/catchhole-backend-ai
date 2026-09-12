import asyncio

import pytest

from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
)
from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    ExtractedCharacterDiscoveryCandidate,
    ExtractedCharacterSettingCandidate,
)
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    ScenarioGold,
    align_prediction_character_refs,
    character_state_ref,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.matching import match_stage1
from evals.multi_stage_setting.report_cli import build_public_diagnostics
from evals.multi_stage_setting.runtime_adapter import RuntimeComponents, run_multi_stage_predictions


def _gold(episodes=1):
    scenarios, stage1, stage2 = [], [], []
    for episode in range(1, episodes + 1):
        scenarios.append(
            ScenarioGold(
                scenario_id=f"S{episode}",
                episode_no=episode,
                source_identifier=f"{episode}.txt",
                source_text="카락은 키가 180이다. 카락은 몸무게가 90이다. 카락은 키가 190이 되었다.",
                target_domains={"CHARACTER"},
                gold_version="v3",
                start_state_mode="SEED" if episode == 1 else "PREVIOUS_GOLD",
                seed_state=EvaluationState() if episode == 1 else None,
                previous_scenario_id=None if episode == 1 else f"S{episode - 1}",
                cumulative_through_episode=episode - 1,
                review_status="FINAL",
            )
        )
        for order, (key, value) in enumerate(
            [("profile.height", 180), ("profile.weight", 90)]
            if episode == 1
            else [("profile.height", 190)],
            start=1,
        ):
            source_id = f"C{episode}-{order}"
            stage1.append(
                CharacterStage1Gold(
                    gold_id=source_id,
                    scenario_id=f"S{episode}",
                    episode_no=episode,
                    sort_order=order,
                    decision="EXTRACT",
                    importance="MUST",
                    evidence_quotes=[
                        "카락은 키가 180이다."
                        if key == "profile.height"
                        else "카락은 몸무게가 90이다."
                    ],
                    review_status="FINAL",
                    domain="CHARACTER",
                    candidate_kind="SETTING",
                    entity_ref="character:karak",
                    entity_name="카락",
                    fact_type="PROFILE",
                    fact_key=key,
                    value_type="NUMBER",
                    display_value=str(value),
                    value_json={"value": value},
                    value_json_provenance="ANNOTATED",
                    structured_scorable=True,
                )
            )
            stage2.append(
                CharacterStage2Gold(
                    decision_id=f"D{episode}-{order}",
                    scenario_id=f"S{episode}",
                    episode_no=episode,
                    sort_order=order,
                    source_gold_ids=[source_id],
                    domain="CHARACTER",
                    operation="ADD" if episode == 1 else "UPDATE",
                    target_ref=None
                    if episode == 1
                    else character_state_ref("character:karak", "PROFILE", key),
                    proposed_value=str(value),
                    proposed_value_json={"value": value},
                    temporal_scope="PRESENT",
                    review_status="FINAL",
                )
            )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="new character comparison",
        scenarios=scenarios,
        stage1=stage1,
        stage2=stage2,
    ).with_fixture_hash()


class _Extractor:
    def __init__(self, extra=False, name="카락", value_delta=0, omit=False, discovery=False):
        self.extra, self.name, self.value_delta = extra, name, value_delta
        self.omit, self.discovery = omit, discovery
        self.known_names = []

    async def extract_from_chunk(self, source_chunk_id, episode_no, known_characters, **kwargs):
        self.known_names.append([item.name for item in known_characters])
        slots = (
            [("profile.height", 180), ("profile.weight", 90)]
            if episode_no == 1
            else [("profile.height", 190)]
        )
        candidates = [
            ExtractedCharacterSettingCandidate(
                source_chunk_id=source_chunk_id,
                candidate_kind="SETTING",
                entity_name=self.name,
                raw_entity_mention=self.name,
                attribute_name=key,
                attribute_value=str(value + self.value_delta),
                value_type="NUMBER",
                value_json={"value": value + self.value_delta},
                evidence_spans=[
                    {
                        "quote": "카락은 키가 180이다."
                        if key == "profile.height"
                        else "카락은 몸무게가 90이다."
                    }
                ],
            )
            for key, value in slots
        ]
        if self.extra:
            candidates.append(
                candidates[0].model_copy(
                    update={
                        "entity_name": "세룸",
                        "raw_entity_mention": "세룸",
                    }
                )
            )
        if self.discovery:
            candidates.insert(
                0,
                ExtractedCharacterDiscoveryCandidate(
                    source_chunk_id=source_chunk_id,
                    candidate_kind="CHARACTER_DISCOVERY",
                    attribute_name=None,
                    attribute_value=None,
                    value_type=None,
                    value_json=None,
                    entity_name=self.name,
                    raw_entity_mention=self.name,
                    evidence_spans=[{"quote": "카락은 키가 180이다."}],
                ),
            )
        return CharacterSettingExtractionResult(candidates=[] if self.omit else candidates)


class _Resolver:
    async def resolve_candidates(self, *, candidates, **kwargs):
        return SubjectResolutionResult(candidates=candidates, fallback_call_count=0)


class _Comparator:
    def __init__(self):
        self.calls = []

    def batch_fits(self, *, candidates, **kwargs):
        return bool(candidates)

    async def compare_batch(
        self, *, candidates, snapshot_entries, matched_character_name, **kwargs
    ):
        self.calls.append((matched_character_name, candidates, snapshot_entries))
        targets = {entry.fact_key: entry.reference for entry in snapshot_entries}
        decisions = []
        for candidate in candidates:
            key = candidate.initial_canonical_fact_key
            target = targets.get(key)
            decisions.append(
                CharacterFactComparisonBatchDecision(
                    candidate_ref=candidate.candidate_ref,
                    operation="UPDATE" if target else "ADD",
                    target_ref=target,
                    resolved_canonical_fact_key=key,
                    proposed_fact_value=candidate.attribute_value,
                    proposed_value_json=candidate.value_json,
                    temporal_scope="PRESENT",
                    comparison_reason="설정 반영",
                )
            )
            targets[key] = candidate.projected_snapshot_ref
        result = CharacterFactComparisonBatchResult(decisions=decisions)
        return result, result.model_dump(mode="json")


def _run(gold, mode="FIXED", extractor=None):
    extractor, comparator = extractor or _Extractor(), _Comparator()
    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode=mode,
            domains={"CHARACTER"},
            components=RuntimeComponents(
                character_extractor=extractor,
                character_subject_resolver=_Resolver(),
                character_comparator=comparator,
                world_comparator=None,
            ),
            character_schema_hints=tuple(
                CharacterSettingSchemaHint(
                    schema_key=key,
                    display_name=key,
                    attribute_pattern=None,
                    aliases=(),
                    value_type="NUMBER",
                )
                for key in ("profile.height", "profile.weight")
            ),
        )
    )
    return bundle, comparator, extractor


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING"])
def test_new_character_settings_reach_comparison_and_score_without_discovery(mode):
    gold = _gold()
    bundle, comparator, extractor = _run(gold, mode)
    prediction = bundle.scenarios[0]

    assert extractor.known_names == [[]]
    assert len(comparator.calls) == 1
    assert comparator.calls[0][0] == "카락"
    assert len(comparator.calls[0][1]) == 2
    assert comparator.calls[0][2] == []
    assert {source.entity_ref for source in prediction.stage1} == {"prediction-character:카락"}
    assert {source.match_status for source in prediction.stage1} == {"EVALUATION_NEW_CHARACTER"}
    assert all(source.entity_ref is None for source in prediction.raw_stage1)
    assert len(prediction.stage2) == 2
    assert prediction.failures == []
    assert prediction.processing_version == 1
    assert len(prediction.processing) == 2
    assert all(record.status == "COMPARED" for record in prediction.processing)
    assert all(record.comparison_forwarded for record in prediction.processing)

    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert {case["result"] for case in report["scenarios"][0]["stage2"]} == {"FULL_MATCH"}
    assert report["stages"]["character"]["stage2"]["counts"]["reachedAndCompared"] == 2
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 1
    public = build_public_diagnostics(report)[0]
    assert all(record["status"] == "COMPARED" for record in public["processing"])
    assert all(record["comparisonForwarded"] for record in public["processing"])


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING"])
def test_next_episode_reuses_new_character_and_compares_actual_previous_settings(mode):
    gold = _gold(episodes=2)
    bundle, comparator, extractor = _run(gold, mode)
    assert extractor.known_names == [[], ["카락"]]
    assert len(comparator.calls) == 2
    assert {entry.fact_key for entry in comparator.calls[1][2]} == {
        "profile.height",
        "profile.weight",
    }
    assert bundle.scenarios[1].stage2[0].operation == "UPDATE"
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert {case["result"] for scenario in report["scenarios"] for case in scenario["stage2"]} == {
        "FULL_MATCH",
    }
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 1


def test_discovery_shares_ref_but_only_settings_enter_comparison():
    gold = _gold()
    bundle, comparator, _ = _run(gold, extractor=_Extractor(discovery=True))
    assert len(bundle.scenarios[0].stage1) == 3
    assert len(bundle.scenarios[0].stage2) == 2
    assert len(comparator.calls[0][1]) == 2
    assert len({source.entity_ref for source in bundle.scenarios[0].stage1}) == 1
    assert [record.status for record in bundle.scenarios[0].processing] == [
        "NOT_APPLICABLE",
        "COMPARED",
        "COMPARED",
    ]


def test_wrong_values_and_extra_characters_are_compared_without_gold_correction():
    gold = _gold()
    bundle, comparator, _ = _run(gold, extractor=_Extractor(extra=True, value_delta=1))
    assert [call[0] for call in comparator.calls] == ["카락", "세룸"]
    assert [len(call[1]) for call in comparator.calls] == [2, 1]
    assert len(bundle.scenarios[0].stage2) == 3
    assert len(bundle.scenarios[0].processing) == 3
    assert all(record.status == "COMPARED" for record in bundle.scenarios[0].processing)
    assert bundle.scenarios[0].stage2[0].proposed_value_json == {"value": 181}
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert report["stages"]["character"]["stage1"]["metrics"]["valueAccuracy"] == 0
    assert report["endToEnd"]["metrics"]["afterStateF1"] < 1


def test_missed_extraction_is_not_supplied_from_gold():
    bundle, comparator, _ = _run(_gold(), extractor=_Extractor(omit=True))
    assert bundle.scenarios[0].stage1 == []
    assert bundle.scenarios[0].stage2 == []
    assert comparator.calls == []


def test_wrong_character_name_is_compared_but_not_marked_correct():
    gold = _gold()
    bundle, comparator, _ = _run(gold, extractor=_Extractor(name="세룸"))
    assert len(comparator.calls) == 1
    assert len(bundle.scenarios[0].stage2) == 2
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert report["stages"]["character"]["stage1"]["metrics"]["candidateF1"] == 0
    assert report["endToEnd"]["metrics"]["afterStateF1"] < 1


def test_existing_character_wrong_id_is_not_hidden_by_matching_display_name():
    gold = _gold()
    bundle, _, _ = _run(gold)
    source = (
        bundle.scenarios[0]
        .stage1[0]
        .model_copy(
            update={
                "entity_ref": "character:someone-else",
                "match_status": "MATCHED",
            }
        )
    )
    matches = match_stage1(
        gold.stage1[:1],
        [source],
        domain="CHARACTER",
        source_text=gold.scenarios[0].source_text,
    )
    assert not matches.matches[0].entity_or_subject_matched


def test_scoring_ref_alignment_preserves_wrong_slots_other_characters_and_raw_output():
    bundle, _, _ = _run(_gold())
    source = bundle.scenarios[0].stage1[0]
    own = character_state_ref(source.entity_ref, "STATUS", "status.injury")
    other = character_state_ref("prediction-character:세룸", "STATUS", "status.injury")
    decision = (
        bundle.scenarios[0]
        .stage2[0]
        .model_copy(
            update={
                "target_ref": character_state_ref(source.entity_ref, "PROFILE", "profile.wrong"),
                "removed_snapshot_refs": [own, other],
            }
        )
    )
    raw = decision.model_dump_json()
    aligned = align_prediction_character_refs(source, decision, "character:karak")
    assert aligned.target_ref == character_state_ref(
        "character:karak",
        "PROFILE",
        "profile.wrong",
    )
    assert aligned.removed_snapshot_refs == [
        character_state_ref("character:karak", "STATUS", "status.injury"),
        other,
    ]
    assert decision.model_dump_json() == raw
    registered = source.model_copy(update={"entity_ref": "character:registered"})
    assert align_prediction_character_refs(registered, decision, "character:karak") == decision


def test_new_character_projected_target_is_scored_in_same_episode():
    gold = _gold()
    extra = gold.stage1[0].model_copy(
        update={
            "gold_id": "C1-3",
            "sort_order": 3,
            "display_value": "190",
            "value_json": {"value": 190},
            "evidence_quotes": ["카락은 키가 190이 되었다."],
        }
    )
    gold.stage1.append(extra)
    gold.stage2.append(
        CharacterStage2Gold.model_validate(
            {
                **gold.stage2[0].model_dump(),
                "decision_id": "D1-3",
                "sort_order": 3,
                "source_gold_ids": ["C1-3"],
                "operation": "UPDATE",
                "target_ref": character_state_ref("character:karak", "PROFILE", "profile.height"),
                "proposed_value": "190",
                "proposed_value_json": {"value": 190},
            }
        )
    )
    gold = gold.with_fixture_hash()

    class ProjectedExtractor(_Extractor):
        async def extract_from_chunk(self, **kwargs):
            result = await super().extract_from_chunk(**kwargs)
            candidate = result.candidates[0].model_dump()
            candidate.update(
                attribute_value="190",
                value_json={"value": 190},
                evidence_spans=[{"quote": "카락은 키가 190이 되었다."}],
            )
            result.candidates.append(ExtractedCharacterSettingCandidate(**candidate))
            return result

    bundle, comparator, _ = _run(gold, extractor=ProjectedExtractor())
    assert len(comparator.calls) == 1
    assert comparator.calls[0][2] == []
    assert [item.operation for item in bundle.scenarios[0].stage2] == ["ADD", "ADD", "UPDATE"]
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert {case["result"] for case in report["scenarios"][0]["stage2"]} == {"FULL_MATCH"}
    assert report["endToEnd"]["counts"]["stateApplicationErrors"] == 0
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 1


@pytest.mark.parametrize("name", ["미상", "그", "카락"])
def test_truly_ambiguous_identity_is_recorded_without_inventing_character(name):
    gold = _gold()
    if name == "카락":
        gold.scenarios[0].seed_state = EvaluationState(
            known_characters=[
                KnownCharacter(entity_ref="character:first", name=name),
                KnownCharacter(entity_ref="character:second", name=name),
            ]
        )
        gold = gold.with_fixture_hash()
    bundle, comparator, _ = _run(gold, extractor=_Extractor(name=name))
    prediction = bundle.scenarios[0]
    assert comparator.calls == []
    assert prediction.stage2 == []
    assert prediction.failures == []
    assert len(prediction.processing) == 2
    assert {item.stage for item in prediction.processing} == {"CHARACTER_HANDOFF"}
    assert {item.status for item in prediction.processing} == {"AMBIGUOUS_CHARACTER"}
    assert all(not item.comparison_forwarded for item in prediction.processing)
    assert all(item.entity_ref is None for item in prediction.stage1)
