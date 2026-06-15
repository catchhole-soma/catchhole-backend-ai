from uuid import UUID

from fastapi import APIRouter

from app.schemas.analysis import (
    AnalysisJobRunRequest,
    AnalysisJobRunResponse,
    AnalysisJobStatusResponse,
)
from app.services.analysis_job_service import AnalysisJobService

router = APIRouter()


@router.post("/{analysis_job_id}/run", response_model=AnalysisJobRunResponse)
def run_analysis_job(
    analysis_job_id: UUID,
    request: AnalysisJobRunRequest,
) -> AnalysisJobRunResponse:
    return AnalysisJobService().run(analysis_job_id=analysis_job_id, force=request.force)


@router.get("/{analysis_job_id}/status", response_model=AnalysisJobStatusResponse)
def get_analysis_job_status(analysis_job_id: UUID) -> AnalysisJobStatusResponse:
    return AnalysisJobService().get_status(analysis_job_id=analysis_job_id)
