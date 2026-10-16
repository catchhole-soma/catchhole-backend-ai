import asyncio
import json

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
from app.analysis.exceptions import ComparisonValidationError
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerCharacterFactComparisonBatchCandidate as Candidate,
    WorkerCharacterFactComparisonBatchContextResponse as Context,
    WorkerCharacterFactComparisonBatchPayload as Batch,
)


def candidate(index, fact_type, key, value):
    return Candidate.model_validate({
        "candidateRef": f"C{index}", "projectedSnapshotRef": f"Q{index}",
        "canonicalFactType": fact_type, "sourceEpisodeNo": 6,
        "rawFactKey": key, "initialCanonicalFactKey": key,
        "canonicalKeyResolution": "EXACT", "attributeValue": value,
        "valueType": "STRING", "valueJson": {"value": value},
        "evidenceSpans": [{"quote": value}],
    })


def decision(index, key, value, target=None):
    return {
        "candidate_ref": f"C{index}", "resolved_canonical_fact_key": key,
        "operation": "UPDATE" if target else "ADD", "target_ref": target,
        "removed_snapshot_refs": [], "proposed_fact_value": value,
        "proposed_value_json": {"value": value}, "temporal_scope": "PRESENT",
        "comparison_reason": "변화 과정이 확인되어 현재 내용을 반영합니다.",
    }


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LlmTextResponse(text=json.dumps(response, ensure_ascii=False))


def test_person_batch_uses_one_request_and_projects_each_candidates_own_type():
    candidates = [candidate(1, "ITEM", "item.마석", "10개"),
                  candidate(2, "PROFILE", "profile.종족", "바바리안"),
                  candidate(3, "ITEM", "item.마석", "44개")]
    client = Client({"decisions": [decision(1, "item.마석", "10개"),
        decision(2, "profile.종족", "바바리안"), decision(3, "item.마석", "44개", "Q1")]})
    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1),
        matched_character_name="비요른", canonical_fact_type=None,
        candidates=candidates, snapshot_entries=[],
    ))
    assert not result.failures
    assert len(result.decisions) == 3
    assert result.decisions[2].dependency_candidate_refs == ["C1"]
    assert len(client.requests) == 1
    payload = json.loads(client.requests[0]["user_prompt"])
    assert "canonical_fact_type" not in payload
    assert [row["canonical_fact_type"] for row in payload["candidates"]] == ["ITEM", "PROFILE", "ITEM"]


def test_person_batch_rejects_target_from_another_fact_type():
    client = Client({"decisions": [decision(1, "item.마석", "10개"),
                                  decision(2, "profile.종족", "바바리안", "Q1")]})
    with pytest.raises(ComparisonValidationError):
        asyncio.run(CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1).compare_batch(
            matched_character_name="비요른", canonical_fact_type=None,
            candidates=[candidate(1, "ITEM", "item.마석", "10개"),
                        candidate(2, "PROFILE", "profile.종족", "바바리안")], snapshot_entries=[],
        ))


def batch_data():
    return {"comparisonBatchId": "00000000-0000-0000-0000-000000000001",
            "workId": "00000000-0000-0000-0000-000000000002",
            "characterRef": "K1", "matchedCharacterName": "비요른", "canonicalFactType": None,
            "candidates": [candidate(1, "ITEM", "item.마석", "10개").model_dump(by_alias=True)]}


def test_new_batch_requires_per_candidate_type_and_old_batch_keeps_its_type():
    raw = batch_data()
    assert Batch.model_validate(raw).candidates[0].canonical_fact_type == "ITEM"
    raw["candidates"][0].pop("canonicalFactType")
    with pytest.raises(ValueError, match="Fact type"):
        Batch.model_validate(raw)
    raw["canonicalFactType"] = "ITEM"
    assert Batch.model_validate(raw).canonical_fact_type == "ITEM"
    raw["candidates"][0]["canonicalFactType"] = "PROFILE"
    with pytest.raises(ValueError, match="Fact type"):
        Batch.model_validate(raw)


def test_person_context_includes_all_snapshot_types_without_legacy_30_limit():
    raw = {**batch_data(), "baseSnapshotVersion": 0, "contextToken": "a" * 64,
           "snapshotEntries": [{"snapshotRef": f"P{i}", "factType": "ITEM",
                                "factKey": f"item.{i}", "factValue": "물건"} for i in range(1, 32)]}
    assert len(Context.model_validate(raw).snapshot_entries) == 31
    raw["canonicalFactType"] = "ITEM"
    with pytest.raises(ValueError, match="legacy limit"):
        Context.model_validate(raw)


def test_output_split_preserves_mixed_type_projection():
    from app.llm.exceptions import LlmOutputTruncatedError

    client = Client(LlmOutputTruncatedError("cap", incomplete_reason="max_output_tokens", max_output_tokens=128000),
                    {"decisions": [decision(1, "item.마석", "10개")]},
                    {"decisions": [decision(2, "profile.종족", "바바리안"),
                                   decision(3, "item.마석", "44개", "Q1")]})
    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1),
        matched_character_name="비요른", canonical_fact_type=None,
        candidates=[candidate(1, "ITEM", "item.마석", "10개"),
                    candidate(2, "PROFILE", "profile.종족", "바바리안"),
                    candidate(3, "ITEM", "item.마석", "44개")], snapshot_entries=[],
    ))
    assert not result.failures
    assert len(result.decisions) == 3
    assert len(client.requests) == 3
    assert json.loads(client.requests[-1]["user_prompt"])["snapshot_entries"][0]["fact_type"] == "ITEM"


def test_ordered_partial_recovery_keeps_independent_other_type_and_retries_failed_candidate():
    first = decision(1, "item.마석", "10개", "Q9")
    client = Client({"decisions": [first, decision(2, "profile.종족", "바바리안")]},
                    {"decisions": [decision(1, "item.마석", "10개")]})
    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1),
        matched_character_name="비요른", canonical_fact_type=None,
        candidates=[candidate(1, "ITEM", "item.마석", "10개"),
                    candidate(2, "PROFILE", "profile.종족", "바바리안")],
        snapshot_entries=[], ordered_context=True,
    ))
    assert not result.failures
    assert [row.candidate_ref for row in result.decisions] == ["C1", "C2"]
    assert len(client.requests) == 2
    assert [row["candidate_ref"] for row in json.loads(client.requests[1]["user_prompt"])["candidates"]] == ["C1"]


def test_status_failure_invalidates_later_other_type_decision_in_person_batch():
    from app.analysis.character_fact_comparator import _independent_ordered_decisions

    sources = [candidate(1, "STATUS", "status.부상", "부상"),
               candidate(2, "PROFILE", "profile.종족", "바바리안")]
    retained = _independent_ordered_decisions({"decisions": [
        decision(1, "status.부상", "부상", "Q9"), decision(2, "profile.종족", "바바리안"),
    ]}, None, sources, [])
    assert retained == (None, None)


def test_ordered_slot_retry_uses_candidates_type_and_safe_existing_ref():
    client = Client({"decisions": [decision(1, "item.마석", "10개"), decision(2, "item.마석", "44개")]},
                    {"decisions": [decision(1, "item.마석", "10개"), decision(2, "item.마석", "44개", "Q1")]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=client, model="test-model", max_attempts=2).compare_batch(
        matched_character_name="비요른", canonical_fact_type=None,
        candidates=[candidate(1, "ITEM", "item.마석", "10개"), candidate(2, "ITEM", "item.마석", "44개")],
        snapshot_entries=[], ordered_context=True,
    ))
    assert result.decisions[1].target_ref == "Q1"
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert "Q1" in json.dumps(feedback)
    assert "44개" not in json.dumps(feedback, ensure_ascii=False)


def test_worker_advertises_person_batch_support_without_changing_lease_contract():
    import httpx
    from uuid import UUID
    from app.clients.spring_worker_client import SpringWorkerClient

    job_id = UUID("00000000-0000-0000-0000-000000000003")
    lease = UUID("00000000-0000-0000-0000-000000000004")

    def handler(request):
        assert request.headers["X-Character-Comparison-Person-Batches"] == "true"
        assert request.headers["X-Worker-Lease-Token"] == str(lease)
        return httpx.Response(200, json={"data": batch_data()})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SpringWorkerClient("http://spring.local", "test", http_client=http)
            batch = await client.claim_next_character_fact_comparison_batch(job_id, lease)
            assert batch.canonical_fact_type is None
            assert batch.candidates[0].canonical_fact_type == "ITEM"

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["confirmed", "ordered"])
def test_actual_java_person_claim_and_context_reach_one_provider_request(mode):
    import os
    from pathlib import Path
    from app.analysis.character_fact_comparison_pipeline import _validate_batch_context

    output = os.environ.get("GH251_CHARACTER_BATCH_OUTPUT")
    if not output:
        pytest.skip("Run Java mixed batch export with GH251_CHARACTER_BATCH_OUTPUT first")
    batch = Batch.model_validate(json.loads((Path(output) / f"{mode}-claim.json").read_text()))
    context = Context.model_validate(json.loads((Path(output) / f"{mode}-context.json").read_text()))
    _validate_batch_context(batch, context)
    assert batch.canonical_fact_type is None
    types = [candidate.canonical_fact_type for candidate in batch.candidates]
    assert len(set(types)) > 1
    client = Client({"decisions": [{
        "candidate_ref": row.candidate_ref, "resolved_canonical_fact_key": row.initial_canonical_fact_key,
        "operation": "REVIEW_REQUIRED", "target_ref": None, "removed_snapshot_refs": [],
        "proposed_fact_value": None, "proposed_value_json": None,
        "temporal_scope": "PRESENT", "comparison_reason": "근거의 관계를 추가 확인해야 합니다.",
    } for row in context.candidates]})
    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1),
        matched_character_name=context.matched_character_name, canonical_fact_type=context.canonical_fact_type,
        candidates=context.candidates, snapshot_entries=context.snapshot_entries,
        ordered_context=context.analysis_context is not None,
        protected_slots=context.protected_slots, advisory_current_context=context.advisory_current_context,
    ))
    assert not result.failures
    assert len(result.decisions) == len(batch.candidates)
    assert len(client.requests) == 1
    payload = json.loads(client.requests[0]["user_prompt"])
    assert [row["canonical_fact_type"] for row in payload["candidates"]] == types
