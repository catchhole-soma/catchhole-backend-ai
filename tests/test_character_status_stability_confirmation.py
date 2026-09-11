import asyncio
import json
from copy import deepcopy

import pytest

from evals.character_status_stability.runner import confirm_with_recomparison

NOT_READY = "SETTING_CANDIDATE_COMPARISON_NOT_READY"


def confirmation(error=None, *, complete=False):
    return {
        "confirmed": [{"groupKey": "already confirmed"}] if error else [],
        "unresolved": [{"error": {"code": error, "statusCode": 409}}] if error else [],
        "pendingGroups": [], "complete": complete,
    }


class Api:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    async def confirm_pending_groups(self, work_id, batch_id):
        self.calls += 1
        return deepcopy(self.responses[min(self.calls - 1, len(self.responses) - 1)])

    async def get_candidate_groups(self, work_id, batch_id, **kwargs):
        assert kwargs == {"review_status": "PENDING_REVIEW"}
        return [{"candidates": [{"id": "candidate-1", "candidateKind": "SETTING"}]}]


def execute(tmp_path, api, job):
    return asyncio.run(confirm_with_recomparison(
        api=api, work_id="expected-work", batch_id="batch", settings=object(),
        episode_dir=tmp_path, run_comparison_job=job,
    ))


def test_not_ready_runs_real_job_callback_then_refetches_and_confirms(tmp_path):
    api = Api([confirmation(NOT_READY), confirmation(complete=True)])
    calls = []

    async def job(**kwargs):
        calls.append(kwargs)
        assert kwargs["expected_work_id"] == "expected-work"
        assert kwargs["expected_candidate_ids"] == {"candidate-1"}
        if len(calls) == 1:
            return {"claimed": True, "success": True, "analysisJobId": "job-1"}
        return {"claimed": False, "failureCode": "NO_CLAIMABLE_JOB"}

    result = execute(tmp_path, api, job)
    assert result["complete"] and api.calls == 2 and len(calls) == 2
    assert len(result["confirmed"]) == 1
    assert result["unresolved"] == []
    assert result["confirmationAttempts"][0]["jobs"][0]["analysisJobId"] == "job-1"
    first = json.loads((tmp_path / "recomparison/attempt-01/confirmation.json").read_text())
    assert first["unresolved"][0]["error"]["code"] == NOT_READY
    assert (tmp_path / "recomparison/attempt-01/job-01/result.json").exists()


@pytest.mark.parametrize("error", [None, "SETTING_CANDIDATE_COMPARISON_OPERATION_INVALID"])
def test_success_unknown_identity_and_other_errors_never_retry(tmp_path, error):
    api = Api([confirmation(error)])

    async def job(**kwargs):
        pytest.fail("No explicit retryable response")

    result = execute(tmp_path, api, job)
    assert api.calls == 1
    assert result["complete"] is False


def test_failed_worker_preserves_diagnostics_without_confirming_again(tmp_path):
    api = Api([confirmation(NOT_READY)])

    async def job(**kwargs):
        return {"claimed": True, "success": False, "failureCode": "COMPARISON_VALIDATION_FAILED"}

    with pytest.raises(RuntimeError, match="Recomparison failed"):
        execute(tmp_path, api, job)
    assert api.calls == 1
    result = json.loads((tmp_path / "confirmation.json").read_text())
    assert result["confirmationAttempts"][0]["jobs"][0]["success"] is False


def test_repeated_not_ready_is_bounded_and_not_misreported_complete(tmp_path):
    api = Api([confirmation(NOT_READY)])

    async def job(**kwargs):
        return {"claimed": False, "failureCode": "NO_CLAIMABLE_JOB"}

    result = execute(tmp_path, api, job)
    assert api.calls == 3 and len(result["confirmationAttempts"]) == 3
    assert not result["complete"]
    assert result["unresolved"][0]["error"]["code"] == NOT_READY


def test_queue_is_bounded_by_candidate_count(tmp_path):
    api = Api([confirmation(NOT_READY)])

    async def job(**kwargs):
        return {"claimed": True, "success": True}

    with pytest.raises(RuntimeError, match="exceeded"):
        execute(tmp_path, api, job)
    assert api.calls == 1


def test_claim_transport_failure_is_not_treated_as_empty_queue(tmp_path):
    api = Api([confirmation(NOT_READY)])

    async def job(**kwargs):
        return {"claimed": False, "failureCode": "INTERNAL_API_NETWORK_ERROR"}

    with pytest.raises(RuntimeError, match="claim failed"):
        execute(tmp_path, api, job)
    assert api.calls == 1


@pytest.mark.parametrize("status_code", [None, 400, 500])
def test_only_http_409_not_ready_can_retry(tmp_path, status_code):
    response = confirmation(NOT_READY)
    response["unresolved"][0]["error"]["statusCode"] = status_code
    api = Api([response])

    async def job(**kwargs):
        pytest.fail("NOT_READY with wrong HTTP status must not retry")

    result = execute(tmp_path, api, job)
    assert api.calls == 1 and not result["complete"]


@pytest.mark.parametrize("diagnostic", [
    {"comparisonStatus": "FAILED", "reason": "COMPARISON_NOT_COMPLETED"},
    {"comparisonStatus": "COMPLETED", "reason": "CANDIDATE_VALUE_INVALID"},
])
def test_not_ready_mixed_with_failure_or_invalid_value_does_not_retry(tmp_path, diagnostic):
    response = confirmation(NOT_READY)
    response["unresolved"].append({"groupKey": "failed", "diagnostics": [diagnostic]})
    api = Api([response])

    async def job(**kwargs):
        pytest.fail("Retain failure instead of starting another paid comparison")

    result = execute(tmp_path, api, job)
    assert api.calls == 1 and not result["complete"]
    assert result["unresolved"][1]["diagnostics"] == [diagnostic]
