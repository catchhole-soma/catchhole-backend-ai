"""Synthetic reproductions of empty-target and differently scoped world comparisons."""

import asyncio
from copy import deepcopy
import json
import logging
import socket
import traceback
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_world_batch_contract import (
    ORDERED_WORLD_BATCH_RESPONSE_SCHEMA,
    OrderedWorldBatchValidationError,
    ordered_world_batch_retry_prompt,
)
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_context import WorkerAnalysisReference
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)


@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("World review tests must not open network connections.")

    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode("utf-8"))

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda name: Encoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


def _candidate(ref="C1", *, name="조명 환경", scope="외곽 지역", subject="미궁"):
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=ref, candidate_id=uuid4(), subject_name=subject,
        scope_name=scope, setting_name=name, extracted_value="외곽으로 갈수록 어두워진다.",
        evidence_spans=[{"quote": "외곽으로 갈수록 빛나는 수정들이 줄었다.",
                         "start_offset": 10, "end_offset": 34}],
    )


def _target(*, scope="1층", empty=False, provisional=False):
    return WorkerWorldSettingComparisonTarget(
        subject_name="미궁" if not empty else "바바리안의 무구", version=1,
        **({"provisional_subject_key": f"provisional-world:{uuid4()}"} if provisional
           else {"world_setting_id": uuid4()}),
        properties=[] if empty else [{
            "scope_name": scope, "setting_name": "광원", "value": "일부 구역은 어둡다.",
            "provenance": {"confirmation_status": "CONFIRMED"},
        }],
    )


def _add(candidate):
    return {
        "source_candidate_refs": [candidate.candidate_ref], "consolidation_status": "SINGLE",
        "operation": "ADD", "review_reason": None, "target_ref": "T1",
        "matched_property_ref": None,
        "proposed_scope_name": candidate.scope_name, "proposed_setting_name": candidate.setting_name,
        "proposed_value": candidate.extracted_value, "comparison_reason": "독립된 새 사실을 추가한다.",
        "existing_root_property_names_to_move": [],
    }


def _review(candidate, target, *, ordered=True):
    result = {
        **_add(candidate), "operation": "REVIEW_REQUIRED", "review_reason": "SCOPE_MISMATCH",
        "matched_property_ref": "T1.P1",
        "comparison_reason": "외곽 지역의 조명과 기존 광원이 관련되어 보이지만 적용 범위 확인이 필요하다.",
    }
    if not ordered:
        result.pop("matched_property_ref")
        result.update(matched_scope_name=target.properties[0].scope_name, matched_property_name="광원")
    return result


def _run(candidates, target, responses, *, ordered=True):
    requests = []

    class Client:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            assert len(requests) <= len(responses), "Unexpected additional provider attempt."
            return LlmTextResponse(text=json.dumps(responses[len(requests) - 1]))

    async def execute():
        comparator = WorldSettingComparator(
            llm_client=Client(), model="offline-test-model", max_attempts=3,
            max_output_tokens=3000, batch_max_output_tokens=16000,
        )
        return await comparator.compare_batch("LOCATION", candidates, [target], ordered_context=ordered)

    try:
        return asyncio.run(execute()), requests
    except Exception as error:
        return error, requests


@pytest.mark.parametrize("kind,code", [
    ("absent_exclude", "MATCHED_PROPERTY_REF_INVALID"),
    ("absent_update", "MATCHED_PROPERTY_REF_INVALID"),
    ("single_child_scope", "GENERATED_SCOPE_REQUIRES_SIBLINGS"),
])
def test_empty_target_retries_specific_path_error_then_preserves_all_new_sources(kind, code, caplog):
    candidates = [_candidate(f"C{i}", name=name, scope=None, subject="바바리안의 무구")
                  for i, name in enumerate(["품질", "재질 특성", "재질"], 1)]
    target = _target(empty=True, provisional=True)
    valid = {"decisions": [_add(candidate) for candidate in candidates]}
    invalid = deepcopy(valid)
    rejected = invalid["decisions"][-1]
    if kind == "single_child_scope":
        rejected["proposed_scope_name"] = "SECRET_GENERATED_SCOPE"
    else:
        rejected.update(operation="EXCLUDE" if kind == "absent_exclude" else "UPDATE",
                        matched_property_ref="SECRET_GENERATED_PROPERTY_REF")
        if kind == "absent_update":
            rejected.update(proposed_scope_name=None, proposed_setting_name=None)
    with caplog.at_level(logging.WARNING):
        result, requests = _run(candidates, target, [invalid, valid])

    assert not isinstance(result, Exception), result
    assert len(requests) == 2
    assert [row.source_candidate_refs for row in result[0].decisions] == [["C1"], ["C2"], ["C3"]]
    assert all(row.operation == "ADD" and row.target_ref == "T1" for row in result[0].decisions)
    first = json.loads(requests[0]["user_prompt"])
    second = json.loads(requests[1]["user_prompt"])
    feedback = second.pop("validation_feedback")
    assert first == second
    assert feedback["reason_code"] == code
    assert feedback["issues"][0]["decision_index"] == 2
    assert feedback["input_paths"]["source_paths"] == [
        {"candidate_ref": "C3", "scope_name": None, "setting_name": "재질"},
    ]
    assert feedback["input_paths"]["existing_paths"] == []
    assert "SECRET_" not in json.dumps(feedback) + caplog.text
    assert "같은 배치의 다른 후보" in requests[0]["system_prompt"]
    assert "properties가 비어 있으면" in requests[0]["system_prompt"]


@pytest.mark.parametrize("operation", ["UPDATE", "MERGE"])
@pytest.mark.parametrize("matched_scope", [None, "1층"])
def test_wrong_scope_update_retries_into_explicit_review_preserving_both_paths(
    operation, matched_scope,
):
    candidate = _candidate()
    target = _target(scope=matched_scope)
    review = _review(candidate, target)
    invalid = {**review, "operation": operation, "review_reason": None,
               "proposed_scope_name": None, "proposed_setting_name": None}
    source_before = candidate.model_dump()
    result, requests = _run([candidate], target, [{"decisions": [invalid]}, {"decisions": [review]}])

    assert not isinstance(result, Exception), result
    decision = result[0].decisions[0]
    assert decision.operation == "REVIEW_REQUIRED"
    assert decision.review_reason == "SCOPE_MISMATCH"
    assert (decision.matched_scope_name, decision.matched_property_name) == (matched_scope, "광원")
    assert (decision.proposed_scope_name, decision.proposed_setting_name) == ("외곽 지역", "조명 환경")
    assert decision.proposed_value == candidate.extracted_value
    assert decision.comparison_reason == review["comparison_reason"]
    assert candidate.model_dump() == source_before
    assert requests[0]["response_schema"] is requests[1]["response_schema"]
    feedback = json.loads(requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason_code"] == "SOURCE_SCOPE_MISMATCH"
    assert feedback["input_paths"]["source_paths"][0]["scope_name"] == "외곽 지역"
    assert feedback["input_paths"]["existing_paths"] == [
        {"property_ref": "T1.P1", "property_index": 0,
         "scope_name": matched_scope, "setting_name": "광원"},
    ]
    assert "SCOPE_MISMATCH" in feedback["correction"]
    assert "SCOPE_MISMATCH" in json.dumps(ORDERED_WORLD_BATCH_RESPONSE_SCHEMA.schema)


@pytest.mark.parametrize("invalid_case", [
    "missing_target", "unknown_target", "missing_property", "unknown_property", "same_scope",
    "unscoped_source", "multiple_sources", "changed_proposed_scope", "changed_proposed_name",
    "move_property", "non_review_operation",
])
def test_scope_mismatch_does_not_accept_invalid_targets_sources_or_rewrite_paths(invalid_case, caplog):
    candidate = _candidate()
    target = _target()
    decision = _review(candidate, target)
    candidates = [candidate]
    if invalid_case == "missing_target":
        decision["target_ref"] = None
    elif invalid_case == "unknown_target":
        decision["target_ref"] = "SECRET_TARGET"
    elif invalid_case == "missing_property":
        decision["matched_property_ref"] = None
    elif invalid_case == "unknown_property":
        decision["matched_property_ref"] = "SECRET_PROPERTY"
    elif invalid_case == "same_scope":
        candidates = [candidate.model_copy(update={"scope_name": "1층"})]
        decision["proposed_scope_name"] = "1층"
    elif invalid_case == "unscoped_source":
        candidates = [candidate.model_copy(update={"scope_name": None})]
    elif invalid_case == "multiple_sources":
        candidates.append(_candidate("C2"))
        decision.update(source_candidate_refs=["C1", "C2"], consolidation_status="MERGED")
    elif invalid_case == "changed_proposed_scope":
        decision["proposed_scope_name"] = "SECRET_SCOPE"
    elif invalid_case == "changed_proposed_name":
        decision["proposed_setting_name"] = "SECRET_NAME"
    elif invalid_case == "move_property":
        decision["existing_root_property_names_to_move"] = ["SECRET_PROPERTY"]
    else:
        decision["operation"] = "MERGE"
    with caplog.at_level(logging.WARNING):
        result, requests = _run(candidates, target, [{"decisions": [decision]}] * 3)

    assert isinstance(result, ComparisonValidationError), result
    assert len(requests) == 3
    assert "SECRET_" not in str(result) + caplog.text + "".join(traceback.format_exception(result))
    for request in requests[1:]:
        assert "SECRET_" not in json.dumps(json.loads(request["user_prompt"])["validation_feedback"])


def test_scope_mismatch_remains_rejected_in_confirmed_only_batch():
    candidate = _candidate()
    target = _target()
    result, requests = _run([candidate], target, [{"decisions": [_review(candidate, target, ordered=False)]}] * 3,
                            ordered=False)
    assert isinstance(result, ComparisonValidationError)
    assert len(requests) == 3
    assert all("response_schema" not in request for request in requests)
    assert all("SCOPE_MISMATCH" not in request["system_prompt"] for request in requests)


def test_same_path_sources_can_each_require_review_while_independent_setting_succeeds():
    sources = [_candidate(ref, name="광원", scope="외곽 지역") for ref in ("C1", "C2")]
    sources[1] = sources[1].model_copy(update={"extracted_value": "외곽의 불빛은 희미하다."})
    sources.append(_candidate("C3", name="통로 너비", scope="외곽 지역"))
    stored = _target(scope=None)
    original = deepcopy([source.model_dump() for source in sources])
    response = {"decisions": [_review(source, stored) for source in sources[:2]] + [_add(sources[2])]}

    result, requests = _run(sources, stored, [response])

    assert not isinstance(result, Exception), result
    assert len(requests) == 1
    rows = result[0].decisions
    assert [row.source_candidate_refs for row in rows] == [["C1"], ["C2"], ["C3"]]
    assert [row.operation for row in rows] == ["REVIEW_REQUIRED", "REVIEW_REQUIRED", "ADD"]
    assert [row.proposed_value for row in rows[:2]] == [source.extracted_value for source in sources[:2]]
    assert all(row.matched_scope_name is None and row.matched_property_name == "광원" for row in rows[:2])
    assert all(row.proposed_scope_name == "외곽 지역" for row in rows)
    assert [source.model_dump() for source in sources] == original
    assert "source 하나당 별도 검토 decision" in requests[0]["system_prompt"]


def test_new_target_allows_content_based_exclude_without_matched_path():
    candidate = _candidate(scope=None)
    decision = {**_add(candidate), "operation": "EXCLUDE", "comparison_reason": "일시적 사건이라 제외한다."}
    result, requests = _run([candidate], _target(empty=True, provisional=True), [{"decisions": [decision]}])
    assert not isinstance(result, Exception), result
    assert result[0].decisions[0].matched_property_name is None
    assert result[0].decisions[0].target_ref == "T1"
    assert len(requests) == 1


@pytest.mark.parametrize("code", [
    "MATCHED_EXCLUDE_PATH_NOT_FOUND", "MATCHED_PROPERTY_PATH_NOT_FOUND",
    "SOURCE_SCOPE_MISMATCH", "GENERATED_SCOPE_REQUIRES_SIBLINGS",
])
def test_targeted_feedback_never_echoes_unowned_references(code):
    error = OrderedWorldBatchValidationError(
        reason_code=code, decision_index=0, source_candidate_refs=["SECRET_REF"],
        target_ref="SECRET_TARGET", property_indices=[0],
    )
    feedback = json.loads(ordered_world_batch_retry_prompt(
        '{"candidates":[],"targets":[]}', error,
    ))["validation_feedback"]
    assert "SECRET_" not in json.dumps(feedback)
    assert "input_paths" not in feedback


@pytest.mark.parametrize("matched_scope", [None, "1층"])
def test_pending_scope_review_preserves_reference_paths_without_changing_legacy_prompt(matched_scope):
    original = {
        "domain": "worldSettings", "subjectName": "미궁", "sourceEpisodeNo": 10,
        "reason": "두 범위의 관련성 확인이 필요하다.", "settingName": "조명 환경",
    }
    legacy = WorkerAnalysisReference.model_validate(original).prompt_data()
    assert all(field not in legacy for field in (
        "scope_name", "matched_scope_name", "matched_property_name",
    ))
    pending = WorkerAnalysisReference.model_validate({
        **original, "scopeName": "외곽 지역", "matchedScopeName": matched_scope,
        "matchedPropertyName": "광원",
    }).prompt_data()
    assert pending["scope_name"] == "외곽 지역"
    assert pending["matched_property_name"] == "광원"
    assert pending.get("matched_scope_name") == matched_scope
    assert pending["confirmation_status"] == "UNRESOLVED"
    assert pending["applied_to_current_state"] is False
