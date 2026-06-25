from uuid import UUID

import httpx

from app.core.config import Settings, get_settings
from app.schemas.worker import (
    WorkerAnalysisJobClaimRequest,
    WorkerAnalysisJobCompleteRequest,
    WorkerAnalysisJobFailRequest,
    WorkerAnalysisJobPayload,
    WorkerAnalysisJobProgressRequest,
)

# Spring 내부 API 인증용 헤더 이름
INTERNAL_API_KEY_HEADER = "X-Internal-Api-Key"


class SpringWorkerClient:
    def __init__(
        self,
        base_url: str,
        internal_api_key: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        # base_url 끝의 / 제거, 예: http://localhost:8080/ -> http://localhost:8080
        self.base_url = base_url.rstrip("/")
        self.internal_api_key = internal_api_key # Spring 내부 API 호출 시 보낼 API key (env 값)
        self.http_client = http_client or httpx.Client(timeout=30) #Python에서 HTTP 요청을 보내는 도구

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "SpringWorkerClient":
        settings = settings or get_settings()
        return cls(
            base_url=settings.spring_internal_api_base_url,
            internal_api_key=settings.spring_internal_api_key,
        )

    def claim(
        self,
        model_name: str | None = None,
        current_step: str | None = None,
    ) -> WorkerAnalysisJobPayload | None:
        # claim 요청 DTO 생성
        request = WorkerAnalysisJobClaimRequest(model_name=model_name, current_step=current_step)
        # Spring에 분석 job claim 요청
        response = self.http_client.post(
            self._url("/api/internal/v1/analysis-jobs/claim"),
            headers=self._headers(),
            # Pydantic DTO를 JSON용 dict로 변환 (alias 형태로 변환, null은 보내지 않음)
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        # 204 No Content면 가져갈 job이 없다는 뜻
        if response.status_code == 204:
            return None
        #2xx, 3xx → 그냥 통과, 4xx, 5xx → httpx.HTTPStatusError 발생
        response.raise_for_status()
        # data 안쪽만 꺼내서 받을 schema와 형식이 동일한지 검증
        return WorkerAnalysisJobPayload.model_validate(response.json()["data"])

    # Spring에 보낼 진행 상태 보고 요청 DTO
    def report_progress(self, analysis_job_id: UUID, current_step: str) -> None:
        request = WorkerAnalysisJobProgressRequest(current_step=current_step)
        
        # Spring 내부 API에 PATCH 요청
        response = self.http_client.patch(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/progress"),
            headers=self._headers(),
            json=request.model_dump(by_alias=True),
        )
        # HTTP 응답이 4xx/5xx이면 예외를 발생
        response.raise_for_status()

    def complete(
        self,
        analysis_job_id: UUID,
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
        response = self.http_client.post(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/complete"),
            headers=self._headers(),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        response.raise_for_status()

    # Spring에 보낼 분석 실패 요청 DTO
    def fail(self, analysis_job_id: UUID, error_message: str) -> None:
        request = WorkerAnalysisJobFailRequest(error_message=error_message)
        response = self.http_client.post(
            self._url(f"/api/internal/v1/analysis-jobs/{analysis_job_id}/fail"),
            headers=self._headers(),
            json=request.model_dump(by_alias=True),
        )
        response.raise_for_status()
        
    # base_url과 path를 합쳐 실제 요청 URL을 만듦
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    # Spring 내부 API 인증 헤더 생성
    def _headers(self) -> dict[str, str]:
        return {INTERNAL_API_KEY_HEADER: self.internal_api_key}
