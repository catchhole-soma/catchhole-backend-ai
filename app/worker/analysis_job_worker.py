from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import get_session_maker
from app.domain.enums import AnalysisStep
from app.repositories.analysis_job_repository import AnalysisJobRepository
from app.schemas.analysis import AnalysisJobRunResponse


class AnalysisJobWorker:
    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self.session_factory = session_factory or get_session_maker()

    def run(self, analysis_job_id: UUID, force: bool = False) -> AnalysisJobRunResponse:
        with self.session_factory() as session:
            repository = AnalysisJobRepository(session)
            analysis_job = repository.get_by_id_or_throw(analysis_job_id)
            analysis_job.mark_running()
            session.commit()

        return AnalysisJobRunResponse(
            analysis_job_id=analysis_job_id,
            status=analysis_job.status,
            current_step=AnalysisStep.LOADING,
            message="Analysis job started.",
        )
