"""Current per-person evaluation contract, separate from legacy request captures."""

import asyncio
import json

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.llm.responses import LlmTextResponse
from evals.multi_stage_setting.contracts import (
    CharacterStage1Prediction,
    CharacterStateEntry,
    EvaluationState,
    ScenarioGold,
    character_state_ref,
)
from evals.multi_stage_setting.runtime_adapter import (
    _CharacterBatchSource,
    _character_batch_snapshots,
    _run_character_batches,
)


class _Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads = []

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        self.payloads.append(payload)
        response = self.responses.pop(0) if self.responses else {
            "decisions": [
                _decision(item["candidate_ref"], item["initial_canonical_fact_key"],
                          item["attribute_value"])
                for item in payload["candidates"]
            ]
        }
        return LlmTextResponse(
            text=response if isinstance(response, str) else json.dumps(response, ensure_ascii=False),
        )


def _decision(ref, key, value, target=None):
    return {
        "candidate_ref": ref,
        "resolved_canonical_fact_key": key,
        "operation": "UPDATE" if target else "ADD",
        "target_ref": target,
        "removed_snapshot_refs": [],
        "proposed_fact_value": value,
        "proposed_value_json": {"value": value},
        "temporal_scope": "PRESENT",
        "comparison_reason": "현재 확인된 설정을 반영합니다.",
    }


def _source(candidate_id, fact_type, key, value, offset, *, entity="character:bjorn"):
    return _CharacterBatchSource(
        source=CharacterStage1Prediction(
            candidate_id=candidate_id,
            sort_order=100 - offset,
            domain="CHARACTER",
            candidate_kind="SETTING",
            entity_ref=entity,
            entity_name="비요른" if entity == "character:bjorn" else "가람",
            fact_type=fact_type,
            fact_key=key,
            value_type="STRING",
            display_value=value,
            value_json={"value": value},
            evidence_spans=[{"quote": value, "start_offset": offset,
                             "end_offset": offset + len(value)}],
        ),
        raw_fact_key=key,
        initial_canonical_fact_key=key,
        canonical_key_resolution="EXACT",
    )


def _run(sources, client, *, state=None):
    scenario = ScenarioGold(
        scenario_id="S6", episode_no=6, source_identifier="06화.txt",
        target_domains={"CHARACTER"}, gold_version="v3", start_state_mode="EMPTY",
        cumulative_through_episode=0, candidate_free=True, review_status="FINAL",
    )
    return asyncio.run(_run_character_batches(
        scenario, state or EvaluationState(), sources,
        CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1),
    ))


@pytest.mark.parametrize("recover", [False, True])
def test_person_batch_keeps_mixed_types_in_evidence_order_and_restores_q_targets(recover):
    decisions = [
        _decision("C1", "item.마석", "10개"),
        _decision("C2", "profile.종족", "바바리안"),
        _decision("C3", "item.마석", "44개", "Q1"),
    ]
    client = _Client(*(
        ["invalid JSON", *[{"decisions": [item]} for item in decisions]]
        if recover else [{"decisions": decisions}]
    ))

    predictions, failures = _run([
        _source("profile", "PROFILE", "profile.종족", "바바리안", 20),
        _source("item-later", "ITEM", "item.마석", "44개", 40),
        _source("item-first", "ITEM", "item.마석", "10개", 0),
    ], client)

    assert failures == []
    assert [item.source_candidate_id for item in predictions] == [
        "item-first", "profile", "item-later",
    ]
    assert predictions[2].target_ref == "gold:character:character%3Abjorn:ITEM:item.마석"
    assert len(client.payloads) == (4 if recover else 1)
    payload = client.payloads[0]
    assert "canonical_fact_type" not in payload
    assert [item["canonical_fact_type"] for item in payload["candidates"]] == [
        "ITEM", "PROFILE", "ITEM",
    ]
    assert [item["source_episode_no"] for item in payload["candidates"]] == [6, 6, 6]
    if recover:
        assert [item["candidates"][0]["canonical_fact_type"]
                for item in client.payloads[1:]] == ["ITEM", "PROFILE", "ITEM"]
        assert [(item["ref"], item["fact_type"])
                for item in client.payloads[-1]["snapshot_entries"]] == [
            ("Q1", "ITEM"), ("Q2", "PROFILE"),
        ]


def test_person_snapshot_keeps_every_type_and_more_than_30_entries():
    state = EvaluationState(character_facts=[
        CharacterStateEntry(
            ref=character_state_ref(entity, fact_type, key), entity_ref=entity,
            entity_name="비요른", fact_type=fact_type, fact_key=key, value="현재값",
        )
        for entity, fact_type, key in [
            ("character:bjorn", "PROFILE", f"profile.field_{index:02d}")
            for index in range(31)
        ] + [
            ("character:bjorn", "ITEM", "item.마석"),
            ("character:bjorn", "STATUS", "status.부상"),
            ("character:other", "PROFILE", "profile.field_00"),
        ]
    ])

    entries, refs = _character_batch_snapshots(
        state, "character:bjorn", None, ["profile.field_30"],
    )

    assert len(entries) == 33
    assert entries[0].fact_type == "ITEM"
    assert entries[-1].fact_type == "STATUS"
    assert [item.snapshot_ref for item in entries] == [f"P{index}" for index in range(1, 34)]
    assert refs["P32"] == "gold:character:character%3Abjorn:PROFILE:profile.field_30"
    assert refs["P33"] == "gold:character:character%3Abjorn:STATUS:status.부상"

    client = _Client({"decisions": [_decision("C1", "profile.field_30", "새 값", "P32")]})
    predictions, failures = _run([
        _source("late-field", "PROFILE", "profile.field_30", "새 값", 0),
    ], client, state=state)
    assert failures == []
    assert predictions[0].target_ref == "gold:character:character%3Abjorn:PROFILE:profile.field_30"
    assert len(client.payloads[0]["snapshot_entries"]) == 33


def test_person_batches_keep_more_than_30_candidates_and_separate_people():
    sources = [
        _source(f"source-{index}", "ITEM" if index % 2 else "PROFILE",
                f"item.field_{index}" if index % 2 else f"profile.field_{index}",
                f"값 {index}", index)
        for index in range(31)
    ]
    sources.append(_source("other", "ITEM", "item.field_1", "다른 값", 40,
                           entity="character:other"))
    client = _Client()

    predictions, failures = _run(sources, client)

    assert failures == []
    assert len(predictions) == 32
    assert [len(item["candidates"]) for item in client.payloads] == [31, 1]
    assert [item.source_candidate_id for item in predictions] == [
        *[f"source-{index}" for index in range(31)], "other",
    ]
