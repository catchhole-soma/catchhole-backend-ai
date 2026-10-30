"""GH340: one validation job's stop/failure never ends the long-running runner (no DB/network)."""
import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.validation.client import ValidationExecutionClient
from app.validation.contracts import IncompleteInspection
from app.worker.episode_validation_worker import EpisodeValidationWorker
from scripts.run_validation_worker import MAX_BACKOFF_SECONDS, _backoff_seconds, run_loop

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
