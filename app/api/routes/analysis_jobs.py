from uuid import UUID

from fastapi import APIRouter

from app.schemas.analysis import (
    AnalysisJobStatusResponse,
)
from app.services.analysis_job_service import AnalysisJobService

router = APIRouter()


@router.get("/{analysis_job_id}/status", response_model=AnalysisJobStatusResponse)
def get_analysis_job_status(analysis_job_id: UUID) -> AnalysisJobStatusResponse:
    return AnalysisJobService().get_status(analysis_job_id=analysis_job_id)
