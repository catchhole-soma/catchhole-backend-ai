"""Automatic review isolates candidate failures without weakening job ownership/input fences."""

import asyncio
from collections import deque
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.analysis.character_fact_comparison_pipeline import (
    CharacterFactComparisonPipeline, CharacterFactComparisonRunResult,
)
from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchResult
from app.analysis.exceptions import (
    ComparisonValidationError, OrderedAnalysisIncompleteError, OrderedInputContextError,
)
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline, WorldSettingComparisonRunResult
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError, WorkerLeaseExpiredError
from app.domain.enums import AnalysisFailureCode, AnalysisReviewMode, AnalysisJobCheckpointStage
from app.exceptions.failure_classification import is_candidate_comparison_failure
from app.models.setting_candidate import SettingCandidate
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker
from tests.test_analysis_job_worker import FakeSpringWorkerClient, _payload
from tests.test_character_fact_batch import FakeBatchComparator, FakeBatchSpring, _batch, _context, _decision
from tests.test_ordered_analysis_runtime import _input_context, _ordered_payload
from tests.test_world_setting_batch_pipeline import FakeBatchSpringApi, _batch as world_batch


def test_review_mode_is_explicit_and_candidate_default_matches_database():
    assert _payload().review_mode == AnalysisReviewMode.MANUAL
    assert WorkerAnalysisJobPayload.model_validate({**_payload().model_dump(), "review_mode": None}).review_mode == AnalysisReviewMode.MANUAL
    automatic = WorkerAnalysisJobPayload.model_validate({**_ordered_payload(), "review_mode": "AUTOMATIC"})
    assert automatic.review_mode == AnalysisReviewMode.AUTOMATIC
    with pytest.raises(ValidationError, match="requires ordered"):
        WorkerAnalysisJobPayload.model_validate({**_payload().model_dump(), "review_mode": "AUTOMATIC"})
    column = SettingCandidate.__table__.c.reviewed_automatically
    assert column.nullable is False and column.default.arg is False
    assert str(column.server_default.arg.compile(dialect=postgresql.dialect())) == "false"


def test_automatic_character_failure_keeps_validated_partial_batch_and_continues_other_batches():
    context = _input_context()
    first = _batch().model_copy(update={"analysis_context": context})
    second = first.model_copy(update={"comparison_batch_id": uuid4(), "candidates": first.candidates[:1]})

    class Spring(FakeBatchSpring):
        async def get_character_fact_comparison_batch_context(self, job_id, batch_id, token):
            batch = first if batch_id == first.comparison_batch_id else second
            return _context(batch).model_copy(update={"analysis_context": context})

    success = CharacterFactComparisonBatchResult(decisions=[
        _decision("C1", operation="ADD", resolved_key="status.부상", value="다리를 다침"),
    ])
    comparator = FakeBatchComparator([
        ComparisonValidationError("synthetic invalid batch"), success,
        httpx.TimeoutException("synthetic provider timeout"), success,
    ])
    spring = Spring(first, _context(first))
    spring.batches = deque([first, second])
    result = asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(
        uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
    ))
    assert result.failed_count == 1 and result.completed_count == 2
    assert [row.candidate_ref for row in spring.completions[0].decisions] == ["C1"]
    assert [row.candidate_ref for row in spring.completions[0].failures] == ["C2"]
    assert len(spring.completions[1].decisions) == 1
    assert spring.completions[1].failures == []
    assert not spring.batches


@pytest.mark.parametrize("automatic", [False, True])
def test_world_continues_after_candidate_failure_only_when_explicitly_automatic(automatic):
    context = _input_context()
    first = world_batch().model_copy(update={"analysis_context": context})
    second = first.model_copy(update={"comparison_batch_id": uuid4()})
    spring = FakeBatchSpringApi([first, second])

    class Pipeline(WorldSettingComparisonPipeline):
        async def _compare_batch_with_fresh_context(self, job, token, batch, *args, **kwargs):
            if batch.comparison_batch_id == first.comparison_batch_id:
                raise ComparisonValidationError("synthetic invalid response")
            return SimpleNamespace(candidate_count=2, decision_count=1, cluster_count=1,
                                   clustered_candidate_count=2, singleton_candidate_count=0, stale_retry_count=0,
                                   failed_count=0, first_failure_code=None)

    call = Pipeline(spring, object(), object()).process_all(
        uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=automatic,
    )
    if automatic:
        result = asyncio.run(call)
        assert result.failed_count == 2 and result.completed_count == 2
        assert spring.claim_count == 3 and not spring.batches
    else:
        with pytest.raises(ComparisonValidationError):
            asyncio.run(call)
        assert spring.claim_count == 1 and len(spring.batches) == 1
    assert len(spring.failures) == 1


def _lease_error():
    request = httpx.Request("POST", "https://synthetic.invalid/claim")
    return WorkerLeaseExpiredError("synthetic lease expired", request=request,
                                  response=httpx.Response(409, request=request))


def _provider_account_error(status, code):
    request = httpx.Request("POST", "https://synthetic.invalid/responses")
    return httpx.HTTPStatusError("synthetic provider account failure", request=request,
                                response=httpx.Response(status, request=request, json={"error": {"code": code}}))


@pytest.mark.parametrize("failure", [
    AiTokenQuotaExhaustedError(), _lease_error(), OrderedInputContextError("synthetic input changed"),
    RuntimeError("synthetic unexpected bug"),
    _provider_account_error(401, "invalid_api_key"),
    _provider_account_error(403, "permission_denied"),
    _provider_account_error(429, "insufficient_quota"),
])
def test_automatic_world_does_not_swallow_job_failures(failure):
    context = _input_context()
    first = world_batch().model_copy(update={"analysis_context": context})
    spring = FakeBatchSpringApi([first, first.model_copy(update={"comparison_batch_id": uuid4()})])

    class Pipeline(WorldSettingComparisonPipeline):
        async def _compare_batch_with_fresh_context(self, *args, **kwargs):
            raise failure

    with pytest.raises(type(failure)):
        asyncio.run(Pipeline(spring, object(), object()).process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
        ))
    assert spring.claim_count == 1 and len(spring.batches) == 1
    assert is_candidate_comparison_failure(failure) is False


def test_automatic_character_rejects_frozen_context_change_before_provider():
    context = _input_context()
    first = _batch().model_copy(update={"analysis_context": context})
    spring = FakeBatchSpring(first, _context(first).model_copy(update={"analysis_context": _input_context()}))
    spring.batches.append(first.model_copy(update={"comparison_batch_id": uuid4()}))
    comparator = FakeBatchComparator([])
    with pytest.raises(OrderedInputContextError):
        asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
        ))
    assert comparator.calls == [] and len(spring.batches) == 1
    assert spring.failures[0][-1] == "UNEXPECTED_ERROR"


def test_automatic_world_rejects_frozen_context_change_with_non_deferrable_failure():
    context = _input_context()
    first = world_batch().model_copy(update={"analysis_context": context})

    class Spring(FakeBatchSpringApi):
        async def get_world_setting_comparison_batch_context(self, *args, **kwargs):
            response = await super().get_world_setting_comparison_batch_context(*args)
            return response.model_copy(update={"analysis_context": _input_context()})

    spring = Spring([first, first.model_copy(update={"comparison_batch_id": uuid4()})])
    with pytest.raises(OrderedInputContextError):
        asyncio.run(WorldSettingComparisonPipeline(spring, object(), object()).process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
        ))
    assert spring.claim_count == 1 and len(spring.batches) == 1
    assert spring.completions == []
    assert spring.failures[0][2:] == ("UNEXPECTED_ERROR", None, None)


@pytest.mark.parametrize("automatic", [False, True])
def test_ordered_oversized_singleton_reports_blocking_failure_without_next_claim(automatic):
    context = _input_context()
    first = _batch().model_copy(update={"analysis_context": context})
    spring = FakeBatchSpring(first, _context(first).model_copy(update={"analysis_context": context}))
    spring.batches.append(first.model_copy(update={"comparison_batch_id": uuid4()}))
    comparator = FakeBatchComparator([], max_candidates_per_call=0)
    with pytest.raises(OrderedInputContextError, match="character_batch_input_limit_exceeded"):
        asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=automatic,
        ))
    assert comparator.calls == []
    assert spring.completions == []
    assert len(spring.batches) == 1
    assert len(spring.failures) == 1
    assert spring.failures[0][-1] == "UNEXPECTED_ERROR"


def test_automatic_character_does_not_fallback_after_wrapped_lease_failure():
    context = _input_context()
    first = _batch().model_copy(update={"analysis_context": context})
    spring = FakeBatchSpring(first, _context(first).model_copy(update={"analysis_context": context}))
    spring.batches.append(first.model_copy(update={"comparison_batch_id": uuid4()}))
    failure = ComparisonValidationError("synthetic wrapped lease failure")
    failure.__cause__ = _lease_error()
    comparator = FakeBatchComparator([failure])
    with pytest.raises(ComparisonValidationError):
        asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
        ))
    assert len(comparator.calls) == 1 and len(spring.batches) == 1
    assert spring.completions == []


@pytest.mark.parametrize(("status", "code"), [(401, "invalid_api_key"), (403, "permission_denied"),
                                            (429, "insufficient_quota")])
def test_automatic_character_stops_account_errors_without_singleton_fallback_or_next_claim(status, code):
    context = _input_context()
    first = _batch().model_copy(update={"analysis_context": context})
    spring = FakeBatchSpring(first, _context(first).model_copy(update={"analysis_context": context}))
    spring.batches.append(first.model_copy(update={"comparison_batch_id": uuid4()}))
    comparator = FakeBatchComparator([_provider_account_error(status, code)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
        ))
    assert len(comparator.calls) == 1 and len(spring.batches) == 1
    assert spring.completions == [] and spring.failures[0][-1] == "UNEXPECTED_ERROR"


def test_backend_validation_errors_are_not_reclassified_as_isolatable_provider_errors():
    request = httpx.Request("POST", "https://synthetic.invalid/complete")
    error = SpringWorkerHttpError("synthetic backend validation", request=request,
                                  response=httpx.Response(400, request=request),
                                  spring_error_code="WORLD_SETTING_COMPARISON_TARGET_INVALID")
    assert is_candidate_comparison_failure(error) is False


@pytest.mark.parametrize("automatic", [False, True])
def test_worker_reaches_completion_checkpoints_with_candidate_failures_only_in_automatic_mode(automatic):
    payload = WorkerAnalysisJobPayload.model_validate({
        **_ordered_payload(), "review_mode": "AUTOMATIC" if automatic else "MANUAL",
    })
    class Spring(FakeSpringWorkerClient):
        def __init__(self, job):
            super().__init__(job)
            self.checkpoints = []

        async def report_progress(self, job, lease, step, status=None, checkpoint_stage=None):
            self.checkpoints.append(checkpoint_stage)
            await super().report_progress(job, lease, step, status, checkpoint_stage)

    spring = Spring(payload)
    calls = []

    class Comparison:
        def __init__(self, result):
            self.result = result

        async def process_all(self, *args, **kwargs):
            assert kwargs.get("continue_on_candidate_failure", False) is automatic
            return self.result

    class Worker(AnalysisJobWorker):
        async def _run_chunk_stage(self, *args):
            return [], {}

        async def _run_character_stage(self, *args):
            return {}

        async def _run_world_extraction_stage(self, *args):
            calls.append("world")
            return 3

    worker = Worker(spring_client=spring,
                    character_fact_comparison_pipeline=Comparison(CharacterFactComparisonRunResult(
                        2, 1, AnalysisFailureCode.LLM_NETWORK_ERROR)),
                    world_setting_comparison_pipeline=Comparison(WorldSettingComparisonRunResult(
                        2, 1, AnalysisFailureCode.COMPARISON_VALIDATION_FAILED)))

    async def run():
        try:
            return await worker._run_analysis_steps(payload)
        finally:
            await worker.aclose()

    if automatic:
        result = asyncio.run(run())
        assert result.summary_json is not None
        assert calls == ["world"]
        assert spring.checkpoints[-1] == AnalysisJobCheckpointStage.WORLD_COMPARISONS_FINISHED
    else:
        with pytest.raises(OrderedAnalysisIncompleteError):
            asyncio.run(run())
        assert calls == spring.progress_calls == []
