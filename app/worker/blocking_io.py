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
        except asyncio.CancelledError as cancellation:
            # Python thread는 강제로 중단할 수 없다. DB/S3 critical section이 백그라운드에
            # 고아로 남지 않도록 완료까지 추적한 뒤 호출 Task의 취소를 다시 전파한다.
            try:
                await asyncio.shield(future)
            except Exception:
                # shutdown 취소의 의미를 동기 작업 예외가 덮으면 Worker가 Job을 FAILED로
                # 잘못 보고할 수 있다. 실제 I/O 오류는 남기되 lease 재회수 경로를 유지한다.
                logger.exception("Blocking I/O failed while its owning Job was being cancelled.")
            raise cancellation

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=False)
