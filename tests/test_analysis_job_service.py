from uuid import uuid4

from app.domain.enums import AnalysisJobStatus, AnalysisJobType
from app.models.analysis_job import AnalysisJob
from app.services.analysis_job_service import AnalysisJobService


def test_service_get_status_reads_analysis_job_from_repository() -> None:
    analysis_job = _analysis_job()
    session = FakeSession(analysis_job)
    service = AnalysisJobService(session_factory=lambda: session)

    response = service.get_status(analysis_job_id=analysis_job.id)

    assert response.analysis_job_id == analysis_job.id
    assert response.status == AnalysisJobStatus.RUNNING
    assert response.current_step == "CHUNKING"
    assert response.summary == {"chunkCount": 12}


def _analysis_job() -> AnalysisJob:
    return AnalysisJob(
        id=uuid4(),
        work_id=uuid4(),
        batch_id=uuid4(),
        episode_id=None,
        job_type=AnalysisJobType.SETTING_EXTRACTION,
        status=AnalysisJobStatus.RUNNING,
        current_step="CHUNKING",
        model_name=None,
        input_token_count=None,
        output_token_count=None,
        summary_json={"chunkCount": 12},
        error_message=None,
        started_at=None,
        completed_at=None,
        created_at=None,
        updated_at=None,
    )


class FakeSession:
    def __init__(self, analysis_job: AnalysisJob) -> None:
        self.analysis_job = analysis_job

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, model, primary_key):
        if primary_key == self.analysis_job.id:
            return self.analysis_job
        return None
