import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import logging
from typing import TypeVar


ResultT = TypeVar("ResultT")
logger = logging.getLogger(__name__)


class BlockingIoExecutor:
    """SQLAlchemy, boto3 등 동기 I/O를 제한된 전용 thread pool에서 실행한다."""

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1.")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ai-worker-blocking-io",
        )
        self._closed = False

    async def run(
        self,
        function: Callable[..., ResultT],
        /,
        *args,
        **kwargs,
    ) -> ResultT:
        if self._closed:
            raise RuntimeError("Blocking I/O executor is already closed.")

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, partial(function, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Python thread 자체는 중단할 수 없으므로 결과만 별도로 회수한다. Job Task에는
            # 취소를 즉시 전파해 heartbeat가 종료되고 Spring lease 회수가 시작되게 한다.
            future.add_done_callback(self._consume_detached_result)
            raise

    @staticmethod
    def _consume_detached_result(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            logger.exception("Detached blocking I/O failed after its owning Job was cancelled.")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=False)
