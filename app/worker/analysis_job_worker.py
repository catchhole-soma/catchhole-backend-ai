import asyncio
import json
import logging
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from app.analysis.character_fact_comparison_pipeline import (
    CharacterFactComparisonPipeline,
    CharacterFactComparisonRunResult,
    CharacterFactComparisonSpringApi,
)
from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionChunkContext,
    SubjectResolutionResult,
)
from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.exceptions import OrderedAnalysisIncompleteError, OrderedInputContextError
from app.analysis.ordered_character_subjects import (
    OrderedSubjectResolutionMetrics,
    initial_ordered_subjects,
    merge_ordered_subjects,
    resolve_ordered_character_subjects,
)
from app.analysis.ordered_context import OrderedExtractionContext
from app.analysis.schemas import CharacterSettingExtractionResult, ExtractedSettingCandidate
from app.analysis.semantic_current_context import (
    SemanticCurrentInput,
    prepare_semantic_current_context,
)
from app.analysis.setting_book_identity import BookIdentityContext, has_book_identity_collision
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.analysis.narrative_time import NarrativeTimePoint, ground_extraction_narrative_time
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
    AnalysisFailureCode,
    AnalysisJobCheckpointStage,
    AnalysisJobType,
    AnalysisMode,
    AnalysisReviewMode,
    AnalysisSourceKind,
    AnalysisStep,
    EpisodeProcessingStatus,
)
from app.embeddings.client import OpenAIEmbeddingsClient
from app.embeddings.exceptions import RecoverableEmbeddingProviderError
from app.embeddings.services.episode_chunk_embedding import (
    EpisodeChunkEmbeddingResult,
    EpisodeChunkEmbeddingService,
)
from app.exceptions.failure_classification import (
    COMPARISON_CANDIDATE_FAILURE_CODES,
    analysis_failure_code,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper
from app.models.episode_chunk import EpisodeChunk
from app.models.setting_book_chunk import SettingBookChunk
from app.schemas.analysis_context import WorkerAnalysisContext
from app.schemas.worker import (
    WorkerAnalysisJobPayload,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingCandidatePublishItem,
)
from app.services.episode_chunk_service import EpisodeChunkService
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext
from app.services.setting_book_chunk_service import SettingBookChunkService
from app.services.setting_book_s3_chunking_service import SettingBookS3ChunkingService
from app.services.setting_candidate_service import (
    SettingCandidateSaveItem,
    SettingCandidateService,
)
from app.storage.s3 import S3TextObjectStorage
from app.usage.metering import (
    AiTokenLedgerApi,
    MeteredEmbeddingClient,
    MeteredTextGenerationClient,
)
from app.worker.blocking_io import BlockingIoExecutor
from app.worker.character_fact_services import create_character_fact_comparison_pipeline
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
    CharacterFactComparisonSpringApi,
    WorldSettingComparisonSpringApi,
    AiTokenLedgerApi,
    HeartbeatSpringApi,
    Protocol,
):
    # Spring 내부 API에서 처리 가능한 analysis job 하나를 점유한다.
    async def claim(
        self,
        allowed_job_types: list[AnalysisJobType],
        model_name: str | None = None,
        current_step: str | None = None,
        supported_analysis_modes: list[AnalysisMode] | None = None,
        supports_character_comparison_groups: bool | None = None,
        supported_source_kinds: list[AnalysisSourceKind] | None = None,
        supports_setting_book_reference_time: bool | None = None,
        supports_semantic_current_context: bool | None = None,
        supports_frozen_comparison_evidence: bool | None = None,
    ) -> WorkerAnalysisJobPayload | None: ...

    # claim 직후 현재 Worker가 어떤 단계에 진입했는지 Spring에 보고한다.
    async def report_progress(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        current_step: str,
        episode_status: EpisodeProcessingStatus | None = None,
        checkpoint_stage: AnalysisJobCheckpointStage | None = None,
    ) -> None: ...

    # 단일 episode의 chunk 분석과 후보 저장이 끝난 뒤 성공 결과를 Spring에 보고한다.
    async def complete(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None: ...

    # 분석 중 예외가 발생하면 Spring에 실패 사유를 보고한다.
    async def fail(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode,
    ) -> None: ...

    async def publish_world_setting_candidates(
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
    async def embed_chunks(self, chunks: list[EpisodeChunk | SettingBookChunk]) -> EpisodeChunkEmbeddingResult: ...


# Worker가 chunk 하나에서 설정 후보를 추출할 때 기대하는 규격(테스트를 위한 목적이 커서 이후에 구현 완료되면 바로 주입 가능)
class SettingExtractorApi(Protocol):
    # chunk_text를 LLM에 전달해 캐릭터 설정 후보를 추출한다.
    async def extract_from_chunk(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        analysis_job_id: UUID | None = None,
        episode_no: int | None = None,
        episode_title: str | None = None,
        schema_hints: tuple[CharacterSettingSchemaHint, ...] = (),
        known_characters: tuple[KnownCharacter, ...] = (),
        ordered_context: OrderedExtractionContext | None = None,
        time_context: tuple[NarrativeTimePoint, ...] = (),
    ) -> CharacterSettingExtractionResult: ...


class SubjectResolverApi(Protocol):
    # 구체 entity_name을 얻지 못한 후보를 앞뒤 chunk 문맥으로 해소해 반환한다.
    async def resolve_candidates(
        self,
        context: SubjectResolutionChunkContext,
        candidates: list[ExtractedSettingCandidate],
        known_characters: list[KnownCharacter],
    ) -> SubjectResolutionResult: ...


class WorldSettingExtractorApi(Protocol):
    async def extract_from_chunk(
        self,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
        analysis_context: WorkerAnalysisContext | None = None,
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
        character_fact_comparison_pipeline: CharacterFactComparisonPipeline | None = None,
        world_setting_comparison_pipeline: WorldSettingComparisonPipeline | None = None,
        setting_candidate_service: SettingCandidateService | None = None,
        extraction_model_name: str | None = None,
        subject_resolution_model_name: str | None = None,
        comparison_model_name: str | None = None,
        embedding_generation_enabled: bool = False,
        llm_provider_client: TextGenerationClient | None = None,
        llm_request_semaphore: asyncio.Semaphore | None = None,
        blocking_io_executor: BlockingIoExecutor | None = None,
        heartbeat_interval_seconds: float = 60.0,
        setting_book_chunk_service: SettingBookChunkService | None = None,
        setting_book_chunking_service: SettingBookS3ChunkingService | None = None,
        retrieval_preparer=None,
        retrieval_preparation_enabled: bool = False,
    ) -> None:
        settings = get_settings()
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self._owns_spring_client = spring_client is None
        self._chunking_service = chunking_service
        self._episode_chunk_service = episode_chunk_service
        self._setting_book_chunk_service = setting_book_chunk_service
        self._setting_book_chunking_service = setting_book_chunking_service
        self._episode_chunk_embedding_service = episode_chunk_embedding_service
        self._setting_extractor = setting_extractor
        self._subject_resolver = subject_resolver
        self._world_setting_extractor = world_setting_extractor
        self._character_fact_comparison_pipeline = character_fact_comparison_pipeline
        self._world_setting_comparison_pipeline = world_setting_comparison_pipeline
        self._setting_candidate_service = setting_candidate_service
        self.extraction_model_name = (
            extraction_model_name or settings.effective_llm_extraction_model
        )
        self.subject_resolution_model_name = (
            subject_resolution_model_name or settings.effective_llm_subject_resolution_model
        )
        self.comparison_model_name = (
            comparison_model_name or settings.effective_llm_comparison_model
        )
        self.embedding_generation_enabled = embedding_generation_enabled
        self._retrieval_preparer = retrieval_preparer
        self._retrieval_preparation_enabled = retrieval_preparation_enabled
        self._llm_provider_client = llm_provider_client
        self._owns_llm_provider_client = llm_provider_client is None
        self._embedding_provider_client = None
        self._llm_request_semaphore = llm_request_semaphore or asyncio.Semaphore(
            settings.llm_max_concurrent_requests
        )
        self._blocking_io_executor = blocking_io_executor or BlockingIoExecutor(
            settings.ai_worker_blocking_max_workers
        )
        self._owns_blocking_io_executor = blocking_io_executor is None
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._llm_http_max_retries = settings.llm_http_max_retries
        self._llm_http_retry_base_seconds = settings.llm_http_retry_base_seconds

    async def aclose(self) -> None:
        """프로세스가 소유한 HTTP client와 blocking executor를 종료한다."""

        close_operations = []
        if self._owns_llm_provider_client and self._llm_provider_client is not None:
            close_provider = getattr(self._llm_provider_client, "aclose", None)
            if close_provider is not None:
                close_operations.append(("LLM provider", close_provider))
        if self._embedding_provider_client is not None:
            close_operations.append(("embedding provider", self._embedding_provider_client.aclose))
        if self._owns_spring_client:
            close_spring = getattr(self.spring_client, "aclose", None)
            if close_spring is not None:
                close_operations.append(("Spring client", close_spring))
        if self._owns_blocking_io_executor:
            close_operations.append(("blocking I/O executor", self._blocking_io_executor.aclose))

        first_error: Exception | None = None
        for resource_name, close_operation in close_operations:
            try:
                await close_operation()
            except Exception as exc:
                logger.exception("Failed to close Worker resource. resource=%s", resource_name)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def claim_next(self) -> WorkerAnalysisJobPayload | None:
        """빈 실행 슬롯을 확보한 scheduler가 호출하는 단일 claim 경계."""

        return await self.spring_client.claim(
            allowed_job_types=[AnalysisJobType.SETTING_EXTRACTION],
            model_name=self.extraction_model_name,
            supported_analysis_modes=["CONFIRMED_ONLY", "ORDERED_PROVISIONAL"],
            supported_source_kinds=[AnalysisSourceKind.EPISODE, AnalysisSourceKind.SETTING_BOOK],
            supports_setting_book_reference_time=True,
            supports_semantic_current_context=True,
            supports_frozen_comparison_evidence=True,
        )

    async def run_once(self) -> WorkerRunResult:
        payload = await self.claim_next()
        # 처리할 job이 없으면 아무 작업도 하지 않고 종료
        if payload is None:
            return WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable analysis job does not exist.",
            )

        return await self.process_claimed(payload)

    async def process_claimed(
        self,
        payload: WorkerAnalysisJobPayload,
    ) -> WorkerRunResult:
        try:
            if payload.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL:
                contract = payload.comparison_evidence_contract
                if (not isinstance(contract, dict)
                        or type(contract.get("formatVersion")) is not int
                        or contract["formatVersion"] != 1):
                    raise OrderedInputContextError(
                        "Ordered analysis requires comparisonEvidenceContract formatVersion 1."
                    )
            if payload.job_type != AnalysisJobType.SETTING_EXTRACTION:
                raise ValueError(f"Unsupported analysis job type: {payload.job_type}")
            if payload.batch_id is None or (
                payload.source_kind == AnalysisSourceKind.EPISODE and payload.episode is None
            ) or (payload.source_kind == AnalysisSourceKind.SETTING_BOOK and payload.setting_book is None):
                target_name = "settingBook" if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else "episode"
                raise ValueError(f"Setting-extraction job must include batchId and {target_name}.")
            # claim한 job의 현재 진행 상태를 Spring에 보고
            await self.spring_client.report_progress(
                analysis_job_id=payload.analysis_job_id,
                lease_token=payload.lease_token,
                current_step=_resume_step(payload.checkpoint_stage).value,
                episode_status=_episode_processing_status(payload),
            )
            async with WorkerLeaseHeartbeat(
                self.spring_client,
                payload.analysis_job_id,
                payload.lease_token,
                interval_seconds=self._heartbeat_interval_seconds,
            ) as lease_heartbeat:
                summary = await self._run_analysis_steps(payload)
                lease_heartbeat.raise_if_failed()
                await self.spring_client.report_progress(
                    analysis_job_id=payload.analysis_job_id,
                    lease_token=payload.lease_token,
                    current_step=AnalysisStep.PERSISTING.value,
                    episode_status=_episode_processing_status(payload),
                )
                lease_heartbeat.raise_if_failed()
            # 분석이 성공하면 Spring에 완료 상태와 요약 정보를 보고
            await self.spring_client.complete(
                analysis_job_id=payload.analysis_job_id,
                lease_token=payload.lease_token,
                summary_json=summary.summary_json,
            )
        except Exception as exc:
            try:
                await self.spring_client.fail(
                    analysis_job_id=payload.analysis_job_id,
                    lease_token=payload.lease_token,
                    error_message=self._error_message(exc),
                    failure_code=analysis_failure_code(exc),
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
            episode_count=0 if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else 1,
        )

    async def _run_analysis_steps(
        self,
        payload: WorkerAnalysisJobPayload,
    ) -> WorkerRunSummary:
        checkpoint = payload.checkpoint_stage
        semantic_current_context = (
            prepare_semantic_current_context(
                payload.semantic_current_context,
                analysis_context=payload.analysis_context,
                known_characters=payload.known_characters,
                provisional_characters=payload.provisional_characters,
                source_hash=payload.frozen_source().content_hash,
            ) if payload.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL else None
        )
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

        chunks, embedding_metrics = await self._run_chunk_stage(payload, checkpoint)
        known_characters = [
            KnownCharacter(
                character_id=character.character_id,
                name=character.name,
                active_statuses=tuple(
                    ActiveCharacterStatus(
                        fact_key=status.fact_key,
                        fact_value=status.fact_value,
                    )
                    for status in character.active_statuses
                ),
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
        character_metrics = await self._run_character_stage(
            payload,
            chunks,
            checkpoint,
            known_characters,
            schema_hints,
            semantic_current_context,
        )
        character_comparison_result = await self._run_character_comparison_stage(
            payload,
            checkpoint,
        )
        world_candidate_count = await self._run_world_extraction_stage(
            payload,
            chunks,
            checkpoint,
            semantic_current_context,
        )
        comparison_result = await self._run_world_comparison_stage(payload, checkpoint)

        summary_json = json.dumps(
            {
                "episodeCount": 0 if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else 1,
                **({"settingBookCount": 1, "sourceKind": "SETTING_BOOK"}
                   if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
                **({"analysisMode": payload.analysis_mode,
                    "inputStateHash": payload.analysis_context.input_state_hash,
                    "contextPolicyVersion": payload.analysis_context.format_version,
                    "provisionalCharacterCount": len(payload.provisional_characters)}
                   if payload.analysis_context is not None else {}),
                "chunkCount": len(chunks),
                **embedding_metrics,
                **character_metrics,
                "characterFactComparisonCompletedCount": (
                    character_comparison_result.completed_count
                ),
                "characterFactComparisonFailedCount": character_comparison_result.failed_count,
                **character_comparison_result.summary_metrics(),
                "worldSettingCandidateCount": world_candidate_count,
                "worldSettingComparisonCompletedCount": comparison_result.completed_count,
                "worldSettingComparisonFailedCount": comparison_result.failed_count,
                **comparison_result.summary_metrics(),
            },
            ensure_ascii=False,
        )
        return WorkerRunSummary(summary_json=summary_json)

    async def _run_chunk_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        checkpoint: AnalysisJobCheckpointStage | None,
    ) -> tuple[list[EpisodeChunk | SettingBookChunk], dict[str, int]]:
        if payload.source_kind == AnalysisSourceKind.SETTING_BOOK:
            book = payload.setting_book
            if _checkpoint_reached(checkpoint, AnalysisJobCheckpointStage.CHUNKS_READY):
                chunks = await self._blocking_io_executor.run(
                    self._get_setting_book_chunking_service().get_chunks_from_s3_content,
                    analysis_job_id=payload.analysis_job_id, setting_book_revision_id=book.revision_id,
                    content_s3_key=book.content_s3_key,
                    work_id=payload.work_id, ordered_write_context=_ordered_write_context(payload),
                )
                if not chunks:
                    raise ValueError("CHUNKS_READY checkpoint has no stored setting-book chunks.")
                skipped = 0
            else:
                chunks = await self._blocking_io_executor.run(
                    self._get_setting_book_chunking_service().replace_chunks_from_s3_content,
                    analysis_job_id=payload.analysis_job_id, work_id=payload.work_id,
                    setting_book_revision_id=book.revision_id, content_s3_key=book.content_s3_key,
                    ordered_write_context=_ordered_write_context(payload),
                )
                if not chunks:
                    raise ValueError("Setting-book source has no analyzable chunks.")
                skipped = len(chunks)
                await self.spring_client.report_progress(
                    payload.analysis_job_id, payload.lease_token, AnalysisStep.SETTING_EXTRACTION,
                    None, AnalysisJobCheckpointStage.CHUNKS_READY,
                )
            return chunks, {"embeddedChunkCount": 0, "embeddingFailedChunkCount": 0,
                            "embeddingSkippedChunkCount": skipped}
        episode = payload.episode
        # Search preparation has its own durable MISSING/READY state, so it also
        # repairs a resumed analysis without deleting its existing analysis chunks.
        if self._retrieval_preparation_enabled:
            await self._prepare_retrieval(payload)
        if _checkpoint_reached(checkpoint, AnalysisJobCheckpointStage.CHUNKS_READY):
            chunks = await self._blocking_io_executor.run(
                self._get_episode_chunk_service().get_episode_chunks,
                episode.episode_id,
            )
            if not chunks:
                raise ValueError("CHUNKS_READY checkpoint has no stored episode chunks.")
            return chunks, {
                "embeddedChunkCount": 0,
                "embeddingFailedChunkCount": 0,
                "embeddingSkippedChunkCount": 0,
            }

        chunks = await self._blocking_io_executor.run(
            self._get_chunking_service().replace_chunks_from_s3_content,
            episode_id=episode.episode_id,
            content_s3_key=episode.content_s3_key,
            **({"ordered_write_context": OrderedCandidateWriteContext(
                lease_token=payload.lease_token, context=payload.analysis_context,
                episode_id=episode.episode_id, content_s3_key=episode.content_s3_key,
                content_s3_version=episode.content_s3_version, episode_no=episode.episode_no),
                "analysis_job_id": payload.analysis_job_id, "work_id": payload.work_id}
               if payload.analysis_context is not None else {}),
        )
        embedded_count = 0
        failed_count = 0
        skipped_count = 0
        if self.embedding_generation_enabled:
            try:
                result = await self._get_episode_chunk_embedding_service(
                    payload.analysis_job_id,
                    payload.lease_token,
                ).embed_chunks(chunks,
                    **({"ordered_write_context": OrderedCandidateWriteContext(
                        lease_token=payload.lease_token, context=payload.analysis_context,
                        episode_id=episode.episode_id, content_s3_key=episode.content_s3_key,
                        content_s3_version=episode.content_s3_version,
                        episode_no=episode.episode_no),
                        "analysis_job_id": payload.analysis_job_id, "work_id": payload.work_id}
                       if payload.analysis_context is not None else {}))
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
        await self.spring_client.report_progress(
            payload.analysis_job_id,
            payload.lease_token,
            AnalysisStep.SETTING_EXTRACTION,
            _episode_processing_status(payload),
            AnalysisJobCheckpointStage.CHUNKS_READY,
        )
        return chunks, {
            "embeddedChunkCount": embedded_count,
            "embeddingFailedChunkCount": failed_count,
            "embeddingSkippedChunkCount": skipped_count,
        }

    async def _prepare_retrieval(self, payload):
        from app.retrieval.contracts import IndexSpec, Scope
        from app.retrieval.service import RetrievalIndexService
        from app.retrieval.store import RetrievalStore

        try:
            if self._retrieval_preparer is None:
                settings = get_settings()
                self._retrieval_preparer = RetrievalIndexService(
                    RetrievalStore(get_session_maker()), S3TextObjectStorage.from_settings(settings),
                    IndexSpec(settings.embedding_model, settings.embedding_version, settings.embedding_dimensions),
                )
            await self._blocking_io_executor.run(
                self._retrieval_preparer.prepare,
                Scope(payload.work_id, (payload.episode.episode_id,), max_sources=1),
            )
        except Exception:
            # Search preparation does not change analysis checkpoints or billing.
            # Operator readiness/dry-run observes the missing/stale source for retry.
            logger.warning("Retrieval preparation failed; bounded index execution can retry")

    async def _run_character_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        chunks: list[EpisodeChunk | SettingBookChunk],
        checkpoint: AnalysisJobCheckpointStage | None,
        known_characters: list[KnownCharacter],
        schema_hints: tuple[CharacterSettingSchemaHint, ...],
        semantic_current_context: SemanticCurrentInput | None = None,
    ) -> dict[str, int]:
        status_context_character_count = sum(
            bool(character.active_statuses) for character in known_characters
        )
        status_context_entry_count = sum(
            len(character.active_statuses) for character in known_characters
        )
        if payload.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL:
            status_context_character_count += sum(bool(item.active_statuses)
                                                  for item in payload.provisional_characters)
            status_context_entry_count += sum(len(item.active_statuses)
                                              for item in payload.provisional_characters)
        if _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.CHARACTER_CANDIDATES_SAVED,
        ):
            return {
                "candidateCount": 0,
                "subjectFallbackCallCount": 0,
                "subjectFallbackResolvedCount": 0,
                "subjectFallbackUnresolvedCount": 0,
                "statusContextCharacterCount": status_context_character_count,
                "statusContextEntryCount": status_context_entry_count,
                "statusInactiveCandidateCount": 0,
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
        status_inactive_candidate_count = 0
        episode = payload.episode
        ordered_context = (
            OrderedExtractionContext(
                analysis_context=payload.analysis_context,
                known_characters=tuple(payload.known_characters),
                provisional_characters=tuple(payload.provisional_characters),
            )
            if payload.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL else None
        )
        ordered_subjects = initial_ordered_subjects(ordered_context) if ordered_context else []
        time_context: list[NarrativeTimePoint] = []
        for index, chunk in enumerate(chunks):
            extraction_result = await setting_extractor.extract_from_chunk(
                source_chunk_id=chunk.id,
                chunk_text=chunk.chunk_text,
                analysis_job_id=payload.analysis_job_id,
                **_source_extraction_arguments(payload),
                schema_hints=schema_hints,
                known_characters=tuple(known_characters),
                **({"ordered_context": ordered_context} if ordered_context else {}),
                **({"semantic_current_context": semantic_current_context}
                   if semantic_current_context is not None else {}),
                **({"time_context": tuple(time_context)} if time_context else {}),
            )
            extraction_result = ground_extraction_narrative_time(
                extraction_result, chunk=chunk, time_context=time_context,
            )
            for point in extraction_result.time_context:
                if point not in time_context:
                    time_context.append(point)
            resolved_candidates = resolve_candidate_evidence_offsets(
                candidates=extraction_result.candidates,
                chunk_text=chunk.chunk_text,
                chunk_start_offset=chunk.start_offset,
            )
            if ordered_context is not None:
                subject_metrics = OrderedSubjectResolutionMetrics()
                resolved, discovered = await resolve_ordered_character_subjects(
                    client=subject_resolver.llm_client,
                    model=subject_resolver.model,
                    max_attempts=get_settings().llm_extraction_max_attempts,
                    max_output_tokens=subject_resolver.max_output_tokens,
                    context=SubjectResolutionChunkContext(
                        previous_chunk_text=chunks[index - 1].chunk_text if index > 0 else None,
                        current_chunk_text=chunk.chunk_text,
                        next_chunk_text=(chunks[index + 1].chunk_text
                                         if index + 1 < len(chunks) else None),
                    ),
                    candidates=resolved_candidates,
                    subjects=ordered_subjects,
                    episode_no=episode.episode_no if payload.source_kind == AnalysisSourceKind.EPISODE else None,
                    **({"analysis_source": payload.frozen_source()}
                       if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
                    unresolved_references=tuple(payload.analysis_context.unresolved_references),
                    metrics=subject_metrics,
                    **({"continue_on_candidate_failure": True}
                       if payload.review_mode == AnalysisReviewMode.AUTOMATIC else {}),
                )
                merge_ordered_subjects(ordered_subjects, discovered)
                fallback_calls += subject_metrics.llm_call_count
                fallback_resolved += subject_metrics.llm_resolved_count
                fallback_unresolved += subject_metrics.llm_unresolved_count
                status_inactive_candidate_count += sum(
                    _is_explicit_inactive_status_candidate(item.candidate) for item in resolved
                )
                save_items.extend(
                    SettingCandidateSaveItem(
                        episode_id=episode.episode_id if payload.source_kind == AnalysisSourceKind.EPISODE else None,
                        source_content_s3_key=(payload.setting_book.content_s3_key
                                               if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else episode.content_s3_key),
                        candidate=item.candidate,
                        ordered_binding=item.binding,
                        **({"source_kind": AnalysisSourceKind.SETTING_BOOK,
                            "setting_book_id": payload.setting_book.setting_book_id,
                            "setting_book_revision_id": payload.setting_book.revision_id}
                           if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
                    ) for item in resolved
                )
                continue
            resolution = await subject_resolver.resolve_candidates(
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
            status_inactive_candidate_count += sum(
                _is_explicit_inactive_status_candidate(candidate)
                for candidate in resolution.candidates
            )
            save_items.extend(
                SettingCandidateSaveItem(
                    episode_id=episode.episode_id,
                    source_content_s3_key=episode.content_s3_key,
                    candidate=candidate,
                    source_content_version=episode.content_s3_version,
                )
                for candidate in resolution.candidates
            )

        # Validate same-name partitions over the whole immutable BOOK origin.
        # A chunk-local group can omit another discovery or split a parent profile.
        if (ordered_context is not None and payload.source_kind == AnalysisSourceKind.SETTING_BOOK
                and has_book_identity_collision([item.candidate for item in save_items],
                                                initial_ordered_subjects(ordered_context))):
            raw_text = await self._blocking_io_executor.run(
                self._get_setting_book_chunking_service().read_frozen_source,
                setting_book_revision_id=payload.setting_book.revision_id,
                content_s3_key=payload.setting_book.content_s3_key,
                ordered_write_context=_ordered_write_context(payload),
            )
            identity_context = BookIdentityContext(payload.analysis_job_id, payload.frozen_source(), raw_text)
            subject_metrics = OrderedSubjectResolutionMetrics()
            resolved, _ = await resolve_ordered_character_subjects(
                client=subject_resolver.llm_client, model=subject_resolver.model,
                max_attempts=get_settings().llm_extraction_max_attempts,
                max_output_tokens=max(subject_resolver.max_output_tokens,
                                      get_settings().llm_setting_extraction_max_output_tokens),
                truncation_retry_max_output_tokens=get_settings().llm_setting_extraction_retry_max_output_tokens,
                context=SubjectResolutionChunkContext(None, raw_text, None),
                candidates=[item.candidate for item in save_items],
                candidate_ids=[item.ordered_binding.candidate_id for item in save_items],
                subjects=initial_ordered_subjects(ordered_context), episode_no=None,
                analysis_source=payload.frozen_source(), book_identity_context=identity_context,
                unresolved_references=tuple(payload.analysis_context.unresolved_references),
                metrics=subject_metrics,
                continue_on_candidate_failure=payload.review_mode == AnalysisReviewMode.AUTOMATIC,
            )
            save_items = [replace(item, candidate=updated.candidate, ordered_binding=updated.binding)
                          for item, updated in zip(save_items, resolved, strict=True)]
            fallback_calls += subject_metrics.llm_call_count
            fallback_resolved += subject_metrics.llm_resolved_count
            fallback_unresolved += subject_metrics.llm_unresolved_count

        saved_candidates = await self._blocking_io_executor.run(
            self._get_setting_candidate_service().replace_candidates_for_analysis_job,
            work_id=payload.work_id,
            analysis_job_id=payload.analysis_job_id,
            save_items=save_items,
            known_characters=known_characters,
            **({"ordered_write_context": _ordered_write_context(payload)} if ordered_context else {}),
        )
        await self.spring_client.report_progress(
            payload.analysis_job_id,
            payload.lease_token,
            AnalysisStep.CHARACTER_FACT_COMPARISON,
            _episode_processing_status(payload),
            AnalysisJobCheckpointStage.CHARACTER_CANDIDATES_SAVED,
        )
        return {
            "candidateCount": len(saved_candidates),
            "subjectFallbackCallCount": fallback_calls,
            "subjectFallbackResolvedCount": fallback_resolved,
            "subjectFallbackUnresolvedCount": fallback_unresolved,
            "statusContextCharacterCount": status_context_character_count,
            "statusContextEntryCount": status_context_entry_count,
            # active=false가 wire 값에 명시된 1차 후보만 세는 lower-bound다.
            # 포괄적 회복 후보처럼 2차에서 REMOVE가 될 모든 후보 수를 뜻하지 않는다.
            "statusInactiveCandidateCount": status_inactive_candidate_count,
        }

    async def _run_character_comparison_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        checkpoint: AnalysisJobCheckpointStage | None,
    ) -> CharacterFactComparisonRunResult:
        if payload.analysis_mode != AnalysisMode.ORDERED_PROVISIONAL:
            if not _checkpoint_reached(
                checkpoint, AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_HANDED_OFF
            ):
                await self.spring_client.report_progress(
                    payload.analysis_job_id,
                    payload.lease_token,
                    AnalysisStep.WORLD_SETTING_EXTRACTION,
                    _episode_processing_status(payload),
                    AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_HANDED_OFF,
                )
            return CharacterFactComparisonRunResult(0, 0)

        finished = _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_FINISHED,
        )
        result = await self._get_character_fact_comparison_pipeline(
            payload.analysis_job_id,
            payload.lease_token,
        ).process_all(payload.analysis_job_id, payload.lease_token,
                      **({"analysis_context": payload.analysis_context}
                         if payload.analysis_context is not None else {}),
                      **({"analysis_source": payload.frozen_source()}
                         if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
                      **({"continue_on_candidate_failure": True}
                         if payload.review_mode == AnalysisReviewMode.AUTOMATIC else {}))
        if payload.analysis_context is not None and result.failed_count and (
            payload.review_mode != AnalysisReviewMode.AUTOMATIC
            or result.first_failure_code not in COMPARISON_CANDIDATE_FAILURE_CODES
        ):
            raise OrderedAnalysisIncompleteError(
                result.first_failure_code or AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
            )
        # 개별 후보의 실패는 fail endpoint에 기록됐으므로 세계관 단계는 계속 수행한다.
        if not finished:
            await self.spring_client.report_progress(
                payload.analysis_job_id,
                payload.lease_token,
                AnalysisStep.WORLD_SETTING_EXTRACTION,
                _episode_processing_status(payload),
                AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_FINISHED,
            )
        return result

    async def _run_world_extraction_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        chunks: list[EpisodeChunk | SettingBookChunk],
        checkpoint: AnalysisJobCheckpointStage | None,
        semantic_current_context: SemanticCurrentInput | None = None,
    ) -> int:
        if _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.WORLD_CANDIDATES_PUBLISHED,
        ):
            return 0

        if _checkpoint_reached(checkpoint, AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_FINISHED):
            await self.spring_client.report_progress(
                payload.analysis_job_id, payload.lease_token,
                AnalysisStep.WORLD_SETTING_EXTRACTION, _episode_processing_status(payload),
            )

        extractor = self._get_world_setting_extractor(
            payload.analysis_job_id,
            payload.lease_token,
        )
        extracted_items: list[WorkerWorldSettingCandidatePublishItem] = []
        for chunk in chunks:
            extraction_result = await extractor.extract_from_chunk(
                chunk_text=chunk.chunk_text,
                **_source_extraction_arguments(payload),
                **({"analysis_context": payload.analysis_context}
                   if payload.analysis_context is not None else {}),
                **({"semantic_current_context": semantic_current_context}
                   if semantic_current_context is not None else {}),
            )
            extracted_items.extend(
                WorldSettingCandidateMapper.to_publish_item(candidate, chunk)
                for candidate in extraction_result.candidates
            )
        candidates = (
            extracted_items if payload.analysis_context is not None
            else WorldSettingCandidateMapper.consolidate_by_key(extracted_items)
        )
        published = await self.spring_client.publish_world_setting_candidates(
            payload.analysis_job_id,
            payload.lease_token,
            candidates,
        )
        return len(published)

    async def _run_world_comparison_stage(
        self,
        payload: WorkerAnalysisJobPayload,
        checkpoint: AnalysisJobCheckpointStage | None,
    ) -> WorldSettingComparisonRunResult:
        finished = _checkpoint_reached(
            checkpoint,
            AnalysisJobCheckpointStage.WORLD_COMPARISONS_FINISHED,
        )
        if finished and payload.analysis_mode != AnalysisMode.ORDERED_PROVISIONAL:
            return WorldSettingComparisonRunResult(0, 0)

        if not finished and payload.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL and _checkpoint_reached(
            checkpoint, AnalysisJobCheckpointStage.WORLD_CANDIDATES_PUBLISHED,
        ):
            await self.spring_client.report_progress(
                payload.analysis_job_id, payload.lease_token,
                AnalysisStep.WORLD_SETTING_COMPARISON, _episode_processing_status(payload),
            )

        result = await self._get_world_setting_comparison_pipeline(
            payload.analysis_job_id,
            payload.lease_token,
        ).process_all(payload.analysis_job_id, payload.lease_token,
                      **({"analysis_context": payload.analysis_context}
                         if payload.analysis_context is not None else {}),
                      **({"analysis_source": payload.frozen_source()}
                         if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else {}),
                      **({"continue_on_candidate_failure": True}
                         if payload.review_mode == AnalysisReviewMode.AUTOMATIC else {}))
        if payload.analysis_context is not None and result.failed_count and (
            payload.review_mode != AnalysisReviewMode.AUTOMATIC
            or result.first_failure_code not in COMPARISON_CANDIDATE_FAILURE_CODES
        ):
            raise OrderedAnalysisIncompleteError(
                result.first_failure_code or AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
            )
        if not finished:
            await self.spring_client.report_progress(
                payload.analysis_job_id,
                payload.lease_token,
                AnalysisStep.WORLD_SETTING_COMPARISON,
                _episode_processing_status(payload),
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

    def _get_setting_book_chunk_service(self) -> SettingBookChunkService:
        if self._setting_book_chunk_service is None:
            self._setting_book_chunk_service = SettingBookChunkService(get_session_maker())
        return self._setting_book_chunk_service

    def _get_setting_book_chunking_service(self) -> SettingBookS3ChunkingService:
        if self._setting_book_chunking_service is None:
            self._setting_book_chunking_service = SettingBookS3ChunkingService(
                S3TextObjectStorage.from_settings(), self._get_setting_book_chunk_service())
        return self._setting_book_chunking_service

    # 저장된 청크의 벡터를 생성하고 episode_chunks에 반영할 서비스를 초기화한다.
    def _get_episode_chunk_embedding_service(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> EpisodeChunkEmbeddingApi:
        if self._episode_chunk_embedding_service is not None:
            return self._episode_chunk_embedding_service

        settings = get_settings()
        if self._embedding_provider_client is None:
            self._embedding_provider_client = OpenAIEmbeddingsClient.from_settings(settings)
        metered_client = MeteredEmbeddingClient(
            delegate=self._embedding_provider_client
            or OpenAIEmbeddingsClient.from_settings(settings),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            model_name=settings.embedding_model,
            lease_token=lease_token,
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
        )
        return EpisodeChunkEmbeddingService(
            session_factory=get_session_maker(),
            embedding_client=metered_client,
            blocking_runner=self._blocking_io_executor.run,
        )

    # llm에 넣을 프롬프트와 api호출을 할 서비스(CharacterSettingExtractor)를 초기화 하는 작업만 한다.
    def _get_setting_extractor(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> SettingExtractorApi:
        if self._setting_extractor is not None:
            return self._setting_extractor

        model_name = self.extraction_model_name
        metered_client = MeteredTextGenerationClient(
            delegate=self._get_llm_provider_client(),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            purpose="SETTING_EXTRACTION",
            default_model=model_name,
            lease_token=lease_token,
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
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

        model_name = self.subject_resolution_model_name
        metered_client = MeteredTextGenerationClient(
            delegate=self._get_llm_provider_client(),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            purpose="SUBJECT_RESOLUTION",
            default_model=model_name,
            lease_token=lease_token,
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
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

        model_name = self.extraction_model_name
        metered_client = MeteredTextGenerationClient(
            delegate=self._get_llm_provider_client(),
            ledger=self.spring_client,
            analysis_job_id=analysis_job_id,
            purpose="WORLD_SETTING_EXTRACTION",
            default_model=model_name,
            lease_token=lease_token,
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
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
            subject_resolution_model_name=self.subject_resolution_model_name,
            comparison_model_name=self.comparison_model_name,
            provider_client=self._get_llm_provider_client(),
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
        )

    def _get_character_fact_comparison_pipeline(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> CharacterFactComparisonPipeline:
        if self._character_fact_comparison_pipeline is not None:
            return self._character_fact_comparison_pipeline

        return create_character_fact_comparison_pipeline(
            spring_client=self.spring_client,
            analysis_job_id=analysis_job_id,
            lease_token=lease_token,
            comparison_model_name=self.comparison_model_name,
            provider_client=self._get_llm_provider_client(),
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
        )

    # 검증된 설정 후보를 setting_candidates 테이블에 저장할 서비스를 필요할 때 초기화한다.
    def _get_setting_candidate_service(self) -> SettingCandidateService:
        if self._setting_candidate_service is None:
            self._setting_candidate_service = SettingCandidateService(
                session_factory=get_session_maker(),
            )
        return self._setting_candidate_service

    def _get_llm_provider_client(self) -> TextGenerationClient:
        if self._llm_provider_client is None:
            self._llm_provider_client = OpenAIResponsesClient.from_settings()
        return self._llm_provider_client


def _resume_step(checkpoint: AnalysisJobCheckpointStage | None) -> AnalysisStep:
    return {
        None: AnalysisStep.SETTING_EXTRACTION,
        AnalysisJobCheckpointStage.CHUNKS_READY: AnalysisStep.SETTING_EXTRACTION,
        AnalysisJobCheckpointStage.CHARACTER_CANDIDATES_SAVED: AnalysisStep.CHARACTER_FACT_COMPARISON,
        AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_FINISHED: AnalysisStep.WORLD_SETTING_EXTRACTION,
        AnalysisJobCheckpointStage.CHARACTER_COMPARISONS_HANDED_OFF: AnalysisStep.WORLD_SETTING_EXTRACTION,
        AnalysisJobCheckpointStage.WORLD_CANDIDATES_PUBLISHED: AnalysisStep.WORLD_SETTING_COMPARISON,
        AnalysisJobCheckpointStage.WORLD_COMPARISONS_FINISHED: AnalysisStep.PERSISTING,
    }[checkpoint]


def _checkpoint_reached(
    current: AnalysisJobCheckpointStage | None,
    target: AnalysisJobCheckpointStage,
) -> bool:
    if current is None:
        return False
    checkpoints = list(AnalysisJobCheckpointStage)
    return checkpoints.index(current) >= checkpoints.index(target)


def _is_explicit_inactive_status_candidate(candidate: ExtractedSettingCandidate) -> bool:
    """wire JSON에 boolean active=false가 명시된 STATUS 후보만 센다.

    이 값은 관측용 lower-bound다. 치료 후 기능 회복처럼 별도 active 필드 없이도
    2차 비교에서 REMOVE가 될 수 있는 후보는 의도적으로 포함하지 않는다.
    """

    return (
        candidate.candidate_kind == "SETTING"
        and candidate.attribute_name is not None
        and candidate.attribute_name.startswith("status.")
        and isinstance(candidate.value_json, dict)
        and candidate.value_json.get("active") is False
    )


def _episode_processing_status(payload):
    return None if payload.source_kind == AnalysisSourceKind.SETTING_BOOK else EpisodeProcessingStatus.ANALYZING


def _source_extraction_arguments(payload):
    if payload.source_kind == AnalysisSourceKind.SETTING_BOOK:
        return {"setting_book": payload.setting_book}
    return {"episode_no": payload.episode.episode_no, "episode_title": payload.episode.title}


def _ordered_write_context(payload):
    if payload.analysis_context is None:
        return None
    if payload.source_kind == AnalysisSourceKind.SETTING_BOOK:
        book = payload.setting_book
        return OrderedCandidateWriteContext(
            payload.lease_token, payload.analysis_context, None, book.content_s3_key,
            book.content_s3_version, None, source_kind=AnalysisSourceKind.SETTING_BOOK,
            setting_book_id=book.setting_book_id, setting_book_revision_id=book.revision_id,
            char_count=book.char_count,
            reference_time_kind=book.reference_time_kind, reference_episode_no=book.reference_episode_no,
        )
    episode = payload.episode
    return OrderedCandidateWriteContext(
        payload.lease_token, payload.analysis_context, episode.episode_id,
        episode.content_s3_key, episode.content_s3_version, episode.episode_no,
    )
