"""Exercise automatic partial recovery through the real pipeline and Spring DTO boundary.

Only the provider and Spring transport are fakes: recovery, coverage validation,
DTO construction, context retry, and aggregate result accounting run unchanged.
"""

import asyncio
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.analysis.world_setting_schemas import WorldSettingComparisonBatchResult
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.domain.enums import AnalysisFailureCode
from app.schemas.worker import WorkerWorldSettingComparisonBatchCompleteRequest
from tests.test_ordered_analysis_runtime import _input_context
from tests.test_world_setting_batch_pipeline import (
    ANALYSIS_JOB_ID,
    LEASE_TOKEN,
    TARGET_ID,
    FakeBatchSpringApi,
    FakeUsageClient,
    _batch,
)


class ContextSpring(FakeBatchSpringApi):
    """Versioned synthetic context; stale completion changes only the next snapshot."""

    async def get_world_setting_comparison_batch_context(
        self, job_id, batch_id, lease_token, target_ids, *, provisional_subject_keys=None,
    ):
        assert provisional_subject_keys in (None, [])
        response = await super().get_world_setting_comparison_batch_context(
            job_id, batch_id, lease_token, target_ids,
        )
        context = self.all_batches[batch_id].analysis_context
        return response.model_copy(update={
            "analysis_context": context,
            "context_token": f"{response.targets[0].version:064x}" if context else None,
        })

    async def fail_world_setting_comparison_batch(self, *args, diagnostics=None, **kwargs):
        await super().fail_world_setting_comparison_batch(*args, **kwargs)


def _decision(candidate, version):
    return {
        "source_candidate_refs": [candidate.candidate_ref],
        "consolidation_status": "SINGLE",
        "operation": "ADD",
        "target_ref": "T1",
        "proposed_scope_name": candidate.scope_name,
        "proposed_setting_name": candidate.setting_name,
        "proposed_value": f"{candidate.extracted_value} (비교 기준 {version})",
        "comparison_reason": "원문에 명시된 별도의 사실을 추가합니다.",
    }


class RecoveringComparator:
    max_attempts = 3

    def __init__(self, action):
        self.action = action
        self.calls = []
        self.llm_client = FakeUsageClient()

    async def compare_batch(self, category, candidates, targets, **kwargs):
        self.calls.append({
            "refs": [candidate.candidate_ref for candidate in candidates],
            "version": targets[0].version,
            "target_values": [item.value for item in targets[0].properties],
            "options": kwargs,
        })
        self.llm_client.record(input_tokens=20, output_tokens=4)
        output = self.action(candidates, targets[0].version)
        if isinstance(output, Exception):
            raise output
        result = WorldSettingComparisonBatchResult(decisions=output)
        return result, result.model_dump(mode="json")


def _spring(*, ordered=True, stale_completion_count=0, queued_batch=False):
    context = _input_context() if ordered else None
    batch = _batch().model_copy(update={"analysis_context": context})
    batches = [batch]
    if queued_batch:
        batches.append(batch.model_copy(update={"comparison_batch_id": uuid4()}))
    return ContextSpring(batches, stale_completion_count), context


def _process(spring, comparator, context, *, automatic=True):
    return WorldSettingComparisonPipeline(spring, object(), comparator).process_all(
        ANALYSIS_JOB_ID, LEASE_TOKEN, analysis_context=context,
        continue_on_candidate_failure=automatic,
    )


def _mixed_action(candidates, version):
    if len(candidates) > 1 or candidates[0].candidate_ref == "C2":
        return ComparisonValidationError("synthetic invalid response; do not salvage")
    return [_decision(candidates[0], version)]


def test_automatic_partial_completion_covers_every_source_and_counts_only_valid_results():
    spring, context = _spring()
    comparator = RecoveringComparator(_mixed_action)
    original_sources = spring.batches[0].model_dump(mode="json")["candidates"]

    result = asyncio.run(_process(spring, comparator, context))

    assert (result.completed_count, result.failed_count, result.decision_count) == (1, 1, 1)
    assert result.first_failure_code == AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert result.batch_validation_failure_count == 1
    assert spring.failures == []
    assert len(spring.completions) == 1
    # Validate the serialized contract, including failures beside successful decisions.
    request = WorkerWorldSettingComparisonBatchCompleteRequest.model_validate(
        spring.completions[0].model_dump(mode="json", by_alias=True),
    )
    assert [item.source_candidate_refs for item in request.decisions] == [["C1"]]
    assert [item.source_candidate_refs for item in request.failures] == [["C2"]]
    assert request.failures[0].failure_code == AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert request.decisions[0].target_world_setting_id == TARGET_ID
    assert request.context_token == f"{7:064x}"
    assert request.context_versions[0].version == 7
    assert request.diagnostics and request.failures[0].diagnostics
    assert [call["refs"] for call in comparator.calls] == [["C1", "C2"], ["C1"], ["C2"]]
    assert all(call["version"] == 7 for call in comparator.calls)
    assert all(call["target_values"] == ["기존 무기 정보 v7"] for call in comparator.calls)
    assert comparator.calls[0]["options"] == {"ordered_context": True}
    assert all(call["options"] == {
        "ordered_context": True, "max_attempts_override": 1, "preserve_source_paths": True,
    } for call in comparator.calls[1:])
    assert next(iter(spring.all_batches.values())).model_dump(mode="json")["candidates"] == original_sources
    assert result.batch_usages[0].provider_request_count == 3
    assert sum(item.provider_request_count for item in result.cluster_usages) == 3


def test_all_recovery_groups_can_finish_as_typed_failures_without_any_decision():
    spring, context = _spring()
    comparator = RecoveringComparator(lambda *_: ComparisonValidationError("synthetic invalid"))

    result = asyncio.run(_process(spring, comparator, context))

    assert (result.completed_count, result.failed_count, result.decision_count) == (0, 2, 0)
    assert result.first_failure_code == AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert len(spring.completions) == 1 and spring.failures == []
    request = WorkerWorldSettingComparisonBatchCompleteRequest.model_validate_json(
        spring.completions[0].model_dump_json(by_alias=True),
    )
    assert request.decisions == []
    assert [item.source_candidate_refs for item in request.failures] == [["C1"], ["C2"]]
    assert len(comparator.calls) == 3


@pytest.mark.parametrize("ordered", [True, False], ids=["ordered-manual", "legacy"])
def test_manual_and_legacy_do_not_attempt_partial_recovery(ordered):
    spring, context = _spring(ordered=ordered)
    comparator = RecoveringComparator(_mixed_action)
    # Even the continuation flag alone cannot enable recovery without ordered input.
    call = _process(spring, comparator, context, automatic=not ordered)
    if ordered:
        with pytest.raises(ComparisonValidationError):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert (result.completed_count, result.failed_count) == (0, 2)
    assert len(comparator.calls) == 1
    assert "preserve_source_paths" not in comparator.calls[0]["options"]
    assert spring.completions == []
    assert len(spring.failures) == 1
    assert spring.failures[0][2] == "COMPARISON_VALIDATION_FAILED"


@pytest.mark.parametrize("during_recovery", [False, True], ids=["initial-call", "after-valid-group"])
def test_quota_during_comparison_aborts_without_partial_completion_or_next_claim(during_recovery):
    spring, context = _spring(queued_batch=True)

    def action(candidates, version):
        if not during_recovery:
            return AiTokenQuotaExhaustedError()
        if len(candidates) > 1:
            return ComparisonValidationError("synthetic invalid batch")
        if candidates[0].candidate_ref == "C1":
            return [_decision(candidates[0], version)]
        return AiTokenQuotaExhaustedError()

    comparator = RecoveringComparator(action)
    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(_process(spring, comparator, context))
    assert spring.completions == []
    assert len(spring.failures) == 1
    assert spring.failures[0][2] == "AI_TOKEN_QUOTA_EXHAUSTED"
    assert spring.claim_count == 1 and len(spring.batches) == 1
    assert len(comparator.calls) == (3 if during_recovery else 1)


def test_stale_partial_completion_regenerates_both_decisions_and_failures_from_new_context():
    spring, context = _spring(stale_completion_count=1)

    def action(candidates, version):
        if len(candidates) > 1:
            return ComparisonValidationError("synthetic invalid batch")
        if version == 7 and candidates[0].candidate_ref == "C2":
            return ComparisonValidationError("synthetic first-context failure")
        return [_decision(candidates[0], version)]

    comparator = RecoveringComparator(action)
    result = asyncio.run(_process(spring, comparator, context))

    assert result.stale_batch_retry_count == 1
    assert (result.completed_count, result.failed_count, result.decision_count) == (2, 0, 2)
    assert result.first_failure_code is None
    assert len(spring.completions) == 2 and spring.failures == []
    stale, accepted = spring.completions
    assert [item.source_candidate_refs for item in stale.failures] == [["C2"]]
    assert accepted.failures is None
    assert [item.source_candidate_refs for item in accepted.decisions] == [["C1"], ["C2"]]
    assert all(item.proposed_value.endswith("(비교 기준 8)") for item in accepted.decisions)
    assert [request.context_token for request in spring.completions] == [f"{7:064x}", f"{8:064x}"]
    assert [request.context_versions[0].version for request in spring.completions] == [7, 8]
    assert [call["version"] for call in comparator.calls] == [7, 7, 7, 8, 8, 8]
    assert result.batch_usages[0].provider_request_count == 6
    assert sum(item.provider_request_count for item in result.cluster_usages) == 6


def test_stale_context_does_not_reset_batch_recovery_call_budget(monkeypatch):
    monkeypatch.setattr("app.analysis.world_setting_pipeline.MAX_RECOVERY_CALLS", 2)
    spring, context = _spring(stale_completion_count=1)
    comparator = RecoveringComparator(_mixed_action)

    result = asyncio.run(_process(spring, comparator, context))

    assert result.stale_batch_retry_count == 1
    assert (result.completed_count, result.failed_count) == (0, 2)
    assert [call["version"] for call in comparator.calls] == [7, 7, 7, 8]
    assert len(spring.completions) == 2
    accepted = spring.completions[-1]
    assert accepted.decisions == []
    assert {ref for item in accepted.failures for ref in item.source_candidate_refs} == {"C1", "C2"}
    assert all(item.diagnostics[-1].rule == "RECOVERY_CALL_LIMIT" for item in accepted.failures)
