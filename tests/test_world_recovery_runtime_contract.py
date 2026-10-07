"""Production recovery remains typed and source-complete in the FIXED adapter."""

import asyncio
import json
from uuid import UUID

import pytest
import httpx

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.clients.exceptions import WorkerLeaseExpiredError
from app.llm.exceptions import LlmOutputTruncatedError, LlmResponseValidationError
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerWorldSettingCandidatePayload
from evals.multi_stage_setting.processing import ProcessingTrace
from evals.multi_stage_setting.runtime_adapter import _run_world_batches, _WorldBatchSource, _WorldTargetSet


def sources():
    targets = _WorldTargetSet([], {})
    return [_WorldBatchSource(str(UUID(int=index)), WorkerWorldSettingCandidatePayload(
        candidate_id=UUID(int=index), work_id=UUID(int=10), source_episode_id=UUID(int=11),
        category="RACE", subject_name="산악족", scope_name=None, setting_name=name,
        extracted_value=value, evidence_spans=[{"quote": value}],
    ), targets) for index, (name, value) in enumerate([
        ("출입", "초대장이 있어야 입장한다"), ("교육", "아이들은 배운다"), ("성장", "어른들이 가르친다"),
    ], 1)]


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LlmTextResponse(text=response if isinstance(response, str) else json.dumps(response))


def response():
    return {"decisions": [
        {"source_candidate_refs": ["C1"], "consolidation_status": "SINGLE", "operation": "EXCLUDE",
         "target_ref": "T99", "matched_property_name": "출입", "proposed_scope_name": None,
         "proposed_setting_name": "출입", "proposed_value": "초대장이 있어야 입장한다",
         "comparison_reason": "기존 조건과 같습니다"},
        {"source_candidate_refs": ["C2", "C3"], "consolidation_status": "MERGED", "operation": "ADD",
         "target_ref": None, "proposed_scope_name": None, "proposed_setting_name": "성장 교육",
         "proposed_value": "어른들에게 배우며 자란다", "comparison_reason": "같은 성장 과정의 정보를 합칩니다"},
    ]}


@pytest.mark.parametrize("error,code", [
    (LlmOutputTruncatedError("output stopped", incomplete_reason="max_output_tokens", max_output_tokens=16000),
     "LLM_OUTPUT_TRUNCATED"),
    (LlmResponseValidationError("invalid provider response"), "LLM_RESPONSE_PARSE_ERROR"),
])
def test_fixed_preserves_merged_sources_and_typed_failed_retry(error, code):
    if isinstance(error, LlmTextResponse):
        error = error.text
    client = Client([response(), error])
    trace = ProcessingTrace()
    predictions, failures = asyncio.run(_run_world_batches(
        sources(), WorldSettingComparator(llm_client=client), trace=trace,
    ))
    assert len(client.requests) == 2
    assert len(predictions) == 1
    assert predictions[0].source_candidate_ids == [str(UUID(int=2)), str(UUID(int=3))]
    assert predictions[0].proposed_setting_name == "성장 교육"
    assert [(item.source_id, item.error_type) for item in failures] == [(str(UUID(int=1)), code)]
    assert len(trace.records) == 3
    assert {item.candidate_id for item in trace.records} == {str(UUID(int=index)) for index in range(1, 4)}
    assert trace.records[0].failure_code == code
    assert all(item.status == "COMPARED" for item in trace.records[1:])
    assert failures[0].comparison_attempts[0].decisions[0].source_candidate_ids == [str(UUID(int=1))]


@pytest.mark.parametrize("error", [
    OrderedInputContextError("frozen context invalid"), WorkerLeaseExpiredError(
        "lease expired", request=httpx.Request("POST", "https://synthetic.test"), response=httpx.Response(409),
    ),
    RuntimeError("unexpected implementation failure"),
])
def test_fixed_job_boundary_stops_instead_of_converting_fault_to_candidate_failures(error):
    class FatalComparator(WorldSettingComparator):
        async def compare_batch(self, *args, **kwargs):
            raise error

    trace = ProcessingTrace()
    with pytest.raises(type(error)) as caught:
        asyncio.run(_run_world_batches(sources(), FatalComparator(llm_client=Client([])), trace=trace))
    assert caught.value is error
    assert trace.records == []


def test_unexpected_fault_during_retry_does_not_inherit_initial_response_failure():
    error = RuntimeError("unexpected implementation failure during retry")
    client = Client([response(), error])
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(_run_world_batches(sources(), WorldSettingComparator(llm_client=client)))
    assert caught.value is error
    assert len(client.requests) == 2
    assert error.__context__ is None
