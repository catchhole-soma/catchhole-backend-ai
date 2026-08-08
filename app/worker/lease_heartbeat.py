import logging
from threading import Event, Thread
from typing import Protocol
from uuid import UUID

from app.schemas.worker import WorkerAnalysisJobHeartbeatResponse

logger = logging.getLogger(__name__)


class HeartbeatSpringApi(Protocol):
    def heartbeat(
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
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero.")
        self.spring_client = spring_client
        self.analysis_job_id = analysis_job_id
        self.lease_token = lease_token
        self.interval_seconds = interval_seconds
        self._stop_event = Event()
        self._thread = Thread(target=self._run, daemon=True)
        self._error: Exception | None = None

    def __enter__(self) -> "WorkerLeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1.0)

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("Worker lease heartbeat failed.") from self._error

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.spring_client.heartbeat(self.analysis_job_id, self.lease_token)
            except Exception as exc:
                self._error = exc
                self._stop_event.set()
                logger.exception(
                    "Worker lease heartbeat failed. analysis_job_id=%s",
                    self.analysis_job_id,
                )
                return
