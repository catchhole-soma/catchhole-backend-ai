import json
from uuid import UUID

import httpx

from app.clients.spring_worker_client import INTERNAL_API_KEY_HEADER, SpringWorkerClient

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")


# claim 성공 시 payload를 파싱하고 요청 헤더/URL/Body가 올바른지 확인
def test_claim_returns_payload_when_spring_returns_job() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _claim_response(request, requests))

    payload = client.claim(model_name="gpt-4.1-mini", current_step="원문 청킹")

    assert payload is not None
    assert payload.analysis_job_id == ANALYSIS_JOB_ID
    assert payload.work_id == WORK_ID
    assert payload.episodes[0].episode_id == EPISODE_ID
    request = requests[0]
    assert request.headers[INTERNAL_API_KEY_HEADER] == "test-api-key"
    assert request.url.path == "/api/internal/v1/analysis-jobs/claim"
    assert json.loads(request.content) == {
        "modelName": "gpt-4.1-mini",
        "currentStep": "원문 청킹",
    }


# claim할 job이 없어서 Spring이 204를 반환하면 None을 반환하는지 확인
def test_claim_returns_none_when_spring_returns_no_content() -> None:
    client = _client(lambda request: httpx.Response(status_code=204))

    payload = client.claim()

    assert payload is None


# 진행 상태 보고 API를 PATCH로 올바른 URL과 Body로 호출하는지 확인
def test_report_progress_calls_spring_progress_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    client.report_progress(analysis_job_id=ANALYSIS_JOB_ID, current_step="설정 추출")

    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/progress"
    assert json.loads(request.content) == {"currentStep": "설정 추출"}


# 완료 보고 API를 POST로 올바른 URL과 Body로 호출하는지 확인
def test_complete_calls_spring_complete_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    client.complete(
        analysis_job_id=ANALYSIS_JOB_ID,
        summary_json='{"candidateCount":3}',
        input_token_count=100,
        output_token_count=20,
    )

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/complete"
    assert json.loads(request.content) == {
        "summaryJson": '{"candidateCount":3}',
        "inputTokenCount": 100,
        "outputTokenCount": 20,
    }


# 실패 보고 API를 POST로 올바른 URL과 Body로 호출하는지 확인
def test_fail_calls_spring_fail_api() -> None:
    requests: list[httpx.Request] = []
    client = _client(lambda request: _empty_success_response(request, requests))

    client.fail(analysis_job_id=ANALYSIS_JOB_ID, error_message="LLM 응답 오류")

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/fail"
    assert json.loads(request.content) == {"errorMessage": "LLM 응답 오류"}


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
                "episodes": [
                    {
                        "episodeId": str(EPISODE_ID),
                        "episodeNo": 1,
                        "title": "첫 번째 회차",
                        "contentS3Key": "works/work-id/episodes/episode-id.txt",
                        "contentS3Version": None,
                        "contentHash": "hash",
                        "charCount": 1234,
                    }
                ],
            },
            "error": None,
            "timestamp": "2026-06-25T00:00:00",
        },
    )


# 성공 응답을 흉내내고 요청을 기록
def _empty_success_response(request: httpx.Request, requests: list[httpx.Request]) -> httpx.Response:
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