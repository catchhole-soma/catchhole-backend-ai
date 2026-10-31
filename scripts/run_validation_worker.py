"""Explicit opt-in validation runner. Reads configuration without loading a local .env.

Continuous mode isolates every claim (GH340): an IncompleteInspection from one job (EXECUTION_STOPPED,
INPUT_CHANGED, SOURCE_CHANGED, SOURCE_PURGED, ...) or any other exception from claim/process is logged
with fixed codes, the exception type and job/report ids only, and the loop claims again. Consecutive
failures back off exponentially (poll seconds x 2^n, capped at 60 s) and reset after a clean iteration.
Cancellation, KeyboardInterrupt and a closing worker end the loop. --once handles at most one claim and
re-raises its failure after logging, so the process exits non-zero.

Continuous mode runs at most --concurrency jobs per process (GH343). A free slot is required before every
claim, so a claimed job starts at once in its own task and is never queued in the process. Every claim
attempt is followed by one delay (poll seconds, or the backoff), taken after a slot is free again; with
one slot this is exactly the sequential loop above. SIGTERM/SIGINT stop claiming, give running jobs
--shutdown-grace-seconds, then cancel the rest for Backend lease recovery. --llm-concurrency caps
concurrent provider requests of all jobs in the process; unset means no cap. CLI defaults come from the
process environment only.

Frozen RAG stays an explicit flag (GH344): --readonly-url-env names the SELECT-only DB URL variable and
--query-embedding adds the query embedding client. An unset or empty variable logs one INFO line
(`frozen retrieval disabled: NAME is empty`) and runs keyword-only; --query-embedding without those sessions
logs `query embedding disabled` and runs without it. --once behaves the same. The read-only pool holds at
most --concurrency connections. Per job, VECTOR is offered only when the frozen index is ready and the claim
configured QUERY_EMBEDDING (`vector search offered=...`).
"""
import argparse
import asyncio
import logging
import math
import os
import signal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.embeddings.client import OpenAIEmbeddingsClient
from app.llm.openai_client import OpenAIResponsesClient
from app.retrieval.contracts import IndexSpec
from app.validation.client import ValidationExecutionClient
from app.validation.contracts import IncompleteInspection, safe_code
from app.worker.episode_validation_worker import EpisodeValidationWorker

logger = logging.getLogger("app.validation.runner")
MAX_BACKOFF_SECONDS = 60.0


def _closing(worker):
    return getattr(worker, "_close_task", None) is not None


def _backoff_seconds(base, failures):
    # The exponent is bounded so a long outage never overflows the float conversion.
    return min(base * 2 ** min(failures, 30), max(MAX_BACKOFF_SECONDS, base))


def _origin(exc):
    """Innermost `module.function:line` (app code first); never the message or a file path."""
    innermost = app = None
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        innermost = f"{frame.f_globals.get('__name__', '?')}.{frame.f_code.co_name}:{tb.tb_lineno}"
        if innermost.startswith("app."):
            app = innermost
        tb = tb.tb_next
    return app or innermost or "-"


def _log_failure(log, exc, claim):
    job = getattr(claim, "job_id", None) or "-"
    report = getattr(getattr(claim, "scope", None), "reportId", None) or "-"
    if isinstance(exc, IncompleteInspection):
        log.warning("validation job incomplete code=%s cause=%s job=%s report=%s",
                    safe_code(exc.code, "-"), exc.cause or "-", job, report)
    else:
        # Exception text may carry response bodies, provider text or URLs; only the type is logged.
        log.error("validation job failed error=%s origin=%s job=%s report=%s",
                  type(exc).__name__, _origin(exc), job, report)


def _validate_options(concurrency, shutdown_grace_seconds, llm_concurrency=None):
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("--concurrency (AI_WORKER_CONCURRENCY) must be an integer of at least 1.")
    if llm_concurrency is not None and (type(llm_concurrency) is not int or llm_concurrency < 1):
        raise ValueError("--llm-concurrency (LLM_MAX_CONCURRENT_REQUESTS) must be an integer of at least 1.")
    if not 0 < shutdown_grace_seconds < math.inf:
        raise ValueError("--shutdown-grace-seconds (AI_WORKER_SHUTDOWN_GRACE_SECONDS) must be a finite "
                         "number greater than zero.")


async def _process_job(worker, claim):
    """Return the job's failure instead of raising it, so the scheduler observes it in claim order.

    KeyboardInterrupt/SystemExit raised inside a task would escape the event loop; the scheduler
    re-raises them from its own task instead. Cancellation stays a cancelled task.
    """
    try:
        await worker.process_claimed(claim)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - re-raised or logged by the scheduler.
        return exc
    return None


async def _finish_helpers(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class _JobScheduler:
    """Slots before claims; failures of one job are logged and never cancel the others."""

    def __init__(self, worker, *, poll_seconds, sleeper, should_stop, log, concurrency,
                 shutdown_grace_seconds, stop_event):
        self.worker, self.sleeper, self.should_stop, self.log = worker, sleeper, should_stop, log
        self.concurrency, self.grace = concurrency, shutdown_grace_seconds
        self.stopping = stop_event if stop_event is not None else asyncio.Event()
        self.base = poll_seconds if poll_seconds is not None and poll_seconds > 0 else 1.0
        self.failures, self.logged_backoff = 0, 0.0
        self.active = {}  # task -> claim, in claim order so outcomes are observed deterministically.
        self.closed = False

    def _stop_requested(self):
        return (self.closed or _closing(self.worker) or self.stopping.is_set()
                or (self.should_stop is not None and self.should_stop()))

    def _failed(self, exc, claim):
        if _closing(self.worker):
            if not self.closed:
                self.log.info("validation runner stopping: worker is closing")
            self.closed = True
            return
        _log_failure(self.log, exc, claim)
        self.failures += 1

    def _reap(self):
        for task in [task for task in self.active if task.done()]:
            claim = self.active.pop(task)
            if task.cancelled():
                # Cancelled outside the scheduler (process shutdown): the job is not failed here.
                raise asyncio.CancelledError()
            exc = task.result()
            if exc is None:
                self.failures = 0
            elif isinstance(exc, Exception):
                self._failed(exc, claim)
            else:
                raise exc

    async def _claim(self):
        # The previous iteration left a free slot; the claimed job starts at once in its own task.
        claim = None
        try:
            claim = await self.worker.claim_next()  # Validates every required explicit server role/branch budget.
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - one claim's failure must keep the poller alive.
            self._failed(exc, None)
            return
        if claim is None:
            self.failures = 0
            return
        task = asyncio.create_task(_process_job(self.worker, claim), name="validation-job")
        self.active[task] = claim

    async def _wait_for_free_slot(self):
        self._reap()
        while len(self.active) >= self.concurrency and not self.closed and not self.stopping.is_set():
            stop = asyncio.create_task(self.stopping.wait())
            try:
                await asyncio.wait((*self.active, stop), return_when=asyncio.FIRST_COMPLETED)
            finally:
                await _finish_helpers(stop)
            self._reap()

    def _delay(self):
        if not self.failures:
            self.logged_backoff = 0.0
            return self.base
        delay = _backoff_seconds(self.base, self.failures)
        if delay > self.logged_backoff:
            self.log.warning("validation runner backoff failures=%d sleep_seconds=%g", self.failures, delay)
            self.logged_backoff = delay
        return delay

    async def _sleep(self, delay):
        # Running jobs keep running and are observed as they end; only the next claim waits.
        if self.stopping.is_set():
            return
        sleep = asyncio.create_task(self.sleeper(delay))
        stop = asyncio.create_task(self.stopping.wait())
        try:
            while not sleep.done() and not stop.done() and not self.closed:
                await asyncio.wait((*self.active, sleep, stop), return_when=asyncio.FIRST_COMPLETED)
                self._reap()
            if sleep.done():
                sleep.result()
        finally:
            await _finish_helpers(sleep, stop)

    async def _drain(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.grace
        while self.active and (remaining := deadline - loop.time()) > 0:
            await asyncio.wait(tuple(self.active), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            self._reap()
        if self.active:
            # Unfinished jobs are not failed here; Spring lease expiry/reclaim recovers them.
            self.log.warning("validation runner shutdown grace expired pending_jobs=%d; "
                             "cancelling for lease recovery", len(self.active))
            await self._cancel_active()

    async def _cancel_active(self):
        tasks = tuple(self.active)
        self.active.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        try:
            while not self._stop_requested():
                await self._claim()
                if self.closed:
                    break
                # With one slot this waits for the claimed job, so its outcome decides the delay.
                await self._wait_for_free_slot()
                if self.closed:
                    break
                await self._sleep(self._delay())
            await self._drain()
        except BaseException:
            # Deploy shutdown: running jobs are cancelled, not failed; lease expiry/reclaim recovers them.
            await self._cancel_active()
            raise


async def _run_once(worker, *, should_stop, stop_event, log):
    if (_closing(worker) or (should_stop is not None and should_stop())
            or (stop_event is not None and stop_event.is_set())):
        return
    claim = None
    try:
        claim = await worker.claim_next()  # Validates every required explicit server role/branch budget.
        if claim is not None:
            await worker.process_claimed(claim)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Deploy shutdown: the job is not failed here; Spring lease expiry/reclaim recovers it.
        raise
    except Exception as exc:
        if _closing(worker):
            log.info("validation runner stopping: worker is closing")
            return
        _log_failure(log, exc, claim)
        raise


async def run_loop(worker, *, once, poll_seconds, sleeper=asyncio.sleep, should_stop=None, log=logger,
                   concurrency=1, shutdown_grace_seconds=180.0, stop_event=None):
    """Claim/process until stopped; one job's failure never ends continuous execution."""
    _validate_options(concurrency, shutdown_grace_seconds)
    if once:
        # At most one claim, processed inline; its failure is re-raised. Concurrency does not apply.
        await _run_once(worker, should_stop=should_stop, stop_event=stop_event, log=log)
        return
    await _JobScheduler(worker, poll_seconds=poll_seconds, sleeper=sleeper, should_stop=should_stop, log=log,
                        concurrency=concurrency, shutdown_grace_seconds=shutdown_grace_seconds,
                        stop_event=stop_event).run()


def _install_signal_handlers(stop_event, log=logger):
    loop = asyncio.get_running_loop()

    def request_stop():
        if not stop_event.is_set():
            log.info("validation runner stop requested")
            stop_event.set()

    for signal_number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_number, request_stop)
        except NotImplementedError:
            signal.signal(signal_number, lambda *_: loop.call_soon_threadsafe(request_stop))


async def run(args):
    if not args.enable:
        raise ValueError("Validation runtime is OFF; pass --enable with explicit server budgets.")
    if not args.once and (args.poll_seconds is None or args.poll_seconds <= 0):
        raise ValueError("An explicit positive --poll-seconds is required for continuous execution.")
    _validate_options(args.concurrency, args.shutdown_grace_seconds, args.llm_concurrency)
    internal_key, model_key = os.environ[args.internal_key_env], os.environ[args.model_key_env]
    spec = IndexSpec(version=args.index_version)
    engine, embedding = None, None
    sessions = None
    if args.readonly_url_env:
        url = os.environ.get(args.readonly_url_env, "").strip()
        if url:
            # The DB account must also have SELECT-only grants. No validation writes use this connection.
            # Each job reads sequentially, so one connection per job slot bounds this process (deploy budget).
            engine = create_engine(url, pool_size=args.concurrency, max_overflow=0,
                                   connect_args={"options": "-c default_transaction_read_only=on"})
            sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
        else:
            # The flag stays explicit; an empty value is a logged keyword-only opt-out (GH344).
            logger.info("frozen retrieval disabled: %s is empty", args.readonly_url_env)
    if args.query_embedding:
        if sessions is None:
            logger.info("query embedding disabled: no read-only retrieval sessions")
        else:
            embedding = OpenAIEmbeddingsClient(model_key, spec.model, spec.dimensions, spec.version,
                                               args.embedding_url)
    raw = OpenAIResponsesClient(model_key, "gpt-6.1-terra", args.responses_url)
    client = ValidationExecutionClient(args.backend_url, internal_key)
    # One process-wide cap shared by every job's metered role and query-embedding clients.
    llm_requests = asyncio.Semaphore(args.llm_concurrency) if args.llm_concurrency is not None else None
    worker = EpisodeValidationWorker(client, raw, enabled=True, worker_id=args.worker_id,
        readonly_sessions=sessions, embedding_client=embedding, index_spec=spec,
        llm_request_semaphore=llm_requests)
    stop_event = asyncio.Event()
    if not args.once:
        _install_signal_handlers(stop_event)
    try:
        await run_loop(worker, once=args.once, poll_seconds=args.poll_seconds, concurrency=args.concurrency,
                       shutdown_grace_seconds=args.shutdown_grace_seconds, stop_event=stop_event)
    finally:
        await worker.aclose()
        await raw.http_client.aclose()
        if embedding:
            await embedding.http_client.aclose()
        if engine:
            engine.dispose()


def _env_default(name, convert, fallback):
    """Reads the process environment only; a local .env file is never loaded."""
    value = os.environ.get(name)
    if value is None:
        return fallback
    try:
        return convert(value)
    except ValueError:
        raise ValueError(f"{name} must be a valid {convert.__name__} value.") from None


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--enable", action="store_true")
    result.add_argument("--once", action="store_true")
    result.add_argument("--poll-seconds", type=float)
    result.add_argument("--backend-url", required=True)
    result.add_argument("--worker-id", required=True)
    result.add_argument("--internal-key-env", default="INTERNAL_API_KEY")
    result.add_argument("--model-key-env", default="LLM_API_KEY")
    result.add_argument("--responses-url", default="https://api.openai.com/v1/responses")
    result.add_argument("--readonly-url-env")
    result.add_argument("--query-embedding", action="store_true")
    result.add_argument("--embedding-url", default="https://api.openai.com/v1/embeddings")
    result.add_argument("--index-version", default="v1")
    result.add_argument("--concurrency", type=int, default=_env_default("AI_WORKER_CONCURRENCY", int, 1),
                        help="Jobs running at once in this process (AI_WORKER_CONCURRENCY, default 1). "
                             "Ignored by --once.")
    result.add_argument("--llm-concurrency", type=int,
                        default=_env_default("LLM_MAX_CONCURRENT_REQUESTS", int, None),
                        help="Provider requests at once across all jobs of this process "
                             "(LLM_MAX_CONCURRENT_REQUESTS; unset means no cap).")
    result.add_argument("--shutdown-grace-seconds", type=float,
                        default=_env_default("AI_WORKER_SHUTDOWN_GRACE_SECONDS", float, 180.0),
                        help="After SIGTERM/SIGINT, time running jobs get before they are cancelled "
                             "for lease recovery (AI_WORKER_SHUTDOWN_GRACE_SECONDS, default 180).")
    return result


if __name__ == "__main__":
    # Fixed-code branch diagnostics only (GH331); HTTP client loggers stay at WARNING.
    logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] %(levelname)s %(name)s %(message)s")
    logging.getLogger("app.validation").setLevel(logging.INFO)
    asyncio.run(run(parser().parse_args()))
