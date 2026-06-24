from uuid import uuid4

from app.domain.enums import AnalysisJobStatus, AnalysisJobType
from app.mappers.analysis_job_mapper import AnalysisJobMapper
from app.models.analysis_job import AnalysisJob


def test_to_status_response_maps_analysis_job() -> None:
    analysis_job = AnalysisJob(
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
        summary_json='{"chunkCount": 12}',
        error_message=None,
        started_at=None,
        completed_at=None,
        created_at=None,
        updated_at=None,
    )

    response = AnalysisJobMapper.to_status_response(analysis_job)

    assert response.analysis_job_id == analysis_job.id
    assert response.status == AnalysisJobStatus.RUNNING
    assert response.current_step == "CHUNKING"
    assert response.summary == {"chunkCount": 12}
