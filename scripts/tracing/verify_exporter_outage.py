"""Opt-in synthetic lab for Worker tracing delivery failure and bounded queues.

Only temporary loopback receivers are used. This does not stop a shared Collector
or call a real LLM, Spring, database, or S3. Lab queue/batch/timeout settings are
explicitly different from the application defaults and are not capacity advice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.llm.responses import LlmTextResponse
from app.monitoring.worker_metrics import set_process_metrics
from app.monitoring.worker_tracing import (
    WorkerTracingRuntime,
    set_process_tracing,
    worker_span,
)
from app.usage.metering import MeteredTextGenerationClient

MODEL = "gpt-5.6-sol"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def isolated_export_environment():
    # Explicit endpoints are not enough: exporter headers and TLS/compression
    # settings can otherwise come from ambient OTel environment variables.
    saved = {key: value for key, value in os.environ.items() if key.startswith("OTEL_")}
    for key in saved:
        del os.environ[key]
    logger = logging.getLogger("opentelemetry")
    old_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(old_level)
        os.environ.update(saved)


class Receiver(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode="healthy", port=0):
        super().__init__(("127.0.0.1", port), ReceiverHandler)
        self.mode = mode
        self.lock = threading.Lock()
        self.request_started = threading.Event()
        self.release = threading.Event()
        self.accepted = set()
        self.request_count = 0
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_port}/v1/traces"

    def accepted_ids(self):
        with self.lock:
            return set(self.accepted)

    def close(self):
        self.release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class ReceiverHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        require(self.path == "/v1/traces" and 0 < size < 1_000_000,
                "Unexpected local OTLP request")
        message = ExportTraceServiceRequest.FromString(self.rfile.read(size))
        ids = {span.span_id.hex() for resource in message.resource_spans
               for scope in resource.scope_spans for span in scope.spans}
        with self.server.lock:
            mode = self.server.mode
            self.server.request_count += 1
        self.server.request_started.set()
        if mode in {"slow", "gate"}:
            self.server.release.wait(timeout=2)
        status = 503 if mode in {"unavailable", "slow"} else 200
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.flush()
            if status == 200:
                with self.server.lock:
                    self.server.accepted.update(ids)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the exporter times out before the slow reply.


class EndedSpans(SpanProcessor):
    """Observe completed IDs only; no changes to Span data or delivery behavior."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ended = {}

    def on_end(self, span):
        with self.lock:
            self.ended[f"{span.context.span_id:016x}"] = f"{span.context.trace_id:032x}"

    def snapshot(self):
        with self.lock:
            return dict(self.ended)


class ObservedExporter(SpanExporter):
    def __init__(self, delegate):
        self.delegate = delegate
        self.lock = threading.Lock()
        self.attempts = []
        self.active = 0

    def export(self, spans):
        attempt = {"span_ids": [f"{span.context.span_id:016x}" for span in spans]}
        start = time.perf_counter()
        with self.lock:
            self.attempts.append(attempt)
            self.active += 1
        try:
            result = self.delegate.export(spans)
            attempt["result"] = "success" if result == SpanExportResult.SUCCESS else "failure"
            return result
        except Exception:
            attempt["result"] = "exception"
            raise
        finally:
            with self.lock:
                attempt["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
                self.active -= 1

    def snapshot(self):
        with self.lock:
            return [dict(attempt) for attempt in self.attempts]

    def shutdown(self):
        self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)


def build_lab_runtime(endpoint, *, queue_size, timeout_seconds):
    session = requests.Session()
    session.trust_env = False
    exporter = ObservedExporter(OTLPSpanExporter(
        endpoint=endpoint, timeout=timeout_seconds, headers={}, session=session,
    ))
    provider = TracerProvider(
        resource=Resource({"service.name": "catchhole-exporter-outage-lab"}),
        sampler=ParentBased(ALWAYS_ON), shutdown_on_exit=False,
    )
    ended = EndedSpans()
    provider.add_span_processor(ended)
    provider.add_span_processor(BatchSpanProcessor(
        exporter, max_queue_size=queue_size, max_export_batch_size=1,
        schedule_delay_millis=10, export_timeout_millis=int(timeout_seconds * 1000),
    ))
    return WorkerTracingRuntime(provider, allowed_models={MODEL}), ended, exporter


async def synthetic_business(wait_for_receiver=None):
    counts = {"provider_calls": 0, "reservations": 0, "settlements": 0, "releases": 0}

    class Ledger:
        async def reserve_ai_tokens(self, **_kwargs):
            counts["reservations"] += 1

        async def settle_ai_tokens(self, *_args, **_kwargs):
            counts["settlements"] += 1

        async def release_ai_tokens(self, *_args, **_kwargs):
            counts["releases"] += 1

    class Llm:
        async def create_text_response(self, **_kwargs):
            counts["provider_calls"] += 1
            if wait_for_receiver is not None:
                started = await asyncio.to_thread(wait_for_receiver.wait, 1)
                require(started, "Exporter did not reach the slow local receiver")
            await asyncio.sleep(0.002)
            return LlmTextResponse("{}", input_token_count=10, output_token_count=5)

    client = MeteredTextGenerationClient(
        delegate=Llm(), ledger=Ledger(), analysis_job_id=uuid4(), lease_token=uuid4(),
        purpose="SETTING_EXTRACTION", default_model=MODEL, max_retries=0,
    )
    start = time.perf_counter()
    with worker_span("analysis.job.execute", root=True):
        context = trace.get_current_span().get_span_context()
        trace_id = f"{context.trace_id:032x}"
        root_span_id = f"{context.span_id:016x}"
        response = await client.create_text_response("synthetic system", "synthetic manuscript")
    elapsed = (time.perf_counter() - start) * 1000
    require(response.text == "{}", "Synthetic business result changed")
    require(counts == {"provider_calls": 1, "reservations": 1, "settlements": 1, "releases": 0},
            "Tracing failure changed reservation/provider/settlement behavior")
    return {"trace_id": trace_id, "root_span_id": root_span_id,
            "elapsed_ms": round(elapsed, 3), **counts}


async def flush(runtime):
    result = await asyncio.wait_for(
        asyncio.to_thread(runtime.provider.force_flush, timeout_millis=5000), timeout=6,
    )
    require(result, "Lab batch processing did not finish within the bounded flush")
    # Flush means the SDK processed the queue, not that delivery succeeded.


async def wait_for_received(receiver, ids):
    # The HTTP exporter can observe the response just before the receiver thread
    # finishes updating its evidence set. Do not turn that race into test failure.
    deadline = time.monotonic() + 1
    while not ids <= receiver.accepted_ids() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    require(ids <= receiver.accepted_ids(), "Local receiver did not acknowledge the expected spans")


async def outage_scenario(mode):
    receiver = None
    held_socket = None
    if mode == "connection-refused":
        held_socket = socket.socket()
        held_socket.bind(("127.0.0.1", 0))  # Bound but never listening: ECONNREFUSED.
        port = held_socket.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}/v1/traces"
    else:
        receiver = Receiver("unavailable" if mode == "http-503" else "slow")
        endpoint = receiver.endpoint
    runtime, ended, exporter = build_lab_runtime(endpoint, queue_size=64, timeout_seconds=0.4)
    previous = set_process_tracing(runtime)
    try:
        business = await synthetic_business(receiver.request_started if mode == "slow-response" else None)
        with exporter.lock:
            business_finished_while_export_active = exporter.active > 0
        if mode == "slow-response":
            require(business_finished_while_export_active,
                    "Slow-export experiment did not demonstrate business finishing before export")
        await flush(runtime)
        failed_attempts = [a for a in exporter.snapshot() if a["result"] != "success"]
        require(bool(failed_attempts), "No real OTLP exporter failure was observed")
        outage_ids = {sid for sid, tid in ended.snapshot().items() if tid == business["trace_id"]}
        failed_ids = {sid for attempt in failed_attempts for sid in attempt["span_ids"]}
        require(failed_ids == outage_ids,
                "Outage spans were not all attempted and failed before receiver recovery")
        require(receiver is None or not receiver.accepted_ids().intersection(outage_ids),
                "Outage receiver unexpectedly acknowledged spans")

        if held_socket is not None:
            held_socket.close()
            held_socket = None
            receiver = Receiver(port=port)
        else:
            receiver.mode = "healthy"
            receiver.release.set()
        recovered = await synthetic_business()
        await flush(runtime)
        recovery_ids = {sid for sid, tid in ended.snapshot().items() if tid == recovered["trace_id"]}
        require(recovered["root_span_id"] in recovery_ids
                and recovered["trace_id"] != business["trace_id"],
                "Recovery did not produce a new completed root trace")
        await wait_for_received(receiver, recovery_ids)
        return {
            "scenario": mode, "lab": {"queue": 64, "batch": 1, "schedule_ms": 10, "export_timeout_s": 0.4},
            "business_during_outage": business,
            "business_finished_while_export_active": business_finished_while_export_active,
            "completed_spans_during_outage": len(outage_ids),
            "failed_export_batches": len(failed_attempts),
            "failed_export_batch_elapsed_ms": [a["elapsed_ms"] for a in failed_attempts],
            "business_after_recovery": recovered, "recovery_received_spans": len(recovery_ids),
            "recovery_delivery_verified": True,
            "old_failed_spans_recovered": len(outage_ids & receiver.accepted_ids()),
        }
    finally:
        set_process_tracing(previous)
        if receiver is not None:
            receiver.release.set()
        await asyncio.to_thread(runtime.shutdown)
        if receiver is not None:
            receiver.close()
        if held_socket is not None:
            held_socket.close()


async def queue_overflow_scenario():
    receiver = Receiver("gate")
    runtime, ended, exporter = build_lab_runtime(receiver.endpoint, queue_size=4, timeout_seconds=3)
    previous = set_process_tracing(runtime)
    try:
        with worker_span("verification.export.blocker", root=True):
            pass
        require(await asyncio.to_thread(receiver.request_started.wait, 1),
                "Queue experiment did not start its first export")
        jobs = [await synthetic_business() for _ in range(4)]
        with exporter.lock:
            require(exporter.active == 1, "First export ended before the queue overflow experiment")
        all_ended = ended.snapshot()
        require(len(all_ended) > 5, "Not enough completed spans to overflow the lab queue")
        receiver.release.set()
        await flush(runtime)
        attempts = exporter.snapshot()
        attempted_ids = {sid for a in attempts for sid in a["span_ids"]}
        dropped = set(all_ended) - attempted_ids
        require(all(a["result"] == "success" for a in attempts),
                "Queue experiment conflated queue overflow and transport failure")
        require(len(attempted_ids) == 5 and len(dropped) == len(all_ended) - 5,
                "Expected one in-flight span plus four queued spans after overflow")
        await wait_for_received(receiver, attempted_ids)
        require(receiver.accepted_ids() == attempted_ids, "Receiver did not acknowledge every exported span")
        require(bool(dropped), "No bounded-queue span loss was observed")
        return {
            "scenario": "bounded-queue-overflow",
            "lab": {"queue": 4, "batch": 1, "schedule_ms": 10, "export_timeout_s": 3},
            "business_jobs": jobs, "successful_business_jobs": len(jobs),
            "completed_span_count": len(all_ended), "export_attempted_span_count": len(attempted_ids),
            "receiver_acknowledged_span_count": len(receiver.accepted_ids()),
            "dropped_before_export_count": len(dropped), "drop_observed": True,
            "drop_evidence": "completed span IDs absent from every export attempt after flush",
            "completed_trace_count": len(set(all_ended.values())),
            "traces_with_acknowledged_spans": len({all_ended[sid] for sid in attempted_ids}),
            "trace_completeness_not_guaranteed": True,
        }
    finally:
        receiver.release.set()
        set_process_tracing(previous)
        await asyncio.to_thread(runtime.shutdown)
        receiver.close()


async def run():
    previous_metrics = set_process_metrics(None)
    try:
        # Avoid tokenizer asset downloads; this experiment tests tracing delivery,
        # not input token estimation or the model/analysis business algorithm.
        with patch("app.usage.metering._estimate_text_token_upper_bound", return_value=100):
            cases = [await outage_scenario(mode) for mode in
                     ("connection-refused", "http-503", "slow-response")]
            cases.append(await queue_overflow_scenario())
        return cases
    finally:
        set_process_metrics(previous_metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="catchhole-gh271-outage-")) / "verification.json"
    require(not output.exists() and output.parent.is_dir(), "Output must be a new file in an existing directory")
    with isolated_export_environment():
        cases = asyncio.run(run())
    summary = {
        "synthetic": True, "actual_worker_tracing_runtime": True, "actual_batch_span_processor": True,
        "actual_otlp_http_exporter": True, "actual_metered_llm_wrapper": True,
        "real_llm_spring_db_s3_called": False, "shared_collector_modified": False,
        "application_runtime_defaults_modified": False,
        "scope": "local delivery-failure behavior, not production performance or durable audit guarantees",
        "shutdown_guarantees_delivery": False, "cases": cases,
    }
    with output.open("x") as result:
        result.write(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"output": str(output), **summary}, indent=2))


if __name__ == "__main__":
    main()
