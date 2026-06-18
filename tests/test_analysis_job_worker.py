from uuid import uuid4

from app.domain.enums import AnalysisJobStatus, AnalysisJobType
from app.models.analysis_job import AnalysisJob
from app.worker.analysis_job_worker import AnalysisJobWorker


def test_worker_marks_analysis_job_running() -> None:
    analysis_job = _analysis_job()
    session = FakeSession(analysis_job)
    worker = AnalysisJobWorker(session_factory=lambda: session)

    response = worker.run(analysis_job_id=analysis_job.id)

    assert analysis_job.status == AnalysisJobStatus.RUNNING
    assert analysis_job.current_step == "LOADING"
    assert session.committed is True
    assert response.status == AnalysisJobStatus.RUNNING
    assert response.current_step == "LOADING"


def _analysis_job() -> AnalysisJob:
    return AnalysisJob(
        id=uuid4(),
        work_id=uuid4(),
        batch_id=uuid4(),
        episode_id=None,
        job_type=AnalysisJobType.SETTING_EXTRACTION,
        status=AnalysisJobStatus.PENDING,
        current_step=None,
        model_name=None,
        input_token_count=None,
        output_token_count=None,
        summary_json=None,
        error_message=None,
        started_at=None,
        completed_at=None,
        created_at=None,
        updated_at=None,
    )


class FakeSession:
    def __init__(self, analysis_job: AnalysisJob) -> None:
        self.analysis_job = analysis_job
        self.committed = False

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, model, primary_key):
        if primary_key == self.analysis_job.id:
            return self.analysis_job
        return None

    def commit(self) -> None:
        self.committed = True
