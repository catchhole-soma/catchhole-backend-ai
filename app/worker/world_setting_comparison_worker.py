import asyncio
import json
import logging

from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import get_settings
from app.domain.enums import AnalysisFailureCode, AnalysisJobType, AnalysisStep
from app.exceptions.failure_classification import analysis_failure_code
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import SpringWorkerApi, WorkerRunResult
from app.worker.lease_heartbeat import WorkerLeaseHeartbeat
from app.worker.world_setting_services import create_world_setting_comparison_pipeline

logger = logging.getLogger(__name__)


class WorldSettingComparisonWorker:
    def __init__(
        self,
        spring_client: SpringWorkerApi | None = None,
        comparison_pipeline: WorldSettingComparisonPipeline | None = None,
        subject_resolution_model_name: str | None = None,
        comparison_model_name: str | None = None,
        llm_provider_client: TextGenerationClient | None = None,
        llm_request_semaphore: asyncio.Semaphore | None = None,
        heartbeat_interval_seconds: float = 60.0,
    ) -> None:
        settings = get_settings()
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self._owns_spring_client = spring_client is None
        self._comparison_pipeline = comparison_pipeline
        self.subject_resolution_model_name = (
            subject_resolution_model_name or settings.effective_llm_subject_resolution_model
        )
        self.comparison_model_name = (
            comparison_model_name or settings.effective_llm_comparison_model
        )
        self._llm_provider_client = llm_provider_client
        self._owns_llm_provider_client = llm_provider_client is None
        self._llm_request_semaphore = llm_request_semaphore or asyncio.Semaphore(
            settings.llm_max_concurrent_requests
        )
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._llm_http_max_retries = settings.llm_http_max_retries
        self._llm_http_retry_base_seconds = settings.llm_http_retry_base_seconds

    async def aclose(self) -> None:
        close_operations = []
        if self._owns_llm_provider_client and self._llm_provider_client is not None:
            close_provider = getattr(self._llm_provider_client, "aclose", None)
            if close_provider is not None:
                close_operations.append(("LLM provider", close_provider))
        if self._owns_spring_client:
            close_spring = getattr(self.spring_client, "aclose", None)
            if close_spring is not None:
                close_operations.append(("Spring client", close_spring))

        first_error: Exception | None = None
        for resource_name, close_operation in close_operations:
            try:
                await close_operation()
            except Exception as exc:
                logger.exception(
                    "Failed to close comparison Worker resource. resource=%s",
                    resource_name,
                )
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def claim_next(self) -> WorkerAnalysisJobPayload | None:
        return await self.spring_client.claim(
            allowed_job_types=[AnalysisJobType.WORLD_SETTING_COMPARISON],
            model_name=self.comparison_model_name,
            current_step=AnalysisStep.WORLD_SETTING_COMPARISON.value,
        )

    async def run_once(self) -> WorkerRunResult:
        payload = await self.claim_next()
        if payload is None:
            return WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable world-setting comparison job does not exist.",
            )

        return await self.process_claimed(payload)

    async def process_claimed(
        self,
        payload: WorkerAnalysisJobPayload,
    ) -> WorkerRunResult:
        candidate_failure_code: AnalysisFailureCode | None = None
        try:
            self._validate_payload(payload)
            await self.spring_client.report_progress(
                payload.analysis_job_id,
                payload.lease_token,
                AnalysisStep.WORLD_SETTING_COMPARISON.value,
            )
            async with WorkerLeaseHeartbeat(
                self.spring_client,
                payload.analysis_job_id,
                payload.lease_token,
                interval_seconds=self._heartbeat_interval_seconds,
            ) as lease_heartbeat:
                result = await self._get_comparison_pipeline(payload).process_all(
                    payload.analysis_job_id,
                    payload.lease_token,
                )
                lease_heartbeat.raise_if_failed()
            if result.failed_count:
                candidate_failure_code = result.first_failure_code
                raise RuntimeError("World-setting candidate recomparison failed.")
            await self.spring_client.complete(
                payload.analysis_job_id,
                payload.lease_token,
                summary_json=json.dumps(
                    {
                        "worldSettingComparisonCompletedCount": result.completed_count,
                        "worldSettingComparisonFailedCount": result.failed_count,
                        **result.summary_metrics(),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:
            try:
                await self.spring_client.fail(
                    payload.analysis_job_id,
                    payload.lease_token,
                    (str(exc) or exc.__class__.__name__)[:1000],
                    candidate_failure_code or analysis_failure_code(exc),
                )
            except Exception:
                logger.exception(
                    "Failed to report comparison job failure. analysis_job_id=%s",
                    payload.analysis_job_id,
                )
            raise

        return WorkerRunResult(
            claimed=True,
            analysis_job_id=payload.analysis_job_id,
            message="World-setting comparison job completed.",
            work_id=payload.work_id,
            work_title=payload.work_title,
            episode_count=None,
        )

    def _validate_payload(self, payload: WorkerAnalysisJobPayload) -> None:
        if payload.job_type != AnalysisJobType.WORLD_SETTING_COMPARISON:
            raise ValueError(f"Unsupported analysis job type: {payload.job_type}")
        if payload.world_setting_candidate_id is None:
            raise ValueError("Comparison job must include worldSettingCandidateId.")

    def _get_comparison_pipeline(
        self,
        payload: WorkerAnalysisJobPayload,
    ) -> WorldSettingComparisonPipeline:
        if self._comparison_pipeline is not None:
            return self._comparison_pipeline
        return create_world_setting_comparison_pipeline(
            spring_client=self.spring_client,
            analysis_job_id=payload.analysis_job_id,
            lease_token=payload.lease_token,
            subject_resolution_model_name=self.subject_resolution_model_name,
            comparison_model_name=self.comparison_model_name,
            provider_client=self._get_llm_provider_client(),
            request_semaphore=self._llm_request_semaphore,
            max_retries=self._llm_http_max_retries,
            retry_base_seconds=self._llm_http_retry_base_seconds,
        )

    def _get_llm_provider_client(self) -> TextGenerationClient:
        if self._llm_provider_client is None:
            self._llm_provider_client = OpenAIResponsesClient.from_settings()
        return self._llm_provider_client
