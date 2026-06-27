import json
from uuid import UUID

import pytest

from app.models.episode_chunk import EpisodeChunk
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


def test_worker_chunks_episode_content_and_extracts_candidates() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 1레벨 바바리안이다.")])
    setting_extractor = FakeSettingExtractor(candidate_counts=[2])
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        setting_extractor=setting_extractor,
    )

    result = worker.run_once()

    assert result.claimed is True
    assert chunking_service.requested_episode_ids == [EPISODE_ID]
    assert setting_extractor.requests == [
        {
            "source_chunk_id": chunking_service.chunks[0].id,
            "chunk_text": "비요른은 1레벨 바바리안이다.",
            "episode_no": 1,
            "episode_title": "첫 번째 회차",
        }
    ]
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 1,
        "candidateCount": 2,
    }
    assert spring_client.fail_calls == []


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


class FakeEpisodeChunkingService:
    # 실제 S3/DB 청킹 대신 Worker가 episode_id를 넘겼는지 기록
    def __init__(self, chunks: list[EpisodeChunk]) -> None:
        self.chunks = chunks
        self.requested_episode_ids: list[UUID] = []

    def replace_chunks_from_s3_content(self, episode_id: UUID) -> list[EpisodeChunk]:
        self.requested_episode_ids.append(episode_id)
        return self.chunks


class FakeSettingExtractor:
    # 실제 OpenAI 호출 대신 chunk별 후보 개수만 흉내
    def __init__(self, candidate_counts: list[int]) -> None:
        self.candidate_counts = candidate_counts
        self.requests = []

    def extract_from_chunk(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
    ):
        self.requests.append(
            {
                "source_chunk_id": source_chunk_id,
                "chunk_text": chunk_text,
                "episode_no": episode_no,
                "episode_title": episode_title,
            }
        )
        candidate_count = self.candidate_counts.pop(0)
        return FakeExtractionResult(candidates=[object() for _ in range(candidate_count)])


class FakeExtractionResult:
    def __init__(self, candidates: list[object]) -> None:
        self.candidates = candidates


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


def _chunk(chunk_index: int, chunk_text: str) -> EpisodeChunk:
    return EpisodeChunk(
        id=UUID(f"00000000-0000-0000-0000-00000000010{chunk_index}"),
        episode_id=EPISODE_ID,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
        start_offset=0,
        end_offset=len(chunk_text),
        paragraph_start_index=0,
        paragraph_end_index=0,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )
