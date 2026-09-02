import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest

from app.clients.exceptions import (
    AiTokenQuotaExhaustedError,
    SpringWorkerHttpError,
    SpringWorkerTransportError,
    WorkerLeaseExpiredError,
)
from app.clients.spring_worker_client import (
    INTERNAL_API_KEY_HEADER,
    WORKER_LEASE_TOKEN_HEADER,
    SpringWorkerClient,
)
from app.domain.enums import AnalysisFailureCode, EpisodeProcessingStatus
from app.schemas.worker import (
    WorkerAnalysisActiveCharacterStatusPayload,
    WorkerAnalysisJobPayload,
    WorkerCharacterFactComparisonCompleteRequest,
    WorkerRemovedSnapshotEntry,
    WorkerWorldSettingComparisonBatchCompleteRequest,
    WorkerWorldSettingComparisonBatchDecision,
    WorkerWorldSettingContextVersion,
    WorkerWorldSettingSubjectResolutionRequest,
    WorkerWorldSettingSubjectResolutionRequestItem,
)

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000005")


# claim 성공 시 payload를 파싱하고 요청 헤더/URL/Body가 올바른지 확인
def test_claim_returns_payload_when_spring_returns_job() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _claim_response(request, requests))

    payload = asyncio.run(
        client.claim(
            allowed_job_types=["SETTING_EXTRACTION"],
            model_name="gpt-4.1-mini",
            current_step="원문 청킹",
        )
    )

    assert payload is not None
    assert payload.analysis_job_id == ANALYSIS_JOB_ID
    assert payload.work_id == WORK_ID
    assert payload.episode.episode_id == EPISODE_ID
    assert payload.character_setting_schemas[0].schema_key == "stats.strength"
    assert payload.character_setting_schemas[0].aliases == ["근력", "힘", "strength"]
    assert payload.character_setting_schemas[1].attribute_pattern == "skill.*"
    assert payload.character_setting_schemas[1].value_type == "JSON"
    assert payload.known_characters[0].name == "비요른 얀델"
    assert [
        (status.fact_key, status.fact_value)
        for status in payload.known_characters[0].active_statuses
    ] == [
        ("status.오른발_부상", "오른발이 크게 다쳐 걷기 어려움"),
        ("status.마비독", None),
    ]
    assert set(payload.character_setting_schemas[0].model_dump()) == {
        "schema_key",
        "display_name",
        "attribute_pattern",
        "aliases",
        "value_type",
    }
    request = requests[0]
    assert request.headers[INTERNAL_API_KEY_HEADER] == "test-api-key"
    assert request.url.path == "/api/internal/v1/analysis-jobs/claim"
    assert json.loads(request.content) == {
        "modelName": "gpt-4.1-mini",
        "currentStep": "원문 청킹",
        "allowedJobTypes": ["SETTING_EXTRACTION"],
    }


# claim할 job이 없어서 Spring이 204를 반환하면 None을 반환하는지 확인
def test_claim_returns_none_when_spring_returns_no_content() -> None:
    client = _client(lambda request: httpx.Response(status_code=204))

    payload = asyncio.run(client.claim(allowed_job_types=["SETTING_EXTRACTION"]))

    assert payload is None


def test_hidden_character_comparison_payload_allows_missing_batch_and_episode() -> None:
    payload = WorkerAnalysisJobPayload.model_validate(
        {
            "analysisJobId": str(ANALYSIS_JOB_ID),
            "jobType": "CHARACTER_FACT_COMPARISON",
            "workId": str(WORK_ID),
            "workTitle": "레거시 작품",
            "batchId": None,
            "leaseToken": str(LEASE_TOKEN),
            "leaseExpiresAt": "2026-08-06T12:05:00",
            "claimAttemptCount": 1,
            "settingCandidateId": "00000000-0000-0000-0000-000000000099",
            "episode": None,
        }
    )

    assert payload.batch_id is None
    assert payload.episode is None


def test_claim_payload_defaults_character_setting_schemas_when_older_spring_omits_field() -> None:
    payload = WorkerAnalysisJobPayload.model_validate(
        {
            "analysisJobId": str(ANALYSIS_JOB_ID),
            "jobType": "SETTING_EXTRACTION",
            "workId": str(WORK_ID),
            "workTitle": "빛나는 검사 로맨스",
            "batchId": str(BATCH_ID),
            "leaseToken": str(LEASE_TOKEN),
            "leaseExpiresAt": "2026-08-06T12:05:00",
            "claimAttemptCount": 1,
            "episode": {
                "episodeId": str(EPISODE_ID),
                "episodeNo": 1,
                "title": "첫 번째 회차",
                "contentS3Key": "works/work-id/episodes/episode-id.txt",
                "contentS3Version": None,
                "contentHash": "hash",
                "charCount": 1234,
            },
        }
    )

    assert payload.character_setting_schemas == []


def test_known_character_defaults_active_statuses_when_older_spring_omits_field() -> None:
    payload = WorkerAnalysisJobPayload.model_validate(
        {
            "analysisJobId": str(ANALYSIS_JOB_ID),
            "jobType": "SETTING_EXTRACTION",
            "workId": str(WORK_ID),
            "workTitle": "빛나는 검사 로맨스",
            "batchId": str(BATCH_ID),
            "leaseToken": str(LEASE_TOKEN),
            "leaseExpiresAt": "2026-08-06T12:05:00",
            "claimAttemptCount": 1,
            "knownCharacters": [
                {
                    "characterId": "00000000-0000-0000-0000-000000000099",
                    "name": "비요른 얀델",
                }
            ],
            "episode": {
                "episodeId": str(EPISODE_ID),
                "episodeNo": 1,
                "title": "첫 번째 회차",
                "contentS3Key": "works/work-id/episodes/episode-id.txt",
                "contentS3Version": None,
                "contentHash": "hash",
                "charCount": 1234,
            },
        }
    )

    assert payload.known_characters[0].active_statuses == []


def test_active_status_requires_fact_key_and_explicit_nullable_fact_value() -> None:
    with pytest.raises(ValueError):
        WorkerAnalysisActiveCharacterStatusPayload.model_validate({"factKey": "status.부상"})

    status = WorkerAnalysisActiveCharacterStatusPayload.model_validate(
        {"factKey": "status.부상", "factValue": None}
    )
    assert status.fact_value is None


# 진행 상태 보고 API를 PATCH로 올바른 URL과 Body로 호출하는지 확인
def test_report_progress_calls_spring_progress_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    asyncio.run(
        client.report_progress(
            analysis_job_id=ANALYSIS_JOB_ID,
            lease_token=LEASE_TOKEN,
            current_step="설정 추출",
            episode_status=EpisodeProcessingStatus.ANALYZING,
        )
    )

    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/progress"
    assert request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN)
    assert json.loads(request.content) == {
        "currentStep": "설정 추출",
        "episodeStatus": "ANALYZING",
    }


# 완료 보고 API를 POST로 올바른 URL과 Body로 호출하는지 확인
def test_complete_calls_spring_complete_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    asyncio.run(
        client.complete(
            analysis_job_id=ANALYSIS_JOB_ID,
            lease_token=LEASE_TOKEN,
            summary_json='{"candidateCount":3}',
            input_token_count=100,
            output_token_count=20,
        )
    )

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/complete"
    assert request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN)
    assert json.loads(request.content) == {
        "summaryJson": '{"candidateCount":3}',
        "inputTokenCount": 100,
        "outputTokenCount": 20,
    }


# 실패 보고 API를 POST로 올바른 URL과 Body로 호출하는지 확인
def test_fail_calls_spring_fail_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    asyncio.run(
        client.fail(
            analysis_job_id=ANALYSIS_JOB_ID,
            lease_token=LEASE_TOKEN,
            error_message="LLM 응답 오류",
        )
    )

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/fail"
    assert request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN)
    assert json.loads(request.content) == {
        "failureCode": "UNEXPECTED_ERROR",
        "errorMessage": "LLM 응답 오류",
    }


def test_ai_token_reserve_settle_and_release_call_internal_apis() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))
    request_id = uuid4()

    other_request_id = uuid4()

    async def call_usage_apis() -> None:
        await client.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=ANALYSIS_JOB_ID,
            purpose="SETTING_EXTRACTION",
            attempt=1,
            model_name="gpt-4.1-mini",
            reserved_tokens=1000,
            lease_token=LEASE_TOKEN,
        )
        await client.settle_ai_tokens(request_id, 100, 10, 20, "SUCCESS")
        await client.release_ai_tokens(other_request_id, "USAGE_UNAVAILABLE")

    asyncio.run(call_usage_apis())

    assert [request.url.path for request in requests] == [
        "/api/internal/v1/ai-token-usages/reserve",
        f"/api/internal/v1/ai-token-usages/{request_id}/settle",
        f"/api/internal/v1/ai-token-usages/{other_request_id}/release",
    ]
    assert json.loads(requests[0].content) == {
        "requestId": str(request_id),
        "analysisJobId": str(ANALYSIS_JOB_ID),
        "purpose": "SETTING_EXTRACTION",
        "attempt": 1,
        "modelName": "gpt-4.1-mini",
        "reservedTokens": 1000,
    }
    assert requests[0].headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN)
    assert json.loads(requests[1].content) == {
        "inputTokens": 100,
        "cachedInputTokens": 10,
        "outputTokens": 20,
        "outcome": "SUCCESS",
    }
    assert json.loads(requests[2].content) == {"outcome": "USAGE_UNAVAILABLE"}


def test_ai_token_settlement_retries_temporary_spring_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(status_code=503, request=request)
        return httpx.Response(status_code=200, request=request)

    client = _client(handler)
    request_id = uuid4()

    asyncio.run(client.settle_ai_tokens(request_id, 100, 10, 20, "SUCCESS"))

    assert len(requests) == 3
    assert {request.url.path for request in requests} == {
        f"/api/internal/v1/ai-token-usages/{request_id}/settle"
    }


def test_ai_token_reservation_retries_with_same_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status_code = 503 if len(requests) < 3 else 200
        return httpx.Response(status_code=status_code, request=request)

    client = _client(handler)
    request_id = uuid4()

    asyncio.run(
        client.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=ANALYSIS_JOB_ID,
            purpose="SETTING_EXTRACTION",
            attempt=1,
            model_name="gpt-5.6-terra",
            reserved_tokens=1000,
            lease_token=LEASE_TOKEN,
        )
    )

    assert len(requests) == 3
    assert {request.url.path for request in requests} == {
        "/api/internal/v1/ai-token-usages/reserve"
    }
    assert {json.loads(request.content)["requestId"] for request in requests} == {str(request_id)}


def test_ai_token_quota_conflict_is_typed_and_never_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=409,
            request=request,
            json={
                "error": {
                    "code": "AI_TOKEN_QUOTA_EXHAUSTED",
                    "message": "충전이 필요합니다.",
                }
            },
        )

    client = _client(handler)

    with pytest.raises(AiTokenQuotaExhaustedError, match="quota is exhausted") as exc_info:
        asyncio.run(
            client.reserve_ai_tokens(
                request_id=uuid4(),
                analysis_job_id=ANALYSIS_JOB_ID,
                purpose="SETTING_EXTRACTION",
                attempt=1,
                model_name="gpt-5.6-terra",
                reserved_tokens=4256,
                lease_token=LEASE_TOKEN,
            )
        )

    assert len(requests) == 1
    assert exc_info.value.status_code == 409
    assert exc_info.value.spring_error_code == "AI_TOKEN_QUOTA_EXHAUSTED"
    assert exc_info.value.spring_reason_code is None


@pytest.mark.parametrize(
    ("error_code", "expected_error_type"),
    [
        ("ANALYSIS_JOB_LEASE_CONFLICT", WorkerLeaseExpiredError),
        ("INTERNAL_SERVER_ERROR", SpringWorkerHttpError),
    ],
)
def test_spring_worker_http_failure_is_typed_by_source(
    error_code: str,
    expected_error_type: type[SpringWorkerHttpError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=409,
            request=request,
            json={"error": {"code": error_code}},
        )

    client = _client(handler)

    with pytest.raises(expected_error_type) as exc_info:
        asyncio.run(
            client.report_progress(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
                "SETTING_EXTRACTION",
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.spring_error_code == error_code
    assert exc_info.value.spring_reason_code is None


def test_spring_worker_http_failure_preserves_allowed_validation_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            request=request,
            json={
                "error": {
                    "code": "WORLD_SETTING_COMPARISON_TARGET_INVALID",
                    "context": {"reasonCode": "PROPOSED_PATH_MISMATCH"},
                }
            },
        )

    client = _client(handler)

    with pytest.raises(SpringWorkerHttpError) as exc_info:
        asyncio.run(
            client.report_progress(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
                "WORLD_SETTING_COMPARISON",
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.spring_error_code == "WORLD_SETTING_COMPARISON_TARGET_INVALID"
    assert exc_info.value.spring_reason_code == "PROPOSED_PATH_MISMATCH"


def test_spring_worker_http_failure_discards_unapproved_reason_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            request=request,
            json={
                "error": {
                    "code": "WORLD_SETTING_COMPARISON_TARGET_INVALID",
                    "context": {"reasonCode": "candidate value was secret"},
                }
            },
        )

    client = _client(handler)

    with pytest.raises(SpringWorkerHttpError) as exc_info:
        asyncio.run(
            client.report_progress(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
                "WORLD_SETTING_COMPARISON",
            )
        )

    assert exc_info.value.spring_error_code == "WORLD_SETTING_COMPARISON_TARGET_INVALID"
    assert exc_info.value.spring_reason_code is None


def test_spring_worker_http_failure_discards_unsafe_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            request=request,
            json={"error": {"code": "internal URL https://secret.example"}},
        )

    client = _client(handler)

    with pytest.raises(SpringWorkerHttpError) as exc_info:
        asyncio.run(
            client.report_progress(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
                "WORLD_SETTING_COMPARISON",
            )
        )

    assert exc_info.value.spring_error_code is None
    assert exc_info.value.spring_reason_code is None


@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError],
)
def test_spring_worker_transport_failure_is_typed_by_source(
    error_type: type[httpx.TransportError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("Spring transport failed", request=request)

    client = _client(handler)

    with pytest.raises(SpringWorkerTransportError) as exc_info:
        asyncio.run(
            client.report_progress(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
                "SETTING_EXTRACTION",
            )
        )

    assert isinstance(exc_info.value.__cause__, error_type)


def test_ai_token_settlement_retries_temporary_spring_transport_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            raise httpx.ConnectError("Spring unavailable", request=request)
        return httpx.Response(status_code=200, request=request)

    client = _client(handler)
    request_id = uuid4()

    asyncio.run(client.settle_ai_tokens(request_id, 100, 10, 20, "SUCCESS"))

    assert len(requests) == 3


def test_ai_token_release_retries_temporary_spring_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status_code = 503 if len(requests) < 3 else 200
        return httpx.Response(status_code=status_code, request=request)

    client = _client(handler)
    request_id = uuid4()

    asyncio.run(client.release_ai_tokens(request_id, "USAGE_UNAVAILABLE"))

    assert len(requests) == 3
    assert {request.url.path for request in requests} == {
        f"/api/internal/v1/ai-token-usages/{request_id}/release"
    }


def test_world_setting_worker_calls_use_lease_and_parse_structured_context() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/world-setting-candidates"):
            return httpx.Response(200, request=request, json={"data": [_candidate_payload()]})
        if request.url.path.endswith("/claim-next"):
            return httpx.Response(200, request=request, json={"data": _candidate_payload()})
        if request.url.path.endswith("/comparison-context"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "candidate": _candidate_payload(),
                        "exactTargetWorldSettingId": None,
                        "targets": [],
                    }
                },
            )
        return _empty_success_response(request, [])

    client = _client(handler)

    async def call_world_setting_apis():
        candidates = await client.publish_world_setting_candidates(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
            [
                {
                    "category": "RACE",
                    "subjectName": "바바리안",
                    "scopeName": "1층",
                    "settingName": "서식지",
                    "extractedValue": "혹한 지역",
                    "evidenceSpans": [{"quote": "바바리안은 혹한 지역에 산다."}],
                    "extractionConfidence": 0.95,
                }
            ],
        )
        candidate = candidates[0]
        claimed = await client.claim_next_world_setting_comparison(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
        context = await client.get_world_setting_comparison_context(
            ANALYSIS_JOB_ID,
            candidate.candidate_id,
            LEASE_TOKEN,
            [],
        )
        await client.fail_world_setting_comparison(
            ANALYSIS_JOB_ID,
            candidate.candidate_id,
            LEASE_TOKEN,
            "backend request failed",
            AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
            source_error_code="WORLD_SETTING_COMPARISON_TARGET_INVALID",
            source_reason_code="PROPOSED_PATH_MISMATCH",
        )
        return candidate, claimed, context

    _candidate_result, claimed, context = asyncio.run(call_world_setting_apis())

    assert claimed is not None
    assert claimed.scope_name == "1층"
    assert context.candidate.evidence_spans[0].quote == "바바리안은 혹한 지역에 산다."
    assert json.loads(requests[-1].content) == {
        "failureCode": "COMPARISON_VALIDATION_FAILED",
        "errorMessage": "backend request failed",
        "sourceErrorCode": "WORLD_SETTING_COMPARISON_TARGET_INVALID",
        "sourceReasonCode": "PROPOSED_PATH_MISMATCH",
    }
    assert all(
        request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN) for request in requests
    )


def test_world_setting_batch_calls_match_spring_contract() -> None:
    requests: list[httpx.Request] = []
    comparison_batch_id = UUID("00000000-0000-0000-0000-000000000040")
    target_id = UUID("00000000-0000-0000-0000-000000000041")
    candidate = _batch_candidate_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/world-setting-subject-resolutions/pending"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "candidates": [
                            {
                                "candidateId": candidate["candidateId"],
                                "sourceEpisodeId": str(EPISODE_ID),
                                "category": "RACE",
                                "subjectName": "고블린족",
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/world-setting-subject-resolutions"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "resolutions": [
                            {
                                "candidateId": candidate["candidateId"],
                                "resolutionType": "EXISTING",
                                "canonicalSubjectKey": f"TARGET:{target_id}",
                                "canonicalSubjectName": "고블린",
                                "targetWorldSettingIds": [str(target_id)],
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/claim-next"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "comparisonBatchId": str(comparison_batch_id),
                        "workId": str(WORK_ID),
                        "sourceEpisodeId": str(EPISODE_ID),
                        "category": "RACE",
                        "resolutionType": "EXISTING",
                        "canonicalSubjectKey": f"TARGET:{target_id}",
                        "canonicalSubjectName": "고블린",
                        "resolvedTargetWorldSettingIds": [str(target_id)],
                        "rawScopeName": "전투 특성",
                        "candidates": [candidate],
                    }
                },
            )
        if request.url.path.endswith("/context"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "comparisonBatchId": str(comparison_batch_id),
                        "candidates": [candidate],
                        "exactTargets": [{"candidateRef": "C1", "worldSettingId": str(target_id)}],
                        "targets": [
                            {
                                "worldSettingId": str(target_id),
                                "subjectName": "고블린",
                                "properties": [],
                                "version": 2,
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200, request=request, json={"data": None})

    client = _client(handler)

    async def call_batch_apis():
        pending = await client.get_pending_world_setting_subject_resolutions(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
        resolution_response = await client.complete_world_setting_subject_resolutions(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
            WorkerWorldSettingSubjectResolutionRequest(
                resolutions=[
                    WorkerWorldSettingSubjectResolutionRequestItem(
                        candidate_id=pending.candidates[0].candidate_id,
                        target_world_setting_ids=[target_id],
                    )
                ]
            ),
        )
        assert resolution_response.resolutions[0].canonical_subject_name == "고블린"
        claimed = await client.claim_next_world_setting_comparison_batch(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
        assert claimed is not None
        context = await client.get_world_setting_comparison_batch_context(
            ANALYSIS_JOB_ID,
            comparison_batch_id,
            LEASE_TOKEN,
            [target_id],
        )
        await client.complete_world_setting_comparison_batch(
            ANALYSIS_JOB_ID,
            comparison_batch_id,
            LEASE_TOKEN,
            WorkerWorldSettingComparisonBatchCompleteRequest(
                context_versions=[
                    WorkerWorldSettingContextVersion(
                        world_setting_id=target_id,
                        version=2,
                    )
                ],
                decisions=[
                    WorkerWorldSettingComparisonBatchDecision(
                        decision_ref="D1",
                        source_candidate_refs=["C1"],
                        existing_root_property_names_to_move=["기존 사냥 습성"],
                        canonical_subject_name="고블린",
                        target_world_setting_id=target_id,
                        consolidation_status="SINGLE",
                        suggested_operation="ADD",
                        proposed_scope_name="전투 특성",
                        proposed_setting_name="사냥 전술",
                        proposed_value="무리를 지어 사냥한다.",
                        comparison_reason="새 canonical 설정이다.",
                    )
                ],
            ),
        )
        return claimed, context

    claimed, context = asyncio.run(call_batch_apis())

    assert claimed.candidates[0].candidate_ref == "C1"
    assert context.exact_targets[0].world_setting_id == target_id
    assert [request.url.path for request in requests] == [
        (
            f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
            "/world-setting-subject-resolutions/pending"
        ),
        (f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/world-setting-subject-resolutions"),
        (
            f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
            "/world-setting-comparison-batches/claim-next"
        ),
        (
            f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
            f"/world-setting-comparison-batches/{comparison_batch_id}/context"
        ),
        (
            f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
            f"/world-setting-comparison-batches/{comparison_batch_id}/complete"
        ),
    ]
    assert requests[0].method == "GET"
    assert requests[1].method == "PUT"
    assert json.loads(requests[1].content) == {
        "resolutions": [
            {
                "candidateId": candidate["candidateId"],
                "targetWorldSettingIds": [str(target_id)],
            }
        ]
    }
    assert json.loads(requests[3].content) == {"targetWorldSettingIds": [str(target_id)]}
    complete_payload = json.loads(requests[4].content)
    assert complete_payload["decisions"][0]["decisionRef"] == "D1"
    assert complete_payload["decisions"][0]["sourceCandidateRefs"] == ["C1"]
    assert complete_payload["decisions"][0]["existingRootPropertyNamesToMove"] == ["기존 사냥 습성"]
    assert all(
        request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN) for request in requests
    )


def test_world_setting_stale_subject_resolution_reset_matches_spring_contract() -> None:
    requests: list[httpx.Request] = []
    comparison_batch_id = UUID("00000000-0000-0000-0000-000000000040")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"data": None})

    client = _client(handler)
    asyncio.run(
        client.reset_stale_world_setting_subject_resolution(
            ANALYSIS_JOB_ID,
            comparison_batch_id,
            LEASE_TOKEN,
        )
    )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == (
        f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
        f"/world-setting-comparison-batches/{comparison_batch_id}"
        "/reset-stale-subject-resolution"
    )
    assert requests[0].headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN)


def test_character_fact_comparison_calls_match_spring_contract() -> None:
    requests: list[httpx.Request] = []
    candidate_id = UUID("00000000-0000-0000-0000-000000000030")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/claim-next"):
            return httpx.Response(
                200,
                request=request,
                json={"data": {"candidateId": str(candidate_id)}},
            )
        if request.url.path.endswith("/character-fact-comparison-context"):
            return httpx.Response(
                200,
                request=request,
                json={"data": _character_comparison_context(candidate_id)},
            )
        return _empty_success_response(request, [])

    client = _client(handler)

    async def call_character_comparison_apis():
        claimed = await client.claim_next_character_fact_comparison(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
        context = await client.get_character_fact_comparison_context(
            ANALYSIS_JOB_ID,
            candidate_id,
            LEASE_TOKEN,
        )
        await client.complete_character_fact_comparison(
            ANALYSIS_JOB_ID,
            candidate_id,
            LEASE_TOKEN,
            WorkerCharacterFactComparisonCompleteRequest(
                operation="ADD",
                removed_snapshot_entries=[
                    WorkerRemovedSnapshotEntry(
                        fact_type="STATUS",
                        fact_key="status.출혈",
                    )
                ],
                proposed_fact_value="완전히 회복됨",
                proposed_value_json={"active": False},
                temporal_scope="PRESENT",
                comparison_reason="완전한 회복 결과가 명시되었다.",
                context_token="snapshot-v1",
                raw_comparison_json={"operation": "ADD"},
            ),
        )
        await client.fail_character_fact_comparison(
            ANALYSIS_JOB_ID,
            candidate_id,
            LEASE_TOKEN,
            "comparison failed",
        )
        return claimed, context

    claimed, context = asyncio.run(call_character_comparison_apis())

    assert claimed is not None and claimed.candidate_id == candidate_id
    assert context.candidate.evidence_spans[0].quote == "상처가 완전히 나았다."
    assert context.snapshot_entries[0].fact_key == "status.출혈"
    assert context.prior_candidates[0].attribute_value == "출혈 중"
    expected_base = (
        f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
        f"/setting-candidates/{candidate_id}/character-fact-comparison"
    )
    assert [(request.method, request.url.path) for request in requests] == [
        (
            "POST",
            (
                f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
                "/character-fact-comparisons/claim-next"
            ),
        ),
        ("POST", f"{expected_base}-context"),
        ("POST", f"{expected_base}-complete"),
        ("POST", f"{expected_base}-fail"),
    ]
    complete_payload = json.loads(requests[2].content)
    assert complete_payload == {
        "operation": "ADD",
        "removedSnapshotEntries": [{"factType": "STATUS", "factKey": "status.출혈"}],
        "proposedFactValue": "완전히 회복됨",
        "proposedValueJson": {"active": False},
        "temporalScope": "PRESENT",
        "comparisonReason": "완전한 회복 결과가 명시되었다.",
        "contextToken": "snapshot-v1",
        "rawComparisonJson": {"operation": "ADD"},
    }
    assert json.loads(requests[3].content) == {
        "failureCode": "COMPARISON_VALIDATION_FAILED",
        "errorMessage": "comparison failed",
    }
    assert all(
        request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN) for request in requests
    )


def test_character_fact_comparison_context_allows_missing_source_episode() -> None:
    candidate_id = UUID("00000000-0000-0000-0000-000000000030")
    response_body = _character_comparison_context(candidate_id)
    response_body["candidate"]["sourceEpisodeId"] = None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"data": response_body})

    client = _client(handler)
    context = asyncio.run(
        client.get_character_fact_comparison_context(
            ANALYSIS_JOB_ID,
            candidate_id,
            LEASE_TOKEN,
        )
    )

    assert context.candidate.source_episode_id is None


def _candidate_payload() -> dict:
    return {
        "candidateId": "00000000-0000-0000-0000-000000000020",
        "workId": str(WORK_ID),
        "sourceEpisodeId": str(EPISODE_ID),
        "category": "RACE",
        "subjectName": "바바리안",
        "scopeName": "1층",
        "settingName": "서식지",
        "extractedValue": "혹한 지역",
        "evidenceSpans": [{"quote": "바바리안은 혹한 지역에 산다."}],
        "extractionConfidence": 0.95,
    }


def _batch_candidate_payload() -> dict:
    return {
        "candidateRef": "C1",
        "candidateId": "00000000-0000-0000-0000-000000000020",
        "subjectName": "고블린",
        "scopeName": "전투 특성",
        "settingName": "사냥 전술",
        "extractedValue": "무리를 지어 사냥한다.",
        "evidenceSpans": [{"quote": "고블린은 무리를 지어 사냥했다."}],
        "extractionConfidence": 0.95,
    }


def _character_comparison_context(candidate_id: UUID) -> dict:
    return {
        "candidate": {
            "candidateId": str(candidate_id),
            "workId": str(WORK_ID),
            "sourceEpisodeId": str(EPISODE_ID),
            "entityName": "비요른",
            "attributeName": "status.회복",
            "attributeValue": "완전히 회복됨",
            "valueJson": {"active": False},
            "valueType": "JSON",
            "evidenceSpans": [{"quote": "상처가 완전히 나았다."}],
            "matchedCharacterId": "00000000-0000-0000-0000-000000000031",
            "matchedCharacterName": "비요른",
            "canonicalFactType": "STATUS",
            "canonicalFactKey": "status.회복",
            "confidence": 0.95,
        },
        "snapshotEntries": [
            {
                "factType": "STATUS",
                "factKey": "status.출혈",
                "factValue": "출혈 중",
                "valueJson": {"active": True},
            }
        ],
        "priorCandidates": [
            {
                "sourceEpisodeNo": 1,
                "attributeName": "status.출혈",
                "attributeValue": "출혈 중",
                "valueJson": {"active": True},
                "evidenceSpans": [{"quote": "상처에서 피가 났다."}],
                "comparisonStatus": "COMPLETED",
                "suggestedOperation": "ADD",
                "proposedFactValue": "출혈 중",
                "proposedValueJson": {"active": True},
            }
        ],
        "contextToken": "snapshot-v1",
    }


# MockTransport를 쓰는 테스트용 SpringWorkerClient 생성
def _client(handler) -> SpringWorkerClient:
    return SpringWorkerClient(
        base_url="http://spring.local",
        internal_api_key="test-api-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# claim 성공 응답을 흉내내고 요청을 기록
def _claim_response(request: httpx.Request, requests: list[httpx.Request]) -> httpx.Response:
    requests.append(request)
    return httpx.Response(
        status_code=200,
        json={
            "success": True,
            "message": "분석 작업을 claim했습니다.",
            "data": {
                "analysisJobId": str(ANALYSIS_JOB_ID),
                "jobType": "SETTING_EXTRACTION",
                "workId": str(WORK_ID),
                "workTitle": "빛나는 검사 로맨스",
                "batchId": str(BATCH_ID),
                "modelName": "gpt-4.1-mini",
                "currentStep": "원문 청킹",
                "leaseToken": str(LEASE_TOKEN),
                "leaseExpiresAt": "2026-08-06T12:05:00",
                "claimAttemptCount": 1,
                "checkpointStage": None,
                "worldSettingCandidateId": None,
                "characterSettingSchemas": [
                    {
                        "schemaKey": "stats.strength",
                        "displayName": "근력",
                        "attributePattern": None,
                        "aliases": ["근력", "힘", "strength"],
                        "valueType": "NUMBER",
                    },
                    {
                        "schemaKey": "skills.skill",
                        "displayName": "스킬",
                        "attributePattern": "skill.*",
                        "aliases": [],
                        "valueType": "JSON",
                    },
                ],
                "knownCharacters": [
                    {
                        "characterId": "00000000-0000-0000-0000-000000000099",
                        "name": "비요른 얀델",
                        "activeStatuses": [
                            {
                                "factKey": "status.오른발_부상",
                                "factValue": "오른발이 크게 다쳐 걷기 어려움",
                            },
                            {
                                "factKey": "status.마비독",
                                "factValue": None,
                            },
                        ],
                    }
                ],
                "episode": {
                    "episodeId": str(EPISODE_ID),
                    "episodeNo": 1,
                    "title": "첫 번째 회차",
                    "contentS3Key": "works/work-id/episodes/episode-id.txt",
                    "contentS3Version": None,
                    "contentHash": "hash",
                    "charCount": 1234,
                },
            },
            "error": None,
            "timestamp": "2026-06-25T00:00:00",
        },
    )


# 성공 응답을 흉내내고 요청을 기록
def _empty_success_response(
    request: httpx.Request, requests: list[httpx.Request]
) -> httpx.Response:
    requests.append(request)
    return httpx.Response(
        status_code=200,
        json={
            "success": True,
            "message": "ok",
            "data": None,
            "error": None,
            "timestamp": "2026-06-25T00:00:00",
        },
    )
