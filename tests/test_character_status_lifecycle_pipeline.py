import asyncio
from collections import deque
from copy import deepcopy
from uuid import UUID

import httpx
import pytest

from app.analysis.character_fact_comparison_pipeline import (
    CharacterFactComparisonPipeline,
    execute_character_fact_comparison_batch,
)
from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
)
from app.analysis.exceptions import ComparisonValidationError
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.schemas.worker import (
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchContextResponse,
    WorkerCharacterFactComparisonBatchPayload,
    WorkerCharacterFactComparisonBatchSnapshotEntry,
)


@pytest.mark.parametrize("use_singleton_fallback", [False, True])
def test_lifecycle_runs_once_with_all_candidates_and_initial_snapshot(
    use_singleton_fallback: bool,
) -> None:
    candidates, snapshots, decisions = _status_case()
    outcomes = (
        [ComparisonValidationError("batch failed"), *[_result(d) for d in decisions]]
        if use_singleton_fallback
        else [_result(*decisions[:2]), _result(decisions[2])]
    )
    comparator = RecordingComparator(outcomes, max_candidates=20 if use_singleton_fallback else 2)

    result = _execute(comparator, candidates, snapshots)

    assert comparator.candidate_calls == (
        [["C1", "C2", "C3"], ["C1"], ["C2"], ["C3"]]
        if use_singleton_fallback
        else [["C1", "C2"], ["C3"]]
    )
    assert len(comparator.lifecycle_calls) == 1
    review = comparator.lifecycle_calls[0]
    assert review["matched_character_name"] == "리안"
    assert review["candidates"] == candidates
    assert review["decisions"] == decisions
    assert [entry.reference for entry in review["snapshot_entries"]] == ["P1", "P2", "P3"]
    assert [entry.fact_value for entry in review["snapshot_entries"]] == [
        entry.fact_value for entry in snapshots
    ]
    # Segments/fallbacks see prior Q state; the reviewer must instead see initial P state.
    assert set(comparator.snapshot_calls[-1]) == {"P1", "P3", "Q1", "Q2"}
    assert result.provider_segment_count == (4 if use_singleton_fallback else 2)
    assert result.singleton_fallback_count == (3 if use_singleton_fallback else 0)
    assert result.failures == []
    assert [decision.candidate_ref for decision in result.decisions] == ["C1", "C2", "C3"]


@pytest.mark.parametrize(
    "error_type, failure_code",
    [
        (ComparisonValidationError, "COMPARISON_VALIDATION_FAILED"),
        (httpx.TimeoutException, "LLM_NETWORK_ERROR"),
        (AiTokenQuotaExhaustedError, "AI_TOKEN_QUOTA_EXHAUSTED"),
    ],
)
def test_lifecycle_failure_propagates_without_fallback_or_atomic_completion(
    error_type: type[Exception],
    failure_code: str,
) -> None:
    candidates, snapshots, decisions = _status_case()
    comparator = RecordingComparator([_result(*decisions)], reviewed=_review_error(error_type))

    with pytest.raises(error_type):
        _execute(comparator, candidates, snapshots)

    assert comparator.candidate_calls == [["C1", "C2", "C3"]]
    assert len(comparator.lifecycle_calls) == 1

    comparator = RecordingComparator([_result(*decisions)], reviewed=_review_error(error_type))
    spring = RecordingSpring(candidates, snapshots)
    process = CharacterFactComparisonPipeline(spring, comparator).process_all(
        UUID(int=1), UUID(int=2)
    )
    if error_type is AiTokenQuotaExhaustedError:
        with pytest.raises(AiTokenQuotaExhaustedError):
            asyncio.run(process)
    else:
        result = asyncio.run(process)
        assert result.completed_count == 0
        assert result.failed_count == 3
        assert result.first_failure_code.value == failure_code

    assert spring.completions == []
    assert spring.failures == [failure_code]
    assert comparator.candidate_calls == [["C1", "C2", "C3"]]
    assert len(comparator.lifecycle_calls) == 1


def test_lifecycle_is_skipped_when_a_singleton_has_failed() -> None:
    candidates, snapshots, decisions = _status_case()
    comparator = RecordingComparator(
        [
            ComparisonValidationError("batch failed"),
            ComparisonValidationError("C1 failed"),
            _result(decisions[1]),
            _result(decisions[2]),
        ],
        reviewed=AssertionError("incomplete batch must not be reviewed"),
    )
    spring = RecordingSpring(candidates, snapshots)

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(UUID(int=1), UUID(int=2))
    )

    assert comparator.lifecycle_calls == []
    assert result.completed_count == 2
    assert result.failed_count == 1
    assert len(spring.completions) == 1
    completion = spring.completions[0]
    assert [failure.candidate_ref for failure in completion.failures] == ["C1"]
    assert [decision.candidate_ref for decision in completion.decisions] == ["C2", "C3"]
    assert all(decision.dependency_candidate_refs == [] for decision in completion.decisions)


def test_non_status_batch_never_calls_lifecycle_reviewer() -> None:
    candidate = _candidate("C1", "profile.occupation", "수습 기사").model_copy(
        update={
            "canonical_key_resolution": "EXACT",
            "value_type": "STRING",
            "value_json": {"value": "수습 기사"},
        }
    )
    decision = _decision("C1", "ADD", "profile.occupation", value="수습 기사").model_copy(
        update={"proposed_value_json": {"value": "수습 기사"}}
    )
    comparator = RecordingComparator(
        [_result(decision)], reviewed=AssertionError("PROFILE must not be reviewed")
    )

    result = _execute(comparator, [candidate], [], fact_type="PROFILE")

    assert comparator.lifecycle_calls == []
    assert len(result.decisions) == 1
    assert result.decisions[0].proposed_value_json == {"value": "수습 기사"}
    assert result.failures == []


def test_lifecycle_expanded_q_removal_replays_dependencies_and_preserves_source_decisions() -> None:
    candidates, snapshots, original = _status_case()
    original_candidates = deepcopy(candidates)
    original_decisions = deepcopy(original)
    reviewed = [
        original[0],
        original[1],
        original[2].model_copy(update={"removed_snapshot_refs": ["P1", "Q1", "Q2"]}),
    ]
    comparator = RecordingComparator([_result(*original)], reviewed=reviewed)
    spring = RecordingSpring(candidates, snapshots)

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(UUID(int=1), UUID(int=2))
    )

    assert result.completed_count == 3
    assert result.failed_count == 0
    assert len(spring.completions) == 1
    first, second, recovery = spring.completions[0].decisions
    assert first.operation.value == "ADD"
    assert first.proposed_fact_value == original[0].proposed_fact_value
    assert first.proposed_value_json == original[0].proposed_value_json
    assert first.dependency_candidate_refs == []
    assert second.operation.value == "UPDATE"
    assert second.target_snapshot_ref == "P2"
    assert second.proposed_fact_value == original[1].proposed_fact_value
    assert second.proposed_value_json == original[1].proposed_value_json
    assert second.dependency_candidate_refs == []
    assert recovery.candidate_ref == "C3"
    assert recovery.operation.value == "REMOVE"
    assert recovery.removed_snapshot_refs == ["P1", "Q1", "Q2"]
    assert recovery.dependency_candidate_refs == ["C1", "C2"]
    assert recovery.proposed_fact_value is None
    assert recovery.proposed_value_json is None
    assert "P3" not in recovery.removed_snapshot_refs
    assert spring.completions[0].failures == []
    assert candidates == original_candidates
    assert original == original_decisions


class RecordingComparator:
    def __init__(self, outcomes, *, max_candidates=20, reviewed=None):
        self.outcomes = deque(outcomes)
        self.max_candidates = max_candidates
        self.reviewed = reviewed
        self.candidate_calls = []
        self.snapshot_calls = []
        self.lifecycle_calls = []

    def batch_fits(self, *, candidates, **kwargs):
        return 0 < len(candidates) <= self.max_candidates

    async def compare_batch(self, *, candidates, snapshot_entries, **kwargs):
        self.candidate_calls.append([candidate.candidate_ref for candidate in candidates])
        self.snapshot_calls.append([entry.reference for entry in snapshot_entries])
        result = self.outcomes.popleft()
        if isinstance(result, Exception):
            raise result
        return result, result.model_dump(mode="json")

    async def reconcile_status_lifecycle(self, **kwargs):
        self.lifecycle_calls.append(deepcopy(kwargs))
        if isinstance(self.reviewed, Exception):
            raise self.reviewed
        return kwargs["decisions"] if self.reviewed is None else self.reviewed


class RecordingSpring:
    def __init__(self, candidates, snapshots):
        batch = WorkerCharacterFactComparisonBatchPayload(
            comparison_batch_id=UUID(int=3),
            work_id=UUID(int=4),
            source_episode_id=UUID(int=5),
            character_ref="K1",
            matched_character_name="리안",
            canonical_fact_type="STATUS",
            candidates=candidates,
        )
        self.batches = deque([batch])
        self.context = WorkerCharacterFactComparisonBatchContextResponse(
            comparison_batch_id=batch.comparison_batch_id,
            character_ref="K1",
            matched_character_name="리안",
            canonical_fact_type="STATUS",
            base_snapshot_version=3,
            candidates=candidates,
            snapshot_entries=snapshots,
            context_token="a" * 64,
        )
        self.completions = []
        self.failures = []

    async def claim_next_character_fact_comparison_batch(self, *args):
        return self.batches.popleft() if self.batches else None

    async def get_character_fact_comparison_batch_context(self, *args):
        return self.context

    async def complete_character_fact_comparison_batch(self, *args):
        self.completions.append(args[-1])

    async def fail_character_fact_comparison_batch(self, *args):
        self.failures.append(args[-1].value)


def _execute(comparator, candidates, snapshots, *, fact_type="STATUS"):
    return asyncio.run(
        execute_character_fact_comparison_batch(
            comparator,
            matched_character_name="리안",
            canonical_fact_type=fact_type,
            candidates=candidates,
            snapshot_entries=snapshots,
        )
    )


def _status_case():
    candidates = [
        _candidate("C1", "status.위기", "출혈로 생명이 위태롭다."),
        _candidate("C2", "status.출혈", "상처에서 출혈이 계속됐다."),
        _candidate("C3", "status.발_부상", "상처가 아물고 혼자 달렸다.", active=False),
    ]
    snapshots = [
        _snapshot("P1", "status.발_부상", "발을 다쳤다."),
        _snapshot("P2", "status.출혈", "상처에서 피가 난다."),
        _snapshot("P3", "status.저주", "저주가 남아 있다."),
    ]
    decisions = [
        _decision("C1", "ADD", "status.위기", value="출혈로 생명이 위태롭다."),
        _decision("C2", "UPDATE", "status.출혈", value="출혈이 계속됐다.", target="P2"),
        _decision("C3", "REMOVE", "status.발_부상", removed=["P1"]),
    ]
    return candidates, snapshots, decisions


def _candidate(ref, key, value, *, active=True):
    return WorkerCharacterFactComparisonBatchCandidate(
        candidate_ref=ref,
        projected_snapshot_ref=f"Q{ref[1:]}",
        source_episode_no=5,
        raw_fact_key=key,
        initial_canonical_fact_key=key,
        canonical_key_resolution="PATTERN",
        attribute_value=value,
        value_type="JSON",
        value_json={"active": active},
        evidence_spans=[{"quote": value}],
        confidence=0.95,
    )


def _snapshot(ref, key, value):
    return WorkerCharacterFactComparisonBatchSnapshotEntry(
        snapshot_ref=ref,
        origin="PERSISTED",
        fact_type="STATUS",
        fact_key=key,
        fact_value=value,
        value_json={"active": True},
    )


def _decision(ref, operation, key, *, value=None, target=None, removed=None):
    return CharacterFactComparisonBatchDecision(
        candidate_ref=ref,
        operation=operation,
        resolved_canonical_fact_key=key,
        target_ref=target,
        removed_snapshot_refs=removed or [],
        proposed_fact_value=value,
        proposed_value_json=None if value is None else {"active": True},
        temporal_scope="PRESENT",
        comparison_reason=f"{ref} 원문 관찰을 반영한다.",
    )


def _result(*decisions):
    return CharacterFactComparisonBatchResult(decisions=list(decisions))


def _review_error(error_type):
    if error_type is AiTokenQuotaExhaustedError:
        return error_type()
    return error_type("lifecycle review failed")
