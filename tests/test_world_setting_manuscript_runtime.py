"""The evaluation runner must use the same packing/recovery boundaries as workers."""

import asyncio
import json
from uuid import UUID

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_manuscript import WorldSettingManuscriptComparator
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerWorldSettingCandidatePayload
from app.schemas.analysis_context import AnalysisStateProvenance
from evals.multi_stage_setting.contracts import WorldStateEntry, world_state_ref
from evals.multi_stage_setting.runtime_adapter import _WorldBatchSource, _world_target_set, _run_world_batches


def sources():
    result = []
    for index in (1, 2):
        name = f"물약{index}"
        entry = WorldStateEntry(ref=world_state_ref("IMPORTANT_ITEM", name, None, "색"),
            category="IMPORTANT_ITEM", subject_name=name, setting_name="색", value="빨강")
        result.append(_WorldBatchSource(f"S{index}", WorkerWorldSettingCandidatePayload(
            candidate_id=UUID(int=index), work_id=UUID(int=500), source_episode_id=UUID(int=501),
            category="IMPORTANT_ITEM", subject_name=name, setting_name="효과", extracted_value="회복한다.",
            evidence_spans=[{"quote": "회복한다."}]), _world_target_set([entry])))
    return result


class Provider:
    def __init__(self, truncate=False):
        self.requests, self.truncate = [], truncate

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        self.requests.append(payload)
        if self.truncate and "groups" in payload:
            raise LlmOutputTruncatedError("truncated", incomplete_reason="max_output_tokens", max_output_tokens=16000)
        def result(group):
            return {"decisions": [{"source_candidate_refs": ["C1"], "consolidation_status": "SINGLE",
                "operation": "ADD", "target_ref": "T1", "proposed_scope_name": None,
                "proposed_setting_name": "효과", "proposed_value": "회복한다.", "comparison_reason": "새 효과다."}]}
        data = {"groups": {g["group_ref"]: result(g) for g in payload["groups"]}} if "groups" in payload else result(payload)
        return LlmTextResponse(text=json.dumps(data))


def run(items, provider):
    comparator = WorldSettingComparator(llm_client=provider, model="gpt-6.1-sol", max_attempts=1)
    return asyncio.run(_run_world_batches(items, comparator,
        manuscript_comparator=WorldSettingManuscriptComparator(comparator)))


def test_evaluator_packs_and_maps_each_group_back_to_its_own_source():
    provider = Provider()
    predictions, failures = run(sources(), provider)
    assert len(provider.requests) == 1
    assert [p.source_candidate_id for p in predictions] == ["S1", "S2"]
    assert failures == []


def test_different_temporal_instructions_split_valid_groups():
    items, provider = sources(), Provider()
    items[1].target_set.targets[0].properties[0].provenance = AnalysisStateProvenance(
        confirmation_status="CONFIRMED", current_source_times=[{
            "sourceKind": "EPISODE", "sourceEpisodeNo": 1, "temporalScope": "PRESENT"}])
    predictions, failures = run(items, provider)
    assert len(provider.requests) == 2
    assert [p.source_candidate_id for p in predictions] == ["S1", "S2"]
    assert failures == []


def test_truncated_packed_response_recovers_every_group():
    provider = Provider(truncate=True)
    predictions, failures = run(sources(), provider)
    assert len(provider.requests) == 3
    assert [p.source_candidate_id for p in predictions] == ["S1", "S2"]
    assert failures == []
