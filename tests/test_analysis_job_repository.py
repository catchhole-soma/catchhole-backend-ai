from uuid import uuid4

import pytest

from app.domain.enums import AnalysisJobStatus, AnalysisJobType, AnalysisStep
from app.exceptions.app_exception import AppException
from app.models.analysis_job import AnalysisJob
from app.repositories.analysis_job_repository import AnalysisJobRepository


def test_get_by_id_or_throw_raises_when_job_missing(fake_session) -> None:
    repository = AnalysisJobRepository(fake_session)
    missing_id = uuid4()

    with pytest.raises(AppException) as exc_info:
        repository.get_by_id_or_throw(missing_id)

    assert exc_info.value.detail == {"analysis_job_id": str(missing_id)}


def test_analysis_job_mark_running_updates_status_and_step() -> None:
    analysis_job = _analysis_job()

    analysis_job.mark_running()

    assert analysis_job.status == AnalysisJobStatus.RUNNING
    assert analysis_job.current_step == "LOADING"
    assert analysis_job.started_at is not None
    assert analysis_job.error_message is None


def test_analysis_job_change_step_updates_current_step() -> None:
    analysis_job = _analysis_job()

    analysis_job.change_step(AnalysisStep.CHUNKING)

    assert analysis_job.current_step == "CHUNKING"


def test_analysis_job_mark_succeeded_updates_status_and_summary() -> None:
    analysis_job = _analysis_job()

    analysis_job.mark_succeeded(summary_json='{"candidateCount": 3}')

    assert analysis_job.status == AnalysisJobStatus.SUCCEEDED
    assert analysis_job.current_step == "DONE"
    assert analysis_job.summary_json == '{"candidateCount": 3}'
    assert analysis_job.completed_at is not None
    assert analysis_job.error_message is None


def test_analysis_job_mark_failed_updates_status_and_error_message() -> None:
    analysis_job = _analysis_job()

    analysis_job.mark_failed(error_message="LLM response parse failed.")

    assert analysis_job.status == AnalysisJobStatus.FAILED
    assert analysis_job.error_message == "LLM response parse failed."
    assert analysis_job.completed_at is not None


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
        error_message="previous error",
        started_at=None,
        completed_at=None,
        created_at=None,
        updated_at=None,
    )


class FakeSession:
    def get(self, model, primary_key):
        return None


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()
