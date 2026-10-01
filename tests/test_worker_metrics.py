import argparse
import asyncio
import socket
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from prometheus_client import CollectorRegistry

from app.clients.exceptions import WorkerLeaseExpiredError
from app.core.config import Settings
from app.llm.exceptions import LlmOutputTruncatedError, LlmResponseValidationError
from app.llm.responses import LlmTextResponse
from app.monitoring.worker_metrics import WorkerMetrics, create_worker_metrics
from app.usage import metering
from app.usage.metering import MeteredTextGenerationClient
from app.worker.analysis_job_worker import WorkerRunResult
from scripts import run_analysis_worker as runner

COMMON = {
    "application": "catchhole-ai",
    "environment": "local",
    "worker_kind": "analysis",
}
LLM = {"purpose": "SETTING_EXTRACTION", "model": "gpt-5.6-sol"}
JOB = {"job_type": "SETTING_EXTRACTION"}
JOB_ID = UUID(int=1)
LEASE = UUID(int=2)


@pytest.fixture
def metrics():
    return WorkerMetrics(
        environment="local",
        worker_kind="analysis",
        allowed_models={"gpt-5.6-sol"},
        registry=CollectorRegistry(),
    )


@pytest.fixture(autouse=True)
def offline_tokenizer(monkeypatch):
    monkeypatch.setattr(metering, "_estimate_text_token_upper_bound", lambda *args: 100)


def value(metrics, name, **labels):
    return metrics.registry.get_sample_value(name, {**COMMON, **labels})


class Ledger:
    def __init__(self, clock=None, reject_second=False):
        self.clock = clock
        self.reject_second = reject_second
        self.reservations = []
        self.settlements = []
        self.releases = []

    async def reserve_ai_tokens(self, **kwargs):
        if self.reject_second and self.reservations:
            raise RuntimeError("reservation rejected")
        self.reservations.append(kwargs)
        if self.clock is not None:
            self.clock[0] += 13_000_000_000

    async def settle_ai_tokens(
        self, request_id, input_tokens, cached_input_tokens, output_tokens, outcome
    ):
        self.settlements.append((input_tokens, cached_input_tokens, output_tokens, outcome))
        if self.clock is not None:
            self.clock[0] += 17_000_000_000

    async def release_ai_tokens(self, request_id, outcome):
        self.releases.append(outcome)


class Delegate:
    def __init__(self, responses, clock=None):
        self.responses = iter(responses)
        self.clock = clock

    async def create_text_response(self, **kwargs):
        if self.clock is not None:
            self.clock[0] += 2_000_000_000
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return result


def client(metrics, responses, ledger=None, clock=None, **kwargs):
    return MeteredTextGenerationClient(
        delegate=Delegate(responses, clock),
        ledger=ledger or Ledger(),
        analysis_job_id=JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-5.6-sol",
        lease_token=LEASE,
        metrics=metrics,
        monotonic_ns=(lambda: clock[0]) if clock is not None else metering.perf_counter_ns,
        **kwargs,
    )


def status_error(status):
    request = httpx.Request("POST", "https://provider.invalid")
    return httpx.HTTPStatusError(
        "private provider message",
        request=request,
        response=httpx.Response(status, request=request),
    )


def test_llm_duration_excludes_reservation_settlement_and_retry_wait(metrics):
    clock = [0]
    ledger = Ledger(clock)

    async def sleep(seconds):
        clock[0] += 19_000_000_000

    metered = client(
        metrics,
        [status_error(429), LlmTextResponse("{}", 120, 20, 30)],
        ledger=ledger,
        clock=clock,
        max_retries=1,
        sleeper=sleep,
    )
    assert asyncio.run(metered.create_text_response("system", "manuscript")).text == "{}"
    assert (
        value(metrics, "catchhole_llm_calls_total", **LLM, outcome="failure", error_type="429") == 1
    )
    assert (
        value(metrics, "catchhole_llm_calls_total", **LLM, outcome="success", error_type="none")
        == 1
    )
    assert value(metrics, "catchhole_llm_call_duration_seconds_sum", **LLM, outcome="failure") == 2
    assert value(metrics, "catchhole_llm_call_duration_seconds_sum", **LLM, outcome="success") == 2
    assert value(metrics, "catchhole_llm_retries_total", **LLM, error_type="429") == 1
    assert value(metrics, "catchhole_llm_usage_unavailable_total", **LLM) == 1
    assert value(metrics, "catchhole_llm_usage_tokens_total", **LLM, token_type="input") == 120
    assert (
        value(metrics, "catchhole_llm_usage_tokens_total", **LLM, token_type="cached_input") == 20
    )
    assert value(metrics, "catchhole_llm_usage_tokens_total", **LLM, token_type="output") == 30
    assert ledger.settlements == [(120, 20, 30, "SUCCESS")]
    assert ledger.releases == ["USAGE_UNAVAILABLE"]


def test_rejected_retry_reservation_does_not_count_an_extra_provider_attempt(metrics):
    ledger = Ledger(reject_second=True)

    async def sleep(seconds):
        pass

    with pytest.raises(RuntimeError, match="reservation rejected"):
        asyncio.run(
            client(
                metrics, [status_error(429)], ledger=ledger, max_retries=1, sleeper=sleep
            ).create_text_response("s", "u")
        )
    assert (
        value(metrics, "catchhole_llm_calls_total", **LLM, outcome="failure", error_type="429") == 1
    )
    assert value(metrics, "catchhole_llm_retries_total", **LLM, error_type="429") is None


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (status_error(503), "5xx"),
        (httpx.ReadTimeout("private timeout"), "timeout"),
        (TimeoutError("private timeout"), "timeout"),
        (httpx.ConnectError("private network"), "network"),
        (
            LlmOutputTruncatedError(
                "private output",
                incomplete_reason="max_output_tokens",
                max_output_tokens=100,
                input_token_count=12,
                cached_input_token_count=2,
                output_token_count=10,
            ),
            "truncated",
        ),
        (LlmResponseValidationError("private response"), "validation"),
        (ValueError("private arbitrary failure"), "other"),
    ],
)
def test_provider_error_labels_are_fixed_and_failure_usage_is_retained(metrics, error, error_type):
    ledger = Ledger()
    with pytest.raises(type(error)):
        asyncio.run(
            client(metrics, [error], ledger=ledger, max_retries=0).create_text_response("s", "u")
        )
    assert (
        value(metrics, "catchhole_llm_calls_total", **LLM, outcome="failure", error_type=error_type)
        == 1
    )
    if error_type == "truncated":
        assert ledger.settlements == [(12, 2, 10, "FAILURE")]
        assert value(metrics, "catchhole_llm_usage_tokens_total", **LLM, token_type="input") == 12
    else:
        assert value(metrics, "catchhole_llm_usage_unavailable_total", **LLM) == 1


def test_provider_cancellation_preserves_ledger_cleanup_and_semaphore(metrics):
    async def scenario():
        ledger = Ledger()
        semaphore = asyncio.Semaphore(1)
        metered = client(
            metrics, [asyncio.CancelledError()], ledger=ledger, request_semaphore=semaphore
        )
        with pytest.raises(asyncio.CancelledError):
            await metered.create_text_response("s", "u")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not semaphore.locked()
        assert ledger.releases == ["USAGE_UNAVAILABLE"]
        assert (
            value(
                metrics,
                "catchhole_llm_calls_total",
                **LLM,
                outcome="canceled",
                error_type="canceled",
            )
            == 1
        )
        assert value(metrics, "catchhole_llm_usage_unavailable_total", **LLM) == 1

    asyncio.run(scenario())


def test_missing_success_usage_is_distinct_from_zero_tokens(metrics):
    asyncio.run(client(metrics, [LlmTextResponse("{}")]).create_text_response("s", "u"))
    assert value(metrics, "catchhole_llm_usage_unavailable_total", **LLM) == 1
    assert value(metrics, "catchhole_llm_usage_tokens_total", **LLM, token_type="input") is None


def test_dynamic_labels_are_normalized_before_series_creation(metrics):
    metrics.record_llm_call(
        "private-purpose", "private-model", "private-outcome", "private-error", 0.5, None
    )
    metrics.record_llm_retry("private-purpose", "private-model", "private-error")
    assert (
        value(
            metrics,
            "catchhole_llm_calls_total",
            purpose="other",
            model="other",
            outcome="unknown",
            error_type="other",
        )
        == 1
    )
    assert (
        value(
            metrics,
            "catchhole_llm_retries_total",
            purpose="other",
            model="other",
            error_type="other",
        )
        == 1
    )
    assert "private-" not in str(list(metrics.registry.collect()))


def test_exporter_serves_one_registry_and_stops(metrics):
    assert value(metrics, "catchhole_worker_jobs_active") == 0
    assert value(metrics, "catchhole_worker_last_job_finished_timestamp_seconds") == 0
    assert metrics.start_exporter("127.0.0.1", 0)
    port = metrics._server.server_port
    assert metrics.start_exporter("127.0.0.1", port)
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/metrics", trust_env=False)
        assert response.status_code == 200
        assert (
            'catchhole_worker_jobs_active{application="catchhole-ai",environment="local",worker_kind="analysis"} 0.0'
            in response.text
        )
        assert "python_info" not in response.text
    finally:
        metrics.stop_exporter()
    assert metrics._server is None
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_bind_failure_is_fixed_warning_and_does_not_disable_recording(metrics, caplog):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert not metrics.start_exporter("127.0.0.1", occupied.getsockname()[1])
    metrics.record_job_started()
    metrics.record_job_finished("SETTING_EXTRACTION", "success", 1)
    assert value(metrics, "catchhole_worker_jobs_active") == 0
    assert value(metrics, "catchhole_worker_job_attempts_total", **JOB, outcome="success") == 1
    assert "Worker metrics operation failed." in caplog.text


def test_metrics_recording_failure_does_not_change_provider_or_ledger(metrics, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private metrics failure")

    monkeypatch.setattr(metrics.llm_calls, "labels", fail)
    ledger = Ledger()
    response = asyncio.run(
        client(metrics, [LlmTextResponse("{}", 3, 1, 2)], ledger=ledger).create_text_response(
            "s", "u"
        )
    )
    assert response.text == "{}"
    assert ledger.settlements == [(3, 1, 2, "SUCCESS")]


@dataclass
class Payload:
    analysis_job_id: UUID = JOB_ID
    job_type: str = "SETTING_EXTRACTION"


class Worker:
    def __init__(self, error=None):
        self.error = error
        self.payload = Payload()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def claim_next(self):
        payload, self.payload = self.payload, None
        return payload

    async def process_claimed(self, payload):
        self.started.set()
        if self.error:
            raise self.error
        await self.release.wait()
        return WorkerRunResult(True, JOB_ID, "completed")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("outcome", ["success", "failure", "lease_lost", "canceled"])
def test_scheduler_records_attempt_and_restores_active_even_on_failure(metrics, outcome):
    async def scenario():
        error = None
        if outcome == "failure":
            error = RuntimeError("job failed")
        if outcome == "lease_lost":
            request = httpx.Request("POST", "http://spring.invalid")
            error = WorkerLeaseExpiredError(
                "lease expired", request=request, response=httpx.Response(409, request=request)
            )
        worker = Worker(error)
        stop = asyncio.Event()
        task = asyncio.create_task(
            runner.run_worker_loop(
                worker,
                0.01,
                1,
                0.01,
                stop_event=stop,
                metrics=metrics,
            )
        )
        await worker.started.wait()
        if error is None:
            assert value(metrics, "catchhole_worker_jobs_active") == 1
        stop.set()
        if outcome == "success":
            worker.release.set()
        await task
        assert value(metrics, "catchhole_worker_jobs_active") == 0
        assert value(metrics, "catchhole_worker_job_attempts_total", **JOB, outcome=outcome) == 1
        assert (
            value(metrics, "catchhole_worker_job_duration_seconds_count", **JOB, outcome=outcome)
            == 1
        )
        assert value(metrics, "catchhole_worker_last_job_finished_timestamp_seconds") > 0

    asyncio.run(scenario())


@pytest.mark.parametrize("close_error", [False, True])
def test_cli_once_uses_same_attempt_metrics_and_stops_exporter_on_close_error(
    metrics,
    monkeypatch,
    close_error,
):
    async def scenario():
        worker = Worker()
        worker.release.set()
        if close_error:

            async def fail_close():
                worker.closed = True
                raise RuntimeError("close failed")

            worker.aclose = fail_close
        monkeypatch.setattr(runner, "AnalysisJobWorker", lambda **kwargs: worker)
        monkeypatch.setattr(runner, "create_worker_metrics", lambda *args, **kwargs: metrics)
        args = argparse.Namespace(
            worker_kind="analysis",
            once=True,
            model_name=None,
            extraction_model_name=None,
            subject_resolution_model_name=None,
            comparison_model_name=None,
        )
        if close_error:
            with pytest.raises(RuntimeError, match="close failed"):
                await runner._async_main(args, Settings(_env_file=None, ai_worker_metrics_port=0))
        else:
            await runner._async_main(args, Settings(_env_file=None, ai_worker_metrics_port=0))
        assert worker.closed
        assert metrics._server is None
        assert value(metrics, "catchhole_worker_job_attempts_total", **JOB, outcome="success") == 1

    asyncio.run(scenario())


def test_disabled_settings_create_no_exporter_or_registry():
    settings = Settings(_env_file=None)
    assert create_worker_metrics(settings, "analysis") is None


def test_registry_creation_failure_keeps_metrics_optional(monkeypatch, caplog):
    from app.monitoring import worker_metrics

    def broken_registry():
        raise RuntimeError("private initialization failure")

    monkeypatch.setattr(worker_metrics, "CollectorRegistry", broken_registry)
    settings = Settings(_env_file=None, ai_worker_metrics_enabled=True)
    assert create_worker_metrics(settings, "analysis") is None
    assert "Worker metrics operation failed." in caplog.text
    assert "private initialization failure" not in caplog.text


def test_startup_model_allowlist_contains_configuration_and_cli_overrides():
    settings = Settings(
        _env_file=None,
        ai_worker_metrics_enabled=True,
        llm_extraction_model="configured-extraction",
        llm_subject_resolution_model="configured-subject",
        llm_comparison_model="configured-comparison",
    )
    metrics = create_worker_metrics(settings, "character-comparison", ["cli-model"])
    for model in (
        "configured-extraction",
        "configured-subject",
        "configured-comparison",
        "cli-model",
    ):
        metrics.record_llm_call("SETTING_EXTRACTION", model, "success", "none", 1, (1, 0, 1))
        assert (
            metrics.registry.get_sample_value(
                "catchhole_llm_calls_total",
                {
                    **COMMON,
                    "worker_kind": "character-comparison",
                    "purpose": "SETTING_EXTRACTION",
                    "model": model,
                    "outcome": "success",
                    "error_type": "none",
                },
            )
            == 1
        )
    metrics.record_llm_call(
        "SETTING_EXTRACTION", "unexpected-response-model", "success", "none", 1, None
    )
    assert (
        metrics.registry.get_sample_value(
            "catchhole_llm_calls_total",
            {
                **COMMON,
                "worker_kind": "character-comparison",
                "purpose": "SETTING_EXTRACTION",
                "model": "other",
                "outcome": "success",
                "error_type": "none",
            },
        )
        == 1
    )


def test_cancel_during_cleanup_does_not_count_retry_or_lose_provider_call(metrics):
    async def scenario():
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        class BlockingCleanupLedger(Ledger):
            async def release_ai_tokens(self, request_id, outcome):
                cleanup_started.set()
                await cleanup_release.wait()
                await super().release_ai_tokens(request_id, outcome)

        ledger = BlockingCleanupLedger()
        metered = client(metrics, [status_error(429)], ledger=ledger, max_retries=1)
        task = asyncio.create_task(metered.create_text_response("s", "u"))
        await cleanup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cleanup_release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert ledger.releases == ["USAGE_UNAVAILABLE"]
        assert value(metrics, "catchhole_llm_retries_total", **LLM, error_type="429") is None
        assert (
            value(metrics, "catchhole_llm_calls_total", **LLM, outcome="failure", error_type="429")
            == 1
        )

    asyncio.run(scenario())


def test_metrics_counter_failure_does_not_leak_worker_slot_or_active(metrics, monkeypatch):
    async def scenario():
        def fail(*args, **kwargs):
            raise RuntimeError("private metrics failure")

        monkeypatch.setattr(metrics.job_attempts, "labels", fail)
        worker = Worker()
        worker.release.set()
        results = await runner.run_worker_loop(
            worker, 0.01, 1, 0.01, max_iterations=2, metrics=metrics
        )
        assert [result.analysis_job_id for result in results] == [JOB_ID]
        assert worker.payload is None
        assert value(metrics, "catchhole_worker_jobs_active") == 0

    asyncio.run(scenario())


def test_transient_active_update_failure_does_not_make_finished_gauge_negative(
    metrics, monkeypatch
):
    labels = metrics.jobs_active.labels
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("private gauge failure")
        return labels(*args, **kwargs)

    monkeypatch.setattr(metrics.jobs_active, "labels", fail_once)
    metrics.record_job_started()
    metrics.record_job_finished("SETTING_EXTRACTION", "success", 1)
    assert value(metrics, "catchhole_worker_jobs_active") == 0
