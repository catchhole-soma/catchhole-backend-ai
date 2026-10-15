"""Complete-prompt budgeting must not cap character candidate counts."""
import asyncio
import json

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline, execute_character_fact_comparison_batch
from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchResult
from app.core.config import Settings
from app.llm.exceptions import LlmOutputTruncatedError
from app.schemas.worker import WorkerCharacterFactComparisonBatchPayload, WorkerCharacterFactComparisonBatchContextResponse, WorkerCharacterFactComparisonBatchCompleteRequest
from tests.test_character_fact_batch import (
    ANALYSIS_JOB_ID, LEASE_TOKEN, FakeBatchComparator, FakeBatchSpring, FakeTextClient,
    _batch, _candidate, _context, _decision, _decision_payload,
)


def test_budget_defaults_allow_full_output_and_input_context():
    settings = Settings(_env_file=None)
    assert settings.llm_character_fact_batch_comparison_max_input_tokens == 256_000
    assert settings.llm_character_fact_batch_comparison_max_output_tokens == 128_000


def test_more_than_twenty_candidates_and_dependencies_survive_wire_provider_and_completion():
    candidates = [_candidate(f"C{i}", f"Q{i}", f"상태 {i}") for i in range(1, 26)]
    raw = _batch().model_dump(by_alias=True, mode="json")
    raw["candidates"] = [c.model_dump(by_alias=True, mode="json") for c in candidates]
    batch = WorkerCharacterFactComparisonBatchPayload.model_validate(raw)
    context = WorkerCharacterFactComparisonBatchContextResponse.model_validate(
        _context(batch).model_dump(by_alias=True, mode="json"))
    rows = [_decision_payload("C1", operation="ADD", resolved_key="status.다리_상태", value="상태 1")]
    rows.extend(_decision_payload(f"C{i}", operation="MERGE", resolved_key="status.다리_상태",
        target_ref=f"Q{i-1}", value=f"상태 {i}") for i in range(2, 26))
    assert len(CharacterFactComparisonBatchResult.model_validate({"decisions": rows}).decisions) == 25
    provider = FakeTextClient({"decisions": rows})
    comparator = CharacterFactComparator(llm_client=provider, max_attempts=1)
    assert asyncio.run(comparator.batch_fits(matched_character_name=batch.matched_character_name,
        canonical_fact_type="STATUS", candidates=candidates, snapshot_entries=[]))
    spring = FakeBatchSpring(batch, context)
    result = asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))
    assert (result.completed_count, result.failed_count) == (25, 0)
    assert len(provider.requests) == 1
    assert provider.requests[0]["max_output_tokens"] == 128_000
    assert [c["candidate_ref"] for c in json.loads(provider.requests[0]["user_prompt"])["candidates"]] == [f"C{i}" for i in range(1, 26)]
    completion = WorkerCharacterFactComparisonBatchCompleteRequest.model_validate(
        spring.completions[0].model_dump(by_alias=True, mode="json"))
    assert [d.candidate_ref for d in completion.decisions] == [f"C{i}" for i in range(1, 26)]
    assert completion.decisions[-1].dependency_candidate_refs == [f"C{i}" for i in range(1, 25)]
    assert completion.failures == []


def _truncated():
    return LlmOutputTruncatedError("output cap", incomplete_reason="max_output_tokens", max_output_tokens=128_000)


def _adds(refs):
    return CharacterFactComparisonBatchResult(decisions=[_decision(ref, operation="ADD",
        resolved_key=f"status.상태_{ref}", value=f"상태 {ref}") for ref in refs])


@pytest.mark.parametrize("ordered", [False, True])
def test_only_output_truncated_segment_is_halved_in_source_order(ordered):
    refs = [f"C{i}" for i in range(1, 6)]
    comparator = FakeBatchComparator([
        _truncated(), _adds(refs[:2]), _truncated(), _adds(refs[2:3]), _adds(refs[3:]),
    ])
    result = asyncio.run(execute_character_fact_comparison_batch(comparator,
        matched_character_name="서린", canonical_fact_type="STATUS",
        candidates=[_candidate(ref, f"Q{i}", ref) for i, ref in enumerate(refs, 1)],
        snapshot_entries=[], ordered_context=ordered))
    assert comparator.calls == [refs, refs[:2], refs[2:], refs[2:3], refs[3:]]
    assert [row.candidate_ref for row in result.decisions] == refs
    assert result.failures == []
    assert result.singleton_fallback_count == 0


@pytest.mark.parametrize("ordered", [False, True])
def test_truncated_singleton_failure_keeps_prior_success_and_continues_remaining_half(ordered):
    refs = [f"C{i}" for i in range(1, 6)]
    comparator = FakeBatchComparator([
        _adds(refs[:3]), _truncated(), _truncated(), _adds(refs[4:]),
    ], max_candidates_per_call=3)
    result = asyncio.run(execute_character_fact_comparison_batch(comparator,
        matched_character_name="서린", canonical_fact_type="STATUS",
        candidates=[_candidate(ref, f"Q{i}", ref) for i, ref in enumerate(refs, 1)],
        snapshot_entries=[], ordered_context=ordered))
    assert comparator.calls == [refs[:3], refs[3:], refs[3:4], refs[4:]]
    assert [row.candidate_ref for row in result.decisions] == [*refs[:3], refs[4]]
    assert [(row.candidate_ref, row.failure_code) for row in result.failures] == [("C4", "LLM_OUTPUT_TRUNCATED")]


@pytest.mark.parametrize("ordered", [False, True])
def test_shared_mandatory_context_overflow_stops_remainder_without_trimming(ordered):
    class OverflowAfterSuccess(FakeBatchComparator):
        def batch_fits(self, *, candidates, snapshot_entries, **kwargs):
            return not snapshot_entries and len(candidates) == 1

        def mandatory_context_fits(self, **kwargs):
            # The first accepted decision grows the mandatory projected context.
            return not kwargs["snapshot_entries"]

    refs = ["C1", "C2", "C3"]
    comparator = OverflowAfterSuccess([_adds(refs[:1])], max_candidates_per_call=1)
    result = asyncio.run(execute_character_fact_comparison_batch(comparator,
        matched_character_name="서린", canonical_fact_type="STATUS",
        candidates=[_candidate(ref, f"Q{i}", ref) for i, ref in enumerate(refs, 1)],
        snapshot_entries=[], ordered_context=ordered,
        advisory_current_context={"currentNarrativeTime": None, "currentSlots": [], "historyFacts": []}))
    assert comparator.calls == [["C1"]]
    assert [row.candidate_ref for row in result.decisions] == ["C1"]
    assert [row.candidate_ref for row in result.failures] == ["C2", "C3"]
    assert all(row.error_message == "character_batch_required_context_input_limit_exceeded" for row in result.failures)


def test_batch_remove_can_cover_more_than_thirty_projected_slots_with_all_dependencies():
    refs = [f"C{i}" for i in range(1, 36)]
    candidates = [_candidate(ref, f"Q{i}", ref) for i, ref in enumerate(refs, 1)]
    candidates.append(_candidate("C36", "Q36", "모든 부상 회복").model_copy(update={"value_json": {"active": False}}))
    rows = [_decision_payload(ref, operation="ADD", resolved_key=f"status.부상_{i}", value=ref)
        for i, ref in enumerate(refs, 1)]
    rows.append(_decision_payload("C36", operation="REMOVE", resolved_key="status.다리_상태",
        removed_refs=[f"Q{i}" for i in range(1, 36)], value=None))
    raw = _batch().model_dump(by_alias=True, mode="json")
    raw["candidates"] = [row.model_dump(by_alias=True, mode="json") for row in candidates]
    batch = WorkerCharacterFactComparisonBatchPayload.model_validate(raw)
    spring = FakeBatchSpring(batch, _context(batch))
    provider = FakeTextClient({"decisions": rows})
    result = asyncio.run(CharacterFactComparisonPipeline(spring,
        CharacterFactComparator(llm_client=provider, max_attempts=1)).process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))
    assert (result.completed_count, result.failed_count) == (36, 0)
    last = WorkerCharacterFactComparisonBatchCompleteRequest.model_validate(
        spring.completions[0].model_dump(by_alias=True, mode="json")).decisions[-1]
    assert last.removed_snapshot_refs == [f"Q{i}" for i in range(1, 36)]
    assert last.dependency_candidate_refs == refs
    assert len(provider.requests) == 1


@pytest.mark.parametrize("ordered", [False, True])
def test_output_truncation_with_fatal_context_cause_is_never_recovered(ordered):
    from app.analysis.exceptions import OrderedInputContextError
    error = _truncated()
    error.__cause__ = OrderedInputContextError("frozen context is invalid")
    comparator = FakeBatchComparator([error])
    with pytest.raises(LlmOutputTruncatedError) as raised:
        asyncio.run(execute_character_fact_comparison_batch(comparator,
            matched_character_name="서린", canonical_fact_type="STATUS",
            candidates=[_candidate("C1", "Q1", "첫 상태"), _candidate("C2", "Q2", "둘째 상태")],
            snapshot_entries=[], ordered_context=ordered))
    assert raised.value is error
    assert comparator.calls == [["C1", "C2"]]


@pytest.mark.parametrize("ordered", [False, True])
def test_mandatory_empty_candidate_probe_keeps_book_status_time_instructions_and_context(monkeypatch, ordered):
    from app.schemas.analysis_source import WorkerAnalysisSource
    from app.analysis.narrative_time import NarrativeTime
    from tests.test_setting_book_source_contract import SOURCE
    import app.analysis.character_fact_comparator as module
    source = WorkerAnalysisSource.model_validate(SOURCE)
    candidate = _candidate("C1", "Q1", "화상").model_copy(update={"source": source,
        "source_episode_no": None, "narrative_time": NarrativeTime.model_validate({"factTime": None,
        "referenceTime": {"description": "회복 이후", "evidenceSpans": [{"quote": "회복 이후", "startOffset": 1, "endOffset": 6}]}})})
    advisory = {"currentNarrativeTime": None, "currentSlots": [], "historyFacts": [{
        "factType": "STATUS", "factKey": "status.화상", "factValue": "회복됨", "operation": "REMOVE"}]}
    captured = []
    monkeypatch.setattr(module, "_estimate_prompt_tokens", lambda system, user, model: captured.append((system, json.loads(user))) or 10)
    comparator = CharacterFactComparator(llm_client=FakeTextClient({}), max_attempts=1)
    kwargs = dict(matched_character_name="서린", canonical_fact_type="STATUS", candidates=[candidate],
        snapshot_entries=[], ordered_context=ordered, advisory_current_context=advisory)
    assert asyncio.run(comparator.batch_fits(**kwargs))
    assert asyncio.run(comparator.mandatory_context_fits(**kwargs))
    full, mandatory = captured
    assert mandatory[0] == full[0]
    assert "설정집" in mandatory[0]
    assert mandatory[1] == {**full[1], "candidates": []}
    assert mandatory[1]["advisory_current_context"]["historyFacts"][0]["operation"] == "REMOVE"
    assert full[1]["candidates"][0]["evidence_spans"][0]["quote"] == "화상"
