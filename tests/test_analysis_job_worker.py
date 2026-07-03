import json
from uuid import UUID

import pytest

from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
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
    # 실제 OpenAI 호출은 FakeSettingExtractor로 대체한다.
    # 여기서는 "LLM 결과가 이미 나왔다"는 가정 아래 Worker가 저장 전에 quote offset을 보정하는지 본다.
    chunk_text = (
        "던전의 입구에는 축축한 안개가 내려앉아 있었다.\n\n"
        "비요른은 1레벨 바바리안이다. 그는 낡은 도끼를 고쳐 쥐고 통로 안쪽을 노려보았다."
    )
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(
        chunks=[_chunk(0, chunk_text, start_offset=100)]
    )
    extracted_candidates = [
        _candidate(chunking_service.chunks[0].id, attribute_name="level"),
        _candidate(chunking_service.chunks[0].id, attribute_name="class"),
    ]
    setting_extractor = FakeSettingExtractor(candidate_groups=[extracted_candidates])
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        setting_extractor=setting_extractor,
        setting_candidate_service=setting_candidate_service,
    )

    result = worker.run_once()

    assert result.claimed is True
    assert chunking_service.requested_episode_ids == [EPISODE_ID]
    assert setting_extractor.requests == [
            {
                "source_chunk_id": chunking_service.chunks[0].id,
                "chunk_text": chunk_text,
                "episode_no": 1,
                "episode_title": "첫 번째 회차",
            }
    ]
    assert setting_candidate_service.request == {
        "work_id": WORK_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "episode_ids": [EPISODE_ID, EPISODE_ID],
        "known_character_names": ["비요른 얀델"],
        "candidate_count": 2,
    }
    saved_candidate = setting_candidate_service.saved_candidates[0]
    expected_start_offset = 100 + chunk_text.index("비요른은 1레벨 바바리안이다.")
    assert saved_candidate.attribute_name == "level"
    assert saved_candidate.evidence_spans[0].start_offset == expected_start_offset
    assert saved_candidate.evidence_spans[0].end_offset == expected_start_offset + len("비요른은 1레벨 바바리안이다.")
    assert extracted_candidates[0].evidence_spans[0].start_offset is None
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
    # 실제 OpenAI 호출 대신 chunk별 추출 후보 목록만 흉내
    def __init__(self, candidate_groups: list[list[ExtractedSettingCandidate]]) -> None:
        self.candidate_groups = candidate_groups
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
        candidates = self.candidate_groups.pop(0)
        return FakeExtractionResult(candidates=candidates)


class FakeExtractionResult:
    def __init__(self, candidates: list[ExtractedSettingCandidate]) -> None:
        self.candidates = candidates


class FakeSettingCandidateService:
    # 실제 DB 저장 대신 Worker가 전달한 저장 요청을 기록
    def __init__(self) -> None:
        self.request = None
        self.saved_candidates: list[ExtractedSettingCandidate] = []

    def replace_candidates_for_analysis_job(
        self,
        work_id,
        analysis_job_id,
        save_items,
        known_characters=None,
    ):
        self.saved_candidates = [item.candidate for item in save_items]
        self.request = {
            "work_id": work_id,
            "analysis_job_id": analysis_job_id,
            "episode_ids": [item.episode_id for item in save_items],
            "known_character_names": [
                character.name for character in known_characters or []
            ],
            "candidate_count": len(save_items),
        }
        return self.saved_candidates


def _payload() -> WorkerAnalysisJobPayload:
    return WorkerAnalysisJobPayload(
        analysis_job_id=ANALYSIS_JOB_ID,
        job_type="SETTING_EXTRACTION",
        work_id=WORK_ID,
        work_title="빛나는 검사 로맨스",
        batch_id=BATCH_ID,
        model_name="gpt-4.1-mini",
        current_step="SETTING_EXTRACTION",
        known_characters=[
            {
                "characterId": "00000000-0000-0000-0000-000000000005",
                "name": "비요른 얀델",
                "aliases": ["비요른"],
            }
        ],
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


def _candidate(source_chunk_id: UUID, attribute_name: str) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=source_chunk_id,
        entity_type="CHARACTER",
        entity_name="비요른",
        attribute_name=attribute_name,
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="비요른은 1레벨 바바리안이다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )


def _chunk(chunk_index: int, chunk_text: str, start_offset: int = 0) -> EpisodeChunk:
    return EpisodeChunk(
        id=UUID(f"00000000-0000-0000-0000-00000000010{chunk_index}"),
        episode_id=EPISODE_ID,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
        start_offset=start_offset,
        end_offset=start_offset + len(chunk_text),
        paragraph_start_index=0,
        paragraph_end_index=0,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )
