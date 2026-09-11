import asyncio
from uuid import UUID

import httpx
import pytest

import app.analysis.character_fact_comparison_pipeline as pipeline_module
from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
from app.clients.exceptions import SpringWorkerHttpError

BUSY = "CHARACTER_FACT_COMPARISON_BATCH_BUSY"


def failure(code=BUSY, status=409):
    request = httpx.Request("POST", "http://localhost/claim-batch")
    return SpringWorkerHttpError(
        "Claim deferred", request=request, response=httpx.Response(status, request=request),
        spring_error_code=code,
    )


class Spring:
    def __init__(self, error, busy_count=1):
        self.error = error
        self.busy_count = busy_count
        self.calls = 0

    async def claim_next_character_fact_comparison_batch(self, job_id, lease_token):
        assert job_id == UUID(int=1) and lease_token == UUID(int=2)
        self.calls += 1
        if self.calls <= self.busy_count:
            raise self.error


def test_busy_claim_waits_without_provider_or_failure_then_finishes(monkeypatch):
    spring = Spring(failure(), busy_count=2)
    waits = []

    async def pause(seconds):
        waits.append(seconds)

    monkeypatch.setattr(pipeline_module, "sleep", pause)
    result = asyncio.run(CharacterFactComparisonPipeline(spring, object()).process_all(
        UUID(int=1), UUID(int=2),
    ))
    assert spring.calls == 3 and waits == [1.0, 1.0]
    assert result.completed_count == result.failed_count == result.batch_count == 0
    assert result.provider_segment_count == 0


@pytest.mark.parametrize("code,status", [
    ("SETTING_CANDIDATE_COMPARISON_NOT_READY", 409),
    ("ANALYSIS_JOB_LEASE_CONFLICT", 409),
    (BUSY, 500),
])
def test_other_errors_propagate_without_wait(monkeypatch, code, status):
    spring = Spring(failure(code, status))

    async def pause(seconds):
        pytest.fail("Only exact 409 BUSY can wait")

    monkeypatch.setattr(pipeline_module, "sleep", pause)
    with pytest.raises(SpringWorkerHttpError) as caught:
        asyncio.run(CharacterFactComparisonPipeline(spring, object()).process_all(
            UUID(int=1), UUID(int=2),
        ))
    assert caught.value is spring.error and spring.calls == 1


def test_wait_yields_to_other_tasks_and_honors_worker_cancellation(monkeypatch):
    async def run():
        waiting = asyncio.Event()

        async def pause(seconds):
            waiting.set()
            await asyncio.Future()

        monkeypatch.setattr(pipeline_module, "sleep", pause)
        spring = Spring(failure())
        task = asyncio.create_task(CharacterFactComparisonPipeline(spring, object()).process_all(
            UUID(int=1), UUID(int=2),
        ))
        await waiting.wait()  # The wait permits the Worker's heartbeat/shutdown tasks to run.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spring.calls == 1

    asyncio.run(run())
