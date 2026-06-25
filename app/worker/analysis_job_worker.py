from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.enums import AnalysisStep
from app.clients.spring_worker_client import SpringWorkerClient
from app.schemas.worker import WorkerAnalysisJobPayload

# Worker 실행 결과를 담는 값 객체
@dataclass(frozen=True)
class WorkerRunResult:
    claimed: bool
    analysis_job_id: UUID | None
    message: str

# 실제 분석 실행 후 Spring에 완료 보고할 요약 정보
@dataclass(frozen=True)
class WorkerRunSummary:
    summary_json: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None

# SpringWorkerClient가 가져야 하는 메서드 규격
class SpringWorkerApi(Protocol):
    def claim(self, model_name: str | None = None, current_step: str | None = None) -> WorkerAnalysisJobPayload | None:
        pass

    def report_progress(self, analysis_job_id: UUID, current_step: str) -> None:
        pass

    def complete(
        self,
        analysis_job_id: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        pass

    def fail(self, analysis_job_id: UUID, error_message: str) -> None:
        pass

# 분석 job 하나를 claim하고, 진행/완료/실패 보고까지 수행하는 Worker
class AnalysisJobWorker:
    def __init__(
        self,
        spring_client: SpringWorkerApi | None = None,
        model_name: str | None = None,
    ) -> None:
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self.model_name = model_name

    def run_once(self) -> WorkerRunResult:
        # Spring 서버에 처리 가능한 분석 job 하나를 요청
        payload = self.spring_client.claim(
            model_name=self.model_name,
            current_step=AnalysisStep.SETTING_EXTRACTION.value,
        )
        # 처리할 job이 없으면 아무 작업도 하지 않고 종료
        if payload is None:
            return WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable analysis job does not exist.",
            )

        try:
            # claim한 job의 현재 진행 상태를 Spring에 보고
            self.spring_client.report_progress(
                analysis_job_id=payload.analysis_job_id,
                current_step=AnalysisStep.SETTING_EXTRACTION.value,
            )
            # 실제 분석 로직 (Todo)
            summary = self._run_analysis_steps(payload)
            # 분석이 성공하면 Spring에 완료 상태와 요약 정보를 보고
            self.spring_client.complete(
                analysis_job_id=payload.analysis_job_id,
                summary_json=summary.summary_json,
                input_token_count=summary.input_token_count,
                output_token_count=summary.output_token_count,
            )
        except Exception as exc:
            self.spring_client.fail(
                analysis_job_id=payload.analysis_job_id,
                error_message=self._error_message(exc),
            )
            raise
        
        # 분석 job 하나를 정상적으로 처리했음을 반환
        return WorkerRunResult(
            claimed=True,
            analysis_job_id=payload.analysis_job_id,
            message="Analysis job completed.",
        )

    def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        raise NotImplementedError("Analysis extraction flow is not implemented yet.")

    def _error_message(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        return message[:1000]
