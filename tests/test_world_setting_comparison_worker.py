import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
from app.domain.enums import AnalysisFailureCode
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.world_setting_comparison_worker import WorldSettingComparisonWorker
from app.worker.world_setting_services import create_world_setting_comparison_pipeline

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000003")


def test_comparison_worker_claims_only_recomparison_jobs_and_completes() -> None:
    spring = FakeSpringApi(_payload())
    pipeline = FakePipeline(WorldSettingComparisonRunResult(1, 0))
    worker = WorldSettingComparisonWorker(
        spring_client=spring,
        comparison_pipeline=pipeline,
        comparison_model_name="comparison-model",
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert spring.allowed_job_types == ["WORLD_SETTING_COMPARISON"]
    assert spring.claim_model_name == "comparison-model"
    assert spring.complete_calls == [ANALYSIS_JOB_ID]
    assert spring.fail_calls == []


def test_comparison_worker_fails_job_when_candidate_comparison_failed() -> None:
    spring = FakeSpringApi(_payload())
    worker = WorldSettingComparisonWorker(
        spring_client=spring,
        comparison_pipeline=FakePipeline(
            WorldSettingComparisonRunResult(
                0,
                1,
                AnalysisFailureCode.LLM_PROVIDER_ERROR,
            )
        ),
    )

    with pytest.raises(RuntimeError, match="recomparison failed"):
        _run_once(worker)

    assert spring.complete_calls == []
    assert spring.fail_calls == [(ANALYSIS_JOB_ID, "LLM_PROVIDER_ERROR")]


def test_comparison_worker_reports_batch_observability_metrics() -> None:
    spring = FakeSpringApi(_payload())
    worker = WorldSettingComparisonWorker(
        spring_client=spring,
        comparison_pipeline=FakePipeline(
            WorldSettingComparisonRunResult(
                completed_count=2,
                failed_count=0,
                batch_count=1,
                decision_count=1,
                cluster_count=1,
                clustered_candidate_count=2,
                singleton_candidate_count=0,
                stale_batch_retry_count=1,
            )
        ),
    )

    _run_once(worker)

    summary = json.loads(spring.complete_summaries[0])
    assert summary["worldComparisonBatchCount"] == 1
    assert summary["worldComparisonClusterCount"] == 1
    assert summary["averageCandidatesPerBatch"] == 2.0
    assert summary["averageCandidatesPerCluster"] == 2.0
    assert summary["clusteredCandidateCount"] == 2
    assert summary["singletonCandidateCount"] == 0
    assert summary["staleBatchRetryCount"] == 1


def test_comparison_pipeline_routes_subject_resolution_and_comparison_models() -> None:
    pipeline = create_world_setting_comparison_pipeline(
        spring_client=FakeSpringApi(_payload()),
        analysis_job_id=ANALYSIS_JOB_ID,
        lease_token=LEASE_TOKEN,
        subject_resolution_model_name="subject-resolution-model",
        comparison_model_name="comparison-model",
    )

    assert pipeline.subject_resolver.model == "subject-resolution-model"
    assert pipeline.comparator.model == "comparison-model"
    assert pipeline.subject_resolver.llm_client.default_model == "subject-resolution-model"
    assert pipeline.comparator.llm_client.default_model == "comparison-model"


class FakePipeline:
    def __init__(self, result: WorldSettingComparisonRunResult) -> None:
        self.result = result

    async def process_all(self, analysis_job_id, lease_token):
        return self.result


class FakeSpringApi:
    def __init__(self, payload: WorkerAnalysisJobPayload) -> None:
        self.payload = payload
        self.allowed_job_types = None
        self.claim_model_name = None
        self.complete_calls = []
        self.complete_summaries = []
        self.fail_calls = []

    async def claim(self, allowed_job_types, model_name=None, current_step=None):
        self.allowed_job_types = allowed_job_types
        self.claim_model_name = model_name
        return self.payload

    async def report_progress(self, *args, **kwargs):
        return None

    async def heartbeat(self, *args, **kwargs):
        return None

    async def complete(self, analysis_job_id, lease_token, **kwargs):
        self.complete_calls.append(analysis_job_id)
        self.complete_summaries.append(kwargs.get("summary_json"))

    async def fail(self, analysis_job_id, lease_token, error_message, failure_code):
        self.fail_calls.append((analysis_job_id, failure_code.value))


def _payload() -> WorkerAnalysisJobPayload:
    return WorkerAnalysisJobPayload.model_validate(
        {
            "analysisJobId": str(ANALYSIS_JOB_ID),
            "jobType": "WORLD_SETTING_COMPARISON",
            "workId": "00000000-0000-0000-0000-000000000010",
            "workTitle": "설원 전기",
            "batchId": "00000000-0000-0000-0000-000000000011",
            "leaseToken": str(LEASE_TOKEN),
            "leaseExpiresAt": "2026-08-06T12:05:00",
            "claimAttemptCount": 1,
            "worldSettingCandidateId": str(CANDIDATE_ID),
            "characterSettingSchemas": [],
            "knownCharacters": [],
            "episode": {
                "episodeId": "00000000-0000-0000-0000-000000000012",
                "episodeNo": 1,
                "title": "1화",
                "contentS3Key": "works/10/episodes/1.txt",
                "contentS3Version": "v1",
                "contentHash": "hash",
                "charCount": 100,
            },
        }
    )


def _run_once(worker: WorldSettingComparisonWorker):
    async def scenario():
        try:
            return await worker.run_once()
        finally:
            await worker.aclose()

    return asyncio.run(scenario())
