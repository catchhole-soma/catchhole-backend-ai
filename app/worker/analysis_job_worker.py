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
        with self.session_factory() as session: #session을 열고, 블록이 끝나면 자동으로 정리
            repository = AnalysisJobRepository(session)
            analysis_job = repository.get_by_id_or_throw(analysis_job_id)
            analysis_job.mark_running()
            session.commit() #이후 with session.begin() 방식 고려

        try:
            self._run_analysis_steps(analysis_job_id, force)
        except Exception as exc:
            self._mark_failed(analysis_job_id, exc)
            raise

        return AnalysisJobRunResponse(
            analysis_job_id=analysis_job_id,
            status=analysis_job.status,
            current_step=AnalysisStep.LOADING,
            message="Analysis job started.",
        )

    def _run_analysis_steps(self, analysis_job_id: UUID, force: bool) -> None:
        return None

    def _mark_failed(self, analysis_job_id: UUID, exc: Exception) -> None:
        with self.session_factory() as session:
            repository = AnalysisJobRepository(session)
            analysis_job = repository.get_by_id_or_throw(analysis_job_id)
            analysis_job.mark_failed(self._error_message(exc))
            session.commit()

    def _error_message(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        return message[:1000]
