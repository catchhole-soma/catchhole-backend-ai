from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import get_session_maker
from app.mappers.analysis_job_mapper import AnalysisJobMapper
from app.repositories.analysis_job_repository import AnalysisJobRepository
from app.schemas.analysis import AnalysisJobStatusResponse


class AnalysisJobService:
    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_maker()

    def get_status(self, analysis_job_id: UUID) -> AnalysisJobStatusResponse:
        with self.session_factory() as session:
            repository = AnalysisJobRepository(session)
            analysis_job = repository.get_by_id_or_throw(analysis_job_id)
            return AnalysisJobMapper.to_status_response(analysis_job)
