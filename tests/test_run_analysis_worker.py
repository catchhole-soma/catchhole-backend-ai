import asyncio
from collections import deque
from dataclasses import dataclass
import re
from uuid import UUID

from app.worker.analysis_job_worker import WorkerRunResult
from scripts.run_analysis_worker import _print_result, run_worker_loop

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_print_result_prefixes_timestamp_when_job_does_not_exist(capsys) -> None:
    _print_result(
        WorkerRunResult(
            claimed=False,
            analysis_job_id=None,
            message="Claimable analysis job does not exist.",
        )
    )

    output = capsys.readouterr().out.strip()

    assert re.match(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] ", output)
    assert "claimed=False" in output


def test_scheduler_sleeps_once_after_first_empty_claim_instead_of_fanning_out() -> None:
    async def scenario() -> None:
        worker = FakeSchedulerWorker([None, None])
        sleeper = RecordingSleeper()

        results = await run_worker_loop(
            worker=worker,
            idle_sleep_seconds=3.0,
            concurrency=5,
            shutdown_grace_seconds=1.0,
            max_iterations=2,
            sleeper=sleeper,
        )

        assert results == []
        assert worker.claim_count == 2
        assert sleeper.calls == [3.0]

    asyncio.run(scenario())


def test_scheduler_acquires_slot_before_claim_and_never_prefetches() -> None:
    async def scenario() -> None:
        payloads = [_payload(1), _payload(2), _payload(3)]
        worker = FakeSchedulerWorker(payloads, block_processing=True)

        scheduler = asyncio.create_task(
            run_worker_loop(
                worker=worker,
                idle_sleep_seconds=1.0,
                concurrency=2,
                shutdown_grace_seconds=1.0,
                max_iterations=3,
            )
        )

        await _wait_until(lambda: worker.started_count == 2)
        assert worker.claim_count == 2
        assert worker.max_active_count == 2

        worker.release(payloads[0].analysis_job_id)
        await _wait_until(lambda: worker.started_count == 3)
        assert worker.claim_count == 3
        assert worker.started_count == 3

        worker.release_all()
        results = await scheduler

        assert {result.analysis_job_id for result in results} == {
            payload.analysis_job_id for payload in payloads
        }
        assert worker.max_active_count == 2

    asyncio.run(scenario())


def test_one_job_failure_does_not_cancel_its_peer() -> None:
    async def scenario() -> None:
        failed = _payload(1)
        succeeded = _payload(2)
        worker = FakeSchedulerWorker(
            [failed, succeeded],
            failures={failed.analysis_job_id},
        )

        results = await run_worker_loop(
            worker=worker,
            idle_sleep_seconds=1.0,
            concurrency=2,
            shutdown_grace_seconds=1.0,
            max_iterations=2,
        )

        assert [result.analysis_job_id for result in results] == [succeeded.analysis_job_id]
        assert worker.processed_ids == {failed.analysis_job_id, succeeded.analysis_job_id}

    asyncio.run(scenario())


def test_stop_event_stops_new_claims_and_drains_active_jobs() -> None:
    async def scenario() -> None:
        payloads = [_payload(1), _payload(2), _payload(3)]
        worker = FakeSchedulerWorker(payloads, block_processing=True)
        stop_event = asyncio.Event()
        scheduler = asyncio.create_task(
            run_worker_loop(
                worker=worker,
                idle_sleep_seconds=1.0,
                concurrency=2,
                shutdown_grace_seconds=1.0,
                stop_event=stop_event,
            )
        )

        await _wait_until(lambda: worker.started_count == 2)
        stop_event.set()
        worker.release_all()
        await scheduler

        assert worker.claim_count == 2
        assert payloads[2].analysis_job_id not in worker.processed_ids

    asyncio.run(scenario())


def test_shutdown_grace_expiry_cancels_job_for_lease_recovery() -> None:
    async def scenario() -> None:
        payload = _payload(1)
        worker = FakeSchedulerWorker([payload], block_processing=True)
        stop_event = asyncio.Event()
        scheduler = asyncio.create_task(
            run_worker_loop(
                worker=worker,
                idle_sleep_seconds=1.0,
                concurrency=1,
                shutdown_grace_seconds=0.01,
                stop_event=stop_event,
            )
        )

        await _wait_until(lambda: worker.started_count == 1)
        stop_event.set()
        await scheduler

        assert worker.cancelled_ids == {payload.analysis_job_id}

    asyncio.run(scenario())


@dataclass(frozen=True)
class FakePayload:
    analysis_job_id: UUID


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


class FakeSchedulerWorker:
    def __init__(
        self,
        payloads: list[FakePayload | None],
        *,
        block_processing: bool = False,
        failures: set[UUID] | None = None,
    ) -> None:
        self.payloads = deque(payloads)
        self.block_processing = block_processing
        self.failures = failures or set()
        self.claim_count = 0
        self.active_count = 0
        self.max_active_count = 0
        self.started_count = 0
        self.processed_ids: set[UUID] = set()
        self.cancelled_ids: set[UUID] = set()
        self._release_events: dict[UUID, asyncio.Event] = {}

    async def claim_next(self):
        self.claim_count += 1
        await asyncio.sleep(0)
        return self.payloads.popleft() if self.payloads else None

    async def process_claimed(self, payload: FakePayload) -> WorkerRunResult:
        self.started_count += 1
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)
        self.processed_ids.add(payload.analysis_job_id)
        try:
            if self.block_processing:
                event = self._release_events.setdefault(payload.analysis_job_id, asyncio.Event())
                await event.wait()
            if payload.analysis_job_id in self.failures:
                raise RuntimeError("job failed")
            return WorkerRunResult(
                claimed=True,
                analysis_job_id=payload.analysis_job_id,
                message="completed",
            )
        except asyncio.CancelledError:
            self.cancelled_ids.add(payload.analysis_job_id)
            raise
        finally:
            self.active_count -= 1

    async def aclose(self) -> None:
        return None

    def release(self, analysis_job_id: UUID) -> None:
        self._release_events[analysis_job_id].set()

    def release_all(self) -> None:
        for event in self._release_events.values():
            event.set()


def _payload(index: int) -> FakePayload:
    return FakePayload(UUID(f"00000000-0000-0000-0000-{index:012d}"))


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)
