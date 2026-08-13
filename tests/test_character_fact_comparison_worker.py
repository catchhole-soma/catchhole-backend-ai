import asyncio
from uuid import UUID

import pytest

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.character_fact_comparison_worker import CharacterFactComparisonWorker
from app.worker.character_fact_services import create_character_fact_comparison_pipeline

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000003")


def test_character_comparison_worker_claims_only_its_job_type_and_completes() -> None:
    spring = FakeSpringApi(_payload())
    worker = CharacterFactComparisonWorker(
        spring_client=spring,
        comparison_pipeline=FakePipeline(CharacterFactComparisonRunResult(1, 0)),
        comparison_model_name="comparison-model",
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert spring.allowed_job_types == ["CHARACTER_FACT_COMPARISON"]
    assert spring.claim_model_name == "comparison-model"
    assert spring.current_step == "CHARACTER_FACT_COMPARISON"
    assert spring.complete_calls == [ANALYSIS_JOB_ID]
    assert spring.fail_calls == []


def test_character_comparison_worker_fails_job_when_candidate_failed() -> None:
    spring = FakeSpringApi(_payload())
    worker = CharacterFactComparisonWorker(
        spring_client=spring,
        comparison_pipeline=FakePipeline(CharacterFactComparisonRunResult(0, 1)),
    )

    with pytest.raises(RuntimeError, match="recomparison failed"):
        _run_once(worker)

    assert spring.complete_calls == []
    assert spring.fail_calls == [ANALYSIS_JOB_ID]


def test_character_comparison_worker_requires_candidate_id() -> None:
    payload = _payload().model_copy(update={"setting_candidate_id": None})
    spring = FakeSpringApi(payload)
    worker = CharacterFactComparisonWorker(
        spring_client=spring,
        comparison_pipeline=FakePipeline(CharacterFactComparisonRunResult(1, 0)),
    )

    with pytest.raises(ValueError, match="settingCandidateId"):
        _run_once(worker)

    assert spring.fail_calls == [ANALYSIS_JOB_ID]


def test_character_comparison_pipeline_uses_comparison_model_and_token_purpose() -> None:
    pipeline = create_character_fact_comparison_pipeline(
        spring_client=FakeSpringApi(_payload()),
        analysis_job_id=ANALYSIS_JOB_ID,
        lease_token=LEASE_TOKEN,
        comparison_model_name="comparison-model",
    )

    assert pipeline.comparator.model == "comparison-model"
    assert pipeline.comparator.llm_client.default_model == "comparison-model"
    assert pipeline.comparator.llm_client.purpose == "CHARACTER_FACT_COMPARISON"


class FakePipeline:
    def __init__(self, result: CharacterFactComparisonRunResult) -> None:
        self.result = result

    async def process_all(self, analysis_job_id, lease_token):
        return self.result


class FakeSpringApi:
    def __init__(self, payload: WorkerAnalysisJobPayload) -> None:
        self.payload = payload
        self.allowed_job_types = None
        self.claim_model_name = None
        self.current_step = None
        self.complete_calls = []
        self.fail_calls = []

    async def claim(self, allowed_job_types, model_name=None, current_step=None):
        self.allowed_job_types = allowed_job_types
        self.claim_model_name = model_name
        self.current_step = current_step
        return self.payload

    async def report_progress(self, *args, **kwargs):
        return None

    async def heartbeat(self, *args, **kwargs):
        return None

    async def complete(self, analysis_job_id, lease_token, **kwargs):
        self.complete_calls.append(analysis_job_id)

    async def fail(self, analysis_job_id, lease_token, error_message):
        self.fail_calls.append(analysis_job_id)


def _payload() -> WorkerAnalysisJobPayload:
    return WorkerAnalysisJobPayload.model_validate(
        {
            "analysisJobId": str(ANALYSIS_JOB_ID),
            "jobType": "CHARACTER_FACT_COMPARISON",
            "workId": "00000000-0000-0000-0000-000000000010",
            "workTitle": "설원 전기",
            "batchId": "00000000-0000-0000-0000-000000000011",
            "leaseToken": str(LEASE_TOKEN),
            "leaseExpiresAt": "2026-08-06T12:05:00",
            "claimAttemptCount": 1,
            "settingCandidateId": str(CANDIDATE_ID),
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


def _run_once(worker: CharacterFactComparisonWorker):
    async def scenario():
        try:
            return await worker.run_once()
        finally:
            await worker.aclose()

    return asyncio.run(scenario())
