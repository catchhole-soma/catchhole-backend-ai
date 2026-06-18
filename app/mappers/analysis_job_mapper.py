from app.domain.enums import AnalysisJobStatus, AnalysisStep
from app.models.analysis_job import AnalysisJob
from app.schemas.analysis import AnalysisJobStatusResponse


class AnalysisJobMapper:
    @staticmethod
    def to_status_response(analysis_job: AnalysisJob) -> AnalysisJobStatusResponse:
        current_step = AnalysisStep(analysis_job.current_step) if analysis_job.current_step else None
        return AnalysisJobStatusResponse(
            analysis_job_id=analysis_job.id,
            status=AnalysisJobStatus(analysis_job.status),
            progress=0,
            current_step=current_step,
            summary=analysis_job.summary_json or {},
        )
