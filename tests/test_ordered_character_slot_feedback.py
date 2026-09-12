"""Offline regressions for the ordered ADD-existing-slot retry boundary."""
import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import pytest

from app.analysis import character_fact_comparator as module
from app.analysis.character_fact_projection import CharacterProjectionEntry
from app.analysis.exceptions import ComparisonValidationError
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.worker import WorkerCharacterFactComparisonBatchCandidate


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("These tests cannot open network connections")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(module, "_estimate_prompt_tokens", lambda *args: 100)


def candidate(ref, key, value="SECRET_SOURCE_VALUE"):
    return WorkerCharacterFactComparisonBatchCandidate(
        candidate_ref=ref, projected_snapshot_ref="Q" + ref[1:], source_episode_no=28,
        raw_fact_key=key, initial_canonical_fact_key=key, canonical_key_resolution="EXACT",
        attribute_value=value, value_type="STRING", value_json={"value": value},
        confidence=0.95, evidence_spans=[{"quote": "SECRET_EVIDENCE"}],
    )


def initial():
    return [CharacterProjectionEntry(
        reference="P1", fact_type="PROFILE", fact_key="profile.attribute",
        fact_value="SECRET_EXISTING_VALUE", value_json={"value": "SECRET_EXISTING_VALUE"},
        provenance=AnalysisStateProvenance(confirmation_status="CONFIRMED", source_episode_no=9),
    )]


def decision(source, operation="ADD", target=None):
    return {"candidate_ref": source.candidate_ref,
            "resolved_canonical_fact_key": source.initial_canonical_fact_key,
            "operation": operation, "target_ref": target, "removed_snapshot_refs": [],
            "proposed_fact_value": "검증된 제안값" if operation in {"ADD", "UPDATE", "MERGE"} else None,
            "proposed_value_json": {"value": "검증된 제안값"} if operation in {"ADD", "UPDATE", "MERGE"} else None,
            "temporal_scope": "PRESENT", "comparison_reason": "기존 정보와 새 습관의 관계를 검토한다."}


def run(responses, sources, *, ordered=True):
    requests = []
    class Client:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            response = responses[min(len(requests) - 1, len(responses) - 1)]
            if isinstance(response, Exception):
                raise response
            return LlmTextResponse(text=json.dumps({"decisions": response}))
    async def execute():
        comparator = module.CharacterFactComparator(
            llm_client=Client(), model="offline", max_attempts=3, max_output_tokens=3000,
            batch_max_output_tokens=16000, batch_max_input_tokens=64000, batch_max_candidates=10,
        )
        return await comparator.compare_batch(matched_character_name="비요른", canonical_fact_type="PROFILE",
            candidates=sources, snapshot_entries=initial(), ordered_context=ordered)
    try:
        return asyncio.run(execute()), requests
    except Exception as error:
        return error, requests


@pytest.mark.parametrize("operation", ["MERGE", "REVIEW_REQUIRED"])
def test_existing_attribute_gets_exact_safe_retry_while_new_occupation_remains_add(operation, caplog):
    sources = [candidate("C1", "profile.occupation", "전사"), candidate("C2", "profile.attribute")]
    invalid = [decision(source) for source in sources]
    valid = [decision(sources[0]), decision(sources[1], operation, "P1" if operation == "MERGE" else None)]
    before = [source.model_dump() for source in sources]
    with caplog.at_level(logging.WARNING):
        result, requests = run([invalid, valid], sources)
    assert not isinstance(result, Exception), result
    assert len(requests) == 2
    feedback = json.loads(requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason_code"] == "CANONICAL_SLOT_ALREADY_EXISTS"
    assert feedback["existing_slot"] == {
        "candidate_ref": "C2", "initial_canonical_fact_key": "profile.attribute",
        "active_target_ref": "P1", "fact_type": "PROFILE", "fact_key": "profile.attribute",
    }
    assert "새 내용이어도 ADD는 금지" in feedback["correction"]
    assert "REVIEW_REQUIRED" in feedback["correction"]
    assert "SECRET_" not in json.dumps(feedback) + caplog.text
    assert [row.operation for row in result[0].decisions] == ["ADD", operation]
    assert [source.model_dump() for source in sources] == before


def test_repeated_invalid_add_still_fails_whole_batch_without_partial_result():
    sources = [candidate("C1", "profile.occupation"), candidate("C2", "profile.attribute")]
    result, requests = run([[decision(source) for source in sources]], sources)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    assert "CANONICAL_SLOT_ALREADY_EXISTS" in str(result)
    assert "SECRET_" not in str(result)


def test_retry_points_to_active_prior_q_instead_of_replaced_initial_p():
    sources = [candidate("C1", "profile.attribute"), candidate("C2", "profile.attribute")]
    invalid = [decision(sources[0], "UPDATE", "P1"), decision(sources[1])]
    valid = [invalid[0], decision(sources[1], "MERGE", "Q1")]
    result, requests = run([invalid, valid], sources)
    assert not isinstance(result, Exception), result
    context = json.loads(requests[1]["user_prompt"])["validation_feedback"]["existing_slot"]
    assert context["active_target_ref"] == "Q1"
    assert context["target_source_candidate_ref"] == "C1"
    assert context["fact_key"] == "profile.attribute"


@pytest.mark.parametrize("target_ref", ["Q2", "SECRET_FORGED_REF", "P99"])
def test_forged_or_future_error_context_is_not_reinjected(target_ref):
    sources = [candidate("C1", "profile.attribute"), candidate("C2", "profile.attribute")]
    payload = module._batch_prompt_payload("비요른", "PROFILE", sources, initial())
    error = module.OrderedCharacterSlotConflict("C1", CharacterProjectionEntry(
        target_ref, "PROFILE", "SECRET_RESPONSE_KEY", "SECRET_RESPONSE_VALUE", None,
        source_candidate_ref="C2",
    ))
    feedback = json.loads(module._build_batch_retry_user_prompt(
        json.dumps(payload), error, ordered_context=True))["validation_feedback"]
    assert "existing_slot" not in feedback
    assert "SECRET_" not in json.dumps(feedback)


def test_provider_value_error_is_not_reclassified_or_retried():
    error = ValueError("ADD is invalid when the canonical Fact slot already exists.")
    result, requests = run([error], [candidate("C1", "profile.attribute")])
    assert result is error
    assert len(requests) == 1


def test_legacy_batch_feedback_and_initial_prompt_do_not_get_ordered_extension():
    source = candidate("C1", "profile.attribute")
    result, requests = run([[decision(source)], [decision(source, "REVIEW_REQUIRED")]], [source], ordered=False)
    assert not isinstance(result, Exception), result
    assert requests[0]["system_prompt"] == (
        module.BATCH_COMPARISON_PROMPT_PATH.read_text(encoding="utf-8")
        + "\n\n" + module.USER_FACING_REASON_INSTRUCTIONS
    )
    feedback = json.loads(requests[1]["user_prompt"])["validation_feedback"]
    assert "reason_code" not in feedback and "existing_slot" not in feedback


def test_other_validation_error_does_not_get_slot_conflict_context():
    source = candidate("C1", "profile.attribute")
    result, requests = run([[decision(source, "UPDATE", "P99")]], [source])
    assert isinstance(result, ComparisonValidationError)
    feedback = json.loads(requests[1]["user_prompt"])["validation_feedback"]
    assert "reason_code" not in feedback and "existing_slot" not in feedback
