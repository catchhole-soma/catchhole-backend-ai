"""Exercise real pipeline packing against a stateful Spring boundary double."""

import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_manuscript import WorldSettingManuscriptComparator
from app.schemas.worker import WorkerWorldSettingComparisonBatchPayload, WorkerWorldSettingComparisonBatchContextResponse
from app.llm.responses import LlmTextResponse
from app.analysis.exceptions import ComparisonValidationError
from app.schemas.analysis_context import WorkerAnalysisContext


def batch(index, target=None):
    target = index if target is None else target
    return WorkerWorldSettingComparisonBatchPayload.model_validate({
        "comparisonBatchId": str(UUID(int=100 + index)), "workId": str(UUID(int=500)),
        "sourceEpisodeId": str(UUID(int=501)), "category": "IMPORTANT_ITEM",
        "resolutionType": "EXISTING", "canonicalSubjectKey": f"world:{UUID(int=target)}",
        "canonicalSubjectName": f"물약{target}", "resolvedTargetWorldSettingIds": [str(UUID(int=target))],
        "rawScopeName": None, "candidates": [{
            "candidateRef": "C1", "candidateId": str(UUID(int=200 + index)),
            "subjectName": f"물약{target}", "settingName": f"효과{index}",
            "extractedValue": "회복한다.", "evidenceSpans": [{"quote": "회복한다."}],
        }],
    })


class Spring:
    def __init__(self, batches):
        self.pending = list(batches)
        self.batches = {b.comparison_batch_id: b for b in batches}
        self.completions, self.failures, self.events = [], [], []
        self.versions = {}

    async def claim_next_world_setting_comparison_batch(self, job, lease):
        item = self.pending.pop(0) if self.pending else None
        self.events.append(("claim", item.comparison_batch_id if item else None))
        return item

    async def get_world_setting_comparison_batch_context(self, job, batch_id, lease, targets, **kwargs):
        item = self.batches[batch_id]
        target = targets[0]
        self.events.append(("context", batch_id))
        return WorkerWorldSettingComparisonBatchContextResponse.model_validate({
            "comparisonBatchId": str(batch_id), "candidates": [c.model_dump(by_alias=True) for c in item.candidates],
            "exactTargets": [{"candidateRef": "C1", "worldSettingId": str(target)}],
            "targets": [{"worldSettingId": str(target), "subjectName": item.canonical_subject_name,
                         "version": self.versions.get(target, 1), "properties": []}],
        })

    async def complete_world_setting_comparison_batch(self, job, batch_id, lease, request):
        self.completions.append((batch_id, request))
        self.events.append(("complete", batch_id))

    async def fail_world_setting_comparison_batch(self, *args, **kwargs):
        self.failures.append((args, kwargs))


class Provider:
    def __init__(self):
        self.requests = []

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        self.requests.append(payload)
        def result(group):
            return {"decisions": [{
                "source_candidate_refs": [c["ref"]], "consolidation_status": "SINGLE",
                "operation": "ADD", "target_ref": "T1", "proposed_scope_name": None,
                "proposed_setting_name": c["setting_name"], "proposed_value": c["extracted_value"],
                "comparison_reason": "새로 확인한 효과다.",
            } for c in group["candidates"]]}
        response = ({"groups": {g["group_ref"]: result(g) for g in payload["groups"]}}
                    if "groups" in payload else result(payload))
        return LlmTextResponse(text=json.dumps(response))


def run(spring, provider, *, limit=64000):
    comparator = WorldSettingComparator(llm_client=provider, model="gpt-6.1-sol", max_attempts=1)
    pipeline = WorldSettingComparisonPipeline(
        spring, None, comparator,
        manuscript_comparator=WorldSettingManuscriptComparator(comparator, max_input_tokens=limit),
    )
    return asyncio.run(pipeline.process_all(UUID(int=900), UUID(int=901)))


def test_pipeline_packs_three_batches_and_completes_each_with_its_own_ids():
    spring, provider = Spring([batch(1), batch(2), batch(3)]), Provider()
    result = run(spring, provider)
    assert len(provider.requests) == 1
    assert result.completed_count == 3
    assert spring.failures == []
    assert [request.decisions[0].target_world_setting_id.int for _, request in spring.completions] == [1, 2, 3]
    assert [request.decisions[0].proposed_setting_name for _, request in spring.completions] == ["효과1", "효과2", "효과3"]


def test_pipeline_preserves_order_and_loads_repeated_target_after_prior_save():
    # Conservative boundary also for confirmed-only avoids silently mixing its
    # state policy with ordered projection in the request scheduler.
    spring, provider = Spring([batch(1), batch(2), batch(3, target=1)]), Provider()
    result = run(spring, provider)
    assert result.completed_count == 3
    assert len(provider.requests) == 2
    assert spring.events.index(("complete", UUID(int=101))) < spring.events.index(("context", UUID(int=103)))


def test_changed_prefetched_context_is_regenerated_instead_of_retokened():
    class ChangingSpring(Spring):
        async def complete_world_setting_comparison_batch(self, job, batch_id, lease, request):
            await super().complete_world_setting_comparison_batch(job, batch_id, lease, request)
            if batch_id.int == 101:
                self.versions[UUID(int=2)] = 2
    spring, provider = ChangingSpring([batch(1), batch(2)]), Provider()
    result = run(spring, provider)
    assert result.completed_count == 2
    assert len(provider.requests) == 2
    assert spring.completions[1][1].context_versions[0].version == 2
    assert "groups" not in provider.requests[1]


def test_manual_ordered_failure_stops_before_saving_prefetched_later_success():
    context = WorkerAnalysisContext(run_id=UUID(int=700), generation=1,
        input_state_hash="a" * 64, source_hash="b" * 64, format_version=1)
    items = [batch(i).model_copy(update={"analysis_context": context}) for i in (1, 2)]
    class OrderedSpring(Spring):
        async def get_world_setting_comparison_batch_context(self, *args, **kwargs):
            value = await super().get_world_setting_comparison_batch_context(*args, **kwargs)
            return value.model_copy(update={"analysis_context": context, "context_token": "c" * 64})
    class InvalidFirstProvider(Provider):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            data = json.loads(response.text)
            groups = data.get("groups", {"single": data})
            for result in groups.values():
                for decision in result["decisions"]:
                    decision.update(review_reason=None, matched_property_ref=None,
                                    existing_root_property_names_to_move=[])
                    if decision["proposed_setting_name"] == "효과1":
                        decision["target_ref"] = "NOT_A_TARGET"
            return LlmTextResponse(text=json.dumps(data))
    spring, provider = OrderedSpring(items), InvalidFirstProvider()
    comparator = WorldSettingComparator(llm_client=provider, model="gpt-6.1-sol", max_attempts=1)
    pipeline = WorldSettingComparisonPipeline(spring, None, comparator,
        manuscript_comparator=WorldSettingManuscriptComparator(comparator))
    with pytest.raises(ComparisonValidationError):
        asyncio.run(pipeline.process_all(UUID(int=900), UUID(int=901), analysis_context=context))
    assert [identifier.int for identifier, _ in spring.completions] == [101]
    assert spring.completions[0][1].failures[0].source_candidate_refs == ["C1"]


def test_reset_reclaims_replacement_before_prefetched_sibling_after_exhaustion():
    import httpx
    from app.clients.exceptions import SpringWorkerHttpError
    from app.usage.metering import TextGenerationUsageSnapshot

    class ResetSpring(Spring):
        async def complete_world_setting_comparison_batch(self, job, batch_id, lease, request):
            if batch_id.int == 101:
                http_request = httpx.Request("POST", "http://spring.local/complete")
                raise SpringWorkerHttpError("stale subject", request=http_request,
                    response=httpx.Response(409, request=http_request),
                    spring_error_code="WORLD_SETTING_SUBJECT_RESOLUTION_STALE")
            await super().complete_world_setting_comparison_batch(job, batch_id, lease, request)

        async def reset_stale_world_setting_subject_resolution(self, *args):
            replacement = batch(1).model_copy(update={"comparison_batch_id": UUID(int=103)})
            self.pending.append(replacement)
            self.batches[replacement.comparison_batch_id] = replacement

    class Pipeline(WorldSettingComparisonPipeline):
        async def _prepare_subject_resolutions(self, *args, **kwargs):
            pass

    spring, provider = ResetSpring([batch(1), batch(2)]), Provider()
    comparator = WorldSettingComparator(llm_client=provider, model="gpt-6.1-sol", max_attempts=1)
    pipeline = Pipeline(spring, None, comparator,
        manuscript_comparator=WorldSettingManuscriptComparator(comparator))
    result = asyncio.run(pipeline._process_all_batches(UUID(int=900), UUID(int=901),
        TextGenerationUsageSnapshot()))
    assert result.completed_count == 2
    assert spring.pending == []
    assert [identifier.int for identifier, _ in spring.completions] == [103, 102]
    assert len(provider.requests) == 2


def test_oversized_single_group_is_held_and_next_group_still_calls_provider():
    class LargeContextSpring(Spring):
        async def get_world_setting_comparison_batch_context(self, *args, **kwargs):
            context = await super().get_world_setting_comparison_batch_context(*args, **kwargs)
            if context.comparison_batch_id.int == 101:
                from app.schemas.worker import WorkerWorldSettingProperty
                context.targets[0].properties = [WorkerWorldSettingProperty(
                    setting_name="기존 효과", value="기존 설정 내용 " * 10000,
                )]
            return context

    spring, provider = LargeContextSpring([batch(1), batch(2)]), Provider()
    comparator = WorldSettingComparator(llm_client=provider, model="gpt-6.1-sol", max_attempts=1)
    comparator.batch_max_input_tokens = 16000
    pipeline = WorldSettingComparisonPipeline(spring, None, comparator,
        manuscript_comparator=WorldSettingManuscriptComparator(comparator, max_input_tokens=16000))
    result = asyncio.run(pipeline.process_all(UUID(int=900), UUID(int=901)))
    assert result.completed_count == 2
    assert spring.failures == []
    assert len(provider.requests) == 1
    assert provider.requests[0]["candidates"][0]["setting_name"] == "효과2"
    first, second = [request for _, request in spring.completions]
    assert first.decisions[0].suggested_operation == "REVIEW_REQUIRED"
    assert first.decisions[0].comparison_review_reason == "BATCH_LIMIT_EXCEEDED"
    assert first.decisions[0].proposed_value == batch(1).candidates[0].extracted_value
    assert first.context_versions[0].version == 1
    assert second.decisions[0].suggested_operation == "ADD"
