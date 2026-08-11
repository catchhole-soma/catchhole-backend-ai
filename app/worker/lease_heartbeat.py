import asyncio
import logging
from typing import Protocol
from uuid import UUID

import httpx

from app.schemas.worker import WorkerAnalysisJobHeartbeatResponse

logger = logging.getLogger(__name__)


class HeartbeatSpringApi(Protocol):
    async def heartbeat(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerAnalysisJobHeartbeatResponse: ...


class WorkerLeaseHeartbeat:
    """긴 provider 호출 중에도 Spring의 Worker lease를 주기적으로 갱신한다."""

    def __init__(
        self,
        spring_client: HeartbeatSpringApi,
        analysis_job_id: UUID,
        lease_token: UUID,
        interval_seconds: float = 60.0,
        max_attempts: int = 3,
        retry_base_seconds: float = 2.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must not be negative.")
        self.spring_client = spring_client
        self.analysis_job_id = analysis_job_id
        self.lease_token = lease_token
        self.interval_seconds = interval_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._owner_task: asyncio.Task | None = None
        self._error: Exception | None = None

    async def __aenter__(self) -> "WorkerLeaseHeartbeat":
        self._owner_task = asyncio.current_task()
        self._task = asyncio.create_task(
            self._run(),
            name=f"lease-heartbeat-{self.analysis_job_id}",
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self._stop_event.set()
        self._owner_task = None
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            # heartbeat child를 우리가 취소한 경우만 삼킨다. 동시에 owning Job 자체가
            # 취소됐다면 그 신호를 보존해야 complete()로 잘못 진행하지 않는다.
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("Worker lease heartbeat failed.") from self._error

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
                return
            except TimeoutError:
                pass

            try:
                completed = await self._heartbeat_with_retry()
                if not completed:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = exc
                self._stop_event.set()
                logger.exception(
                    "Worker lease heartbeat failed. analysis_job_id=%s",
                    self.analysis_job_id,
                )
                # lease를 갱신할 수 없는데 분석을 계속하면 만료 후 다른 Worker가 같은
                # Job을 재claim해 중복 저장할 수 있다. 이 Job만 취소해 lease 회수에 맡긴다.
                owner_task = self._owner_task
                if owner_task is not None and not owner_task.done():
                    owner_task.cancel("worker lease heartbeat failed")
                return

    async def _heartbeat_with_retry(self) -> bool:
        for attempt in range(1, self.max_attempts + 1):
            try:
                await self.spring_client.heartbeat(self.analysis_job_id, self.lease_token)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt == self.max_attempts or not _is_retryable_heartbeat_error(exc):
                    raise
                delay = self.retry_base_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Worker lease heartbeat failed temporarily; retrying. "
                    "analysis_job_id=%s attempt=%s/%s delay_seconds=%s",
                    self.analysis_job_id,
                    attempt,
                    self.max_attempts,
                    delay,
                )
                if await self._wait_for_stop(delay):
                    return False
        raise AssertionError("Heartbeat retry loop terminated unexpectedly.")

    async def _wait_for_stop(self, timeout: float) -> bool:
        if timeout == 0:
            await asyncio.sleep(0)
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False


def _is_retryable_heartbeat_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in {408, 429} or status_code >= 500
    return isinstance(
        exc,
        (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
