from uuid import UUID

import pytest

from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
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

    result = worker.run_once()

    assert result.claimed is True
    assert spring.allowed_job_types == ["WORLD_SETTING_COMPARISON"]
    assert spring.claim_model_name == "comparison-model"
    assert spring.complete_calls == [ANALYSIS_JOB_ID]
    assert spring.fail_calls == []


def test_comparison_worker_fails_job_when_candidate_comparison_failed() -> None:
    spring = FakeSpringApi(_payload())
    worker = WorldSettingComparisonWorker(
        spring_client=spring,
        comparison_pipeline=FakePipeline(WorldSettingComparisonRunResult(0, 1)),
    )

    with pytest.raises(RuntimeError, match="recomparison failed"):
        worker.run_once()

    assert spring.complete_calls == []
    assert spring.fail_calls == [ANALYSIS_JOB_ID]


def test_comparison_pipeline_uses_comparison_model_for_calls_and_metering() -> None:
    pipeline = create_world_setting_comparison_pipeline(
        spring_client=FakeSpringApi(_payload()),
        analysis_job_id=ANALYSIS_JOB_ID,
        lease_token=LEASE_TOKEN,
        comparison_model_name="comparison-model",
    )

    assert pipeline.subject_resolver.model == "comparison-model"
    assert pipeline.comparator.model == "comparison-model"
    assert pipeline.subject_resolver.llm_client.default_model == "comparison-model"
    assert pipeline.comparator.llm_client.default_model == "comparison-model"


class FakePipeline:
    def __init__(self, result: WorldSettingComparisonRunResult) -> None:
        self.result = result

    def process_all(self, analysis_job_id, lease_token):
        return self.result


class FakeSpringApi:
    def __init__(self, payload: WorkerAnalysisJobPayload) -> None:
        self.payload = payload
        self.allowed_job_types = None
        self.claim_model_name = None
        self.complete_calls = []
        self.fail_calls = []

    def claim(self, allowed_job_types, model_name=None, current_step=None):
        self.allowed_job_types = allowed_job_types
        self.claim_model_name = model_name
        return self.payload

    def report_progress(self, *args, **kwargs):
        return None

    def heartbeat(self, *args, **kwargs):
        return None

    def complete(self, analysis_job_id, lease_token, **kwargs):
        self.complete_calls.append(analysis_job_id)

    def fail(self, analysis_job_id, lease_token, error_message):
        self.fail_calls.append(analysis_job_id)


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
