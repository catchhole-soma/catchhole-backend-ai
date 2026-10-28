"""Rejected request diagnostics preserve control flow without logging private content."""

import traceback

import httpx
import pytest

from app.clients.exceptions import SpringWorkerHttpError, WorkerLeaseExpiredError
from app.clients.spring_worker_client import _raise_for_spring_status
from app.exceptions.failure_classification import comparison_failure_code, is_candidate_comparison_failure


def rejected(code="REQUEST_VALIDATION_FAILED", *, status=400, fields=None, reason="PRIVATE_REASON"):
    request = httpx.Request("POST", "http://internal-private.example/complete?secret=PRIVATE_QUERY",
                           headers={"X-Internal-Api-Key": "PRIVATE_KEY"}, content="PRIVATE_REQUEST")
    return httpx.Response(status, request=request, json={
        "message": "PRIVATE_MANUSCRIPT", "error": {
            "code": code, "status": status, "context": {"reasonCode": reason},
            "details": fields or [
                {"field": "decisions[2].proposedFactValue", "message": "PRIVATE_REJECTED_VALUE"},
                {"field": "decisions[2].proposedFactValue", "message": "duplicate private detail"},
                {"field": "decisions[2].proposedValueJson.PRIVATE_FIELD", "message": "PRIVATE_SECRET"},
                {"field": "PRIVATE_FIELD", "message": "PRIVATE_SECRET"},
            ],
        },
    })


@pytest.mark.parametrize("code", [
    "REQUEST_VALIDATION_FAILED", "REQUEST_INVALID_ARGUMENT",
    "SETTING_CANDIDATE_COMPARISON_TARGET_INVALID",
    "SETTING_CANDIDATE_COMPARISON_BATCH_RESPONSE_INVALID", "SETTING_CANDIDATE_VALUE_JSON_INVALID",
])
def test_character_complete_400_keeps_safe_code_and_remains_job_blocking(code):
    response = rejected(code)
    with pytest.raises(SpringWorkerHttpError) as caught:
        _raise_for_spring_status(response)
    error = caught.value
    assert error.status_code == 400 and error.spring_error_code == code
    assert error.validation_fields == ("decisions[].proposedFactValue",)
    assert error.spring_reason_code is None
    assert code in str(error) and '"status":400' in str(error)
    assert comparison_failure_code(error).value == "UNEXPECTED_ERROR"
    assert not is_candidate_comparison_failure(error)
    rendered = str(error) + "".join(traceback.format_exception(error))
    assert "PRIVATE_" not in rendered and "internal-private.example" not in rendered
    assert "http://" not in rendered and "https://" not in rendered
    # Transport objects remain available in memory; logs and persisted fail
    # strings use only the safe exception message, not request/response bodies.
    assert error.response is response and error.request is response.request


def test_existing_lease_and_world_reason_classification_are_preserved():
    with pytest.raises(WorkerLeaseExpiredError) as caught:
        _raise_for_spring_status(rejected("ANALYSIS_JOB_LEASE_CONFLICT", status=409))
    assert comparison_failure_code(caught.value).value == "WORKER_LEASE_EXPIRED"
    with pytest.raises(SpringWorkerHttpError) as caught:
        _raise_for_spring_status(rejected("WORLD_SETTING_COMPARISON_TARGET_INVALID",
                                        reason="PROPOSED_PATH_MISMATCH"))
    assert caught.value.spring_reason_code == "PROPOSED_PATH_MISMATCH"
    assert "PROPOSED_PATH_MISMATCH" in str(caught.value)
    assert comparison_failure_code(caught.value).value == "COMPARISON_VALIDATION_FAILED"
    assert not is_candidate_comparison_failure(caught.value)


def test_unrecognized_codes_and_malformed_body_never_enter_messages():
    responses = [rejected("PRIVATE_UPPERCASE_CODE"),
                 httpx.Response(500, request=httpx.Request("POST", "http://internal-private.example"),
                                text="PRIVATE_NON_JSON_RESPONSE")]
    for response in responses:
        with pytest.raises(SpringWorkerHttpError) as caught:
            _raise_for_spring_status(response)
        assert "PRIVATE_" not in str(caught.value)
        assert comparison_failure_code(caught.value).value == "UNEXPECTED_ERROR"


def test_fields_are_bounded_and_constraints_are_not_inferred_from_messages():
    response = rejected(fields=[
        {"field": "decisions[19].dependencyCandidateRefs[2]", "message": "must be UUID PRIVATE"},
        {"field": "decisions[99].operation", "message": "PRIVATE"},
        {"field": "failures[0].failureCode", "constraint": "PRIVATE_CONSTRAINT", "message": "PRIVATE"},
        {"field": "rawComparisonJson.PRIVATE_KEY", "message": "PRIVATE"},
    ])
    with pytest.raises(SpringWorkerHttpError) as caught:
        _raise_for_spring_status(response)
    assert caught.value.validation_fields == (
        "decisions[].dependencyCandidateRefs", "failures[].failureCode",
    )
    assert "constraint" not in str(caught.value)
    assert "PRIVATE" not in str(caught.value)


def test_failure_diagnostics_contract_fixture_and_untrusted_fields():
    import json
    from pathlib import Path
    from app.schemas.worker import WorkerFailureDiagnostics
    fixture = json.loads((Path(__file__).parent / "fixtures/analysis-failure-diagnostics-v1.json").read_text())
    result = WorkerFailureDiagnostics.model_validate(fixture)
    assert result.model_dump(mode="json", by_alias=True) == fixture
    malicious = {**fixture, "sourceErrorCode": "PRIVATE_CODE", "requestId": "@everyone https://private.example",
                 "traceId": "x" * 1000, "errorMessage": "PRIVATE_MANUSCRIPT", "leaseToken": "PRIVATE_TOKEN"}
    result = WorkerFailureDiagnostics.model_validate(malicious).model_dump(mode="json", by_alias=True)
    assert result["sourceErrorCode"] == "UNKNOWN"
    assert result["requestId"] is None and result["traceId"] is None
    assert "PRIVATE" not in json.dumps(result)


def test_spring_specific_diagnostics_survive_generic_wrapper_and_invalid_optional_metadata():
    from app.exceptions.failure_classification import failure_diagnostics
    response = rejected("ANALYSIS_CURRENT_EVIDENCE_INVALID", status=409)
    payload = response.json()
    payload["error"]["context"] = {"failureDiagnostics": {
        "schemaVersion": 1, "origin": "SPRING", "stage": "CHARACTER_CONTEXT",
        "reasonCode": "CURRENT_EVIDENCE_OFFSET_INVALID", "sourceErrorCode": "ANALYSIS_CURRENT_EVIDENCE_INVALID",
    }}
    response = httpx.Response(409, request=response.request, json=payload,
                             headers={"X-Request-ID": "11111111-1111-4111-8111-111111111111"})
    with pytest.raises(SpringWorkerHttpError) as caught:
        _raise_for_spring_status(response)
    wrapper = RuntimeError("generic")
    wrapper.__cause__ = caught.value
    diagnostic = failure_diagnostics(wrapper, stage="FINALIZE")
    assert diagnostic.reason_code == "CURRENT_EVIDENCE_OFFSET_INVALID"
    assert diagnostic.stage == "CHARACTER_CONTEXT" and diagnostic.http_status == 409
    assert diagnostic.request_id == "11111111-1111-4111-8111-111111111111"
    payload["error"]["context"]["failureDiagnostics"] = ["invalid"]
    with pytest.raises(SpringWorkerHttpError) as malformed:
        _raise_for_spring_status(httpx.Response(500, request=response.request, json=payload))
    assert failure_diagnostics(malformed.value, stage="WORLD_CONTEXT").reason_code == "INTERNAL_API_HTTP_ERROR"


@pytest.mark.parametrize("optional", [None, [], "PRIVATE", {"schemaVersion": 2}, {"schemaVersion": True}])
def test_malformed_or_legacy_spring_diagnostics_do_not_replace_http_failure(optional):
    from app.exceptions.failure_classification import failure_diagnostics
    request = httpx.Request("POST", "https://internal.example/character-fact-comparison-context")
    response = httpx.Response(500, request=request, json={"error": {"code": "COMMON_INTERNAL_SERVER_ERROR",
        "context": {"failureDiagnostics": optional}}}, headers={"X-Request-ID": "11111111-1111-4111-8111-111111111111"})
    with pytest.raises(SpringWorkerHttpError) as caught:
        _raise_for_spring_status(response)
    diagnostic = failure_diagnostics(caught.value, stage="FINALIZE")
    assert diagnostic.origin == "SPRING" and diagnostic.stage == "CHARACTER_CONTEXT"
    assert diagnostic.http_status == 500 and diagnostic.reason_code == "INTERNAL_API_HTTP_ERROR"
    assert diagnostic.request_id == "11111111-1111-4111-8111-111111111111"
    assert comparison_failure_code(caught.value).value == "UNEXPECTED_ERROR"
