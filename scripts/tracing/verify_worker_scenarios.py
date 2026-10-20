"""Reproduce six synthetic Worker trace scenarios, without live business I/O.

Run from the repository with its editable Python installation:
  python -m scripts.tracing.verify_worker_scenarios --output /tmp/worker-scenarios.json
  python -m scripts.tracing.verify_worker_scenarios --export-otlp --output /tmp/worker-otlp.json

OTLP export is opt-in and restricted to a loopback Collector. The JSON records
in-memory assertions and expected spans for separate Tempo readback; successful
flush/HTTP acceptance alone does not prove Tempo storage or production behavior.
"""

import argparse
import asyncio
import json
import math
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

from app.core.config import Settings
from app.monitoring.worker_metrics import WorkerMetrics
from app.monitoring.worker_tracing import WorkerTracingRuntime, set_process_tracing
from app.usage import metering
from scripts.run_analysis_worker import _run_claimed_with_metrics
from scripts.tracing._worker_scenario_fixtures import (
    CANARY,
    MODEL,
    NotifyingSemaphore,
    SyntheticProvider,
    SyntheticSpring,
    make_worker,
)

SERVICE_NAME = "catchhole-ai-worker-stage6-verification"
SCENARIOS = (
    "normal", "slow_call", "semaphore_wait", "retry_429",
    "cancellation_settlement", "concurrent_roots",
)
WAIT_SECONDS = 0.15
SLOW_SECONDS = 0.18


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def local_endpoint(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid loopback OTLP endpoint.") from None
    if (
        parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None or parsed.password is not None
        or parsed.path != "/v1/traces" or parsed.query or parsed.fragment
        or port == 0
    ):
        raise argparse.ArgumentTypeError("OTLP must use a loopback HTTP /v1/traces endpoint.")
    return value


@contextmanager
def isolated_telemetry_environment():
    """Keep inherited exporter headers, credentials, SDK limits, and resources out."""
    previous = {key: value for key, value in os.environ.items() if key.startswith("OTEL_")}
    for key in previous:
        os.environ.pop(key)
    try:
        yield
    finally:
        for key in tuple(os.environ):
            if key.startswith("OTEL_"):
                os.environ.pop(key)
        os.environ.update(previous)


def synthetic_settings():
    # _env_file=None alone still reads environment variables. Fixture settings must
    # not inherit a production database, model, API credential, or worker limit.
    with patch.dict(os.environ, {}, clear=True):
        return Settings(
            _env_file=None, app_env="local", llm_model=MODEL,
            llm_extraction_model=MODEL, llm_subject_resolution_model=MODEL,
            llm_comparison_model=MODEL, llm_max_concurrent_requests=2,
        )


class ObservedOTLPExporter(SpanExporter):
    def __init__(self, endpoint):
        session = requests.Session()
        session.trust_env = False  # No environment proxy or .netrc credentials.
        try:
            with isolated_telemetry_environment():
                self.delegate = OTLPSpanExporter(
                    endpoint=local_endpoint(endpoint), timeout=5, headers={}, session=session,
                )
        except BaseException:
            session.close()
            raise
        self.accepted_spans = 0
        self.failed_batches = 0

    def export(self, spans):
        result = self.delegate.export(spans)
        if result == SpanExportResult.SUCCESS:
            self.accepted_spans += len(spans)
        else:
            self.failed_batches += 1
        return result

    def shutdown(self):
        self.delegate.shutdown()


def duration_seconds(span):
    return (span.end_time - span.start_time) / 1_000_000_000


def named(spans, name):
    return [span for span in spans if span.name == name]


def metric_sum(metrics, metric_name, **filters):
    return sum(
        sample.value
        for family in metrics.registry.collect()
        for sample in family.samples
        if sample.name == metric_name
        and all(sample.labels.get(key) == value for key, value in filters.items())
    )


def summarize_trace(spans, trace_id):
    selected = sorted(
        (span for span in spans if span.context.trace_id == trace_id),
        key=lambda span: (span.start_time, span.context.span_id),
    )
    return {
        "trace_id": f"{trace_id:032x}", "expected_span_count": len(selected),
        "spans": [{
            "span_id": f"{span.context.span_id:016x}",
            "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
            "name": span.name, "kind": span.kind.name,
            "status": span.status.status_code.name,
            "start_unix_nano": span.start_time, "end_unix_nano": span.end_time,
            "duration_ms": round(duration_seconds(span) * 1000, 3),
            "attributes": dict(span.attributes),
        } for span in selected],
    }


def check_scenario(name, spans, metrics, providers, springs):
    roots = named(spans, "analysis.job.execute")
    requests = named(spans, "llm.request")
    calls = named(spans, "llm.call")
    expected_jobs = 2 if name == "concurrent_roots" else 1
    expected_calls = 2 if name in {"concurrent_roots", "retry_429"} else 1
    root_ids = {span.context.trace_id for span in roots}
    require(len(roots) == len(root_ids) == expected_jobs, "Independent root count mismatch.")
    require(all(span.parent is None for span in roots), "Execution root unexpectedly has a parent.")
    require(len(requests) == expected_jobs, "Unexpected logical LLM request count.")
    require(len(calls) == sum(provider.call_count for provider in providers) == expected_calls,
            "Actual delegate calls do not match call spans.")
    by_id = {span.context.span_id: span for span in spans}
    for span in spans:
        require(span.context.trace_id in root_ids, "A child escaped its execution trace.")
        if span.parent:
            parent = by_id.get(span.parent.span_id)
            require(parent is not None and parent.context.trace_id == span.context.trace_id,
                    "Missing parent or cross-job parent relationship.")
            require(span.start_time >= parent.start_time, "Child started before its parent.")
        require(not span.events and span.status.description is None,
                "Unexpected exception event or status description.")
        require(CANARY not in span.to_json(), "Synthetic privacy canary escaped into telemetry.")
    request_ids = {span.context.span_id for span in requests}
    for span in spans:
        if span.name in {"llm.call", "llm.semaphore.wait", "llm.tokens.reserve",
                         "llm.tokens.finalize", "llm.tokens.release", "llm.retry.backoff"}:
            require(span.parent.span_id in request_ids, "LLM child has the wrong logical parent.")

    llm_duration = metric_sum(metrics, "catchhole_llm_call_duration_seconds_sum")
    call_duration = sum(duration_seconds(span) for span in calls)
    request_duration = sum(duration_seconds(span) for span in requests)
    require(metric_sum(metrics, "catchhole_llm_calls_total") == expected_calls,
            "Call Counter changed its meaning.")
    require(metric_sum(metrics, "catchhole_llm_call_duration_seconds_count") == expected_calls,
            "Histogram observation count does not match delegate calls.")
    require(abs(llm_duration - call_duration) <= 0.05,
            "LLM Histogram includes time outside delegate calls.")
    require(request_duration > llm_duration + 0.02,
            "Logical request failed to include synthetic ledger/wait overhead.")
    require(metric_sum(metrics, "catchhole_worker_jobs_active") == 0,
            "Job active Gauge did not return to zero.")
    retries = metric_sum(metrics, "catchhole_llm_retries_total")
    require(retries == int(name == "retry_429"), "Retry Counter is not actual extra calls.")
    require(sum(spring.failed for spring in springs) == 0, "Unexpected business failure report.")

    checks = ["independent_roots", "parent_and_trace_consistency", "privacy_canary_absent",
              "delegate_only_histogram", "actual_retry_counter", "job_gauge_restored"]
    if name == "slow_call":
        require(duration_seconds(calls[0]) >= SLOW_SECONDS * 0.9, "Slow delegate delay is missing.")
        checks.append("slow_time_is_in_llm_call")
    elif name == "semaphore_wait":
        wait, = named(spans, "llm.semaphore.wait")
        require(duration_seconds(wait) >= WAIT_SECONDS * 0.9, "Semaphore wait was not measured.")
        require(calls[0].start_time >= wait.end_time, "Provider started before acquiring its slot.")
        checks.append("semaphore_wait_excluded_from_delegate_time")
    elif name == "retry_429":
        backoff, = named(spans, "llm.retry.backoff")
        ordered = sorted(calls, key=lambda span: span.start_time)
        require(ordered[0].attributes.get("error.type") == "429", "Failed attempt lost 429.")
        require(ordered[1].attributes.get("outcome") == "success", "Second attempt did not succeed.")
        require(ordered[0].end_time <= backoff.start_time < backoff.end_time <= ordered[1].start_time,
                "Retry/backoff timeline is inconsistent.")
        require(duration_seconds(backoff) >= WAIT_SECONDS * 0.9, "Backoff duration is missing.")
        require(springs[0].releases == 1 and springs[0].settlements == 1,
                "Retry changed token cleanup/settlement behavior.")
        checks.append("429_backoff_then_success")
    elif name == "cancellation_settlement":
        settlement, = named(spans, "llm.tokens.finalize")
        require(roots[0].attributes.get("outcome") == "canceled", "Root cancellation was lost.")
        require(requests[0].attributes.get("outcome") == "canceled", "LLM cancellation was lost.")
        require(settlement.start_time < roots[0].end_time < settlement.end_time,
                "Shielded settlement did not outlive the canceled root.")
        require(settlement.attributes.get("outcome") == "success" and springs[0].settlements == 1,
                "Shielded settlement did not finish exactly once.")
        require(springs[0].completed == 0 and springs[0].releases == 0,
                "Canceled Worker incorrectly completed or released successful usage.")
        require(metric_sum(metrics, "catchhole_worker_job_attempts_total", outcome="canceled") == 1,
                "Canceled attempt metric changed.")
        checks.append("task_canceled_settlement_finished_after_root")
    elif name == "concurrent_roots":
        require({span.context.trace_id for span in calls} == root_ids,
                "Concurrent delegate calls were attached to the same trace.")
        for trace_id in root_ids:
            require(sum(span.context.trace_id == trace_id for span in requests) == 1
                    and sum(span.context.trace_id == trace_id for span in calls) == 1,
                    "Each concurrent execution must own exactly one request and call.")
        require(max(span.start_time for span in calls) < min(span.end_time for span in calls),
                "The two controlled provider calls did not overlap.")
        checks.append("overlapping_calls_in_distinct_traces")
    if name != "cancellation_settlement":
        require(all(span.attributes.get("outcome") == "success" for span in roots),
                "Successful Worker did not report success.")
        require(sum(spring.completed for spring in springs) == expected_jobs,
                "Normal Worker completion count mismatch.")
    return {
        "scenario": name, "synthetic": True, "passed": True, "checks": checks,
        "metrics": {
            "delegate_calls": expected_calls, "actual_additional_retries": retries,
            "llm_histogram_count": expected_calls,
            "llm_histogram_sum_seconds": llm_duration,
            "llm_call_span_sum_seconds": call_duration,
            "llm_request_span_sum_seconds": request_duration,
            "job_active_final": 0,
        },
        "traces": [summarize_trace(spans, trace_id) for trace_id in sorted(root_ids)],
    }


async def drain_ledger_tasks():
    # Existing production cancellation helper intentionally retains shielded tasks.
    tasks = tuple(metering._detached_ledger_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)


async def run_scenario(name, memory):
    before = len(memory.get_finished_spans())
    metrics = WorkerMetrics("local", "analysis", {MODEL})
    providers, springs, workers, tasks = [], [], [], []
    concurrent_release = asyncio.Event() if name == "concurrent_roots" else None
    semaphore = NotifyingSemaphore() if name == "semaphore_wait" else asyncio.Semaphore(2)
    if name == "semaphore_wait":
        await semaphore.acquire()
        semaphore.acquire_started.clear()
    try:
        for _ in range(2 if name == "concurrent_roots" else 1):
            provider = SyntheticProvider(
                SLOW_SECONDS if name == "slow_call" else 0.03,
                retry_once=name == "retry_429", release=concurrent_release,
            )
            spring = SyntheticSpring(hold_settlement=name == "cancellation_settlement")
            worker, payload = make_worker(provider, spring, metrics, semaphore)
            providers.append(provider)
            springs.append(spring)
            workers.append(worker)
            tasks.append(asyncio.create_task(_run_claimed_with_metrics(worker, payload, metrics)))
        if name == "semaphore_wait":
            await asyncio.wait_for(semaphore.acquire_started.wait(), timeout=3)
            await asyncio.sleep(WAIT_SECONDS)
            require(providers[0].call_count == 0, "Delegate ran while its slot was held.")
            semaphore.release()
        elif name == "concurrent_roots":
            await asyncio.wait_for(asyncio.gather(*(p.started.wait() for p in providers)), timeout=3)
            concurrent_release.set()
        elif name == "cancellation_settlement":
            await asyncio.wait_for(springs[0].settlement_started.wait(), timeout=3)
            tasks[0].cancel()
            try:
                await tasks[0]
            except asyncio.CancelledError:
                pass
            else:
                raise VerificationError("Worker swallowed Task cancellation.")
            require(not named(memory.get_finished_spans()[before:], "llm.tokens.finalize"),
                    "Settlement span ended with its owner before settlement finished.")
            await asyncio.sleep(WAIT_SECONDS)
            springs[0].allow_settlement.set()
            await drain_ledger_tasks()
        if name != "cancellation_settlement":
            await asyncio.gather(*tasks)
        spans = memory.get_finished_spans()[before:]
        return check_scenario(name, spans, metrics, providers, springs)
    finally:
        for spring in springs:
            spring.allow_settlement.set()
        if concurrent_release is not None:
            concurrent_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await drain_ledger_tasks()
        for worker in workers:
            await worker.aclose()


async def verify_scenarios(*, export_otlp=False, endpoint="http://127.0.0.1:4318/v1/traces"):
    # This standalone verification owns its SDK runtime for the entire run.
    with isolated_telemetry_environment():
        return await _verify_scenarios(export_otlp=export_otlp, endpoint=endpoint)


async def _verify_scenarios(*, export_otlp, endpoint):
    if export_otlp:
        local_endpoint(endpoint)
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({
        "service.name": SERVICE_NAME, "service.instance.id": str(uuid4()),
        "deployment.environment.name": "local", "verification.synthetic": True,
    }), sampler=ParentBased(ALWAYS_ON), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    runtime = None
    try:
        network_exporter = ObservedOTLPExporter(endpoint) if export_otlp else None
        if network_exporter:
            provider.add_span_processor(BatchSpanProcessor(
                network_exporter, schedule_delay_millis=100, max_queue_size=1024,
                max_export_batch_size=128, export_timeout_millis=5000,
            ))
        runtime = WorkerTracingRuntime(provider, allowed_models={MODEL})
        previous = set_process_tracing(runtime)
        settings = synthetic_settings()
        with patch("app.worker.analysis_job_worker.get_settings", return_value=settings):
            async with asyncio.timeout(15):
                results = [await run_scenario(name, memory) for name in SCENARIOS]
        require(provider.force_flush(timeout_millis=10000), "SDK flush did not finish.")
        if network_exporter:
            require(network_exporter.failed_batches == 0, "Collector rejected an OTLP export.")
            require(network_exporter.accepted_spans == len(memory.get_finished_spans()),
                    "Collector did not accept all generated spans.")
        return {
            "schema_version": 1, "synthetic": True, "passed": True,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "service_name": SERVICE_NAME, "scenario_count": len(results),
            "trace_count": sum(len(result["traces"]) for result in results),
            "span_count": len(memory.get_finished_spans()),
            "injected_delays_seconds": {
                "normal_delegate": 0.03, "slow_delegate": SLOW_SECONDS,
                "semaphore_hold": WAIT_SECONDS, "retry_backoff": WAIT_SECONDS,
                "post_cancellation_settlement_hold": WAIT_SECONDS,
            },
            "otlp": {
                "enabled": export_otlp,
                "collector_accepted_spans": network_exporter.accepted_spans if network_exporter else 0,
                "tempo_readback": "not_checked",
            },
            "limits": [
                "All inputs and dependencies are synthetic; no LLM, business HTTP, DB, or S3 calls.",
                "Task cancellation is not SIGKILL, process restart, lease recovery, or production proof.",
                "Retry delay is shortened and jitter disabled only in these injected fixtures.",
                "Empty extraction/comparison results do not validate model quality or persistence.",
                "Collector acceptance is separate from Tempo readback and retention.",
            ],
            "scenarios": results,
        }
    finally:
        if runtime is not None:
            set_process_tracing(previous)
            runtime.shutdown()
        else:
            provider.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-otlp", action="store_true", help="Send synthetic spans to local Collector.")
    parser.add_argument("--endpoint", type=local_endpoint, default="http://127.0.0.1:4318/v1/traces")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must be a new file; existing verification evidence is preserved.")
    report = asyncio.run(verify_scenarios(export_otlp=args.export_otlp, endpoint=args.endpoint))
    require(all(math.isfinite(value) for scenario in report["scenarios"]
                for value in scenario["metrics"].values()), "Non-finite metric summary.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({
        "passed": report["passed"], "synthetic": True, "scenario_count": report["scenario_count"],
        "trace_count": report["trace_count"], "span_count": report["span_count"],
        "output": str(args.output.resolve()), "otlp": report["otlp"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
