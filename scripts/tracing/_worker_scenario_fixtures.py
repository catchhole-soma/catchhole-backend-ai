"""Synthetic I/O fixtures for the real Worker tracing verification.

No production client, database, object store, or model endpoint is constructed.
The extraction adapters intentionally return no candidates: this verifies execution
and telemetry, not model quality, business persistence, or lease recovery.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.schemas import CharacterSettingExtractionResult
from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.llm.responses import LlmTextResponse
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import WorkerAnalysisJobPayload
from app.usage.metering import MeteredTextGenerationClient
from app.worker.analysis_job_worker import AnalysisJobWorker

MODEL = "synthetic-tracing-model"
CANARY = "SYNTHETIC_PRIVATE_CONTENT_NOT_FOR_TELEMETRY"
LEDGER_DELAY_SECONDS = 0.02


class SyntheticProvider:
    def __init__(self, delay_seconds=0.03, *, retry_once=False, release=None):
        self.delay_seconds = delay_seconds
        self.retry_once = retry_once
        self.release = release
        self.started = asyncio.Event()
        self.call_count = 0

    async def create_text_response(self, **kwargs):
        self.call_count += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        await asyncio.sleep(self.delay_seconds)
        if self.retry_once and self.call_count == 1:
            request = httpx.Request("POST", "https://synthetic-provider.invalid")
            raise httpx.HTTPStatusError(
                CANARY, request=request,
                response=httpx.Response(429, request=request, json={
                    "error": {"code": "rate_limit_exceeded"},
                }),
            )
        return LlmTextResponse(CANARY, input_token_count=10, output_token_count=5)


class SyntheticSpring:
    """Local fake for progress and the token ledger, with an optional settle gate."""

    def __init__(self, *, hold_settlement=False):
        self.reservations = 0
        self.settlements = 0
        self.releases = 0
        self.completed = 0
        self.failed = 0
        self.settlement_started = asyncio.Event()
        self.allow_settlement = asyncio.Event()
        if not hold_settlement:
            self.allow_settlement.set()

    async def report_progress(self, *args, **kwargs):
        pass

    async def heartbeat(self, *args, **kwargs):
        pass

    async def complete(self, *args, **kwargs):
        self.completed += 1

    async def fail(self, *args, **kwargs):
        self.failed += 1

    async def publish_world_setting_candidates(self, *args, **kwargs):
        return []

    async def reserve_ai_tokens(self, **kwargs):
        await asyncio.sleep(LEDGER_DELAY_SECONDS)
        self.reservations += 1

    async def settle_ai_tokens(self, *args, **kwargs):
        self.settlement_started.set()
        await self.allow_settlement.wait()
        await asyncio.sleep(LEDGER_DELAY_SECONDS)
        self.settlements += 1

    async def release_ai_tokens(self, *args, **kwargs):
        await asyncio.sleep(LEDGER_DELAY_SECONDS)
        self.releases += 1


class SyntheticStore:
    def __init__(self, episode_id):
        self.chunk = EpisodeChunk(
            id=uuid4(), episode_id=episode_id, chunk_index=0, chunk_text=CANARY,
            start_offset=0, end_offset=len(CANARY), paragraph_start_index=0,
            paragraph_end_index=0, metadata_json=None, created_at=None, updated_at=None,
        )

    def replace_chunks_from_s3_content(self, *args, **kwargs):
        return [self.chunk]

    def get_episode_chunks(self, *args, **kwargs):
        return [self.chunk]

    def replace_candidates_for_analysis_job(self, *args, **kwargs):
        return []


class SyntheticCharacterExtractor:
    def __init__(self, client):
        self.client = client

    async def extract_from_chunk(self, **kwargs):
        await self.client.create_text_response(CANARY, CANARY)
        return CharacterSettingExtractionResult(candidates=[])


class SyntheticWorldExtractor:
    async def extract_from_chunk(self, **kwargs):
        return WorldSettingExtractionResult(candidates=[])


class SyntheticResolver:
    async def resolve_candidates(self, **kwargs):
        return SubjectResolutionResult(candidates=[])


class SyntheticComparison:
    async def process_all(self, *args, **kwargs):
        return WorldSettingComparisonRunResult(0, 0)


class NotifyingSemaphore(asyncio.Semaphore):
    def __init__(self):
        super().__init__(1)
        self.acquire_started = asyncio.Event()

    async def acquire(self):
        self.acquire_started.set()
        return await super().acquire()


def make_worker(provider, spring, metrics, semaphore, *, retry_base_seconds=0.15):
    """Use real process_claimed, stage orchestration, metering, and slot cleanup."""
    payload = WorkerAnalysisJobPayload.model_validate({
        "analysisJobId": str(uuid4()), "jobType": "SETTING_EXTRACTION",
        "workId": str(uuid4()), "workTitle": CANARY, "batchId": str(uuid4()),
        "leaseToken": str(uuid4()),
        "leaseExpiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "claimAttemptCount": 1, "analysisMode": "CONFIRMED_ONLY",
        "characterSettingSchemas": [{
            "schemaKey": "synthetic.value", "displayName": "Synthetic field",
            "attributePattern": None, "aliases": [], "valueType": "STRING",
        }],
        "episode": {
            "episodeId": str(uuid4()), "episodeNo": 1, "title": CANARY,
            "contentS3Key": CANARY, "contentS3Version": None,
            "contentHash": "0" * 64, "charCount": len(CANARY),
        },
    })
    store = SyntheticStore(payload.episode.episode_id)
    metered = MeteredTextGenerationClient(
        provider, spring, payload.analysis_job_id, "SETTING_EXTRACTION", MODEL,
        payload.lease_token, request_semaphore=semaphore, max_retries=1,
        retry_base_seconds=retry_base_seconds, jitter_max_seconds=0,
        metrics=metrics,
    )
    worker = AnalysisJobWorker(
        spring_client=spring, chunking_service=store, episode_chunk_service=store,
        setting_extractor=SyntheticCharacterExtractor(metered),
        world_setting_extractor=SyntheticWorldExtractor(),
        subject_resolver=SyntheticResolver(), setting_candidate_service=store,
        world_setting_comparison_pipeline=SyntheticComparison(),
        llm_provider_client=provider, llm_request_semaphore=semaphore,
        embedding_generation_enabled=False, retrieval_preparation_enabled=False,
    )
    return worker, payload
