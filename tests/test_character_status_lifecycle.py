import asyncio
import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchDecision
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
)
from app.analysis.character_status_lifecycle import reconcile_status_lifecycle
from app.analysis.exceptions import ComparisonValidationError
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerCharacterFactComparisonBatchCandidate
from app.usage.metering import MeteredTextGenerationClient


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LlmTextResponse(text=json.dumps(response, ensure_ascii=False),
                               input_token_count=20, output_token_count=10)


def _candidate(index, key, *, active=True, quote="리안에게 변화가 관찰됐다."):
    return WorkerCharacterFactComparisonBatchCandidate(
        candidate_ref=f"C{index}", projected_snapshot_ref=f"Q{index}", source_episode_no=5,
        raw_fact_key=key, initial_canonical_fact_key=key, canonical_key_resolution="PATTERN",
        attribute_value="상태 변화", value_type="JSON", value_json={"name": key[7:], "active": active},
        evidence_spans=[{"quote": quote, "startOffset": index * 100,
                         "endOffset": index * 100 + len(quote)}],
    )


def _decision(candidate, operation, *, target=None, removed=(), scope="PRESENT"):
    mutating = operation in {"ADD", "UPDATE", "MERGE"}
    return CharacterFactComparisonBatchDecision(
        candidate_ref=candidate.candidate_ref,
        resolved_canonical_fact_key=candidate.initial_canonical_fact_key,
        operation=operation, target_ref=target, removed_snapshot_refs=list(removed),
        proposed_fact_value="상태가 지속됨" if mutating else None,
        proposed_value_json=deepcopy(candidate.value_json) if mutating else None,
        temporal_scope=scope, comparison_reason="기존 관찰을 반영합니다.",
    )


def _entry(ref, key, *, fact_type="STATUS"):
    return CharacterProjectionEntry(
        reference=ref, fact_type=fact_type, fact_key=key, fact_value="기존 상태",
        value_json={"name": key[7:], "active": True},
    )


def _fixture():
    initial = [_entry("P1", "status.출혈"), _entry("P2", "status.발_부상"),
               _entry("P3", "status.저주"), _entry("P4", "profile.직업", fact_type="PROFILE")]
    candidates = [
        _candidate(1, "status.급성_위기", quote="출혈 때문에 생명이 위태로웠다."),
        _candidate(2, "status.출혈", quote="상처에서 계속 피가 흘렀다."),
        _candidate(3, "status.발_부상", active=False,
                   quote="상처가 닫히고 피가 멎었다. 위기를 넘긴 그는 발을 딛고 달렸다."),
    ]
    decisions = [_decision(candidates[0], "ADD"),
                 _decision(candidates[1], "UPDATE", target="P1"),
                 _decision(candidates[2], "REMOVE", removed=("P2",))]
    return candidates, initial, decisions


def _check(ref, verdict="KEEP", carrier=None, reason="독립적인 상태는 그대로 유지합니다."):
    return {"snapshot_ref": ref, "verdict": verdict,
            "carrier_candidate_ref": carrier, "reason": reason}


def _response(*checks):
    return {"checks": list(checks)}


def _end_response():
    return _response(_check("P3"),
                     _check("Q1", "END", "C3", "위기를 넘겼다는 후속 관찰로 급성 위기를 끝냅니다."),
                     _check("Q2", "END", "C3", "상처가 닫히고 피가 멎어 출혈을 끝냅니다."))


def _run(client, fixture=None, **kwargs):
    candidates, initial, decisions = fixture or _fixture()
    return asyncio.run(reconcile_status_lifecycle(
        llm_client=client, model="gpt-5.6-luna", max_output_tokens=3000,
        max_attempts=kwargs.pop("max_attempts", 1),
        max_input_tokens=kwargs.pop("max_input_tokens", 64000),
        matched_character_name="리안", candidates=candidates, initial_entries=initial,
        decisions=decisions, **kwargs,
    ))


def test_ends_p_and_q_on_existing_carrier_and_replays_dependencies_without_mutation():
    fixture = _fixture()
    candidates, initial, decisions = fixture
    original_candidates = [c.model_dump() for c in candidates]
    original_decisions = [d.model_dump() for d in decisions]
    original_entries = deepcopy(initial)
    client = Client(_end_response())
    result = _run(client, fixture)

    assert result[:2] == decisions[:2]
    assert result[2].removed_snapshot_refs == ["P2", "Q1", "Q2"]
    assert result[2].comparison_reason.startswith(decisions[2].comparison_reason)
    assert "급성 위기" in result[2].comparison_reason
    state = CharacterProjectionState(initial)
    for candidate, decision in zip(candidates, result, strict=True):
        applied = state.apply(candidate_ref=candidate.candidate_ref,
                              projected_snapshot_ref=candidate.projected_snapshot_ref,
                              fact_type="STATUS", resolved_fact_key=decision.resolved_canonical_fact_key,
                              value_type=candidate.value_type,
                              candidate_value_json=candidate.value_json, decision=decision)
    assert set(state.entries_by_ref) == {"P3", "P4"}
    assert applied.dependency_candidate_refs == ("C1", "C2")
    assert [c.model_dump() for c in candidates] == original_candidates
    assert [d.model_dump() for d in decisions] == original_decisions
    assert initial == original_entries
    payload = json.loads(client.requests[0]["user_prompt"])
    assert {r["snapshot_ref"] for r in payload["remaining_statuses"]} == {"P3", "Q1", "Q2"}
    q2 = next(r for r in payload["remaining_statuses"] if r["snapshot_ref"] == "Q2")
    assert q2["eligible_carrier_candidate_refs"] == ["C3"]
    q1 = next(r for r in payload["remaining_statuses"] if r["snapshot_ref"] == "Q1")
    assert q1["eligible_carrier_candidate_refs"] == ["C3"]
    for original, row in zip(candidates, payload["candidates"], strict=True):
        assert row["value_json"] == original.value_json
        assert row["fact_key"] == original.initial_canonical_fact_key
        assert row["temporal_scope"] == "PRESENT"
        assert "decision" not in row
        assert "comparison_reason" not in row
        assert "proposed_fact_value" not in row
        assert "proposed_value_json" not in row
    assert "기존 관찰을 반영합니다." not in client.requests[0]["user_prompt"]
    assert client.requests[0]["prompt_cache_key"] == "character-status-lifecycle:v2"


def test_keep_and_unknown_leave_the_original_decision_objects_unchanged():
    fixture = _fixture()
    result = _run(Client(_response(_check("P3"), _check("Q1", "UNKNOWN"), _check("Q2"))), fixture)
    assert result is fixture[2]


@pytest.mark.parametrize("operation", ["HISTORY_ONLY", "EXCLUDE"])
def test_present_nonprojecting_observation_can_carry_an_existing_status_end(operation):
    candidate = _candidate(1, "status.회복", active=False)
    decision = _decision(candidate, operation)
    result = _run(Client(_response(_check("P1", "END", "C1", "실제 회복 관찰로 상태를 끝냅니다."))),
                  ([candidate], [_entry("P1", "status.발_부상")], [decision]))
    assert result[0].operation == "REMOVE"
    assert result[0].target_ref is None
    assert result[0].proposed_value_json is None
    assert result[0].proposed_fact_value is None
    assert result[0].removed_snapshot_refs == ["P1"]
    assert decision.operation == operation


@pytest.mark.parametrize("operation", ["ADD", "UPDATE", "MERGE"])
def test_active_intermediate_observation_cannot_carry_additional_status_end(operation):
    candidate = _candidate(1, "status.정화_효과")
    initial = [_entry("P1", "status.발열")]
    if operation != "ADD":
        initial.append(_entry("P2", "status.정화_효과"))
    decision = _decision(candidate, operation, target=None if operation == "ADD" else "P2")
    before = decision.model_dump()
    client = Client(_response(_check("P1", "END", "C1", "정화 뒤 열이 내려 발열을 끝냅니다."),
                              _check("Q1")))
    with pytest.raises(ComparisonValidationError):
        _run(client, ([candidate], initial, [decision]))
    assert decision.model_dump() == before
    payload = json.loads(client.requests[0]["user_prompt"])
    assert all(row["eligible_carrier_candidate_refs"] == []
               for row in payload["remaining_statuses"])


@pytest.mark.parametrize("checks", [
    [_check("P3"), _check("Q1")],
    [_check("P3"), _check("Q1"), _check("Q1")],
    [_check("P3"), _check("Q1"), _check("Q2"), _check("P2", "END", "C3")],
    [_check("P3"), _check("Q1"), _check("P4", "END", "C3")],
    [_check("P3"), _check("Q1"), _check("Q99")],
])
def test_requires_exact_remaining_status_coverage(checks):
    with pytest.raises(ComparisonValidationError):
        _run(Client(_response(*checks)))


@pytest.mark.parametrize("carrier", [None, "C99", "C1", "C2"])
def test_rejects_missing_foreign_future_and_own_q_carriers(carrier):
    with pytest.raises(ComparisonValidationError):
        _run(Client(_response(_check("P3"), _check("Q1"), _check("Q2", "END", carrier))))


def test_active_intermediate_cannot_borrow_later_recovery_evidence_for_an_earlier_q():
    response = _response(_check("P3"),
                         _check("Q1", "END", "C2", "나중에 달릴 수 있게 되어 위기를 끝냅니다."),
                         _check("Q2", "UNKNOWN"))
    with pytest.raises(ComparisonValidationError):
        _run(Client(response))


@pytest.mark.parametrize("scope, operation", [
    ("PAST", "HISTORY_ONLY"), ("HYPOTHETICAL", "HISTORY_ONLY"),
    ("UNKNOWN", "REVIEW_REQUIRED"), ("PRESENT", "REVIEW_REQUIRED"),
])
def test_noncurrent_or_review_required_carrier_cannot_end_snapshot(scope, operation):
    candidate = _candidate(1, "status.회복", active=False)
    decision = _decision(candidate, operation, scope=scope)
    with pytest.raises(ComparisonValidationError):
        _run(Client(_response(_check("P1", "END", "C1"))),
             ([candidate], [_entry("P1", "status.부상")], [decision]))


def test_keep_must_not_specify_a_carrier():
    with pytest.raises(ComparisonValidationError):
        _run(Client(_response(_check("P3", carrier="C3"), _check("Q1"), _check("Q2"))))


def test_carrier_without_its_own_evidence_is_rejected():
    candidates, initial, decisions = _fixture()
    candidates[2] = candidates[2].model_copy(update={"evidence_spans": []})
    with pytest.raises(ComparisonValidationError):
        _run(Client(_end_response()), (candidates, initial, decisions))


@pytest.mark.parametrize("reason", [
    "Q1을 종료합니다.", "C3에서 회복됐습니다.", "status.출혈을 끝냅니다.",
    "REMOVE로 끝납니다.", "UNKNOWN으로 유지합니다.",
    "12345678-1234-1234-1234-123456789012 상태를 끝냅니다.", "Ended.", " 이유 앞 공백",
])
def test_internal_reason_values_are_rejected_with_safe_retry_feedback(reason, caplog):
    bad = _end_response()
    bad["checks"][1]["reason"] = reason
    client = Client(bad, _end_response())
    result = _run(client, max_attempts=2)
    assert result[2].removed_snapshot_refs == ["P2", "Q1", "Q2"]
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason_code"] == "STATUS_LIFECYCLE_REASON_INVALID"
    assert reason not in caplog.text


def test_error_does_not_fall_back_to_unreviewed_decisions():
    client = Client({"checks": []}, {"checks": []})
    with pytest.raises(ComparisonValidationError):
        _run(client, max_attempts=2)
    assert len(client.requests) == 2


def test_quota_and_truncation_propagate_without_validation_retries():
    for error in (AiTokenQuotaExhaustedError(), LlmOutputTruncatedError(
        "truncated", incomplete_reason="max_output_tokens", max_output_tokens=3000,
        input_token_count=50, output_token_count=3000,
    )):
        client = Client(error)
        with pytest.raises(type(error)):
            _run(client, max_attempts=2)
        assert len(client.requests) == 1


def test_no_remaining_status_skips_provider():
    candidate = _candidate(1, "status.부상", active=False)
    decision = _decision(candidate, "REMOVE", removed=("P1",))
    client = Client()
    result = _run(client, ([candidate], [_entry("P1", "status.부상")], [decision]))
    assert result == [decision]
    assert client.requests == []


def test_input_limit_fails_before_provider_instead_of_truncating_coverage():
    client = Client()
    with pytest.raises(ComparisonValidationError, match="INPUT_LIMIT_EXCEEDED"):
        _run(client, max_input_tokens=1)
    assert client.requests == []


def test_invalid_initial_decision_coverage_fails_before_provider():
    candidates, initial, decisions = _fixture()
    client = Client()
    with pytest.raises(ComparisonValidationError, match="INITIAL_PROJECTION_INVALID"):
        _run(client, (candidates, initial, decisions[:-1]))
    assert client.requests == []


def test_new_recurrence_after_recovery_cannot_be_ended_by_earlier_observation():
    initial = [_entry("P1", "status.발열")]
    candidates = [_candidate(1, "status.발열", active=False), _candidate(2, "status.발열")]
    decisions = [_decision(candidates[0], "REMOVE", removed=("P1",)),
                 _decision(candidates[1], "ADD")]
    client = Client(_response(_check("Q2", "END", "C1")))
    with pytest.raises(ComparisonValidationError):
        _run(client, (candidates, initial, decisions))
    payload = json.loads(client.requests[0]["user_prompt"])
    assert payload["remaining_statuses"][0]["eligible_carrier_candidate_refs"] == []


def test_unknown_extra_schema_fields_are_not_silently_accepted():
    response = _end_response()
    response["checks"][1]["invented_quote"] = "PRIVATE_REJECTED_OUTPUT"
    with pytest.raises(ComparisonValidationError):
        _run(Client(response))


def test_validation_retries_reuse_metered_client_and_settle_each_real_response():
    class Ledger:
        def __init__(self):
            self.reservations = []
            self.settlements = []

        async def reserve_ai_tokens(self, **kwargs):
            self.reservations.append(kwargs)

        async def settle_ai_tokens(self, **kwargs):
            self.settlements.append(kwargs)

        async def release_ai_tokens(self, **kwargs):
            raise AssertionError("Both provider responses must settle real usage.")

    delegate = Client({"checks": []}, _end_response())
    ledger = Ledger()
    metered = MeteredTextGenerationClient(
        delegate=delegate, ledger=ledger, analysis_job_id=UUID(int=1),
        purpose="CHARACTER_FACT_COMPARISON", default_model="gpt-5.6-luna",
        lease_token=UUID(int=2),
    )
    result = _run(metered, max_attempts=2)
    assert result[2].removed_snapshot_refs == ["P2", "Q1", "Q2"]
    assert len(ledger.reservations) == len(ledger.settlements) == 2
    assert {request["model"] for request in delegate.requests} == {"gpt-5.6-luna"}
    usage = metered.usage_snapshot()
    assert usage.provider_request_count == 2
    assert usage.input_token_count == 40
    assert usage.output_token_count == 20
