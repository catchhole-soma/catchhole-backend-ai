"""Offline production contract tests for semantic paths and atomic salvage."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_batch_recovery import compare_world_batch_with_recovery, recover_world_batch
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerWorldSettingComparisonBatchCandidate, WorkerWorldSettingComparisonTarget


def candidate(ref, name, value, scope=None):
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=ref, candidate_id=uuid4(), subject_name="서고",
        scope_name=scope, setting_name=name, extracted_value=value,
        evidence_spans=[{"quote": value}],
    )


def target(properties=()):
    return WorkerWorldSettingComparisonTarget(
        world_setting_id=uuid4(), subject_name="서고", version=1, properties=[{**prop, "provenance": {"confirmation_status": "CONFIRMED"}} for prop in properties],
    )


def decision(refs, name, value, *, operation="ADD", scope=None, matched=None, ordered=False):
    row = {
        "source_candidate_refs": refs, "consolidation_status": "MERGED" if len(refs) > 1 else "SINGLE",
        "operation": operation, "review_reason": None, "target_ref": "T1",
        "proposed_scope_name": scope, "proposed_setting_name": name,
        "proposed_value": value, "comparison_reason": "원문과 기존 설명의 적용 대상과 조건이 같습니다.",
        "existing_root_property_names_to_move": [],
    }
    if ordered:
        row["matched_property_ref"] = matched
        if operation in {"UPDATE", "MERGE"}:
            row.update(proposed_scope_name=None, proposed_setting_name=None)
    else:
        row.update(matched_scope_name=scope if matched else None, matched_property_name=matched)
    return row


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


def comparator(client):
    return WorldSettingComparator(
        llm_client=client, model="offline-test-model", max_attempts=3,
        max_output_tokens=3000, batch_max_output_tokens=16000,
    )


@pytest.mark.parametrize("ordered", [False, True])
@pytest.mark.parametrize("operation", ["EXCLUDE", "MERGE", "UPDATE"])
@pytest.mark.parametrize("source_scope", [None, "의례"])
def test_semantic_match_retains_actual_existing_path_across_organizational_paths(ordered, operation, source_scope):
    source = candidate("C1", "서고 출입", "인증 문장을 아는 자만 입장한다.", source_scope)
    existing = target([{"scope_name": "서고 출입", "setting_name": "인증 조건",
                        "value": "인증 문장을 아는 자만 입장하며 방문 기록을 남긴다."}])
    row = decision(["C1"], "인증 조건", source.extracted_value, operation=operation,
                   scope="서고 출입", matched="T1.P1" if ordered else "인증 조건", ordered=ordered)
    result, _ = asyncio.run(comparator(Client([{"decisions": [row]}])).compare_batch(
        "WORLD_RULE_HISTORY", [source], [existing], ordered_context=ordered, max_attempts_override=1,
    ))
    assert result.decisions[0].operation == operation
    assert result.decisions[0].matched_scope_name == "서고 출입"
    assert result.decisions[0].matched_property_name == "인증 조건"
    assert existing.properties[0].value == "인증 문장을 아는 자만 입장하며 방문 기록을 남긴다."
    assert source.extracted_value == "인증 문장을 아는 자만 입장한다."


@pytest.mark.parametrize("ordered", [False, True])
def test_salvage_keeps_valid_merged_decision_and_retries_only_invalid_source(ordered):
    sources = [candidate("C1", "서고 출입", "인증 문장을 알아야 입장한다."),
               candidate("C2", "생존 규칙", "한 번 죽으면 끝이다."),
               candidate("C3", "생존 방식", "끝까지 살아남아야 한다."),
               candidate("C4", "생존 목표", "생존이 최우선이다."),
               candidate("C5", "성장 교육", "장로가 아이들을 가르친다.")]
    merged = decision(["C2", "C3", "C4"], "생존 조건", "한 번뿐인 목숨으로 끝까지 생존해야 한다.", ordered=ordered)
    good = decision(["C5"], "성장 교육", sources[4].extracted_value, ordered=ordered)
    invalid = decision(["C1"], "서고 출입", sources[0].extracted_value, operation="EXCLUDE",
                       matched="T1.P99" if ordered else "없는 속성", ordered=ordered)
    error = ComparisonValidationError("initial rejected")
    error.world_batch_response_payload = {"decisions": [invalid, merged, good]}
    error.world_batch_request_payload = {"targets": [{"ref": "T1", "properties": []}]}
    client = Client([{"decisions": [decision(["C1"], "서고 출입", sources[0].extracted_value, ordered=ordered)]}])
    result = asyncio.run(recover_world_batch(
        comparator(client), "WORLD_RULE_HISTORY", sources, [target()], error, ordered_context=ordered,
    ))
    assert not result.failures
    assert [row.source_candidate_refs for row in result.decisions] == [["C1"], ["C2", "C3", "C4"], ["C5"]]
    assert result.decisions[1].proposed_setting_name == "생존 조건"
    assert result.decisions[1].proposed_value == "한 번뿐인 목숨으로 끝까지 생존해야 한다."
    assert len(client.requests) == 1
    assert [row["ref"] for row in json.loads(client.requests[0]["user_prompt"])["candidates"]] == ["C1"]


def execute(sources, responses, *, existing=None, ordered=False):
    client = Client(responses)
    result, raw = asyncio.run(compare_world_batch_with_recovery(
        comparator(client), "WORLD_RULE_HISTORY", sources, [existing or target()], ordered_context=ordered,
    ))
    return result, client.requests


@pytest.mark.parametrize("ordered", [False, True])
def test_shared_boundary_preserves_independent_merge_after_one_failed_retry(ordered):
    sources = [candidate("C1", "규칙", "잘못된 응답의 근거"), candidate("C2", "교육", "자란다"),
               candidate("C3", "양육", "배운다")]
    bad = decision(["C1"], "규칙", "값", operation="EXCLUDE", matched="T1.P99" if ordered else "없음", ordered=ordered)
    good = decision(["C2", "C3"], "성장 교육", "자라면서 배운다", ordered=ordered)
    result, requests = execute(sources, [{"decisions": [bad, good]}, {"decisions": [bad]}], ordered=ordered)
    assert [row.source_candidate_refs for row in result.decisions] == [["C2", "C3"]]
    assert result.decisions[0].proposed_value == "자라면서 배운다"
    assert [row.source_candidate_refs for row in result.failures] == [["C1"]]
    assert result.failures[0].failure_code == "COMPARISON_VALIDATION_FAILED"
    assert len(requests) == 2


def test_whole_malformed_response_retries_whole_once_then_fails_without_salvage():
    sources = [candidate("C1", "규칙", "값1"), candidate("C2", "교육", "값2")]
    result, requests = execute(sources, ["not-json", "not-json"])
    assert result.decisions == []
    assert [row.source_candidate_refs for row in result.failures] == [["C1", "C2"]]
    assert len(requests) == 2
    assert all(len(json.loads(request["user_prompt"])["candidates"]) == 2 for request in requests)


def test_duplicate_source_invalidates_all_overlapping_decisions_and_retries_atomically():
    sources = [candidate("C1", "규칙", "값1"), candidate("C2", "교육", "값2"), candidate("C3", "무기", "값3")]
    first = decision(["C1"], "규칙", "값1")
    overlap = decision(["C1", "C2"], "조건", "값1과 값2")
    independent = decision(["C3"], "무기", "값3")
    joint = decision(["C1", "C2"], "조건", "새로 검증한 값")
    result, requests = execute(sources, [{"decisions": [first, overlap, independent]}, {"decisions": [joint]}])
    assert [row.source_candidate_refs for row in result.decisions] == [["C1", "C2"], ["C3"]]
    assert result.decisions[0].proposed_value == "새로 검증한 값"
    assert [row["ref"] for row in json.loads(requests[1]["user_prompt"])["candidates"]] == ["C1", "C2"]
    assert not result.failures


def test_schema_invalid_write_retries_dependent_valid_writer_as_one_group():
    sources = [candidate("C1", "조건", "값1"), candidate("C2", "교육", "값2")]
    invalid = {**decision(["C1"], "조건", "값1"), "consolidation_status": "invalid"}
    dependent = decision(["C2"], "조건", "값2")
    result, requests = execute(sources, [{"decisions": [invalid, dependent]},
                                      {"decisions": [decision(["C1", "C2"], "조건", "두 근거를 검증한 값")]}])
    assert not result.failures
    assert result.decisions[0].source_candidate_refs == ["C1", "C2"]
    assert len(json.loads(requests[1]["user_prompt"])["candidates"]) == 2


@pytest.mark.parametrize("ordered", [False, True])
def test_missing_sole_target_write_retries_connected_writer_atomically(ordered):
    sources = [candidate("C1", "조건", "값1"), candidate("C2", "교육", "값2")]
    missing_target = {**decision(["C1"], "조건", "값1", ordered=ordered), "target_ref": None}
    sibling = decision(["C2"], "조건", "값2", ordered=ordered)
    result, requests = execute(sources, [{"decisions": [missing_target, sibling]},
        {"decisions": [decision(["C1", "C2"], "조건", "두 근거를 검증한 값", ordered=ordered)]},
    ], ordered=ordered)

    assert not result.failures
    assert [row.source_candidate_refs for row in result.decisions] == [["C1", "C2"]]
    assert result.decisions[0].target_ref == "T1"
    assert [row["ref"] for row in json.loads(requests[1]["user_prompt"])["candidates"]] == ["C1", "C2"]
    assert missing_target["target_ref"] is None
    assert sibling["target_ref"] == "T1"


@pytest.mark.parametrize("ordered", [False, True])
def test_dependency_target_inference_does_not_authorize_missing_target_response(ordered):
    sources = [candidate("C1", "조건", "값1"), candidate("C2", "교육", "값2")]
    missing_target = {**decision(["C1"], "조건", "값1", ordered=ordered), "target_ref": None}
    result, requests = execute(sources, [{"decisions": [missing_target,
        decision(["C2"], "교육", "값2", ordered=ordered)]}, {"decisions": [missing_target]}], ordered=ordered)

    assert [row.source_candidate_refs for row in result.decisions] == [["C2"]]
    assert result.decisions[0].target_ref == "T1"
    assert [row.source_candidate_refs for row in result.failures] == [["C1"]]
    assert len(requests) == 2
    assert missing_target["target_ref"] is None


@pytest.mark.parametrize("multiple_targets", [False, True])
def test_rejected_write_does_not_guess_target_from_unknown_or_multiple_targets(multiple_targets):
    sources = [candidate("C1", "조건", "값1"), candidate("C2", "교육", "값2")]
    bad = {**decision(["C1"], "조건", "값1"), "target_ref": None if multiple_targets else "T99"}
    if multiple_targets:
        bad["consolidation_status"] = "invalid"
    retry = {**decision(["C1"], "조건" if multiple_targets else "별도 조건", "값1"),
             "target_ref": "T2" if multiple_targets else "T1"}
    client = Client([{"decisions": [bad, decision(["C2"], "조건", "값2")]}, {"decisions": [retry]}])
    result, _ = asyncio.run(compare_world_batch_with_recovery(
        comparator(client), "WORLD_RULE_HISTORY", sources, [target(), target()] if multiple_targets else [target()],
    ))

    assert not result.failures
    assert [row.source_candidate_refs for row in result.decisions] == [["C1"], ["C2"]]
    assert [row["ref"] for row in json.loads(client.requests[1]["user_prompt"])["candidates"]] == ["C1"]
    assert bad["target_ref"] == (None if multiple_targets else "T99")


def test_generated_scope_siblings_are_retried_together_and_retained():
    sources = [candidate("C1", "규칙", "값1"), candidate("C2", "교육", "값2"), candidate("C3", "무기", "값3")]
    first = decision(["C1"], "규칙", "값1", scope="문화")
    invalid = {**decision(["C2"], "교육", "값2", scope="문화"), "consolidation_status": "invalid"}
    independent = decision(["C3"], "무기", "값3")
    result, requests = execute(sources, [{"decisions": [first, invalid, independent]},
                                      {"decisions": [first, decision(["C2"], "교육", "값2", scope="문화")]}])
    assert not result.failures
    assert [row.proposed_scope_name for row in result.decisions] == ["문화", "문화", None]
    assert [row["ref"] for row in json.loads(requests[1]["user_prompt"])["candidates"]] == ["C1", "C2"]


def test_failed_retry_new_path_dependency_fails_preserved_sibling_without_another_call():
    sources = [candidate("C1", "규칙", "값1"), candidate("C2", "교육", "값2"), candidate("C3", "무기", "값3")]
    bad = decision(["C1"], "규칙", "값1", operation="EXCLUDE", matched="없음")
    retained = decision(["C2"], "교육", "값2")
    unaffected = decision(["C3"], "무기", "값3")
    new_conflict = {**decision(["C1"], "교육", "값1"), "consolidation_status": "invalid"}
    result, requests = execute(sources, [{"decisions": [bad, retained, unaffected]}, {"decisions": [new_conflict]}])
    assert [row.source_candidate_refs for row in result.decisions] == [["C3"]]
    assert {ref for row in result.failures for ref in row.source_candidate_refs} == {"C1", "C2"}
    assert len(requests) == 2


def test_standard_explicit_scope_review_accepts_different_property_name_and_preserves_source():
    source = candidate("C1", "서고 출입", "적용 맥락이 불분명하다")
    existing = target([{"scope_name": "의례", "setting_name": "인증 조건", "value": "부족의 절차"}])
    row = decision(["C1"], source.setting_name, source.extracted_value, operation="REVIEW_REQUIRED",
                   scope="의례", matched="인증 조건")
    row.update(review_reason="SCOPE_UNRESOLVED", proposed_scope_name=None)
    result, _ = execute([source], [{"decisions": [row]}], existing=existing)
    assert result.decisions[0].review_reason == "SCOPE_UNRESOLVED"
    assert result.decisions[0].proposed_setting_name == "서고 출입"
    assert result.decisions[0].proposed_value == source.extracted_value


def test_all_initial_independent_path_conflicts_receive_one_retry_each():
    sources = [candidate(f"C{i}", f"원본{i}", f"값{i}") for i in range(1, 5)]
    initial = [decision(["C1"], "통합A", "값1"), decision(["C2"], "통합A", "값2"),
               decision(["C3"], "통합B", "값3"), decision(["C4"], "통합B", "값4")]
    result, requests = execute(sources, [{"decisions": initial},
        {"decisions": [decision(["C1", "C2"], "통합A", "검증A")]},
        {"decisions": [decision(["C3", "C4"], "통합B", "검증B")]},
    ])
    assert not result.failures
    assert [row.source_candidate_refs for row in result.decisions] == [["C1", "C2"], ["C3", "C4"]]
    assert len(requests) == 3
