from uuid import UUID

from sqlalchemy.orm import Session

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.models.analysis_job import AnalysisJob


class AnalysisJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_id(self, analysis_job_id: UUID) -> AnalysisJob | None:
        return self.session.get(AnalysisJob, analysis_job_id)

    def get_by_id_or_throw(self, analysis_job_id: UUID) -> AnalysisJob:
        analysis_job = self.find_by_id(analysis_job_id)
        if analysis_job is None:
            raise AppException(
                ErrorCode.ANALYSIS_JOB_NOT_FOUND,
                detail={"analysis_job_id": str(analysis_job_id)},
            )
        return analysis_job
