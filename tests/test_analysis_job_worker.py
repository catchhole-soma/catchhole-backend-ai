from uuid import UUID

import pytest

from app.schemas.worker import WorkerAnalysisEpisodePayload, WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker, WorkerRunSummary

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")


def test_worker_returns_without_error_when_claimable_job_does_not_exist() -> None:
    spring_client = FakeSpringWorkerClient(payload=None)
    worker = SuccessfulAnalysisJobWorker(spring_client=spring_client)

    result = worker.run_once()

    assert result.claimed is False
    assert result.analysis_job_id is None
    assert spring_client.claim_called is True
    assert spring_client.progress_calls == []
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == []


def test_worker_reports_progress_and_complete_to_spring() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = SuccessfulAnalysisJobWorker(spring_client=spring_client)

    result = worker.run_once()

    assert result.claimed is True
    assert result.analysis_job_id == ANALYSIS_JOB_ID
    assert spring_client.progress_calls == [(ANALYSIS_JOB_ID, "SETTING_EXTRACTION")]
    assert spring_client.complete_calls == [
        (ANALYSIS_JOB_ID, '{"candidateCount": 0}', 10, 2),
    ]
    assert spring_client.fail_calls == []


def test_worker_reports_fail_to_spring_when_analysis_fails() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = FailingAnalysisJobWorker(spring_client=spring_client)

    with pytest.raises(RuntimeError):
        worker.run_once()

    assert spring_client.progress_calls == [(ANALYSIS_JOB_ID, "SETTING_EXTRACTION")]
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [(ANALYSIS_JOB_ID, "LLM response parse failed.")]


class SuccessfulAnalysisJobWorker(AnalysisJobWorker):
    def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        return WorkerRunSummary(
            summary_json='{"candidateCount": 0}',
            input_token_count=10,
            output_token_count=2,
        )


class FailingAnalysisJobWorker(AnalysisJobWorker):
    def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        raise RuntimeError("LLM response parse failed.")


class FakeSpringWorkerClient:
    def __init__(self, payload: WorkerAnalysisJobPayload | None) -> None:
        self.payload = payload
        self.claim_called = False
        self.progress_calls: list[tuple[UUID, str]] = []
        self.complete_calls: list[tuple[UUID, str | None, int | None, int | None]] = []
        self.fail_calls: list[tuple[UUID, str]] = []

    def claim(self, model_name: str | None = None, current_step: str | None = None) -> WorkerAnalysisJobPayload | None:
        self.claim_called = True
        return self.payload

    def report_progress(self, analysis_job_id: UUID, current_step: str) -> None:
        self.progress_calls.append((analysis_job_id, current_step))

    def complete(
        self,
        analysis_job_id: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        self.complete_calls.append((analysis_job_id, summary_json, input_token_count, output_token_count))

    def fail(self, analysis_job_id: UUID, error_message: str) -> None:
        self.fail_calls.append((analysis_job_id, error_message))


def _payload() -> WorkerAnalysisJobPayload:
    return WorkerAnalysisJobPayload(
        analysis_job_id=ANALYSIS_JOB_ID,
        job_type="SETTING_EXTRACTION",
        work_id=WORK_ID,
        work_title="빛나는 검사 로맨스",
        batch_id=BATCH_ID,
        model_name="gpt-4.1-mini",
        current_step="SETTING_EXTRACTION",
        episodes=[
            WorkerAnalysisEpisodePayload(
                episode_id=EPISODE_ID,
                episode_no=1,
                title="첫 번째 회차",
                content_s3_key="works/work-id/episodes/episode-id.txt",
                content_s3_version=None,
                content_hash="hash",
                char_count=1234,
            )
        ],
    )
