from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import get_session_maker
from app.mappers.analysis_job_mapper import AnalysisJobMapper
from app.repositories.analysis_job_repository import AnalysisJobRepository
from app.schemas.analysis import AnalysisJobRunResponse, AnalysisJobStatusResponse
from app.worker.analysis_job_worker import AnalysisJobWorker


class AnalysisJobService:
    def __init__(
        self,
        worker: AnalysisJobWorker | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_maker()
        self.worker = worker or AnalysisJobWorker(session_factory=self.session_factory)

    def run(self, analysis_job_id: UUID, force: bool = False) -> AnalysisJobRunResponse:
        return self.worker.run(analysis_job_id=analysis_job_id, force=force)

    def get_status(self, analysis_job_id: UUID) -> AnalysisJobStatusResponse:
        with self.session_factory() as session:
            repository = AnalysisJobRepository(session)
            analysis_job = repository.get_by_id_or_throw(analysis_job_id)
            return AnalysisJobMapper.to_status_response(analysis_job)
