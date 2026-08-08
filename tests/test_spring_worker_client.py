import json
from uuid import UUID, uuid4

import httpx

from app.clients.spring_worker_client import (
    INTERNAL_API_KEY_HEADER,
    WORKER_LEASE_TOKEN_HEADER,
    SpringWorkerClient,
)
from app.domain.enums import EpisodeProcessingStatus
from app.schemas.worker import WorkerAnalysisJobPayload

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000005")


# claim 성공 시 payload를 파싱하고 요청 헤더/URL/Body가 올바른지 확인
def test_claim_returns_payload_when_spring_returns_job() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _claim_response(request, requests))

    payload = client.claim(
        allowed_job_types=["SETTING_EXTRACTION"],
        model_name="gpt-4.1-mini",
        current_step="원문 청킹",
    )

    assert payload is not None
    assert payload.analysis_job_id == ANALYSIS_JOB_ID
    assert payload.work_id == WORK_ID
    assert payload.episode.episode_id == EPISODE_ID
    assert payload.character_setting_schemas[0].schema_key == "stats.strength"
    assert payload.character_setting_schemas[0].aliases == ["근력", "힘", "strength"]
    assert payload.character_setting_schemas[1].attribute_pattern == "skill.*"
    assert payload.character_setting_schemas[1].value_type == "JSON"
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

    payload = client.claim(allowed_job_types=["SETTING_EXTRACTION"])

    assert payload is None


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


# 진행 상태 보고 API를 PATCH로 올바른 URL과 Body로 호출하는지 확인
def test_report_progress_calls_spring_progress_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    client.report_progress(
        analysis_job_id=ANALYSIS_JOB_ID,
        lease_token=LEASE_TOKEN,
        current_step="설정 추출",
        episode_status=EpisodeProcessingStatus.ANALYZING,
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

    client.complete(
        analysis_job_id=ANALYSIS_JOB_ID,
        lease_token=LEASE_TOKEN,
        summary_json='{"candidateCount":3}',
        input_token_count=100,
        output_token_count=20,
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

    client.fail(
        analysis_job_id=ANALYSIS_JOB_ID,
        lease_token=LEASE_TOKEN,
        error_message="LLM 응답 오류",
    )

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/fail"
    assert request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN)
    assert json.loads(request.content) == {"errorMessage": "LLM 응답 오류"}


def test_ai_token_reserve_settle_and_release_call_internal_apis() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))
    request_id = uuid4()

    client.reserve_ai_tokens(
        request_id=request_id,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        attempt=1,
        model_name="gpt-4.1-mini",
        reserved_tokens=1000,
        lease_token=LEASE_TOKEN,
    )
    client.settle_ai_tokens(request_id, 100, 10, 20, "SUCCESS")
    other_request_id = uuid4()
    client.release_ai_tokens(other_request_id, "USAGE_UNAVAILABLE")

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

    client.settle_ai_tokens(request_id, 100, 10, 20, "SUCCESS")

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

    client.reserve_ai_tokens(
        request_id=request_id,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        attempt=1,
        model_name="gpt-5.6-terra",
        reserved_tokens=1000,
        lease_token=LEASE_TOKEN,
    )

    assert len(requests) == 3
    assert {request.url.path for request in requests} == {
        "/api/internal/v1/ai-token-usages/reserve"
    }
    assert {json.loads(request.content)["requestId"] for request in requests} == {str(request_id)}


def test_ai_token_release_retries_temporary_spring_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status_code = 503 if len(requests) < 3 else 200
        return httpx.Response(status_code=status_code, request=request)

    client = _client(handler)
    request_id = uuid4()

    client.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")

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
    candidate = client.publish_world_setting_candidates(
        ANALYSIS_JOB_ID,
        LEASE_TOKEN,
        [
            {
                "category": "RACE",
                "subjectName": "바바리안",
                "settingName": "서식지",
                "extractedValue": "혹한 지역",
                "evidenceSpans": [{"quote": "바바리안은 혹한 지역에 산다."}],
                "extractionConfidence": 0.95,
            }
        ],
    )[0]
    claimed = client.claim_next_world_setting_comparison(ANALYSIS_JOB_ID, LEASE_TOKEN)
    context = client.get_world_setting_comparison_context(
        ANALYSIS_JOB_ID,
        candidate.candidate_id,
        LEASE_TOKEN,
        [],
    )

    assert claimed is not None
    assert context.candidate.evidence_spans[0].quote == "바바리안은 혹한 지역에 산다."
    assert all(
        request.headers[WORKER_LEASE_TOKEN_HEADER] == str(LEASE_TOKEN) for request in requests
    )


def _candidate_payload() -> dict:
    return {
        "candidateId": "00000000-0000-0000-0000-000000000020",
        "workId": str(WORK_ID),
        "sourceEpisodeId": str(EPISODE_ID),
        "category": "RACE",
        "subjectName": "바바리안",
        "settingName": "서식지",
        "extractedValue": "혹한 지역",
        "evidenceSpans": [{"quote": "바바리안은 혹한 지역에 산다."}],
        "extractionConfidence": 0.95,
    }


# MockTransport를 쓰는 테스트용 SpringWorkerClient 생성
def _client(handler) -> SpringWorkerClient:
    return SpringWorkerClient(
        base_url="http://spring.local",
        internal_api_key="test-api-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
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
