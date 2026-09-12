"""Offline provider-boundary regression tests for property selection and diagnostics."""

import asyncio
import json
import logging
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)


@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Property selection tests may not access the network.")

    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode("utf-8"))

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda name: Encoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


def candidate(ref="C1", *, scope=None, name="전투 특징"):
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=ref, candidate_id=uuid4(), subject_name="고블린",
        scope_name=scope, setting_name=name, extracted_value="덫을 뿌리고 매복한다.",
        evidence_spans=[{"quote": "SECRET_SOURCE_EVIDENCE", "start_offset": 0, "end_offset": 22}],
    )


def target(*, provisional=False, scope="행동 및 사냥 방식"):
    return WorkerWorldSettingComparisonTarget(
        subject_name="고블린", version=1,
        **({"provisional_subject_key": f"provisional-world:{uuid4()}"} if provisional
           else {"world_setting_id": uuid4()}),
        properties=[{"scope_name": scope, "setting_name": "함정 사용", "value": "SECRET_STORED_VALUE",
                     "provenance": {"confirmation_status": "CONFIRMED"}}],
    )


def decision(source, *, operation="ADD", matched_ref=None, review_reason=None):
    return {
        "source_candidate_refs": [source.candidate_ref], "consolidation_status": "SINGLE",
        "operation": operation, "review_reason": review_reason, "target_ref": "T1",
        "matched_property_ref": matched_ref,
        "proposed_scope_name": None if operation in {"UPDATE", "MERGE"} else source.scope_name,
        "proposed_setting_name": None if operation in {"UPDATE", "MERGE"} else source.setting_name,
        "proposed_value": source.extracted_value,
        "comparison_reason": "매복 전투 방식과 기존 함정 사용의 관련성 및 적용 범위 확인이 필요하다.",
        "existing_root_property_names_to_move": [],
    }


def run(responses, sources, targets, **options):
    requests = []

    class Client:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            response = responses[min(len(requests) - 1, len(responses) - 1)]
            if isinstance(response, Exception):
                raise response
            return LlmTextResponse(text=response if isinstance(response, str) else json.dumps(response))

    async def execute():
        comparator = WorldSettingComparator(
            llm_client=Client(), model="offline-test", max_attempts=3,
            max_output_tokens=3000, batch_max_output_tokens=16000,
        )
        return await comparator.compare_batch("MONSTER", sources, targets, **options)

    try:
        return asyncio.run(execute()), requests
    except Exception as error:
        return error, requests


@pytest.mark.parametrize("operation", ["UPDATE", "MERGE"])
@pytest.mark.parametrize("provisional", [False, True])
def test_property_ref_restores_existing_matched_and_proposed_paths(operation, provisional):
    source = candidate(scope="행동 및 사냥 방식")
    stored = target(provisional=provisional)
    before = source.model_dump()
    result, requests = run([{"decisions": [decision(source, operation=operation, matched_ref="T1.P1")]}],
                           [source], [stored], ordered_context=True)
    assert not isinstance(result, Exception), result
    row = result[0].decisions[0]
    assert (row.matched_scope_name, row.matched_property_name) == ("행동 및 사냥 방식", "함정 사용")
    assert (row.proposed_scope_name, row.proposed_setting_name) == ("행동 및 사냥 방식", "함정 사용")
    assert source.model_dump() == before
    prompt = json.loads(requests[0]["user_prompt"])
    assert prompt["targets"][0]["properties"][0]["ref"] == "T1.P1"
    schema = requests[0]["response_schema"].schema["$defs"]["OrderedWorldPropertyDecision"]
    assert "matched_scope_name" not in schema["properties"]
    assert "matched_property_name" not in schema["properties"]
    assert schema["properties"]["matched_property_ref"]["anyOf"][0]["enum"] == ["T1.P1"]
    assert result[1]["validation_diagnostics"] == []


@pytest.mark.parametrize("operation", ["UPDATE", "MERGE", "EXCLUDE"])
def test_unscoped_different_name_requires_explicit_review_and_preserves_original(operation):
    source, stored = candidate(), target()
    invalid = decision(source, operation=operation, matched_ref="T1.P1")
    valid = decision(source, operation="REVIEW_REQUIRED", matched_ref="T1.P1",
                     review_reason="SCOPE_UNRESOLVED")
    valid["proposed_value"] = "SECRET_REWRITTEN_VALUE"
    before = source.model_dump()
    result, requests = run([{"decisions": [invalid]}, {"decisions": [valid]}], [source], [stored],
                           ordered_context=True)
    assert not isinstance(result, Exception), result
    assert len(requests) == 2
    row = result[0].decisions[0]
    assert row.operation == "REVIEW_REQUIRED" and row.review_reason == "SCOPE_UNRESOLVED"
    assert (row.proposed_scope_name, row.proposed_setting_name) == (None, source.setting_name)
    assert row.proposed_value == source.extracted_value
    assert row.comparison_reason == valid["comparison_reason"]
    assert source.model_dump() == before
    diagnostic = result[1]["validation_diagnostics"][0]
    assert diagnostic["rule_code"] == "SOURCE_SCOPE_MISMATCH"
    assert diagnostic["candidate_refs"] == ["C1"]
    assert diagnostic["selected_properties"] == [
        {"ref": "T1.P1", "target_ref": "T1", "scope_name": stored.properties[0].scope_name,
         "setting_name": stored.properties[0].setting_name},
    ]


@pytest.mark.parametrize("kind", ["source_scope", "proposed_scope", "proposed_name", "root", "multiple"])
def test_explicit_unscoped_review_rejects_incompatible_input_or_path(kind):
    source, stored = candidate(), target()
    sources = [source]
    row = decision(source, operation="REVIEW_REQUIRED", matched_ref="T1.P1", review_reason="SCOPE_UNRESOLVED")
    if kind == "source_scope":
        sources = [source.model_copy(update={"scope_name": "원본 범위"})]
    elif kind == "proposed_scope":
        row["proposed_scope_name"] = "추측 범위"
    elif kind == "proposed_name":
        row["proposed_setting_name"] = "다른 이름"
    elif kind == "root":
        stored = target(scope=None)
    else:
        sources.append(candidate("C2"))
        row["source_candidate_refs"] = ["C1", "C2"]
        row["consolidation_status"] = "MERGED"
    result, requests = run([{"decisions": [row]}], sources, [stored], ordered_context=True)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    assert [entry["attempt_number"] for entry in result.validation_diagnostics] == [1, 2, 3]


def test_same_name_automatic_scope_review_is_preserved():
    source = candidate(name="함정 사용")
    result, _ = run([{"decisions": [decision(source, operation="UPDATE", matched_ref="T1.P1")]}],
                    [source], [target()], ordered_context=True)
    assert not isinstance(result, Exception), result
    assert result[0].decisions[0].review_reason == "SCOPE_UNRESOLVED"


@pytest.mark.parametrize("selected_ref", ["T1.P1", "T1.P2"])
def test_null_scope_retry_explains_review_reason_and_keeps_original_episode_19_path(selected_ref, caplog):
    """The live rejected-response history is reproduced with offline responses."""
    source = candidate(name="근접 무기 효과").model_copy(update={
        "extracted_value": "근접 무기 사용 시 마비독을 상시 부여한다.",
    })
    stored = WorkerWorldSettingComparisonTarget(
        subject_name="고블린", version=1, world_setting_id=uuid4(),
        properties=[{"scope_name": "전투 및 능력", "setting_name": name, "value": "SECRET_STORED_VALUE",
                     "provenance": {"confirmation_status": "CONFIRMED"}}
                    for name in ("독", "패시브 능력")],
    )
    concrete = decision(source, operation="MERGE", matched_ref="T1.P1")
    wrong_review = decision(source, operation="REVIEW_REQUIRED", matched_ref=selected_ref,
                            review_reason="SCOPE_MISMATCH")
    valid_review = {**wrong_review, "review_reason": "SCOPE_UNRESOLVED",
                    "comparison_reason": "근접 무기에 부여되는 마비독과 기존 전투 능력의 관련성 및 적용 범위 확인이 필요하다."}
    before = source.model_dump()
    with caplog.at_level(logging.WARNING):
        result, requests = run([{"decisions": [row]} for row in (concrete, wrong_review, valid_review)],
                               [source], [stored], ordered_context=True)
    assert not isinstance(result, Exception), result
    assert len(requests) == 3
    for request in requests[1:]:
        feedback = json.loads(request["user_prompt"])["validation_feedback"]
        correction = json.dumps(feedback, ensure_ascii=False)
        assert "SCOPE_UNRESOLVED" in correction
        assert "원본" in correction and "null" in correction
        assert "복사" in correction
        assert "SECRET_" not in correction
    schema_issue = json.loads(requests[2]["user_prompt"])["validation_feedback"]["issues"][0]
    assert schema_issue["reason_code"] == "SCOPE_MISMATCH_MATCH_REQUIRED"
    row = result[0].decisions[0]
    assert (row.operation, row.review_reason) == ("REVIEW_REQUIRED", "SCOPE_UNRESOLVED")
    assert (row.proposed_scope_name, row.proposed_setting_name, row.proposed_value) == (
        None, source.setting_name, source.extracted_value,
    )
    assert row.matched_scope_name == "전투 및 능력"
    assert row.matched_property_name == stored.properties[int(selected_ref[-1]) - 1].setting_name
    assert row.existing_root_property_names_to_move == []
    assert source.model_dump() == before
    assert [entry["rule_code"] for entry in result[1]["validation_diagnostics"]] == [
        "SOURCE_SCOPE_MISMATCH", "SCOPE_MISMATCH_MATCH_REQUIRED",
    ]
    assert "SECRET_" not in caplog.text


@pytest.mark.parametrize("proposed_scope", [None, "행동 및 사냥 방식"])
def test_null_scope_mismatch_remains_invalid_without_an_explicit_valid_review(proposed_scope):
    source, stored = candidate(), target()
    invalid = decision(source, operation="REVIEW_REQUIRED", matched_ref="T1.P1",
                       review_reason="SCOPE_MISMATCH")
    invalid["proposed_scope_name"] = proposed_scope
    before = source.model_dump()
    result, requests = run([{"decisions": [invalid]}], [source], [stored], ordered_context=True)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    expected = "SCOPE_MISMATCH_MATCH_REQUIRED" if proposed_scope is None else "SCOPE_MISMATCH_REVIEW_INVALID"
    assert {entry["rule_code"] for entry in result.validation_diagnostics} == {expected}
    assert source.model_dump() == before


def test_legacy_different_name_scope_review_still_rejected():
    source = candidate()
    row = decision(source, operation="REVIEW_REQUIRED", review_reason="SCOPE_UNRESOLVED")
    row.pop("matched_property_ref")
    row.update(matched_scope_name="행동 및 사냥 방식", matched_property_name="함정 사용")
    result, requests = run([{"decisions": [row]}], [source], [target()], ordered_context=False)
    assert isinstance(result, ComparisonValidationError)
    assert all("response_schema" not in request for request in requests)
    assert all("validation_failure_callback" not in request for request in requests)


@pytest.mark.parametrize("selected_ref", ["T1.P999", "SECRET_FORGED_REF", "T2.P1"])
def test_unknown_or_wrong_target_property_never_becomes_a_selected_diagnostic(selected_ref, caplog):
    source = candidate()
    invalid = decision(source, operation="REVIEW_REQUIRED", matched_ref=selected_ref,
                       review_reason="SCOPE_UNRESOLVED")
    with caplog.at_level(logging.WARNING):
        result, requests = run([{"decisions": [invalid]}], [source], [target(), target()], ordered_context=True)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    assert all(not entry["selected_properties"] for entry in result.validation_diagnostics)
    assert all(entry["candidate_refs"] == ["C1"] for entry in result.validation_diagnostics)
    emitted = json.dumps(result.validation_diagnostics) + caplog.text + str(result)
    assert "SECRET_" not in emitted
    assert "T1.P999" not in emitted
    assert all("SECRET_" not in json.dumps(json.loads(request["user_prompt"]).get("validation_feedback", {}))
               for request in requests)


def test_existing_proposed_path_must_be_null_even_when_it_matches_the_selected_input():
    source = candidate(scope="행동 및 사냥 방식")
    good = decision(source, operation="UPDATE", matched_ref="T1.P1")
    bad = {**good, "proposed_scope_name": "행동 및 사냥 방식", "proposed_setting_name": "함정 사용"}
    result, requests = run([{"decisions": [bad]}, {"decisions": [good]}], [source], [target()],
                           ordered_context=True)
    assert not isinstance(result, Exception), result
    assert len(requests) == 2
    assert result[1]["validation_diagnostics"][0]["rule_code"] == "EXISTING_PROPOSED_PATH_FORBIDDEN"


def test_schema_failure_attributes_only_input_owned_candidate_and_selected_path(caplog):
    source = candidate()
    row = decision(source, operation="REVIEW_REQUIRED", matched_ref="T1.P1", review_reason="SCOPE_UNRESOLVED")
    row["SECRET_DYNAMIC_KEY"] = "SECRET_RESPONSE_VALUE"
    with caplog.at_level(logging.WARNING):
        result, _ = run([{"decisions": [row]}], [source], [target()], ordered_context=True)
    assert isinstance(result, ComparisonValidationError)
    assert len(result.validation_diagnostics) == 3
    assert all(entry["candidate_refs"] == ["C1"] and entry["selected_properties"]
               for entry in result.validation_diagnostics)
    assert "SECRET_" not in json.dumps(result.validation_diagnostics) + caplog.text + str(result)


@pytest.mark.parametrize("response", ["SECRET_NON_JSON", {"decisions": "SECRET_INVALID_LIST"}])
def test_unattributable_response_has_no_candidate_or_selected_property(response):
    result, _ = run([response], [candidate()], [target()], ordered_context=True)
    assert isinstance(result, ComparisonValidationError)
    assert len(result.validation_diagnostics) == 3
    assert all(entry["candidate_refs"] == [] and entry["selected_properties"] == []
               for entry in result.validation_diagnostics)


def test_recovery_limits_attempts_preserves_add_source_and_allows_existing_update_path():
    source = candidate()
    wrong_add = {**decision(source), "proposed_setting_name": "새로운 이름"}
    result, requests = run([{"decisions": [wrong_add]}], [source], [target()], ordered_context=True,
                           max_attempts_override=1, preserve_source_paths=True)
    assert isinstance(result, ComparisonValidationError) and len(requests) == 1
    assert result.validation_diagnostics[0]["rule_code"] == "RECOVERY_SOURCE_PATH_CHANGED"
    same_scope_source = candidate(scope="행동 및 사냥 방식")
    good = decision(same_scope_source, operation="UPDATE", matched_ref="T1.P1")
    result, _ = run([{"decisions": [good]}], [same_scope_source], [target()], ordered_context=True,
                    max_attempts_override=1, preserve_source_paths=True)
    assert not isinstance(result, Exception), result
    assert result[0].decisions[0].proposed_setting_name == "함정 사용"


def test_recovery_forbids_root_moves():
    source = candidate()
    row = decision(source)
    row["existing_root_property_names_to_move"] = ["임의 속성"]
    result, requests = run([{"decisions": [row]}], [source], [target()], ordered_context=True,
                           max_attempts_override=1, preserve_source_paths=True)
    assert isinstance(result, ComparisonValidationError) and len(requests) == 1
    assert result.validation_diagnostics[0]["rule_code"] == "RECOVERY_SOURCE_PATH_CHANGED"


@pytest.mark.parametrize("error", [ValueError("SECRET_CLIENT_FAILURE"), OrderedInputContextError("fixed input")])
def test_execution_and_input_errors_never_gain_response_diagnostics_or_retry(error):
    result, requests = run([error], [candidate()], [target()], ordered_context=True)
    assert result is error and len(requests) == 1
    assert not hasattr(result, "validation_diagnostics")


def test_empty_target_schema_only_allows_null_property_selection():
    source = candidate()
    empty = target().model_copy(update={"properties": []})
    result, requests = run([{"decisions": [decision(source)]}], [source], [empty], ordered_context=True)
    assert not isinstance(result, Exception), result
    field = requests[0]["response_schema"].schema["$defs"]["OrderedWorldPropertyDecision"]["properties"]
    assert field["matched_property_ref"] == {"type": "null"}


@pytest.mark.parametrize("reference", ["T1.P1", "P1"])
def test_user_reason_cannot_leak_new_property_reference(reference):
    source = candidate()
    good = decision(source, operation="REVIEW_REQUIRED", matched_ref="T1.P1", review_reason="SCOPE_UNRESOLVED")
    bad = {**good, "comparison_reason": f"{reference}의 범위와 관련되어 검토가 필요하다."}
    result, requests = run([{"decisions": [bad]}, {"decisions": [good]}], [source], [target()], ordered_context=True)
    assert not isinstance(result, Exception), result
    assert len(requests) == 2
    assert result[1]["validation_diagnostics"][0]["rule_code"] == "COMPARISON_REASON_REFERENCE_INVALID"
    assert result[0].decisions[0].comparison_reason == good["comparison_reason"]
