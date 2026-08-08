import json
import logging

from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import get_settings
from app.domain.enums import AnalysisJobType, AnalysisStep
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
        comparison_model_name: str | None = None,
    ) -> None:
        settings = get_settings()
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self._comparison_pipeline = comparison_pipeline
        self.comparison_model_name = (
            comparison_model_name or settings.effective_llm_comparison_model
        )

    def run_once(self) -> WorkerRunResult:
        payload = self.spring_client.claim(
            allowed_job_types=[AnalysisJobType.WORLD_SETTING_COMPARISON],
            model_name=self.comparison_model_name,
            current_step=AnalysisStep.WORLD_SETTING_COMPARISON.value,
        )
        if payload is None:
            return WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable world-setting comparison job does not exist.",
            )

        try:
            self._validate_payload(payload)
            self.spring_client.report_progress(
                payload.analysis_job_id,
                payload.lease_token,
                AnalysisStep.WORLD_SETTING_COMPARISON.value,
            )
            with WorkerLeaseHeartbeat(
                self.spring_client,
                payload.analysis_job_id,
                payload.lease_token,
            ) as lease_heartbeat:
                result = self._get_comparison_pipeline(payload).process_all(
                    payload.analysis_job_id,
                    payload.lease_token,
                )
                lease_heartbeat.raise_if_failed()
            if result.failed_count:
                raise RuntimeError("World-setting candidate recomparison failed.")
            self.spring_client.complete(
                payload.analysis_job_id,
                payload.lease_token,
                summary_json=json.dumps(
                    {
                        "worldSettingComparisonCompletedCount": result.completed_count,
                        "worldSettingComparisonFailedCount": result.failed_count,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:
            try:
                self.spring_client.fail(
                    payload.analysis_job_id,
                    payload.lease_token,
                    (str(exc) or exc.__class__.__name__)[:1000],
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
            comparison_model_name=self.comparison_model_name,
        )
