import json
from uuid import UUID

import pytest

from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from app.embeddings.exceptions import (
    EmbeddingDataIntegrityError,
    RecoverableEmbeddingProviderError,
)
from app.embeddings.services.episode_chunk_embedding import EpisodeChunkEmbeddingResult
from app.domain.enums import EpisodeProcessingStatus
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import WorkerAnalysisEpisodePayload, WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker, WorkerRunSummary

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")
SCHEMA_HINTS = (
    CharacterSettingSchemaHint(
        schema_key="stats.strength",
        display_name="근력",
        attribute_pattern=None,
        aliases=("근력", "힘", "strength"),
        value_type="NUMBER",
    ),
    CharacterSettingSchemaHint(
        schema_key="stats.strength",
        display_name="작품 근력",
        attribute_pattern=None,
        aliases=("완력",),
        value_type="NUMBER",
    ),
)


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
    assert spring_client.progress_calls == [
        (ANALYSIS_JOB_ID, "SETTING_EXTRACTION", EpisodeProcessingStatus.ANALYZING)
    ]
    assert spring_client.complete_calls == [
        (ANALYSIS_JOB_ID, '{"candidateCount": 0}', 10, 2),
    ]
    assert spring_client.fail_calls == []


def test_worker_reports_fail_to_spring_when_analysis_fails() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = FailingAnalysisJobWorker(spring_client=spring_client)

    with pytest.raises(RuntimeError):
        worker.run_once()

    assert spring_client.progress_calls == [
        (ANALYSIS_JOB_ID, "SETTING_EXTRACTION", EpisodeProcessingStatus.ANALYZING)
    ]
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
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService()
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        setting_candidate_service=setting_candidate_service,
    )

    result = worker.run_once()

    assert result.claimed is True
    assert result.analysis_job_id == ANALYSIS_JOB_ID
    assert result.work_id == WORK_ID
    assert result.work_title == "빛나는 검사 로맨스"
    assert result.episode_count == 1
    assert chunking_service.requested_episode_ids == [EPISODE_ID]
    assert chunking_service.requested_content_s3_keys == ["works/work-id/episodes/episode-id.txt"]
    assert episode_chunk_embedding_service.requested_chunk_ids == [
        [chunking_service.chunks[0].id]
    ]
    assert setting_extractor.requests == [
            {
                "source_chunk_id": chunking_service.chunks[0].id,
                "chunk_text": chunk_text,
                "episode_no": 1,
                "episode_title": "첫 번째 회차",
                "schema_hints": SCHEMA_HINTS,
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
        "embeddedChunkCount": 1,
        "embeddingFailedChunkCount": 0,
        "candidateCount": 2,
        "subjectFallbackCallCount": 0,
        "subjectFallbackResolvedCount": 0,
        "subjectFallbackDiscardedCount": 0,
    }
    assert spring_client.fail_calls == []


def test_worker_applies_subject_resolution_before_saving_candidates() -> None:
    current_chunk_text = "나는 1레벨 바바리안으로 깨어났다."
    resolved_candidate = _candidate(
        UUID("00000000-0000-0000-0000-000000000100"),
        attribute_name="level",
        entity_name="비요른 얀델",
        raw_entity_mention="나",
        quote="나는 1레벨 바바리안으로 깨어났다.",
    )
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(
        chunks=[
            _chunk(0, "비요른 얀델은 낡은 도끼를 들고 있었다."),
            _chunk(1, current_chunk_text),
            _chunk(2, "주변에는 다른 인물이 없었다."),
        ]
    )
    setting_extractor = FakeSettingExtractor(
        candidate_groups=[
            [],
            [
                _candidate(
                    chunking_service.chunks[1].id,
                    attribute_name="level",
                    entity_name="미상",
                    raw_entity_mention="나",
                    quote="나는 1레벨 바바리안으로 깨어났다.",
                )
            ],
            [],
        ]
    )
    subject_resolver = FakeSubjectResolver(
        result=SubjectResolutionResult(
            candidates=[resolved_candidate],
            fallback_call_count=1,
            fallback_resolved_count=1,
            fallback_discarded_count=0,
        )
    )
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService()
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        subject_resolver=subject_resolver,
        setting_candidate_service=setting_candidate_service,
    )

    result = worker.run_once()

    assert result.claimed is True
    assert subject_resolver.requests == [
        {
            "previous_chunk_text": "비요른 얀델은 낡은 도끼를 들고 있었다.",
            "current_chunk_text": current_chunk_text,
            "next_chunk_text": "주변에는 다른 인물이 없었다.",
            "candidate_count": 1,
            "known_character_names": ["비요른 얀델"],
        }
    ]
    assert setting_candidate_service.saved_candidates == [resolved_candidate]
    assert all(request["schema_hints"] == SCHEMA_HINTS for request in setting_extractor.requests)
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 3,
        "embeddedChunkCount": 3,
        "embeddingFailedChunkCount": 0,
        "candidateCount": 1,
        "subjectFallbackCallCount": 1,
        "subjectFallbackResolvedCount": 1,
        "subjectFallbackDiscardedCount": 0,
    }


def test_worker_continues_setting_extraction_when_embedding_provider_temporarily_fails() -> None:
    # 일시적인 provider 장애만 요약에 기록하고 설정 후보 추출과 작업 완료를 계속하는지 검증한다.
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")])
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService(
        error=RecoverableEmbeddingProviderError("embedding API failed temporarily")
    )
    setting_extractor = FakeSettingExtractor(candidate_groups=[[]])
    subject_resolver = FakeSubjectResolver(result=SubjectResolutionResult(candidates=[]))
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        subject_resolver=subject_resolver,
        setting_candidate_service=setting_candidate_service,
    )

    result = worker.run_once()

    assert result.claimed is True
    assert len(setting_extractor.requests) == 1
    assert setting_candidate_service.request["candidate_count"] == 0
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 1,
        "embeddedChunkCount": 0,
        "embeddingFailedChunkCount": 1,
        "candidateCount": 0,
        "subjectFallbackCallCount": 0,
        "subjectFallbackResolvedCount": 0,
        "subjectFallbackDiscardedCount": 0,
    }
    assert spring_client.fail_calls == []


def test_worker_fails_analysis_when_chunk_embedding_data_is_inconsistent() -> None:
    # 중복·누락 청크 같은 정합성 오류를 삼키지 않고 Spring 실패 보고까지 전파하는지 검증한다.
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")])
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService(
        error=EmbeddingDataIntegrityError("embedding update target is missing")
    )
    setting_extractor = FakeSettingExtractor(candidate_groups=[[]])
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
    )

    with pytest.raises(EmbeddingDataIntegrityError, match="target is missing"):
        worker.run_once()

    assert setting_extractor.requests == []
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [
        (ANALYSIS_JOB_ID, "embedding update target is missing")
    ]


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
        self.progress_calls: list[tuple[UUID, str, EpisodeProcessingStatus]] = []
        self.complete_calls: list[tuple[UUID, str | None, int | None, int | None]] = []
        self.fail_calls: list[tuple[UUID, str]] = []

    def claim(self, model_name: str | None = None, current_step: str | None = None) -> WorkerAnalysisJobPayload | None:
        self.claim_called = True
        return self.payload

    def report_progress(
        self,
        analysis_job_id: UUID,
        current_step: str,
        episode_status: EpisodeProcessingStatus,
    ) -> None:
        self.progress_calls.append((analysis_job_id, current_step, episode_status))

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
    # 실제 S3/DB 청킹 대신 Worker가 claim payload의 episode_id/content_s3_key를 넘겼는지 기록
    def __init__(self, chunks: list[EpisodeChunk]) -> None:
        self.chunks = chunks
        self.requested_episode_ids: list[UUID] = []
        self.requested_content_s3_keys: list[str] = []

    def replace_chunks_from_s3_content(
        self,
        episode_id: UUID,
        content_s3_key: str,
    ) -> list[EpisodeChunk]:
        self.requested_episode_ids.append(episode_id)
        self.requested_content_s3_keys.append(content_s3_key)
        return self.chunks


class FakeEpisodeChunkEmbeddingService:
    # 실제 OpenAI/DB 호출 대신 Worker가 청킹 직후 임베딩을 요청했는지 기록한다.
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requested_chunk_ids: list[list[UUID]] = []

    def embed_chunks(self, chunks: list[EpisodeChunk]) -> EpisodeChunkEmbeddingResult:
        self.requested_chunk_ids.append([chunk.id for chunk in chunks])
        if self.error is not None:
            raise self.error
        return EpisodeChunkEmbeddingResult(embedded_chunk_count=len(chunks))


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
        schema_hints: tuple[CharacterSettingSchemaHint, ...] = (),
    ):
        self.requests.append(
            {
                "source_chunk_id": source_chunk_id,
                "chunk_text": chunk_text,
                "episode_no": episode_no,
                "episode_title": episode_title,
                "schema_hints": schema_hints,
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
        known_characters,
    ):
        self.saved_candidates = [item.candidate for item in save_items]
        self.request = {
            "work_id": work_id,
            "analysis_job_id": analysis_job_id,
            "episode_ids": [item.episode_id for item in save_items],
            "known_character_names": [
                character.name for character in known_characters
            ],
            "candidate_count": len(save_items),
        }
        return self.saved_candidates


class FakeSubjectResolver:
    def __init__(self, result: SubjectResolutionResult) -> None:
        self.result = result
        self.requests = []

    def resolve_candidates(
        self,
        context,
        candidates,
        known_characters,
    ) -> SubjectResolutionResult:
        if not candidates:
            return SubjectResolutionResult(candidates=[])

        self.requests.append(
            {
                "previous_chunk_text": context.previous_chunk_text,
                "current_chunk_text": context.current_chunk_text,
                "next_chunk_text": context.next_chunk_text,
                "candidate_count": len(candidates),
                "known_character_names": [
                    character.name for character in known_characters
                ],
            }
        )
        return self.result


def _payload() -> WorkerAnalysisJobPayload:
    return WorkerAnalysisJobPayload(
        analysis_job_id=ANALYSIS_JOB_ID,
        job_type="SETTING_EXTRACTION",
        work_id=WORK_ID,
        work_title="빛나는 검사 로맨스",
        batch_id=BATCH_ID,
        model_name="gpt-4.1-mini",
        current_step="SETTING_EXTRACTION",
        character_setting_schemas=[
            {
                "schemaKey": "stats.strength",
                "displayName": "근력",
                "attributePattern": None,
                "aliases": ["근력", "힘", "strength"],
                "valueType": "NUMBER",
            },
            {
                "schemaKey": "stats.strength",
                "displayName": "작품 근력",
                "attributePattern": None,
                "aliases": ["완력"],
                "valueType": "NUMBER",
            }
        ],
        known_characters=[
            {
                "characterId": "00000000-0000-0000-0000-000000000005",
                "name": "비요른 얀델",
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


def _candidate(
    source_chunk_id: UUID,
    attribute_name: str,
    entity_name: str = "비요른",
    raw_entity_mention: str | None = None,
    quote: str = "비요른은 1레벨 바바리안이다.",
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=source_chunk_id,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name=attribute_name,
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote=quote,
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
