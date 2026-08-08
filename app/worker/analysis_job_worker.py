from dataclasses import dataclass
import json
import logging
from typing import Protocol
from uuid import UUID

from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionChunkContext,
    SubjectResolutionResult,
)
from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.schemas import CharacterSettingExtractionResult, ExtractedSettingCandidate
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.analysis.world_setting_pipeline import (
    WorldSettingComparisonPipeline,
    WorldSettingComparisonRunResult,
    WorldSettingComparisonSpringApi,
)
from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import get_settings
from app.db.session import get_session_maker
from app.domain.enums import (
    AnalysisJobCheckpointStage,
    AnalysisJobType,
    AnalysisStep,
    EpisodeProcessingStatus,
)
from app.embeddings.client import OpenAIEmbeddingsClient
from app.embeddings.exceptions import RecoverableEmbeddingProviderError
from app.embeddings.services.episode_chunk_embedding import (
    EpisodeChunkEmbeddingResult,
    EpisodeChunkEmbeddingService,
)
from app.models.episode_chunk import EpisodeChunk
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper
from app.schemas.worker import (
    WorkerAnalysisJobPayload,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingCandidatePublishItem,
)
from app.services.episode_chunk_service import EpisodeChunkService
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService
from app.services.setting_candidate_service import (
    SettingCandidateSaveItem,
    SettingCandidateService,
)
from app.storage.s3 import S3TextObjectStorage
from app.llm.openai_client import OpenAIResponsesClient
from app.usage.metering import (
    AiTokenLedgerApi,
    MeteredEmbeddingClient,
    MeteredTextGenerationClient,
)
from app.worker.lease_heartbeat import HeartbeatSpringApi, WorkerLeaseHeartbeat
from app.worker.world_setting_services import create_world_setting_comparison_pipeline

logger = logging.getLogger(__name__)


# Worker 실행 결과를 담는 값 객체
@dataclass(frozen=True)
class WorkerRunResult:
    claimed: bool
    analysis_job_id: UUID | None
    message: str
    work_id: UUID | None = None
    work_title: str | None = None
    episode_count: int | None = None


# 실제 분석 실행 후 Spring에 완료 보고할 요약 정보
@dataclass(frozen=True)
class WorkerRunSummary:
    summary_json: str | None = None


# SpringWorkerClient가 가져야 하는 메서드 규격
class SpringWorkerApi(
    WorldSettingComparisonSpringApi,
    AiTokenLedgerApi,
    HeartbeatSpringApi,
    Protocol,
):
    # Spring 내부 API에서 처리 가능한 analysis job 하나를 점유한다.
    def claim(
        self,
        allowed_job_types: list[AnalysisJobType],
        model_name: str | None = None,
        current_step: str | None = None,
    ) -> WorkerAnalysisJobPayload | None: ...

    # claim 직후 현재 Worker가 어떤 단계에 진입했는지 Spring에 보고한다.
    def report_progress(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        current_step: str,
        episode_status: EpisodeProcessingStatus | None = None,
        checkpoint_stage: AnalysisJobCheckpointStage | None = None,
    ) -> None: ...

    # 단일 episode의 chunk 분석과 후보 저장이 끝난 뒤 성공 결과를 Spring에 보고한다.
    def complete(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None: ...

    # 분석 중 예외가 발생하면 Spring에 실패 사유를 보고한다.
    def fail(self, analysis_job_id: UUID, lease_token: UUID, error_message: str) -> None: ...

    def publish_world_setting_candidates(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidates: list[WorkerWorldSettingCandidatePublishItem],
    ) -> list[WorkerWorldSettingCandidatePayload]: ...


# 회차 원문을 읽고 청킹 결과를 교체하는 최소 계약.
class EpisodeChunkingApi(Protocol):
    def replace_chunks_from_s3_content(
        self,
        episode_id: UUID,
        content_s3_key: str,
    ) -> list[EpisodeChunk]: ...


class EpisodeChunkStoreApi(Protocol):
    def get_episode_chunks(self, episode_id: UUID) -> list[EpisodeChunk]: ...


# Worker가 저장된 청크의 임베딩 생성과 DB 반영을 요청할 때 기대하는 규격
class EpisodeChunkEmbeddingApi(Protocol):
    def embed_chunks(self, chunks: list[EpisodeChunk]) -> EpisodeChunkEmbeddingResult: ...


# Worker가 chunk 하나에서 설정 후보를 추출할 때 기대하는 규격(테스트를 위한 목적이 커서 이후에 구현 완료되면 바로 주입 가능)
class SettingExtractorApi(Protocol):
    # chunk_text를 LLM에 전달해 캐릭터 설정 후보를 추출한다.
    def extract_from_chunk(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
        schema_hints: tuple[CharacterSettingSchemaHint, ...] = (),
        known_characters: tuple[KnownCharacter, ...] = (),
    ) -> CharacterSettingExtractionResult: ...


class SubjectResolverApi(Protocol):
    # 구체 entity_name을 얻지 못한 후보를 앞뒤 chunk 문맥으로 해소해 반환한다.
    def resolve_candidates(
        self,
        context: SubjectResolutionChunkContext,
        candidates: list[ExtractedSettingCandidate],
        known_characters: list[KnownCharacter],
    ) -> SubjectResolutionResult: ...


class WorldSettingExtractorApi(Protocol):
    def extract_from_chunk(
        self,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
    ) -> WorldSettingExtractionResult: ...


# 분석 job 하나를 claim하고, 진행/완료/실패 보고까지 수행하는 Worker
class AnalysisJobWorker:
    def __init__(
        self,
        spring_client: SpringWorkerApi | None = None,
        chunking_service: EpisodeChunkingApi | None = None,
        episode_chunk_service: EpisodeChunkStoreApi | None = None,
        episode_chunk_embedding_service: EpisodeChunkEmbeddingApi | None = None,
        setting_extractor: SettingExtractorApi | None = None,
        subject_resolver: SubjectResolverApi | None = None,
        world_setting_extractor: WorldSettingExtractorApi | None = None,
        world_setting_comparison_pipeline: WorldSettingComparisonPipeline | None = None,
        setting_candidate_service: SettingCandidateService | None = None,
        extraction_model_name: str | None = None,
        comparison_model_name: str | None = None,
        embedding_generation_enabled: bool = False,
    ) -> None:
        settings = get_settings()
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self._chunking_service = chunking_service
        self._episode_chunk_service = episode_chunk_service
        self._episode_chunk_embedding_service = episode_chunk_embedding_service
        self._setting_extractor = setting_extractor
        self._subject_resolver = subject_resolver
        self._world_setting_extractor = world_setting_extractor
        self._world_setting_comparison_pipeline = world_setting_comparison_pipeline
        self._setting_candidate_service = setting_candidate_service
        self.extraction_model_name = (
            extraction_model_name or settings.effective_llm_extraction_model
        )
        self.comparison_model_name = (
            comparison_model_name or settings.effective_llm_comparison_model
        )
        self.embedding_generation_enabled = embedding_generation_enabled

    def run_once(self) -> WorkerRunResult:
        # Spring 서버에 처리 가능한 분석 job 하나를 요청
        payload = self.spring_client.claim(
            allowed_job_types=[AnalysisJobType.SETTING_EXTRACTION],
            model_name=self.extraction_model_name,
            current_step=AnalysisStep.SETTING_EXTRACTION.value,
        )
        # 처리할 job이 없으면 아무 작업도 하지 않고 종료
        if payload is None:
            return WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable analysis job does not exist.",
            )

        try:
            if payload.job_type != AnalysisJobType.SETTING_EXTRACTION:
                raise ValueError(f"Unsupported analysis job type: {payload.job_type}")
            # claim한 job의 현재 진행 상태를 Spring에 보고
            self.spring_client.report_progress(
                analysis_job_id=payload.analysis_job_id,
                lease_token=payload.lease_token,
                current_step=AnalysisStep.SETTING_EXTRACTION.value,
                episode_status=EpisodeProcessingStatus.ANALYZING,
            )
            with WorkerLeaseHeartbeat(
                self.spring_client,
                payload.analysis_job_id,
                payload.lease_token,
            ) as lease_heartbeat:
                summary = self._run_analysis_steps(payload)
                lease_heartbeat.raise_if_failed()
            # 분석이 성공하면 Spring에 완료 상태와 요약 정보를 보고
            self.spring_client.complete(
                analysis_job_id=payload.analysis_job_id,
                lease_token=payload.lease_token,
                summary_json=summary.summary_json,
            )
        except Exception as exc:
            try:
                self.spring_client.fail(
                    analysis_job_id=payload.analysis_job_id,
                    lease_token=payload.lease_token,
                    error_message=self._error_message(exc),
                )
            except Exception:
                logger.exception(
                    "Failed to report analysis job failure. analysis_job_id=%s",
                    payload.analysis_job_id,
                )
            raise

        # 분석 job 하나를 정상적으로 처리했음을 반환
        return WorkerRunResult(
            claimed=True,
            analysis_job_id=payload.analysis_job_id,
            message="Analysis job completed.",
            work_id=payload.work_id,
            work_title=payload.work_title,
            episode_count=1,
        )

    def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        checkpoint = payload.checkpoint_stage
        if (
            not _checkpoint_reached(
                checkpoint,
                AnalysisJobCheckpointStage.CHARACTER_CANDIDATES_SAVED,
            )
            and not payload.character_setting_schemas
        ):
            raise ValueError(
                "Analysis job claim must include at least one characterSettingSchemas entry."
            )

        chunks, embedding_metrics = self._run_chunk_stage(payload, checkpoint)
        known_characters = [
            KnownCharacter(
                character_id=character.character_id,
                name=character.name,
            )
            for character in payload.known_characters
        ]
        schema_hints = tuple(
            CharacterSettingSchemaHint(
                schema_key=schema.schema_key,
                display_name=schema.display_name,
                attribute_pattern=schema.attribute_pattern,
                aliases=tuple(schema.aliases),
                value_type=schema.value_type,
            )
            for schema in payload.character_setting_schemas
        )
        character_metrics = self._run_character_stage(
            payload,
            chunks,
            checkpoint,
            known_characters,
            schema_hints,
        )
        world_candidate_count = self._run_world_extraction_stage(payload, chunks, checkpoint)
        comparison_result = self._run_world_comparison_stage(payload, checkpoint)

        summary_json = json.dumps(
            {
                "episodeCount": 1,
                "chunkCount": len(chunks),
                **embedding_metrics,
                **character_metrics,
                "worldSettingCandidateCount": world_candidate_count,
                "worldSettingComparisonCompletedCount": comparison_result.completed_count,
                "worldSettingComparisonFailedCount": comparison_result.failed_count,
            },
            ensure_ascii=False,
        )
        return WorkerRunSummary(summary_json=summary_json)

    def _run_chunk_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        checkpoint: AnalysisJobCheckpointStage | None,
    ) -> tuple[list[EpisodeChunk], dict[str, int]]:
        episode = payload.episode
        if _checkpoint_reached(checkpoint, AnalysisJobCheckpointStage.CHUNKS_READY):
            chunks = self._get_episode_chunk_service().get_episode_chunks(episode.episode_id)
            if not chunks:
                raise ValueError("CHUNKS_READY checkpoint has no stored episode chunks.")
            return chunks, {
                "embeddedChunkCount": 0,
                "embeddingFailedChunkCount": 0,
                "embeddingSkippedChunkCount": 0,
            }

        chunks = self._get_chunking_service().replace_chunks_from_s3_content(
            episode_id=episode.episode_id,
            content_s3_key=episode.content_s3_key,
        )
        embedded_count = 0
        failed_count = 0
        skipped_count = 0
        if self.embedding_generation_enabled:
            try:
                result = self._get_episode_chunk_embedding_service(
                    payload.analysis_job_id,
                    payload.lease_token,
                ).embed_chunks(chunks)
                embedded_count = result.embedded_chunk_count
            except RecoverableEmbeddingProviderError:
                failed_count = len(chunks)
                logger.exception(
                    "Chunk embedding provider failed temporarily; setting extraction will continue. "
                    "episode_id=%s chunk_count=%s",
                    episode.episode_id,
                    len(chunks),
                )
        else:
            skipped_count = len(chunks)
        self.spring_client.report_progress(
            payload.analysis_job_id,
            payload.lease_token,
            AnalysisStep.SETTING_EXTRACTION,
            EpisodeProcessingStatus.ANALYZING,
            AnalysisJobCheckpointStage.CHUNKS_READY,
        )
        return chunks, {
            "embeddedChunkCount": embedded_count,
            "embeddingFailedChunkCount": failed_count,
            "embeddingSkippedChunkCount": skipped_count,
        }

    def _run_character_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        chunks: list[EpisodeChunk],
        checkpoint: AnalysisJobCheckpointStage | None,
        known_characters: list[KnownCharacter],
        schema_hints: tuple[CharacterSettingSchemaHint, ...],
    ) -> dict[str, int]:
        if _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.CHARACTER_CANDIDATES_SAVED,
        ):
            return {
                "candidateCount": 0,
                "subjectFallbackCallCount": 0,
                "subjectFallbackResolvedCount": 0,
                "subjectFallbackUnresolvedCount": 0,
            }

        setting_extractor = self._get_setting_extractor(
            payload.analysis_job_id,
            payload.lease_token,
        )
        subject_resolver = self._get_subject_resolver(
            payload.analysis_job_id,
            payload.lease_token,
        )
        save_items: list[SettingCandidateSaveItem] = []
        fallback_calls = 0
        fallback_resolved = 0
        fallback_unresolved = 0
        episode = payload.episode
        for index, chunk in enumerate(chunks):
            extraction_result = setting_extractor.extract_from_chunk(
                source_chunk_id=chunk.id,
                chunk_text=chunk.chunk_text,
                episode_no=episode.episode_no,
                episode_title=episode.title,
                schema_hints=schema_hints,
                known_characters=tuple(known_characters),
            )
            resolved_candidates = resolve_candidate_evidence_offsets(
                candidates=extraction_result.candidates,
                chunk_text=chunk.chunk_text,
                chunk_start_offset=chunk.start_offset,
            )
            resolution = subject_resolver.resolve_candidates(
                context=SubjectResolutionChunkContext(
                    previous_chunk_text=chunks[index - 1].chunk_text if index > 0 else None,
                    current_chunk_text=chunk.chunk_text,
                    next_chunk_text=(
                        chunks[index + 1].chunk_text if index + 1 < len(chunks) else None
                    ),
                ),
                candidates=resolved_candidates,
                known_characters=known_characters,
            )
            fallback_calls += resolution.fallback_call_count
            fallback_resolved += resolution.fallback_resolved_count
            fallback_unresolved += resolution.fallback_unresolved_count
            save_items.extend(
                SettingCandidateSaveItem(
                    episode_id=episode.episode_id,
                    source_content_s3_key=episode.content_s3_key,
                    candidate=candidate,
                )
                for candidate in resolution.candidates
            )

        saved_candidates = (
            self._get_setting_candidate_service().replace_candidates_for_analysis_job(
                work_id=payload.work_id,
                analysis_job_id=payload.analysis_job_id,
                save_items=save_items,
                known_characters=known_characters,
            )
        )
        self.spring_client.report_progress(
            payload.analysis_job_id,
            payload.lease_token,
            AnalysisStep.WORLD_SETTING_EXTRACTION,
            EpisodeProcessingStatus.ANALYZING,
            AnalysisJobCheckpointStage.CHARACTER_CANDIDATES_SAVED,
        )
        return {
            "candidateCount": len(saved_candidates),
            "subjectFallbackCallCount": fallback_calls,
            "subjectFallbackResolvedCount": fallback_resolved,
            "subjectFallbackUnresolvedCount": fallback_unresolved,
        }

    def _run_world_extraction_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        chunks: list[EpisodeChunk],
        checkpoint: AnalysisJobCheckpointStage | None,
    ) -> int:
        if _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.WORLD_CANDIDATES_PUBLISHED,
        ):
            return 0

        extractor = self._get_world_setting_extractor(
            payload.analysis_job_id,
            payload.lease_token,
        )
        extracted_items = [
            WorldSettingCandidateMapper.to_publish_item(candidate, chunk)
            for chunk in chunks
            for candidate in extractor.extract_from_chunk(
                chunk_text=chunk.chunk_text,
                episode_no=payload.episode.episode_no,
                episode_title=payload.episode.title,
            ).candidates
        ]
        candidates = WorldSettingCandidateMapper.deduplicate(extracted_items)
        published = self.spring_client.publish_world_setting_candidates(
            payload.analysis_job_id,
            payload.lease_token,
            candidates,
        )
        return len(published)

    def _run_world_comparison_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        checkpoint: AnalysisJobCheckpointStage | None,
    ) -> WorldSettingComparisonRunResult:
        if _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.WORLD_COMPARISONS_FINISHED,
        ):
            return WorldSettingComparisonRunResult(0, 0)

        result = self._get_world_setting_comparison_pipeline(
            payload.analysis_job_id,
            payload.lease_token,
        ).process_all(payload.analysis_job_id, payload.lease_token)
        self.spring_client.report_progress(
            payload.analysis_job_id,
            payload.lease_token,
            AnalysisStep.WORLD_SETTING_COMPARISON,
            EpisodeProcessingStatus.ANALYZING,
            AnalysisJobCheckpointStage.WORLD_COMPARISONS_FINISHED,
        )
        return result

    def _error_message(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        return message[:1000]

    # S3에 접근해서 에피소드 원문을 청크로 나눌 EpisodeS3ChunkingService를 초기화 하는 작업만 한다.
    def _get_chunking_service(self) -> EpisodeChunkingApi:
        if self._chunking_service is None:
            session_factory = get_session_maker()
            self._episode_chunk_service = EpisodeChunkService(session_factory=session_factory)
            self._chunking_service = EpisodeS3ChunkingService(
                storage=S3TextObjectStorage.from_settings(),
                chunk_service=self._episode_chunk_service,
            )
        return self._chunking_service

    def _get_episode_chunk_service(self) -> EpisodeChunkStoreApi:
        if self._episode_chunk_service is None:
            self._episode_chunk_service = EpisodeChunkService(session_factory=get_session_maker())
        return self._episode_chunk_service

    # 저장된 청크의 벡터를 생성하고 episode_chunks에 반영할 서비스를 초기화한다.
    def _get_episode_chunk_embedding_service(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> EpisodeChunkEmbeddingApi:
        if self._episode_chunk_embedding_service is not None:
            return self._episode_chunk_embedding_service

        settings = get_settings()
        metered_client = MeteredEmbeddingClient(
            delegate=OpenAIEmbeddingsClient.from_settings(settings),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            model_name=settings.embedding_model,
            lease_token=lease_token,
        )
        return EpisodeChunkEmbeddingService(
            session_factory=get_session_maker(),
            embedding_client=metered_client,
        )

    # llm에 넣을 프롬프트와 api호출을 할 서비스(CharacterSettingExtractor)를 초기화 하는 작업만 한다.
    def _get_setting_extractor(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> SettingExtractorApi:
        if self._setting_extractor is not None:
            return self._setting_extractor

        settings = get_settings()
        model_name = self.extraction_model_name
        metered_client = MeteredTextGenerationClient(
            delegate=OpenAIResponsesClient.from_settings(settings),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            purpose="SETTING_EXTRACTION",
            default_model=model_name,
            lease_token=lease_token,
        )
        return CharacterSettingExtractor(
            llm_client=metered_client,
            model=model_name,
        )

    def _get_subject_resolver(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> SubjectResolverApi:
        if self._subject_resolver is not None:
            return self._subject_resolver

        settings = get_settings()
        model_name = self.extraction_model_name
        metered_client = MeteredTextGenerationClient(
            delegate=OpenAIResponsesClient.from_settings(settings),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            purpose="SUBJECT_RESOLUTION",
            default_model=model_name,
            lease_token=lease_token,
        )
        # 구체 entity_name을 얻지 못한 후보의 주체 해소 호출도 별도 request로 정산한다.
        return CharacterSubjectResolver(
            llm_client=metered_client,
            model=model_name,
        )

    def _get_world_setting_extractor(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorldSettingExtractorApi:
        if self._world_setting_extractor is not None:
            return self._world_setting_extractor

        settings = get_settings()
        model_name = self.extraction_model_name
        metered_client = MeteredTextGenerationClient(
            delegate=OpenAIResponsesClient.from_settings(settings),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            purpose="WORLD_SETTING_EXTRACTION",
            default_model=model_name,
            lease_token=lease_token,
        )
        return WorldSettingExtractor(llm_client=metered_client, model=model_name)

    def _get_world_setting_comparison_pipeline(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorldSettingComparisonPipeline:
        if self._world_setting_comparison_pipeline is not None:
            return self._world_setting_comparison_pipeline

        return create_world_setting_comparison_pipeline(
            spring_client=self.spring_client,
            analysis_job_id=analysis_job_id,
            lease_token=lease_token,
            comparison_model_name=self.comparison_model_name,
        )

    # 검증된 설정 후보를 setting_candidates 테이블에 저장할 서비스를 필요할 때 초기화한다.
    def _get_setting_candidate_service(self) -> SettingCandidateService:
        if self._setting_candidate_service is None:
            self._setting_candidate_service = SettingCandidateService(
                session_factory=get_session_maker(),
            )
        return self._setting_candidate_service


def _checkpoint_reached(
    current: AnalysisJobCheckpointStage | None,
    target: AnalysisJobCheckpointStage,
) -> bool:
    if current is None:
        return False
    checkpoints = list(AnalysisJobCheckpointStage)
    return checkpoints.index(current) >= checkpoints.index(target)
