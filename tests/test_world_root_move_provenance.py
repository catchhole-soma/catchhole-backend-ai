"""A structural root move must not become a new narrative source for its value."""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonBatchContextResponse,
    WorkerWorldSettingComparisonTarget,
)
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_setting_book_reference_time import book_source

EXPORT_PATH = os.environ.get("GH221_JAVA_ROOT_MOVE_WIRE")


def _target():
    return WorkerWorldSettingComparisonTarget(
        world_setting_id=uuid4(), subject_name="왕국", version=2,
        properties=[{
            "settingName": "세금", "value": "2",
            "provenance": {
                "confirmationStatus": "CONFIRMED", "sourceKind": "EPISODE",
                "sourceEpisodeNo": 1, "sourceCandidateIds": [str(uuid4())],
                "currentSourceTimes": [{
                    "sourceKind": "EPISODE", "sourceEpisodeNo": 1,
                    "temporalScope": "PRESENT",
                }],
            },
        }],
    )


def _candidate(scope, name, value, *, effective_no=None):
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref="C1", candidate_id=uuid4(), subject_name="왕국",
        scope_name=scope, setting_name=name, extracted_value=value,
        evidence_spans=[{"quote": f"{name}: {value}"}],
        temporal_scope="PRESENT", effective_from_episode_no=effective_no,
        rawExtractionJson={"epistemic_status": "ASSERTED"},
    )


def _decision(candidate, *, operation, matched_ref=None, moves=()):
    return {
        "source_candidate_refs": ["C1"], "consolidation_status": "SINGLE",
        "operation": operation, "target_ref": "T1", "matched_property_ref": matched_ref,
        "review_reason": "GENERAL_UNCERTAINTY" if operation == "REVIEW_REQUIRED" else None,
        "proposed_scope_name": None if operation == "UPDATE" else candidate.scope_name,
        "proposed_setting_name": None if operation == "UPDATE" else candidate.setting_name,
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "원문의 세금과 징수 시기를 각각 보존해 비교합니다.",
        "existing_root_property_names_to_move": list(moves),
    }


def _compare(candidate, target, source, *responses):
    provider = RecordingClient(*({"decisions": [row]} for row in responses))
    result, _ = asyncio.run(WorldSettingComparator(
        llm_client=provider, model="offline-test", max_attempts=len(responses),
    ).compare_batch("REGION_PLACE", [candidate], [target],
                    ordered_context=True, analysis_source=source))
    return result.decisions[0], provider.requests


def test_permitted_root_move_keeps_original_property_provenance_in_the_comparator_input():
    target = _target()
    before = target.model_dump(mode="json")
    candidate = _candidate("재정", "징수 시기", "봄")
    result, requests = _compare(candidate, target, book_source("EPISODE", 3),
        _decision(candidate, operation="ADD", moves=["세금"]))

    assert result.operation == "ADD"
    assert result.existing_root_property_names_to_move == ["세금"]
    assert target.model_dump(mode="json") == before
    original = json.loads(requests[0]["user_prompt"])["targets"][0]["properties"][0]
    assert original["source_kind"] == "EPISODE" and original["source_episode_no"] == 1
    assert original["current_source_times"][0]["source_episode_no"] == 1
    assert str(target.properties[0].provenance.source_candidate_ids[0]) not in requests[0]["user_prompt"]


@pytest.mark.parametrize("blocked_by", ["later_source", "protected_source", "protected_destination", "future_body"])
def test_root_move_still_checks_all_source_times_protected_paths_and_body_time(blocked_by):
    target = _target()
    candidate = _candidate("재정", "징수 시기", "봄", effective_no=4 if blocked_by == "future_body" else None)
    if blocked_by == "later_source":
        target.properties[0].provenance = target.properties[0].provenance.model_copy(update={
            "current_source_times": target.properties[0].provenance.current_source_times +
                [target.properties[0].provenance.current_source_times[0].model_copy(update={"source_episode_no": 4})],
        })
    elif blocked_by.startswith("protected_"):
        target = WorkerWorldSettingComparisonTarget.model_validate({
            **target.model_dump(by_alias=True),
            "protectedPaths": [{"scopeName": None if blocked_by == "protected_source" else "재정",
                                "settingName": "세금"}],
        })
    before = target.model_dump(mode="json")
    result, requests = _compare(candidate, target, book_source("EPISODE", 3),
        _decision(candidate, operation="ADD", moves=["세금"]),
        _decision(candidate, operation="REVIEW_REQUIRED"))

    assert result.operation == "REVIEW_REQUIRED" and not result.existing_root_property_names_to_move
    assert len(requests) == 2
    assert target.model_dump(mode="json") == before


@pytest.mark.skipif(not EXPORT_PATH, reason="Actual Java root-move HTTP export is not supplied.")
@pytest.mark.parametrize("incoming_no", [2, 4])
def test_actual_moved_and_reloaded_property_keep_episode_origin_for_a_later_book(incoming_no):
    export = json.loads(Path(EXPORT_PATH).read_text(encoding="utf-8"))
    contexts = {key: WorkerWorldSettingComparisonBatchContextResponse.model_validate(export[key])
                for key in ("beforeContext", "movedContext", "reloadedContext")}
    source_id = UUID(export["originalSourceCandidateId"])
    original_target, original = next(
        (target, prop) for target in contexts["beforeContext"].targets for prop in target.properties
        if prop.provenance is not None and source_id in prop.provenance.source_candidate_ids
    )
    assert original.scope_name is None
    assert original.provenance.source_kind == "EPISODE" and original.provenance.source_episode_no == 1
    assert original.provenance.current_source_times
    assert all(time.source_kind == "EPISODE" and time.source_episode_no == 1
               for time in original.provenance.current_source_times)
    for key in ("movedContext", "reloadedContext"):
        target, moved = next(
            (target, prop) for target in contexts[key].targets for prop in target.properties
            if target.identity_key() == original_target.identity_key() and prop.setting_name == original.setting_name
        )
        assert moved.scope_name is not None and moved.value == original.value
        assert moved.provenance == original.provenance
        candidate = _candidate(moved.scope_name, moved.setting_name, "3")
        candidate = candidate.model_copy(update={"subject_name": target.subject_name})
        selected_ref = f"T1.P{target.properties.index(moved) + 1}"
        decision, requests = _compare(candidate, target, book_source("EPISODE", incoming_no),
            _decision(candidate, operation="UPDATE", matched_ref=selected_ref))
        assert decision.operation == "UPDATE"
        assert decision.matched_scope_name == moved.scope_name
        assert decision.matched_property_name == moved.setting_name
        assert len(requests) == 1
