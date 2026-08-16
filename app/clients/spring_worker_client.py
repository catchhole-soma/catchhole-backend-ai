from uuid import UUID

import httpx

from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.core.config import Settings, get_settings
from app.domain.enums import (
    AnalysisFailureCode,
    AnalysisJobCheckpointStage,
    AnalysisJobType,
    EpisodeProcessingStatus,
    WorldSettingCategory,
)
from app.schemas.worker import (
    AiTokenReleaseRequest,
    AiTokenReserveRequest,
    AiTokenSettleRequest,
    WorkerAnalysisJobClaimRequest,
    WorkerAnalysisJobCompleteRequest,
    WorkerAnalysisJobFailRequest,
    WorkerAnalysisJobHeartbeatResponse,
    WorkerAnalysisJobPayload,
    WorkerAnalysisJobProgressRequest,
    WorkerCharacterFactComparisonClaimPayload,
    WorkerCharacterFactComparisonCompleteRequest,
    WorkerCharacterFactComparisonContextResponse,
    WorkerCharacterFactComparisonFailRequest,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingCandidatePublishItem,
    WorkerWorldSettingCandidatePublishRequest,
    WorkerWorldSettingComparisonCompleteRequest,
    WorkerWorldSettingComparisonContextRequest,
    WorkerWorldSettingComparisonContextResponse,
    WorkerWorldSettingComparisonFailRequest,
    WorkerWorldSettingSubjectPageResponse,
)

# Spring 내부 API 인증용 헤더 이름
INTERNAL_API_KEY_HEADER = "X-Internal-Api-Key"
WORKER_LEASE_TOKEN_HEADER = "X-Worker-Lease-Token"


class SpringWorkerClient:
    def __init__(
        self,
        base_url: str,
        internal_api_key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_api_key = internal_api_key
        self.http_client = http_client or httpx.AsyncClient(timeout=30)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "SpringWorkerClient":
        settings = settings or get_settings()
        return cls(
            base_url=settings.spring_internal_api_base_url,
            internal_api_key=settings.spring_internal_api_key,
        )

    async def claim(
        self,
        allowed_job_types: list[AnalysisJobType],
        model_name: str | None = None,
        current_step: str | None = None,
    ) -> WorkerAnalysisJobPayload | None:
        request = WorkerAnalysisJobClaimRequest(
            model_name=model_name,
            current_step=current_step,
            allowed_job_types=allowed_job_types,
        )
        response = await self.http_client.post(
            self._url("/api/internal/v1/analysis-jobs/claim"),
            headers=self._headers(),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        # 204 No Content면 가져갈 job이 없다는 뜻
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return WorkerAnalysisJobPayload.model_validate(response.json()["data"])

    # Spring에 보낼 진행 상태 보고 요청 DTO
    async def report_progress(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        current_step: str,
        episode_status: EpisodeProcessingStatus | None = None,
        checkpoint_stage: AnalysisJobCheckpointStage | None = None,
    ) -> None:
        request = WorkerAnalysisJobProgressRequest(
            current_step=current_step,
            episode_status=episode_status,
            checkpoint_stage=checkpoint_stage,
        )
        response = await self.http_client.patch(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/progress"),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        # HTTP 응답이 4xx/5xx이면 예외를 발생
        response.raise_for_status()

    async def complete(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        # Spring에 보낼 분석 완료 요청 DTO
        request = WorkerAnalysisJobCompleteRequest(
            summary_json=summary_json,
            input_token_count=input_token_count,
            output_token_count=output_token_count,
        )
        # Spring 내부 API에 완료 보고 POST 요청
        response = await self.http_client.post(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/complete"),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        response.raise_for_status()

    # Spring에 보낼 분석 실패 요청 DTO
    async def fail(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode = AnalysisFailureCode.UNEXPECTED_ERROR,
    ) -> None:
        request = WorkerAnalysisJobFailRequest(
            failure_code=failure_code,
            error_message=error_message,
        )
        response = await self.http_client.post(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/fail"),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True),
        )
        response.raise_for_status()

    # AI provider 호출 전에 예상 최대량을 Spring 원장에 예약한다.
    # 같은 requestId 재요청은 멱등하므로 일시 장애에는 정산과 동일하게 재시도한다.
    async def reserve_ai_tokens(
        self,
        request_id: UUID,
        analysis_job_id: UUID,
        purpose: str,
        attempt: int,
        model_name: str,
        reserved_tokens: int,
        lease_token: UUID,
    ) -> None:
        request = AiTokenReserveRequest(
            request_id=request_id,
            analysis_job_id=analysis_job_id,
            purpose=purpose,
            attempt=attempt,
            model_name=model_name,
            reserved_tokens=reserved_tokens,
        )
        await self._post_usage_update_with_retry(
            path="/api/internal/v1/ai-token-usages/reserve",
            payload=request.model_dump(by_alias=True, mode="json"),
            lease_token=lease_token,
        )

    async def heartbeat(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerAnalysisJobHeartbeatResponse:
        response = await self.http_client.post(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/heartbeat"),
            headers=self._headers(lease_token),
        )
        response.raise_for_status()
        return WorkerAnalysisJobHeartbeatResponse.model_validate(response.json()["data"])

    async def claim_next_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerCharacterFactComparisonClaimPayload | None:
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                "/character-fact-comparisons/claim-next"
            ),
            headers=self._headers(lease_token),
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return WorkerCharacterFactComparisonClaimPayload.model_validate(response.json()["data"])

    async def get_character_fact_comparison_context(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
    ) -> WorkerCharacterFactComparisonContextResponse:
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                f"/setting-candidates/{candidate_id}/character-fact-comparison-context"
            ),
            headers=self._headers(lease_token),
        )
        response.raise_for_status()
        return WorkerCharacterFactComparisonContextResponse.model_validate(response.json()["data"])

    async def complete_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        request: WorkerCharacterFactComparisonCompleteRequest,
    ) -> None:
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                f"/setting-candidates/{candidate_id}/character-fact-comparison-complete"
            ),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        response.raise_for_status()

    async def fail_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode = AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
    ) -> None:
        request = WorkerCharacterFactComparisonFailRequest(
            failure_code=failure_code,
            error_message=error_message,
        )
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                f"/setting-candidates/{candidate_id}/character-fact-comparison-fail"
            ),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True),
        )
        response.raise_for_status()

    async def publish_world_setting_candidates(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidates: list[WorkerWorldSettingCandidatePublishItem],
    ) -> list[WorkerWorldSettingCandidatePayload]:
        request = WorkerWorldSettingCandidatePublishRequest(candidates=candidates)
        response = await self.http_client.put(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/world-setting-candidates"),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        response.raise_for_status()
        return [
            WorkerWorldSettingCandidatePayload.model_validate(candidate_payload)
            for candidate_payload in response.json()["data"]
        ]

    async def claim_next_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerWorldSettingCandidatePayload | None:
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                "/world-setting-comparisons/claim-next"
            ),
            headers=self._headers(lease_token),
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return WorkerWorldSettingCandidatePayload.model_validate(response.json()["data"])

    async def get_world_setting_subjects(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        category: WorldSettingCategory,
        page: int,
        size: int = 500,
    ) -> WorkerWorldSettingSubjectPageResponse:
        response = await self.http_client.get(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/world-setting-subjects"),
            headers=self._headers(lease_token),
            params={"category": category, "page": page, "size": size},
        )
        response.raise_for_status()
        return WorkerWorldSettingSubjectPageResponse.model_validate(response.json()["data"])

    async def get_world_setting_comparison_context(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        target_world_setting_ids: list[UUID],
    ) -> WorkerWorldSettingComparisonContextResponse:
        request = WorkerWorldSettingComparisonContextRequest(
            target_world_setting_ids=target_world_setting_ids
        )
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                f"/world-setting-candidates/{candidate_id}/comparison-context"
            ),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True, mode="json"),
        )
        response.raise_for_status()
        return WorkerWorldSettingComparisonContextResponse.model_validate(response.json()["data"])

    async def complete_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        request: WorkerWorldSettingComparisonCompleteRequest,
    ) -> None:
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                f"/world-setting-candidates/{candidate_id}/comparison-complete"
            ),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        response.raise_for_status()

    async def fail_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode = AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
    ) -> None:
        request = WorkerWorldSettingComparisonFailRequest(
            failure_code=failure_code,
            error_message=error_message,
        )
        response = await self.http_client.post(
            self._url(
                f"/api/internal/v1/analysis-jobs/{analysis_job_id}"
                f"/world-setting-candidates/{candidate_id}/comparison-fail"
            ),
            headers=self._headers(lease_token),
            json=request.model_dump(by_alias=True),
        )
        response.raise_for_status()

    # provider가 반환한 실제 usage로 예약을 정산하고 남은 예약량을 반환한다.
    async def settle_ai_tokens(
        self,
        request_id: UUID,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        outcome: str,
    ) -> None:
        request = AiTokenSettleRequest(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            outcome=outcome,
        )
        await self._post_usage_update_with_retry(
            path=f"/api/internal/v1/ai-token-usages/{request_id}/settle",
            payload=request.model_dump(by_alias=True),
        )

    # provider 사용량을 확인할 수 없을 때 기존 예약을 전액 해제한다.
    async def release_ai_tokens(self, request_id: UUID, outcome: str) -> None:
        request = AiTokenReleaseRequest(outcome=outcome)
        await self._post_usage_update_with_retry(
            path=f"/api/internal/v1/ai-token-usages/{request_id}/release",
            payload=request.model_dump(by_alias=True),
        )

    async def _post_usage_update_with_retry(
        self,
        path: str,
        payload: dict,
        lease_token: UUID | None = None,
    ) -> None:
        """일시적인 Spring 연결 장애에는 같은 requestId의 멱등 원장 요청을 재시도한다."""

        for attempt in range(3):
            try:
                response = await self.http_client.post(
                    self._url(path),
                    headers=self._headers(lease_token),
                    json=payload,
                )
                if (
                    response.status_code == 409
                    and _spring_error_code(response) == "AI_TOKEN_QUOTA_EXHAUSTED"
                ):
                    raise AiTokenQuotaExhaustedError()
                response.raise_for_status()
                return
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt == 2:
                    raise
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code in {408, 409, 429} or (
                    exc.response.status_code >= 500
                )
                if not retryable or attempt == 2:
                    raise

    async def aclose(self) -> None:
        await self.http_client.aclose()

    # base_url과 path를 합쳐 실제 요청 URL을 만듦
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    # Spring 내부 API 인증 헤더 생성
    def _headers(self, lease_token: UUID | None = None) -> dict[str, str]:
        headers = {INTERNAL_API_KEY_HEADER: self.internal_api_key}
        if lease_token is not None:
            headers[WORKER_LEASE_TOKEN_HEADER] = str(lease_token)
        return headers


def _spring_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None
