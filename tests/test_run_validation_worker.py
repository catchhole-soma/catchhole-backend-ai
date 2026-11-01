"""GH340: one validation job's stop/failure never ends the long-running runner (no DB/network).

GH343: per-process job slots, graceful stop and CLI/env options of the same runner.
"""
import asyncio
import logging
import os
import signal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.validation.client import ValidationExecutionClient
from app.validation.contracts import IncompleteInspection
from app.worker.episode_validation_worker import EpisodeValidationWorker
from scripts.run_validation_worker import (
    MAX_BACKOFF_SECONDS,
    _backoff_seconds,
    parser,
    run,
    run_loop,
)

RUNNER_LOGGER = "app.validation.runner"


def _claim():
    return SimpleNamespace(job_id=uuid4(), scope=SimpleNamespace(reportId=uuid4()))


class FakeWorker:
    """Each script step is a claim, None (idle) or an exception raised by claim_next.

    A claim mapped in ``failures`` raises that exception from process_claimed.
    """

    def __init__(self, steps, failures=None):
        self.steps, self.failures = list(steps), dict(failures or {})
        self.claims, self.processed = 0, []
        self._close_task = None

    async def claim_next(self):
        self.claims += 1
        step = self.steps.pop(0) if self.steps else None
        if isinstance(step, BaseException):
            raise step
        return step

    async def process_claimed(self, claim):
        self.processed.append(claim)
        failure = self.failures.get(id(claim))
        if failure is not None:
            raise failure

    async def aclose(self):
        self._close_task = object()


class RecordingSleeper:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def _stop_after(worker, claims):
    return lambda: worker.claims >= claims


def _run(worker, *, once=False, poll_seconds=2.0, claims=None, sleeper=None):
    sleeper = sleeper or RecordingSleeper()
    should_stop = None if claims is None else _stop_after(worker, claims)
    asyncio.run(run_loop(worker, once=once, poll_seconds=poll_seconds, sleeper=sleeper,
                         should_stop=should_stop))
    return sleeper


@pytest.mark.parametrize(("code", "cause", "logged_cause"), [
    ("EXECUTION_STOPPED", None, "-"),
    ("SOURCE_PURGED", None, "-"),
    ("INVALID_INSPECTION_CONTRACT", "INVALID_EXACT_SPAN", "INVALID_EXACT_SPAN"),
])
def test_incomplete_inspection_is_logged_and_next_claim_is_processed(caplog, code, cause, logged_cause):
    stopped, following = _claim(), _claim()
    worker = FakeWorker([stopped, following], {id(stopped): IncompleteInspection(code, cause)})
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    _run(worker, claims=2)

    assert worker.processed == [stopped, following]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "incomplete" in r.message]
    assert [r.message for r in warnings] == [(
        f"validation job incomplete code={code} cause={logged_cause} "
        f"job={stopped.job_id} report={stopped.scope.reportId}"
    )]


def test_unexpected_error_logs_type_only_and_backoff_grows_then_resets(caplog):
    claims = [_claim() for _ in range(4)]
    secret = RuntimeError("provider body https://spring.invalid/x manuscript text")
    worker = FakeWorker(claims, {id(claims[0]): secret, id(claims[1]): RuntimeError("again"),
                                 id(claims[3]): RuntimeError("later")})
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    sleeper = _run(worker, poll_seconds=2.0, claims=5)

    assert worker.processed == claims
    # fail, fail (grows), success (resets to poll), fail (starts over), idle.
    assert sleeper.calls == [4.0, 8.0, 2.0, 4.0, 2.0]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 3
    assert errors[0].message.startswith("validation job failed error=RuntimeError origin=")
    assert f"job={claims[0].job_id} report={claims[0].scope.reportId}" in errors[0].message
    assert all(r.exc_info is None for r in errors)
    assert "provider body" not in caplog.text and "spring.invalid" not in caplog.text
    backoff = [r.message for r in caplog.records if "backoff" in r.message]
    assert backoff == ["validation runner backoff failures=1 sleep_seconds=4",
                       "validation runner backoff failures=2 sleep_seconds=8",
                       "validation runner backoff failures=1 sleep_seconds=4"]


def test_claim_failures_back_off_and_successful_claim_is_still_processed(caplog):
    claim = _claim()
    worker = FakeWorker([IncompleteInspection("EXECUTION_TRANSPORT_FAILED"), ValueError("bad"), claim])
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    sleeper = _run(worker, poll_seconds=1.5, claims=3)

    assert worker.processed == [claim]
    assert sleeper.calls == [3.0, 6.0, 1.5]
    assert "validation job incomplete code=EXECUTION_TRANSPORT_FAILED cause=- job=- report=-" in caplog.text
    assert "validation job failed error=ValueError origin=" in caplog.text
    assert "job=- report=-" in caplog.text


def test_backoff_is_capped_and_logged_only_while_it_grows(caplog):
    worker = FakeWorker([RuntimeError("down")] * 5)
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    sleeper = _run(worker, poll_seconds=10.0, claims=5)

    assert sleeper.calls == [20.0, 40.0, 60.0, 60.0, 60.0]
    assert sum("backoff" in r.message for r in caplog.records) == 3
    # A long outage never overflows, and backoff is never shorter than the idle poll.
    assert _backoff_seconds(1.0, 100_000) == MAX_BACKOFF_SECONDS
    assert _backoff_seconds(120.0, 3) == 120.0


@pytest.mark.parametrize("interrupt", [asyncio.CancelledError, KeyboardInterrupt])
def test_shutdown_interrupt_from_process_propagates(interrupt):
    claim = _claim()
    worker = FakeWorker([claim, _claim()], {id(claim): interrupt()})
    sleeper = RecordingSleeper()

    async def scenario():
        with pytest.raises(interrupt):
            await run_loop(worker, once=False, poll_seconds=1.0, sleeper=sleeper)

    asyncio.run(scenario())
    assert worker.claims == 1 and sleeper.calls == []


@pytest.mark.parametrize("failure", [IncompleteInspection("EXECUTION_STOPPED"), RuntimeError("boom")])
def test_once_reraises_failed_claim_after_logging(caplog, failure):
    claim = _claim()
    worker = FakeWorker([claim, _claim()], {id(claim): failure})
    sleeper = RecordingSleeper()
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    with pytest.raises(type(failure)):
        _run(worker, once=True, poll_seconds=None, sleeper=sleeper)

    assert worker.claims == 1 and sleeper.calls == []
    assert f"job={claim.job_id}" in caplog.text


def test_once_reraises_claim_failure():
    worker = FakeWorker([IncompleteInspection("EXECUTION_HTTP_REJECTED")])
    with pytest.raises(IncompleteInspection, match="EXECUTION_HTTP_REJECTED"):
        _run(worker, once=True, poll_seconds=None)
    assert worker.claims == 1


@pytest.mark.parametrize("steps", [[None], ["claim"]])
def test_once_returns_after_one_successful_or_idle_claim(steps):
    claim = _claim()
    worker = FakeWorker([claim if step == "claim" else step for step in steps] + [_claim()])

    sleeper = _run(worker, once=True, poll_seconds=None)

    assert worker.claims == 1 and sleeper.calls == []
    assert worker.processed == ([claim] if steps == ["claim"] else [])


def test_closing_worker_exits_without_claiming():
    worker = FakeWorker([_claim()])
    worker._close_task = object()

    sleeper = _run(worker)

    assert worker.claims == 0 and worker.processed == [] and sleeper.calls == []


def test_worker_closing_during_a_job_exits_cleanly_instead_of_spinning(caplog):
    first = _claim()
    worker = FakeWorker([first, _claim()])

    async def stopped_by_close(claim):
        worker.processed.append(claim)
        worker._close_task = object()
        raise IncompleteInspection("EXECUTION_STOPPED")

    worker.process_claimed = stopped_by_close
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    sleeper = _run(worker, poll_seconds=1.0)

    assert worker.processed == [first] and worker.claims == 1 and sleeper.calls == []
    assert "worker is closing" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_closed_real_worker_stops_the_loop_without_contacting_spring():
    calls = []

    def server(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http, \
                httpx.AsyncClient(transport=httpx.MockTransport(server)) as provider:
            raw = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses",
                                        http_client=provider)
            worker = EpisodeValidationWorker(ValidationExecutionClient("https://spring.invalid", "synthetic", http),
                                             raw, enabled=True)
            await worker.aclose()
            sleeper = RecordingSleeper()
            await run_loop(worker, once=False, poll_seconds=1.0, sleeper=sleeper)
            assert sleeper.calls == []

    asyncio.run(scenario())
    assert calls == []


# GH343 -------------------------------------------------------------------------------------------

GRACE_LOG = "validation runner shutdown grace expired pending_jobs=%d; cancelling for lease recovery"


class BlockingWorker:
    """process_claimed waits on a per-claim Event, so several jobs stay in flight at once."""

    def __init__(self, steps, failures=None):
        self.steps, self.failures = list(steps), dict(failures or {})
        self.claims, self.in_flight, self.max_in_flight = 0, 0, 0
        self.started, self.finished, self.cancelled = [], [], []
        self.gates = {}
        self._close_task = None

    def gate(self, claim):
        return self.gates.setdefault(id(claim), asyncio.Event())

    def release(self, *claims):
        for claim in claims:
            self.gate(claim).set()

    async def claim_next(self):
        self.claims += 1
        step = self.steps.pop(0) if self.steps else None
        if isinstance(step, BaseException):
            raise step
        return step

    async def process_claimed(self, claim):
        self.started.append(claim)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self.gate(claim).wait()
            failure = self.failures.get(id(claim))
            if failure is not None:
                raise failure
            self.finished.append(claim)
        except asyncio.CancelledError:
            self.cancelled.append(claim)
            raise
        finally:
            self.in_flight -= 1


class YieldingSleeper(RecordingSleeper):
    """Records like RecordingSleeper but lets running jobs and the test driver advance."""

    async def __call__(self, seconds):
        self.calls.append(seconds)
        await asyncio.sleep(0)


async def _until(predicate, turns=2000):
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("scheduler did not reach the expected state")


async def _settle(turns=50):
    for _ in range(turns):
        await asyncio.sleep(0)


def _start(worker, sleeper, **options):
    options.setdefault("poll_seconds", 1.0)
    return asyncio.create_task(run_loop(worker, once=False, sleeper=sleeper, **options))


def test_concurrency_caps_in_flight_jobs_and_reuses_freed_slots_in_completion_order():
    claims = [_claim() for _ in range(5)]
    worker = BlockingWorker(claims)

    async def scenario():
        stop = asyncio.Event()
        runner = _start(worker, YieldingSleeper(), concurrency=3, stop_event=stop)
        await _until(lambda: len(worker.started) == 3)
        await _settle()
        # Every slot is busy: no claim (and so no queued job) until one of them ends.
        assert worker.claims == 3 and worker.started == claims[:3]
        worker.release(claims[1])
        await _until(lambda: len(worker.started) == 4)
        await _settle()
        assert worker.claims == 4 and worker.started[3] is claims[3]
        worker.release(claims[0])
        await _until(lambda: len(worker.started) == 5)
        await _settle()
        assert worker.claims == 5 and worker.started[4] is claims[4]
        worker.release(*claims[2:])
        await _until(lambda: len(worker.finished) == 5)
        stop.set()
        await asyncio.wait_for(runner, 1)

    asyncio.run(scenario())
    assert worker.max_in_flight == 3
    assert worker.finished == [claims[1], claims[0], *claims[2:]]
    assert worker.cancelled == []


def test_one_incomplete_job_is_logged_once_and_the_running_jobs_finish(caplog):
    stopped, first, second = _claim(), _claim(), _claim()
    worker = BlockingWorker([stopped, first, second], {id(stopped): IncompleteInspection("EXECUTION_STOPPED")})
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    async def scenario():
        stop = asyncio.Event()
        runner = _start(worker, YieldingSleeper(), concurrency=3, stop_event=stop)
        await _until(lambda: len(worker.started) == 3)
        worker.release(stopped)
        await _until(lambda: "validation job incomplete" in caplog.text)
        await _settle()
        assert worker.in_flight == 2 and worker.cancelled == [] and not runner.done()
        worker.release(first, second)
        await _until(lambda: len(worker.finished) == 2)
        stop.set()
        await asyncio.wait_for(runner, 1)

    asyncio.run(scenario())
    assert worker.finished == [first, second] and worker.cancelled == []
    warnings = [r.message for r in caplog.records if "incomplete" in r.message]
    assert warnings == [(f"validation job incomplete code=EXECUTION_STOPPED cause=- "
                         f"job={stopped.job_id} report={stopped.scope.reportId}")]


def test_idle_claim_sleeps_poll_seconds_while_running_jobs_continue():
    running = _claim()
    worker = BlockingWorker([running, None])
    sleeper = YieldingSleeper()

    async def scenario():
        runner = _start(worker, sleeper, poll_seconds=2.0, concurrency=2,
                        should_stop=_stop_after(worker, 2))
        await _until(lambda: worker.claims == 2 and len(sleeper.calls) == 2)
        await _settle()
        assert worker.in_flight == 1 and worker.finished == [] and not runner.done()
        worker.release(running)
        await asyncio.wait_for(runner, 1)

    asyncio.run(scenario())
    assert sleeper.calls == [2.0, 2.0]
    assert worker.finished == [running] and worker.cancelled == []


def test_claim_failure_while_jobs_run_backs_off_without_touching_them(caplog):
    running = _claim()
    worker = BlockingWorker([running, RuntimeError("spring down https://spring.invalid")])
    sleeper = YieldingSleeper()
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    async def scenario():
        runner = _start(worker, sleeper, poll_seconds=1.5, concurrency=2,
                        should_stop=_stop_after(worker, 2))
        await _until(lambda: worker.claims == 2 and len(sleeper.calls) == 2)
        await _settle()
        assert worker.in_flight == 1 and worker.cancelled == [] and not runner.done()
        worker.release(running)
        await asyncio.wait_for(runner, 1)

    asyncio.run(scenario())
    assert sleeper.calls == [1.5, 3.0]
    assert worker.finished == [running] and worker.cancelled == []
    errors = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].startswith("validation job failed error=RuntimeError origin=")
    assert errors[0].endswith("job=- report=-")
    assert "spring.invalid" not in caplog.text
    assert "validation runner backoff failures=1 sleep_seconds=3" in caplog.text


@pytest.mark.parametrize("trigger", ["stop_event", "should_stop"])
def test_graceful_stop_claims_no_more_and_waits_for_running_jobs(caplog, trigger):
    claims = [_claim(), _claim()]
    worker = BlockingWorker([*claims, _claim(), _claim()])
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)
    requested = []

    async def scenario():
        stop = asyncio.Event()
        runner = _start(worker, YieldingSleeper(), concurrency=2, stop_event=stop,
                        should_stop=lambda: bool(requested))
        await _until(lambda: len(worker.started) == 2)
        if trigger == "stop_event":
            stop.set()
        else:
            requested.append(True)
        worker.release(claims[0])
        await _until(lambda: len(worker.finished) == 1)
        await _settle()
        assert worker.claims == 2 and not runner.done()
        worker.release(claims[1])
        await asyncio.wait_for(runner, 1)

    asyncio.run(scenario())
    assert worker.claims == 2 and worker.finished == claims and worker.cancelled == []
    assert "grace expired" not in caplog.text


def test_shutdown_grace_expiry_cancels_the_remaining_job_for_lease_recovery(caplog):
    done, stuck = _claim(), _claim()
    worker = BlockingWorker([done, stuck])
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    async def scenario():
        stop = asyncio.Event()
        runner = _start(worker, YieldingSleeper(), concurrency=2, stop_event=stop,
                        shutdown_grace_seconds=0.05)
        await _until(lambda: len(worker.started) == 2)
        stop.set()
        worker.release(done)
        await asyncio.wait_for(runner, 2)
        assert worker.cancelled == [stuck] and worker.in_flight == 0  # Before asyncio.run teardown.

    asyncio.run(scenario())
    assert worker.finished == [done] and worker.claims == 2
    expired = [r for r in caplog.records if "grace expired" in r.message]
    assert [(r.levelno, r.message) for r in expired] == [(logging.WARNING, GRACE_LOG % 1)]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_cancelled_runner_cancels_running_jobs_and_propagates():
    claims = [_claim(), _claim()]
    worker = BlockingWorker(claims)

    async def scenario():
        runner = _start(worker, YieldingSleeper(), concurrency=2)
        await _until(lambda: len(worker.started) == 2)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        # Checked before asyncio.run's own teardown could cancel leftover tasks.
        assert sorted(map(id, worker.cancelled)) == sorted(map(id, claims)) and worker.in_flight == 0

    asyncio.run(scenario())
    assert worker.finished == []


@pytest.mark.parametrize("failure", [None, IncompleteInspection("EXECUTION_STOPPED"), RuntimeError("boom")])
def test_once_ignores_concurrency_and_processes_one_claim_inline(caplog, failure):
    claim = _claim()
    worker = FakeWorker([claim, _claim()], {id(claim): failure} if failure else {})
    sleeper = RecordingSleeper()
    tasks = []
    process = worker.process_claimed

    async def inline(claimed):
        tasks.append(asyncio.current_task())
        await process(claimed)

    worker.process_claimed = inline
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)

    async def scenario():
        await run_loop(worker, once=True, poll_seconds=None, sleeper=sleeper, concurrency=4)
        return asyncio.current_task()

    if failure is None:
        owner = asyncio.run(scenario())
        assert tasks == [owner]
    else:
        with pytest.raises(type(failure)):
            asyncio.run(scenario())
        assert f"job={claim.job_id}" in caplog.text
    assert worker.claims == 1 and worker.processed == [claim] and sleeper.calls == []


def _cli(*extra):
    return parser().parse_args(["--enable", "--poll-seconds", "1", "--backend-url", "https://never-called.invalid",
                                "--worker-id", "fixture", *extra])


SCHEDULER_ENV = ("AI_WORKER_CONCURRENCY", "LLM_MAX_CONCURRENT_REQUESTS", "AI_WORKER_SHUTDOWN_GRACE_SECONDS")


@pytest.mark.parametrize(("flag", "value"), [("--concurrency", "0"), ("--llm-concurrency", "0"),
                                             ("--shutdown-grace-seconds", "0"),
                                             ("--shutdown-grace-seconds", "nan")])
def test_run_rejects_invalid_scheduler_options_before_any_client(monkeypatch, flag, value):
    for name in ("INTERNAL_API_KEY", "LLM_API_KEY", *SCHEDULER_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=flag):
        asyncio.run(run(_cli(flag, value)))


def test_scheduler_defaults_come_from_process_environment_not_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AI_WORKER_CONCURRENCY=9\nLLM_MAX_CONCURRENT_REQUESTS=9\n"
                                   "AI_WORKER_SHUTDOWN_GRACE_SECONDS=9\n")
    for name in SCHEDULER_ENV:
        monkeypatch.delenv(name, raising=False)
    args = _cli()
    assert (args.concurrency, args.llm_concurrency, args.shutdown_grace_seconds) == (1, None, 180.0)

    monkeypatch.setenv("AI_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("LLM_MAX_CONCURRENT_REQUESTS", "2")
    monkeypatch.setenv("AI_WORKER_SHUTDOWN_GRACE_SECONDS", "30")
    args = _cli()
    assert (args.concurrency, args.llm_concurrency, args.shutdown_grace_seconds) == (4, 2, 30.0)
    assert _cli("--concurrency", "3").concurrency == 3

    monkeypatch.setenv("AI_WORKER_CONCURRENCY", "many")
    with pytest.raises(ValueError, match="AI_WORKER_CONCURRENCY"):
        parser()


@pytest.mark.parametrize("llm_concurrency", [None, 2])
def test_run_wires_slots_llm_cap_and_signal_stop_into_the_loop(monkeypatch, llm_concurrency):
    for name in SCHEDULER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INTERNAL_API_KEY", "synthetic")
    monkeypatch.setenv("LLM_API_KEY", "synthetic")
    seen = {}

    async def fake_loop(worker, **options):
        seen.update(options, worker=worker)
        semaphore = worker.llm_request_semaphore
        if semaphore is not None:  # Exactly llm_concurrency provider requests may run at once.
            for _ in range(llm_concurrency):
                await asyncio.wait_for(semaphore.acquire(), 1)
            seen["capacity_exhausted"] = semaphore.locked()
        # The handler is installed on this loop; a real SIGTERM only sets the stop event.
        assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN, None)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(options["stop_event"].wait(), 1)

    monkeypatch.setattr("scripts.run_validation_worker.run_loop", fake_loop)
    extra = ["--concurrency", "3", "--shutdown-grace-seconds", "5"]
    if llm_concurrency is not None:
        extra += ["--llm-concurrency", str(llm_concurrency)]
    asyncio.run(run(_cli(*extra)))

    assert (seen["once"], seen["concurrency"], seen["shutdown_grace_seconds"]) == (False, 3, 5.0)
    assert seen["stop_event"].is_set()
    semaphore = seen["worker"].llm_request_semaphore
    if llm_concurrency is None:
        assert semaphore is None
    else:
        assert isinstance(semaphore, asyncio.Semaphore) and seen["capacity_exhausted"]


# --- GH344: frozen RAG stays an explicit flag; an empty URL value is a logged keyword-only opt-out ---------

READONLY_ENV = "VALIDATION_READONLY_DATABASE_URL"


def _captured_worker(monkeypatch, *, engines=None):
    for name in SCHEDULER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INTERNAL_API_KEY", "synthetic")
    monkeypatch.setenv("LLM_API_KEY", "synthetic")
    seen = {}

    async def fake_loop(worker, **options):
        seen.update(options, worker=worker)

    def fake_engine(url, **options):
        if engines is None:
            raise AssertionError("no read-only engine may be created")
        engine = SimpleNamespace(url=url, options=options, disposed=False)
        engine.dispose = lambda: setattr(engine, "disposed", True)
        engines.append(engine)
        return engine

    monkeypatch.setattr("scripts.run_validation_worker.run_loop", fake_loop)
    monkeypatch.setattr("scripts.run_validation_worker.create_engine", fake_engine)
    return seen


@pytest.mark.parametrize("once", [True, False])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_readonly_url_runs_keyword_only_with_one_info_line(monkeypatch, caplog, once, value):
    seen = _captured_worker(monkeypatch)
    if value is None:
        monkeypatch.delenv(READONLY_ENV, raising=False)
    else:
        monkeypatch.setenv(READONLY_ENV, value)
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)
    extra = ["--readonly-url-env", READONLY_ENV, "--query-embedding", "--index-version", "v1"]
    asyncio.run(run(_cli(*extra, *(["--once"] if once else []))))

    worker = seen["worker"]
    assert worker.search is None and worker.embedding is None
    assert seen["once"] is once
    messages = [row.getMessage() for row in caplog.records if row.name == RUNNER_LOGGER]
    assert messages.count(f"frozen retrieval disabled: {READONLY_ENV} is empty") == 1
    assert messages.count("query embedding disabled: no read-only retrieval sessions") == 1


@pytest.mark.parametrize("once", [True, False])
def test_query_embedding_without_readonly_sessions_is_logged_not_rejected(monkeypatch, caplog, once):
    seen = _captured_worker(monkeypatch)
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)
    asyncio.run(run(_cli("--query-embedding", *(["--once"] if once else []))))

    assert seen["worker"].search is None and seen["worker"].embedding is None
    assert "query embedding disabled: no read-only retrieval sessions" in caplog.text
    assert "frozen retrieval disabled" not in caplog.text  # No --readonly-url-env flag was given.


def test_readonly_url_builds_a_bounded_read_only_pool_and_the_query_embedding_client(monkeypatch, caplog):
    from app.embeddings.client import OpenAIEmbeddingsClient
    from app.validation.search import FrozenRetrievalSearchService

    engines = []
    seen = _captured_worker(monkeypatch, engines=engines)
    url = "postgresql+psycopg://readonly:synthetic@db.invalid/catchhole"
    monkeypatch.setenv(READONLY_ENV, url)
    caplog.set_level(logging.INFO, logger=RUNNER_LOGGER)
    asyncio.run(run(_cli("--readonly-url-env", READONLY_ENV, "--query-embedding", "--index-version", "v1",
                         "--concurrency", "3")))

    [engine] = engines
    assert engine.url == url and engine.disposed
    # One connection per job slot and no overflow: the deploy budget counts replicas x concurrency.
    # Connect, statement, idle-in-transaction and pool waits are bounded below the driver: a cancelled
    # asyncio wait never stops a blocking read that holds a pooled connection.
    assert engine.options == {
        "pool_size": 3, "max_overflow": 0, "pool_timeout": 5,
        "connect_args": {"connect_timeout": 5,
                         "options": "-c default_transaction_read_only=on -c statement_timeout=5000"
                                    " -c idle_in_transaction_session_timeout=10000"},
    }
    worker = seen["worker"]
    assert isinstance(worker.search, FrozenRetrievalSearchService)
    assert isinstance(worker.embedding, OpenAIEmbeddingsClient)
    assert (worker.embedding.model, worker.embedding.version, worker.embedding.dimensions) == (
        "text-embedding-3-small", "v1", 1536)
    assert "disabled" not in caplog.text and "synthetic@" not in caplog.text


def test_readonly_engine_bounds_match_the_service_readiness_timeout():
    """The database ends a stuck read no later than the asyncio readiness timeout gives up on it."""
    from app.validation.service import VECTOR_READINESS_TIMEOUT_SECONDS
    from scripts import run_validation_worker as runner

    assert runner.READONLY_STATEMENT_TIMEOUT_MS == VECTOR_READINESS_TIMEOUT_SECONDS * 1000 == 5000
    assert runner.READONLY_CONNECT_TIMEOUT_SECONDS == runner.READONLY_POOL_TIMEOUT_SECONDS == 5
    assert runner.READONLY_IDLE_IN_TRANSACTION_TIMEOUT_MS == 10000
    engine = runner.readonly_engine("postgresql+psycopg://readonly:synthetic@db.invalid/catchhole", 2)
    try:  # Pool configuration only; no connection is opened.
        assert (engine.pool.size(), engine.pool._max_overflow, engine.pool._timeout) == (2, 0, 5)
    finally:
        engine.dispose()
