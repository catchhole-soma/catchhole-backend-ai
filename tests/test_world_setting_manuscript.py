"""Request packing must not change per-target validation or lose sources."""

import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_manuscript import (
    WorldSettingManuscriptComparator, WorldSettingManuscriptGroup,
)
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate, WorkerWorldSettingComparisonTarget,
)


def group(index, *, value="회복한다.", ordered=False):
    return WorldSettingManuscriptGroup(
        group_ref=f"G{index}", category="IMPORTANT_ITEM",
        candidates=[WorkerWorldSettingComparisonBatchCandidate(
            candidate_ref="C1", candidate_id=UUID(int=100 + index),
            subject_name=f"물약{index}", setting_name="효과", extracted_value=value,
            evidence_spans=[{"quote": value}],
        )],
        targets=[WorkerWorldSettingComparisonTarget(
            world_setting_id=UUID(int=index), subject_name=f"물약{index}",
            version=1, properties=[],
        )], ordered_context=ordered,
    )


def decision(*, ordered=False, target="T1"):
    result = {
        "source_candidate_refs": ["C1"], "consolidation_status": "SINGLE",
        "operation": "ADD", "review_reason": None, "target_ref": target,
        "proposed_scope_name": None, "proposed_setting_name": "효과",
        "proposed_value": "회복한다.", "comparison_reason": "새로운 회복 효과다.",
        "existing_root_property_names_to_move": [],
    }
    if ordered:
        result["matched_property_ref"] = None
    else:
        result.update(matched_scope_name=None, matched_property_name=None)
    return result


class Provider:
    def __init__(self, transform=None):
        self.requests = []
        self.transform = transform

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        payload = json.loads(kwargs["user_prompt"])
        result = {"groups": {
            item["group_ref"]: {"decisions": [decision(ordered=item.get("ordered_context", False))]}
            for item in payload["groups"]
        }}
        if self.transform:
            result = self.transform(result)
        return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))


def comparator(provider, **kwargs):
    return WorldSettingManuscriptComparator(
        WorldSettingComparator(llm_client=provider, model="gpt-6.1-sol", max_attempts=1),
        **kwargs,
    )


@pytest.mark.parametrize("ordered", [False, True])
def test_packs_independent_groups_and_validates_repeated_local_refs(ordered):
    provider = Provider()
    runner = comparator(provider)
    results = asyncio.run(runner.compare_manuscript([group(i, ordered=ordered) for i in (1, 2, 3)]))
    assert len(provider.requests) == 1
    assert list(results) == ["G1", "G2", "G3"]
    assert all(item.result.decisions[0].source_candidate_refs == ["C1"] for item in results.values())
    assert all(item.error is None for item in results.values())
    request = provider.requests[0]
    assert request["max_output_tokens"] == 16000
    assert str(UUID(int=101)) not in request["user_prompt"]
    assert request["system_prompt"].count("이번 요청은 여러 비교 묶음") == 1


@pytest.mark.parametrize("ordered", [False, True])
def test_every_nested_response_object_satisfies_provider_strict_schema(ordered):
    provider = Provider()
    asyncio.run(comparator(provider).compare_manuscript([group(1, ordered=ordered), group(2, ordered=ordered)]))
    schema = provider.requests[0]["response_schema"].schema

    def check(node):
        if isinstance(node, list):
            for value in node:
                check(value)
        elif isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            if "$ref" in node:
                assert node["$ref"].removeprefix("#/$defs/") in schema["$defs"]
            for value in node.values():
                check(value)

    check(schema)


def test_input_budget_splits_requests_without_dropping_candidates():
    provider = Provider()
    runner = comparator(provider)
    groups = [group(i, value="회복한다. " * 300) for i in (1, 2, 3)]
    # Use the measured first request to place the boundary; coverage is asserted
    # independently against literal group IDs and provider payloads.
    runner.max_input_tokens = runner.request_size(groups[:2]).input_tokens - 1
    results = asyncio.run(runner.compare_manuscript(groups))
    assert len(provider.requests) == 3
    assert [json.loads(r["user_prompt"])["groups"][0]["group_ref"] for r in provider.requests] == ["G1", "G2", "G3"]
    assert list(results) == ["G1", "G2", "G3"]
    assert all(row.input_tokens <= runner.max_input_tokens for row in runner.requests)


def test_output_budget_splits_even_when_input_fits():
    provider = Provider()
    runner = comparator(provider)
    groups = [group(i) for i in (1, 2, 3)]
    runner.max_output_tokens = runner.request_size(groups[:2]).output_tokens - 1
    asyncio.run(runner.compare_manuscript(groups))
    assert len(provider.requests) == 3
    assert all(r["max_output_tokens"] == runner.max_output_tokens for r in provider.requests)


def test_oversized_single_group_never_calls_provider_or_truncates_data():
    provider = Provider()
    runner = comparator(provider, max_input_tokens=100)
    with pytest.raises(OrderedInputContextError):
        asyncio.run(runner.compare_manuscript([group(1)]))
    assert provider.requests == []


def test_missing_group_keeps_other_valid_results_for_targeted_recovery():
    def omit(result):
        del result["groups"]["G2"]
        return result
    results = asyncio.run(comparator(Provider(omit)).compare_manuscript([group(1), group(2)]))
    assert results["G1"].result.decisions[0].operation.value == "ADD"
    assert isinstance(results["G2"].error, ComparisonValidationError)
    assert results["G2"].result is None


@pytest.mark.parametrize("ordered", [False, True])
def test_cross_group_reference_does_not_contaminate_valid_group(ordered):
    def foreign(result):
        result["groups"]["G2"]["decisions"][0]["target_ref"] = "G1.T1"
        return result
    results = asyncio.run(comparator(Provider(foreign)).compare_manuscript(
        [group(1, ordered=ordered), group(2, ordered=ordered)],
    ))
    assert results["G1"].error is None
    assert isinstance(results["G2"].error, ComparisonValidationError)
    assert results["G2"].error.world_batch_response_payload["decisions"][0]["target_ref"] == "G1.T1"


def test_ordered_shared_target_requires_fresh_context_instead_of_frozen_projection():
    provider = Provider()
    first, second = group(1, ordered=True), group(2, ordered=True)
    second.targets = first.targets
    with pytest.raises(OrderedInputContextError):
        asyncio.run(comparator(provider).compare_manuscript([first, second]))
    assert provider.requests == []


def test_duplicate_response_group_is_rejected_before_json_last_value_wins():
    class DuplicatingProvider:
        async def create_text_response(self, **kwargs):
            item = json.dumps({"decisions": [decision()]})
            return LlmTextResponse(text='{"groups":{"G1":' + item + ',"G1":' + item + '}}')
    results = asyncio.run(comparator(DuplicatingProvider()).compare_manuscript([group(1)]))
    assert isinstance(results["G1"].error, ComparisonValidationError)
