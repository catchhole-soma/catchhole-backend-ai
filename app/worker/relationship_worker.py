"""Relationship-only jobs: no core candidates, chunk persistence, retrieval or embedding."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid5

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.relationship_comparator import RelationshipComparator
from app.analysis.relationship_extractor import DEFAULT_RELATIONSHIP_MODEL, RelationshipExtractor
from app.analysis.relationship_pipeline import RelationshipAnalysisPipeline
from app.chunking.chunk_splitter import split_episode_into_chunks
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import get_settings
from app.domain.enums import AnalysisJobType, AnalysisMode, AnalysisSourceKind
from app.exceptions.failure_classification import analysis_failure_code, failure_diagnostics
from app.llm.openai_client import OpenAIResponsesClient
from app.monitoring.worker_tracing import worker_span
from app.schemas.worker import WorkerAnalysisJobPayload
from app.storage.s3 import S3TextObjectStorage, SourceObjectTooLargeError
from app.usage.metering import MeteredTextGenerationClient
from app.worker.analysis_job_worker import WorkerRunResult
from app.worker.lease_heartbeat import WorkerLeaseHeartbeat

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelationshipSourceChunk:
    id: UUID
    chunk_text: str
    start_offset: int


class RelationshipWorker:
    """Server-owned campaign/cursor selection; one relationship request at a time."""

    def __init__(
        self,
        *,
        spring_client=None,
        storage=None,
        llm_provider_client=None,
        pipeline_factory=None,
        # The dedicated server lease is 60s; renew well before its expiry.
        heartbeat_interval_seconds=20.0,
        max_source_bytes=None,
        max_chunks=None,
    ):
        settings = get_settings()
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self._owns_spring_client = spring_client is None
        self._storage = storage
        self._llm_provider_client = llm_provider_client
        self._owns_llm_provider_client = llm_provider_client is None
        self._pipeline_factory = pipeline_factory
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._request_semaphore = asyncio.Semaphore(1)
        self._max_source_bytes = (
            max_source_bytes
            if max_source_bytes is not None
            else settings.relationship_worker_max_source_bytes
        )
        self._max_chunks = (
            max_chunks if max_chunks is not None else settings.relationship_worker_max_chunks
        )
        if any(
            type(value) is not int or value < 1
            for value in (self._max_source_bytes, self._max_chunks)
        ):
            raise ValueError("Relationship source limits must be positive integers.")

    async def aclose(self):
        first_error = None
        for resource, owned in (
            (self._llm_provider_client, self._owns_llm_provider_client),
            (self.spring_client, self._owns_spring_client),
        ):
            close = getattr(resource, "aclose", None)
            if not owned or close is None:
                continue
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - finish closing owned resources, then propagate
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def claim_next(self):
        return await self.spring_client.claim_relationship_job()

    async def heartbeat(self, analysis_job_id, lease_token):
        return await self.spring_client.heartbeat_relationship_job(analysis_job_id, lease_token)

    async def process_claimed(self, payload: WorkerAnalysisJobPayload):
        try:
            self._validate_payload(payload)
            async with WorkerLeaseHeartbeat(
                self,
                payload.analysis_job_id,
                payload.lease_token,
                interval_seconds=self._heartbeat_interval_seconds,
            ) as heartbeat:
                pipeline = self._pipeline(payload)
                await pipeline.load()
                chunks = []
                # Frozen observations/decisions are the resume checkpoint. Do not read
                # the manuscript or repeat any extraction after PREPARED.
                if pipeline.context is None:
                    with worker_span("analysis.relationship.source"):
                        try:
                            chunks = await asyncio.to_thread(self._read_chunks, payload)
                        except SourceObjectTooLargeError:
                            pipeline.failure_code = "INPUT_TOO_LARGE"
                    if len(chunks) > self._max_chunks:
                        pipeline.failure_code = "INPUT_TOO_LARGE"
                        chunks = []
                heartbeat.raise_if_failed()
                with worker_span("analysis.relationship.process"):
                    summary = await pipeline.finish(chunks)
                if summary.get("relationshipAnalysisStatus") == "FAILED":
                    raise ComparisonValidationError("Relationship analysis did not complete.")
                heartbeat.raise_if_failed()
            # This endpoint publishes only the relation generation/cursor. It never
            # completes the original core analysis or changes Episode processing state.
            await self.spring_client.complete_relationship_job(
                payload.analysis_job_id,
                payload.lease_token,
                summary_json=json.dumps(summary),
            )
        except Exception as exc:
            try:
                await self.spring_client.fail_relationship_job(
                    payload.analysis_job_id,
                    payload.lease_token,
                    error_message="Relationship-only analysis failed.",
                    failure_code=analysis_failure_code(exc),
                    failure_diagnostics=failure_diagnostics(exc, stage="EXTRACTION"),
                )
            except Exception:  # noqa: BLE001 - retain the original failure if reporting fails
                # Do not emit the source/provider/transport exception body.
                logger.warning("Failed to report relationship-only job failure.")
            raise
        return WorkerRunResult(
            claimed=True,
            analysis_job_id=payload.analysis_job_id,
            work_id=payload.work_id,
            message="Relationship-only job completed.",
        )

    @staticmethod
    def _validate_payload(payload):
        contract = payload.relationship_analysis_contract
        episode = payload.episode
        if (
            payload.job_type != AnalysisJobType.RELATIONSHIP_EXTRACTION
            or payload.source_kind != AnalysisSourceKind.EPISODE
            or payload.analysis_mode != AnalysisMode.CONFIRMED_ONLY
            or episode is None
            or episode.episode_no < 1
            or not episode.content_s3_key.strip()
            or not isinstance(episode.content_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", episode.content_hash)
            or not isinstance(contract, dict)
            or type(contract.get("formatVersion")) is not int
            or contract["formatVersion"] != 1
        ):
            raise OrderedInputContextError("Invalid relationship-only job contract.")

    def _read_chunks(self, payload):
        if self._storage is None:
            self._storage = S3TextObjectStorage.from_settings()
        episode = payload.episode
        raw_text = self._storage.get_text(
            episode.content_s3_key,
            version_id=episode.content_s3_version,
            max_bytes=self._max_source_bytes,
        )
        if sha256(raw_text.encode("utf-8")).hexdigest() != episode.content_hash:
            raise OrderedInputContextError("Relationship source content hash changed.")
        return [
            RelationshipSourceChunk(
                id=uuid5(
                    payload.analysis_job_id,
                    f"{episode.content_hash}:{draft.start_offset}:{draft.end_offset}",
                ),
                chunk_text=draft.chunk_text,
                start_offset=draft.start_offset,
            )
            for draft in split_episode_into_chunks(raw_text)
        ]

    def _pipeline(self, payload):
        if self._pipeline_factory is not None:
            return self._pipeline_factory(payload)
        if self._llm_provider_client is None:
            self._llm_provider_client = OpenAIResponsesClient.from_settings()
        settings = get_settings()

        def client(purpose):
            return MeteredTextGenerationClient(
                delegate=self._llm_provider_client,
                ledger=self.spring_client,
                analysis_job_id=payload.analysis_job_id,
                lease_token=payload.lease_token,
                purpose=purpose,
                default_model=DEFAULT_RELATIONSHIP_MODEL,
                request_semaphore=self._request_semaphore,
                max_retries=0,
                retry_base_seconds=settings.llm_http_retry_base_seconds,
            )

        return RelationshipAnalysisPipeline(
            spring_client=self.spring_client,
            payload=payload,
            extractor=RelationshipExtractor(llm_client=client("RELATIONSHIP_EXTRACTION")),
            comparator=RelationshipComparator(llm_client=client("RELATIONSHIP_COMPARISON")),
        )
