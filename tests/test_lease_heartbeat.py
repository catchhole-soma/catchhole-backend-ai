import asyncio
from uuid import UUID

import pytest
import httpx

from app.worker.lease_heartbeat import WorkerLeaseHeartbeat


ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")


def test_heartbeat_runs_periodically_and_stops_before_context_returns() -> None:
    async def scenario() -> None:
        spring = RecordingHeartbeatSpringApi()
        async with WorkerLeaseHeartbeat(
            spring,
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
            interval_seconds=0.005,
        ) as heartbeat:
            await _wait_until(lambda: spring.call_count >= 2)
            heartbeat.raise_if_failed()

        call_count_after_exit = spring.call_count
        await asyncio.sleep(0.02)

        assert spring.call_count == call_count_after_exit
        assert spring.calls == [(ANALYSIS_JOB_ID, LEASE_TOKEN)] * call_count_after_exit

    asyncio.run(scenario())


def test_heartbeat_failure_is_reported_to_the_owning_job() -> None:
    async def scenario() -> None:
        spring = RecordingHeartbeatSpringApi(error=RuntimeError("lease rejected"))
        heartbeat = WorkerLeaseHeartbeat(
            spring,
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
            interval_seconds=0.005,
        )

        with pytest.raises(asyncio.CancelledError, match="heartbeat failed"):
            async with heartbeat:
                await asyncio.Event().wait()

        with pytest.raises(RuntimeError, match="heartbeat failed"):
            heartbeat.raise_if_failed()

    asyncio.run(scenario())


def test_transient_heartbeat_failures_are_retried_before_cancelling_job() -> None:
    async def scenario() -> None:
        spring = SequencedHeartbeatSpringApi(
            [
                httpx.ReadTimeout("spring timeout"),
                httpx.ConnectError("spring unavailable"),
                None,
            ]
        )
        async with WorkerLeaseHeartbeat(
            spring,
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
            interval_seconds=0.005,
            retry_base_seconds=0,
        ) as heartbeat:
            await _wait_until(lambda: spring.call_count == 3)
            heartbeat.raise_if_failed()

    asyncio.run(scenario())


def test_job_cancellation_during_heartbeat_cleanup_is_not_swallowed() -> None:
    async def scenario() -> None:
        spring = SlowCancellationHeartbeatSpringApi()
        leave_context = asyncio.Event()
        completed_normally = False

        async def job() -> None:
            nonlocal completed_normally
            async with WorkerLeaseHeartbeat(
                spring,
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
                interval_seconds=0.005,
            ):
                await leave_context.wait()
            completed_normally = True

        task = asyncio.create_task(job())
        await spring.started.wait()
        leave_context.set()
        await spring.cancellation_cleanup_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed_normally is False

    asyncio.run(scenario())


class RecordingHeartbeatSpringApi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[UUID, UUID]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def heartbeat(self, analysis_job_id: UUID, lease_token: UUID):
        self.calls.append((analysis_job_id, lease_token))
        if self.error is not None:
            raise self.error
        return None


class SequencedHeartbeatSpringApi(RecordingHeartbeatSpringApi):
    def __init__(self, results: list[Exception | None]) -> None:
        super().__init__()
        self.results = results

    async def heartbeat(self, analysis_job_id: UUID, lease_token: UUID):
        self.calls.append((analysis_job_id, lease_token))
        result = self.results.pop(0)
        if result is not None:
            raise result
        return None


class SlowCancellationHeartbeatSpringApi:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancellation_cleanup_started = asyncio.Event()

    async def heartbeat(self, analysis_job_id: UUID, lease_token: UUID):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancellation_cleanup_started.set()
            await asyncio.Event().wait()
            raise


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)
