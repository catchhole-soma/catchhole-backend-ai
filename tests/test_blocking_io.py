import asyncio
import threading
import time

import pytest

from app.worker.blocking_io import BlockingIoExecutor


def test_blocking_io_executor_enforces_its_own_thread_limit() -> None:
    async def scenario() -> None:
        executor = BlockingIoExecutor(max_workers=2)
        lock = threading.Lock()
        active_count = 0
        max_active_count = 0

        def blocking_call(value: int) -> int:
            nonlocal active_count, max_active_count
            with lock:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
            try:
                time.sleep(0.02)
                return value
            finally:
                with lock:
                    active_count -= 1

        try:
            results = await asyncio.gather(
                *(executor.run(blocking_call, value) for value in range(6))
            )
        finally:
            await executor.aclose()

        assert results == list(range(6))
        assert max_active_count == 2

    asyncio.run(scenario())


def test_cancellation_waits_for_started_blocking_write_before_propagating() -> None:
    async def scenario() -> None:
        executor = BlockingIoExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def blocking_write() -> None:
            started.set()
            release.wait(timeout=1)

        task = asyncio.create_task(executor.run(blocking_write))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await executor.aclose()

    asyncio.run(scenario())


def test_blocking_failure_during_cancellation_does_not_replace_cancelled_error() -> None:
    async def scenario() -> None:
        executor = BlockingIoExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def failing_write() -> None:
            started.set()
            release.wait(timeout=1)
            raise RuntimeError("database write failed")

        task = asyncio.create_task(executor.run(failing_write))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        await executor.aclose()

    asyncio.run(scenario())
