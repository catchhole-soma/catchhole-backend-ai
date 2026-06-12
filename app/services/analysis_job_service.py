from uuid import UUID

from app.domain.enums import AnalysisJobStatus, AnalysisStep
from app.schemas.analysis import AnalysisJobRunResponse, AnalysisJobStatusResponse
from app.worker.analysis_job_worker import AnalysisJobWorker


class AnalysisJobService:
    def __init__(self, worker: AnalysisJobWorker | None = None) -> None:
        self.worker = worker or AnalysisJobWorker()

    def run(self, analysis_job_id: UUID, force: bool = False) -> AnalysisJobRunResponse:
        return self.worker.run(analysis_job_id=analysis_job_id, force=force)

    def get_status(self, analysis_job_id: UUID) -> AnalysisJobStatusResponse:
        # TODO: PostgreSQL analysis_jobs 테이블에서 상태를 조회하도록 연결한다.
        return AnalysisJobStatusResponse(
            analysis_job_id=analysis_job_id,
            status=AnalysisJobStatus.PENDING,
            progress=0,
            current_step=AnalysisStep.LOADING,
        )
