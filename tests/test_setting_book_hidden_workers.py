import asyncio
from uuid import uuid4

import pytest

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.character_fact_comparison_worker import CharacterFactComparisonWorker
from app.worker.world_setting_comparison_worker import WorldSettingComparisonWorker
from tests.test_setting_book_source_contract import book_payload_data


@pytest.mark.parametrize("job_type", ["CHARACTER_FACT_COMPARISON", "WORLD_SETTING_COMPARISON"])
def test_hidden_book_recomparison_claims_source_capability_and_passes_frozen_manifest(job_type):
    data = book_payload_data()
    data.update(jobType=job_type, settingCandidateId=str(uuid4()), worldSettingCandidateId=str(uuid4()),
                analysisMode="CONFIRMED_ONLY", reviewMode="MANUAL", analysisContext=None)
    payload = WorkerAnalysisJobPayload.model_validate(data)

    class Spring:
        claim_request = None
        complete_count = 0

        async def claim(self, **kwargs):
            self.claim_request = kwargs
            return payload

        async def report_progress(self, *_args, **_kwargs):
            pass

        async def complete(self, *_args, **_kwargs):
            self.complete_count += 1

        async def fail(self, *_args, **_kwargs):
            raise AssertionError("A valid frozen book comparison cannot be treated as missing episode.")

    class Pipeline:
        source = None

        async def process_all(self, job_id, token, *, analysis_source):
            assert job_id == payload.analysis_job_id and token == payload.lease_token
            self.source = analysis_source
            return (CharacterFactComparisonRunResult(1, 0) if job_type == "CHARACTER_FACT_COMPARISON"
                    else WorldSettingComparisonRunResult(1, 0))

    spring, pipeline = Spring(), Pipeline()
    worker_class = CharacterFactComparisonWorker if job_type == "CHARACTER_FACT_COMPARISON" else WorldSettingComparisonWorker

    async def scenario():
        worker = worker_class(spring_client=spring, comparison_pipeline=pipeline)
        try:
            result = await worker.run_once()
            assert result.claimed
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert spring.claim_request["allowed_job_types"] == [job_type]
    assert spring.claim_request["supported_source_kinds"] == ["EPISODE", "SETTING_BOOK"]
    assert spring.claim_request.get("supports_character_comparison_groups") == (True if job_type == "CHARACTER_FACT_COMPARISON" else None)
    assert pipeline.source.identity() == payload.frozen_source().identity()
    assert pipeline.source.episode_no is None and pipeline.source.episode_id is None
    assert spring.complete_count == 1
