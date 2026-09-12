"""Offline HTTP contract tests using synthetic seven-source collision scenarios.

These fixtures reproduce validation categories, not an unavailable provider response.
"""

import asyncio
from copy import deepcopy
import json
import logging
import socket
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.comparison_reason import USER_FACING_REASON_INSTRUCTIONS
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_world_batch_contract import (
    OrderedWorldBatchValidationError,
    ordered_world_batch_error_summary,
    ordered_world_batch_retry_prompt,
)
from app.analysis.world_setting_comparator import (
    BATCH_COMPARISON_PROMPT_PATH,
    WorldSettingComparator,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)
from app.usage import metering
from app.usage.metering import MeteredTextGenerationClient


JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000003")
REJECTED_UUID = "f8b011b7-b872-46e7-9132-197ac89d72a1"
REJECTED_KEY = "PROVIDER_UNKNOWN_KEY"
REJECTED_VALUE = "PROVIDER_PRIVATE_VALUE"


class _OfflineEncoding:
    def encode(self, text, **kwargs):
        return list(text.encode("utf-8"))


class _Ledger:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)

    async def settle_ai_tokens(self, request_id, input_tokens, cached_input_tokens,
                               output_tokens, outcome):
        self.settlements.append((request_id, input_tokens, cached_input_tokens,
                                 output_tokens, outcome))

    async def release_ai_tokens(self, request_id, outcome):
        raise AssertionError("Every mocked completed response supplies usage.")


@pytest.fixture(autouse=True)
def _offline_dependencies(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("This contract test must not open network connections.")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda name: _OfflineEncoding())
    monkeypatch.setattr(metering, "_encoding_for_model", lambda name: _OfflineEncoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


def _candidates():
    return [WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=f"C{index}", candidate_id=UUID(int=100 + index),
        subject_name="합성 종족", setting_name="신체 능력" if index == 2 else f"특징{index}",
        extracted_value=f"관찰{index}", evidence_spans=[{"quote": f"합성 근거{index}"}],
    ) for index in range(1, 8)]


def _target(*, provisional=True):
    identity = ({"provisional_subject_key": f"provisional-world:{SUBJECT_ID}"}
                if provisional else {"world_setting_id": SUBJECT_ID})
    return WorkerWorldSettingComparisonTarget(
        **identity, subject_name="합성 종족", version=2,
        properties=[{
            "setting_name": "신체 능력", "value": "과거 관찰",
            "provenance": {
                "confirmationStatus": "PROVISIONAL" if provisional else "CONFIRMED",
                "sourceEpisodeNo": 2,
            },
        }],
    )


def _valid_result(*, ordered=True):
    decisions = []
    for index, candidate in enumerate(_candidates(), start=1):
        decisions.append({
            "source_candidate_refs": [candidate.candidate_ref],
            "consolidation_status": "SINGLE",
            "operation": "UPDATE" if index == 2 else "ADD",
            "review_reason": None,
            "target_ref": "T1",
            **({"matched_property_ref": "T1.P1" if index == 2 else None} if ordered else {
                "matched_scope_name": None,
                "matched_property_name": "신체 능력" if index == 2 else None,
            }),
            "proposed_scope_name": None,
            "proposed_setting_name": None if ordered and index == 2 else candidate.setting_name,
            "proposed_value": candidate.extracted_value,
            "comparison_reason": "이번 관찰이 기존 내용의 변화를 보여 준다." if index == 2
                                 else "이번 관찰에서 새로운 특징이 드러난다.",
            "existing_root_property_names_to_move": [],
        })
    return {"decisions": decisions}


def _collision_result(kind):
    result = _valid_result()
    decision = result["decisions"][1]
    decision.update(operation="ADD", matched_property_ref=None, proposed_setting_name="신체 능력")
    if kind == "ADD_ROOT_SCOPE_CONFLICT":
        decision.update(proposed_scope_name="신체 능력", proposed_setting_name="근력")
    return result


def _run_batch(outputs, *, ordered=True, target=None, targets=None):
    """Use the real bound, metering and Responses client; HTTP ends at MockTransport."""
    requests = []
    ledger = _Ledger()
    target = target or _target(provisional=ordered)
    targets = [target] if targets is None else targets

    def respond(request):
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        assert index < len(outputs), "Unexpected provider attempt."
        output = outputs[index]
        return httpx.Response(200, json={
            "status": "completed",
            "output_text": output if isinstance(output, str) else json.dumps(output),
            "usage": {"input_tokens": 120, "output_tokens": 80,
                      "input_tokens_details": {"cached_tokens": 10}},
        })

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            provider = OpenAIResponsesClient(
                api_key="test-only", model="gpt-5.6-luna", reasoning_effort="none",
                responses_api_url="https://api.openai.test/v1/responses", http_client=http_client,
            )
            client = MeteredTextGenerationClient(
                delegate=provider, ledger=ledger, analysis_job_id=JOB_ID,
                purpose="WORLD_SETTING_COMPARISON", default_model="gpt-5.6-luna",
                lease_token=LEASE_TOKEN, max_retries=0,
            )
            comparator = WorldSettingComparator(
                llm_client=client, model="gpt-5.6-luna", max_attempts=3,
                max_output_tokens=3000, batch_max_output_tokens=16000,
            )
            return await comparator.compare_batch(
                "RACE", _candidates(), targets, ordered_context=ordered,
            )

    try:
        return asyncio.run(execute()), requests, ledger
    except Exception as error:
        return error, requests, ledger


def _user(body):
    return json.loads(body["input"][1]["content"][0]["text"])


def _assert_seven_sources(result):
    assert not isinstance(result, Exception), result
    normalized, raw = result
    source_refs = [ref for decision in normalized.decisions
                   for ref in decision.source_candidate_refs]
    assert sorted(source_refs) == [f"C{index}" for index in range(1, 8)]
    assert {key: value for key, value in raw.items() if key != "validation_diagnostics"} == normalized.model_dump(mode="json")


@pytest.mark.parametrize("provisional", [False, True])
def test_ordered_batch_strict_schema_reaches_http_and_reservation(provisional):
    result, requests, ledger = _run_batch([_valid_result()], target=_target(provisional=provisional))
    _assert_seven_sources(result)
    assert len(requests) == 1
    body = requests[0]
    response_format = body["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["name"] == "ordered_world_setting_comparison_batch"
    assert response_format["strict"] is True
    schema = response_format["schema"]
    object_schemas = [schema, *[definition for definition in schema["$defs"].values()
                               if definition.get("type") == "object"]]
    assert len(object_schemas) == 2
    for node in object_schemas:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])
    decision_schema = object_schemas[1]
    for field in ["review_reason", "target_ref", "matched_property_ref",
                  "proposed_setting_name", "proposed_scope_name"]:
        prop = decision_schema["properties"][field]
        assert {"type": "null"} in prop["anyOf"]
        assert "default" not in prop
    assert body["prompt_cache_key"] == "world-setting-comparison-batch:v8:ordered-provisional-v6"
    assert body["model"] == "gpt-5.6-luna"
    assert body["reasoning"] == {"effort": "none"}
    assert body["store"] is False
    system, user = [item["content"][0]["text"] for item in body["input"]]
    assert "matched_property_ref" in system and "신체 능력" in system
    assert "matched_scope_name" not in decision_schema["properties"]
    assert "matched_property_name" not in decision_schema["properties"]
    assert decision_schema["properties"]["matched_property_ref"]["anyOf"][0]["enum"] == ["T1.P1"]
    assert _user(body)["targets"][0]["confirmation_status"] == (
        "PROVISIONAL" if provisional else "CONFIRMED"
    )
    assert str(SUBJECT_ID) not in system + user
    assert ledger.reservations[0]["reserved_tokens"] > metering._estimate_text_token_upper_bound(
        system, user, "gpt-5.6-luna", 16000,
    )
    assert ledger.settlements[0][1:] == (120, 10, 80, "SUCCESS")


def _all_add_result():
    result = _valid_result()
    for decision, source in zip(result["decisions"], _candidates(), strict=True):
        decision.update(operation="ADD", matched_property_ref=None,
                        proposed_setting_name=source.setting_name)
    return result


@pytest.mark.parametrize("provisional", [False, True])
@pytest.mark.parametrize("empty_properties", [False, True])
@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE"])
def test_connected_target_null_is_retried_with_input_reference_even_for_new_subject(
    provisional, empty_properties, operation, caplog,
):
    target = _target(provisional=provisional)
    if empty_properties:
        target = target.model_copy(update={"properties": []})
    valid = _all_add_result() if empty_properties else _valid_result()
    valid["decisions"][0]["operation"] = operation
    invalid = deepcopy(valid)
    invalid["decisions"][0].update(target_ref=None, proposed_value=REJECTED_VALUE)
    with caplog.at_level(logging.WARNING):
        result, requests, _ = _run_batch([invalid, invalid, valid], target=target)
    _assert_seven_sources(result)
    assert len(requests) == 3
    original = _user(requests[0])
    assert original["targets"][0]["confirmation_status"] == (
        "PROVISIONAL" if provisional else "CONFIRMED"
    )
    assert "properties가 빈 주체" in requests[0]["input"][0]["content"][0]["text"]
    for request in requests[1:]:
        user = _user(request)
        feedback = user.pop("validation_feedback")
        assert user == original
        assert feedback["reason_code"] == "CANONICAL_TARGET_REQUIRED"
        assert feedback["required_target"] == {
            "decision_index": 0, "source_candidate_refs": ["C1"], "target_ref": "T1",
        }
        assert feedback["issues"] == [{
            "field_path": "decisions[].target_ref", "decision_index": 0,
            "error_type": "target_required",
        }]
        assert REJECTED_VALUE not in json.dumps(feedback) + caplog.text
    assert result[0].decisions[0].operation == operation
    assert result[0].decisions[0].target_ref == "T1"
    assert result[0].decisions[0].proposed_value != REJECTED_VALUE


def test_repeated_missing_target_fails_atomically_and_retains_safe_final_reason(caplog):
    target = _target().model_copy(update={"properties": []})
    invalid = _all_add_result()
    invalid["decisions"][1].update(target_ref=None, proposed_value=REJECTED_VALUE)
    with caplog.at_level(logging.WARNING):
        result, requests, _ = _run_batch([invalid] * 3, target=target)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    assert "CANONICAL_TARGET_REQUIRED" in str(result)
    assert "decision_index=1" in str(result)
    for forbidden in [REJECTED_VALUE, str(SUBJECT_ID)]:
        assert forbidden not in str(result) + caplog.text


def test_no_connected_target_still_allows_new_subject_without_inventing_reference():
    valid = _all_add_result()
    for decision in valid["decisions"]:
        decision["target_ref"] = None
    result, requests, _ = _run_batch([valid], targets=[])
    _assert_seven_sources(result)
    assert len(requests) == 1
    assert all(decision.target_ref is None for decision in result[0].decisions)


@pytest.mark.parametrize("kind", ["ADD_EXISTING_PATH", "ADD_ROOT_SCOPE_CONFLICT"])
def test_ordered_batch_conflict_feedback_identifies_input_path_and_preserves_seven_sources(kind):
    invalid = _collision_result(kind)
    invalid["decisions"][1]["proposed_value"] = REJECTED_VALUE
    result, requests, ledger = _run_batch([invalid, _valid_result()])
    _assert_seven_sources(result)
    assert len(requests) == 2
    first, retried = [_user(body) for body in requests]
    feedback = retried.pop("validation_feedback")
    assert retried == first
    assert feedback["previous_response_rejected"] is True
    assert feedback["reason_code"] == kind
    assert feedback["conflict"] == {
        "decision_index": 1, "source_candidate_refs": ["C2"], "target_ref": "T1",
        "existing_paths": [{"property_ref": "T1.P1", "property_index": 0, "scope_name": None,
                            "setting_name": "신체 능력"}],
    }
    assert REJECTED_VALUE not in json.dumps(feedback)
    assert requests[0]["text"] == requests[1]["text"]
    assert [row["attempt"] for row in ledger.reservations] == [1, 2]
    assert ledger.reservations[0]["request_id"] != ledger.reservations[1]["request_id"]
    assert result[0].decisions[1].operation == "UPDATE"


@pytest.mark.parametrize("kind", ["ADD_EXISTING_PATH", "ADD_ROOT_SCOPE_CONFLICT"])
def test_ordered_batch_three_conflicts_fail_whole_batch_without_partial_result(kind, caplog):
    with caplog.at_level(logging.WARNING):
        result, requests, ledger = _run_batch([_collision_result(kind)] * 3)
    assert isinstance(result, ComparisonValidationError)
    assert "failed after 3 attempts" in str(result)
    assert len(requests) == len(ledger.settlements) == 3
    original = _user(requests[0])
    for body in requests[1:]:
        user = _user(body)
        feedback = user.pop("validation_feedback")
        assert feedback["reason_code"] == kind
        assert user == original
    assert caplog.text.count("response validation failed") == 2


def _invalid_response(kind):
    result = _valid_result()
    row = result["decisions"][1]
    if kind == "missing_nullable":
        del row["matched_property_ref"]
    elif kind == "extra_root":
        result[REJECTED_KEY] = {REJECTED_UUID: REJECTED_VALUE}
    elif kind == "extra_row":
        row[REJECTED_KEY] = {REJECTED_UUID: REJECTED_VALUE}
    elif kind == "invalid_type":
        row["proposed_value"] = {REJECTED_KEY: REJECTED_UUID}
    elif kind == "invalid_operation_payload":
        row["matched_property_ref"] = None
    elif kind == "unknown_source":
        row["source_candidate_refs"] = [REJECTED_UUID]
    elif kind == "unknown_target":
        row["target_ref"] = REJECTED_UUID
    elif kind == "duplicate_source":
        result["decisions"][-1]["source_candidate_refs"] = ["C1"]
    elif kind == "missing_source":
        result["decisions"].pop()
    elif kind == "invalid_json":
        return f"{REJECTED_KEY}:{REJECTED_UUID}:{REJECTED_VALUE}"
    else:
        raise AssertionError(kind)
    return result


@pytest.mark.parametrize("kind,reason", [
    ("missing_nullable", "RESPONSE_SCHEMA_INVALID"),
    ("extra_root", "RESPONSE_SCHEMA_INVALID"),
    ("extra_row", "RESPONSE_SCHEMA_INVALID"),
    ("invalid_type", "RESPONSE_SCHEMA_INVALID"),
    ("invalid_operation_payload", "RESPONSE_SCHEMA_INVALID"),
    ("unknown_source", "COMPARISON_VALIDATION_FAILED"),
    ("unknown_target", "COMPARISON_VALIDATION_FAILED"),
    ("duplicate_source", "COMPARISON_VALIDATION_FAILED"),
    ("missing_source", "COMPARISON_VALIDATION_FAILED"),
    ("invalid_json", "RESPONSE_JSON_INVALID"),
])
def test_ordered_batch_schema_or_coverage_retry_is_safe_and_can_recover(kind, reason, caplog):
    with caplog.at_level(logging.WARNING):
        result, requests, _ = _run_batch([_invalid_response(kind), _valid_result()])
    _assert_seven_sources(result)
    assert len(requests) == 2
    retried = _user(requests[1])
    feedback = retried.pop("validation_feedback")
    assert feedback["reason_code"] == reason
    if kind == "missing_nullable":
        assert feedback["issues"] == [{
            "field_path": "decisions[].matched_property_ref", "decision_index": 1,
            "error_type": "missing",
        }]
    if kind == "invalid_operation_payload":
        assert feedback["issues"][0]["reason_code"] == "UPDATE_MERGE_MATCH_REQUIRED"
        assert feedback["issues"][0]["field_path"] == "decisions[]"
        assert feedback["issues"][0]["decision_index"] == 1
    assert retried == _user(requests[0])
    safe_output = json.dumps(feedback) + caplog.text
    for forbidden in [REJECTED_KEY, REJECTED_VALUE, REJECTED_UUID, str(SUBJECT_ID)]:
        assert forbidden not in safe_output
    assert requests[0]["text"] == requests[1]["text"]


@pytest.mark.parametrize("cause", [
    ValueError(f"{REJECTED_KEY}:{REJECTED_UUID}:{REJECTED_VALUE}"),
    ValueError("UPDATE and MERGE require target_ref and matched_property_name.", REJECTED_UUID),
])
def test_ordered_feedback_does_not_promote_arbitrary_validator_context_to_rule(cause):
    error = ValidationError.from_exception_data("SyntheticRejectedResponse", [{
        "type": "value_error", "loc": ("decisions", 1, REJECTED_KEY),
        "input": REJECTED_VALUE, "ctx": {"error": cause, REJECTED_KEY: REJECTED_UUID},
    }])
    output = ordered_world_batch_retry_prompt('{"candidates": [], "targets": []}', error)
    feedback = json.loads(output)["validation_feedback"]
    assert feedback["reason_code"] == "RESPONSE_SCHEMA_INVALID"
    assert feedback["issues"] == [{
        "field_path": "decisions[]", "decision_index": 1, "error_type": "value_error",
    }]
    for forbidden in [REJECTED_KEY, REJECTED_VALUE, REJECTED_UUID]:
        assert forbidden not in output


def test_ordered_batch_two_path_conflicts_then_schema_failure_remains_atomic(caplog):
    with caplog.at_level(logging.WARNING):
        result, requests, _ = _run_batch([
            _collision_result("ADD_EXISTING_PATH"),
            _collision_result("ADD_ROOT_SCOPE_CONFLICT"),
            _invalid_response("invalid_operation_payload"),
        ])
    assert isinstance(result, ComparisonValidationError)
    assert "failed after 3 attempts" in str(result)
    assert "ValidationError(types=value_error)" in str(result)
    assert "decisions[1]:value_error:UPDATE_MERGE_MATCH_REQUIRED" in str(result)
    assert len(requests) == 3
    assert _user(requests[1])["validation_feedback"]["reason_code"] == "ADD_EXISTING_PATH"
    assert _user(requests[2])["validation_feedback"]["reason_code"] == "ADD_ROOT_SCOPE_CONFLICT"


@pytest.mark.parametrize("cause", [
    ValueError(f"{REJECTED_KEY}:{REJECTED_UUID}:{REJECTED_VALUE}"),
    ValueError("UPDATE and MERGE require target_ref and matched_property_name.", REJECTED_UUID),
])
def test_final_ordered_schema_diagnostics_omit_unknown_fields_values_and_context(cause):
    error = ValidationError.from_exception_data("SyntheticRejectedResponse", [{
        "type": "value_error", "loc": ("decisions", 1, REJECTED_KEY),
        "input": REJECTED_VALUE, "ctx": {"error": cause, REJECTED_KEY: REJECTED_UUID},
    }])
    summary = ordered_world_batch_error_summary(error)
    assert "decisions[1]:value_error" in summary
    assert "UPDATE_MERGE_MATCH_REQUIRED" not in summary
    for forbidden in [REJECTED_KEY, REJECTED_VALUE, REJECTED_UUID]:
        assert forbidden not in summary


def test_final_ordered_schema_diagnostics_allowlist_custom_error_types():
    error = ValidationError.from_exception_data("SyntheticRejectedResponse", [{
        "type": PydanticCustomError(REJECTED_KEY, REJECTED_VALUE),
        "loc": ("decisions", 1, REJECTED_UUID), "input": REJECTED_VALUE,
    }])
    summary = ordered_world_batch_error_summary(error)
    assert "decisions[1]:schema_invalid" in summary
    for forbidden in [REJECTED_KEY, REJECTED_VALUE, REJECTED_UUID]:
        assert forbidden not in summary


@pytest.mark.parametrize("kind", ["extra_root", "extra_row", "invalid_type", "invalid_json"])
def test_final_rejected_response_is_not_exposed_in_logs_or_failure_message(kind, caplog):
    with caplog.at_level(logging.WARNING):
        result, requests, _ = _run_batch([_invalid_response(kind)] * 3)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    for forbidden in [REJECTED_KEY, REJECTED_VALUE, REJECTED_UUID, str(SUBJECT_ID)]:
        assert forbidden not in str(result) + caplog.text


@pytest.mark.parametrize("target_ref,source_refs", [
    (REJECTED_UUID, ["C1"]), ("T1", [REJECTED_UUID]), ("T1", ["C1", REJECTED_UUID]),
])
def test_missing_target_feedback_never_copies_references_not_owned_by_input(target_ref, source_refs):
    error = OrderedWorldBatchValidationError(
        reason_code="CANONICAL_TARGET_REQUIRED", decision_index=0,
        source_candidate_refs=source_refs, target_ref=target_ref, property_indices=[],
    )
    result = json.loads(ordered_world_batch_retry_prompt(json.dumps({
        "candidates": [{"ref": "C1"}], "targets": [{"ref": "T1", "properties": []}],
    }), error))
    feedback = result["validation_feedback"]
    assert "required_target" not in feedback
    assert feedback["reason_code"] == "COMPARISON_VALIDATION_FAILED"
    assert REJECTED_UUID not in json.dumps(feedback) + ordered_world_batch_error_summary(error)


def test_ordered_batch_preserves_merged_source_group_among_all_seven_sources():
    valid = _valid_result()
    merged = valid["decisions"][2]
    merged.update(source_candidate_refs=["C3", "C4"], consolidation_status="MERGED",
                  proposed_value="관찰3과 관찰4", comparison_reason="두 관찰이 같은 특징을 보완한다.")
    del valid["decisions"][3]
    invalid = deepcopy(valid)
    invalid["decisions"][1].update(operation="ADD", matched_property_ref=None,
                                    proposed_setting_name="신체 능력")
    result, requests, _ = _run_batch([invalid, valid])
    _assert_seven_sources(result)
    assert len(requests) == 2
    assert len(result[0].decisions) == 6
    assert result[0].decisions[2].source_candidate_refs == ["C3", "C4"]
    assert result[0].decisions[2].consolidation_status == "MERGED"


def test_legacy_world_batch_http_request_preserves_transport_with_integrated_prompt():
    result, requests, _ = _run_batch([_valid_result(ordered=False)], ordered=False)
    _assert_seven_sources(result)
    expected_user = {
        "category": "RACE",
        "candidates": [{
            "ref": candidate.candidate_ref, "subject_name": candidate.subject_name,
            "scope_name": None, "setting_name": candidate.setting_name,
            "extracted_value": candidate.extracted_value,
            "extracted_values": [candidate.extracted_value],
            "evidence_spans": [{"quote": f"합성 근거{index}", "start_offset": None,
                                "end_offset": None}],
        } for index, candidate in enumerate(_candidates(), start=1)],
        "targets": [{"ref": "T1", "subject_name": "합성 종족", "properties": [{
            "scope_name": None, "setting_name": "신체 능력", "value": "과거 관찰",
        }]}],
    }
    assert requests == [{
        "model": "gpt-5.6-luna", "store": False,
        "input": [
            {"role": "system", "content": [{"type": "input_text",
                "text": BATCH_COMPARISON_PROMPT_PATH.read_text(encoding="utf-8")
                    + "\n\n" + USER_FACING_REASON_INSTRUCTIONS} ]},
            {"role": "user", "content": [{"type": "input_text",
                "text": json.dumps(expected_user, ensure_ascii=False)}]},
        ],
        "max_output_tokens": 16000,
        "prompt_cache_key": "world-setting-comparison-batch:v8",
        "reasoning": {"effort": "none"},
    }]
