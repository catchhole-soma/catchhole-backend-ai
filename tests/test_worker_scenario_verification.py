import argparse
import asyncio
import json
import os
import socket

import httpx
import pytest
import requests
from opentelemetry import trace

from app.monitoring.worker_tracing import get_process_tracing
from scripts.tracing import verify_worker_scenarios as verification
from scripts.tracing._worker_scenario_fixtures import CANARY, MODEL


def test_worker_scenarios_are_offline_isolated_and_report_real_boundaries(monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("In-memory verification attempted external I/O.")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(requests.Session, "request", forbid_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbid_network)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=synthetic-secret")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private=synthetic-secret")
    monkeypatch.setenv("DATABASE_URL", "synthetic-invalid-database")
    monkeypatch.setenv("LLM_MODEL", "synthetic-env-model-must-not-be-used")
    monkeypatch.setenv("LLM_MAX_CONCURRENT_REQUESTS", "not-an-integer")
    previous_runtime = get_process_tracing()
    global_provider = trace.get_tracer_provider()

    report = asyncio.run(verification.verify_scenarios())

    assert report["passed"] and report["synthetic"]
    assert report["scenario_count"] == 6
    assert report["trace_count"] == 7
    assert report["otlp"] == {
        "enabled": False, "collector_accepted_spans": 0, "tempo_readback": "not_checked",
    }
    assert get_process_tracing() is previous_runtime
    assert trace.get_tracer_provider() is global_provider
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "authorization=synthetic-secret"
    serialized = json.dumps(report)
    assert CANARY not in serialized
    assert "synthetic-secret" not in serialized
    assert "synthetic-env-model-must-not-be-used" not in serialized
    results = {item["scenario"]: item for item in report["scenarios"]}
    assert results["retry_429"]["metrics"]["delegate_calls"] == 2
    assert results["retry_429"]["metrics"]["actual_additional_retries"] == 1
    assert results["semaphore_wait"]["metrics"]["actual_additional_retries"] == 0
    for result in results.values():
        metrics = result["metrics"]
        assert metrics["llm_request_span_sum_seconds"] > metrics["llm_histogram_sum_seconds"]
        assert metrics["llm_call_span_sum_seconds"] == pytest.approx(
            metrics["llm_histogram_sum_seconds"], abs=0.05,
        )
    concurrent = results["concurrent_roots"]["traces"]
    assert len({item["trace_id"] for item in concurrent}) == 2
    for item in concurrent:
        names = [span["name"] for span in item["spans"]]
        assert names.count("analysis.job.execute") == names.count("llm.request") == 1
        assert names.count("llm.call") == 1
        assert {"analysis.prepare", "analysis.character.extract", "analysis.finalize"} <= set(names)
    canceled = results["cancellation_settlement"]["traces"][0]["spans"]
    root = next(span for span in canceled if span["name"] == "analysis.job.execute")
    settlement = next(span for span in canceled if span["name"] == "llm.tokens.finalize")
    assert root["attributes"]["outcome"] == "canceled"
    assert settlement["attributes"]["outcome"] == "success"
    assert settlement["start_unix_nano"] < root["end_unix_nano"] < settlement["end_unix_nano"]


def test_exporter_does_not_inherit_headers_proxy_or_credentials(monkeypatch):
    captured = {}
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=synthetic-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "cookie=synthetic-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.invalid")

    def fake_exporter(**kwargs):
        captured.update(kwargs)
        assert not any(key.startswith("OTEL_") for key in os.environ)
        return object()

    monkeypatch.setattr(verification, "OTLPSpanExporter", fake_exporter)
    verification.ObservedOTLPExporter("http://127.0.0.1:4318/v1/traces")
    assert captured["headers"] == {}
    assert captured["session"].trust_env is False
    assert captured["session"].auth is None
    captured["session"].close()
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == "cookie=synthetic-secret"


def test_fixture_settings_ignore_environment_and_dotenv(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "synthetic-database-must-not-be-used")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setenv("LLM_COMPARISON_MODEL", "synthetic-override-must-not-be-used")
    settings = verification.synthetic_settings()
    assert settings.app_env == "local"
    assert settings.database_url == settings.llm_api_key == ""
    assert settings.effective_llm_comparison_model == MODEL


@pytest.mark.parametrize("endpoint", [
    "https://collector.example/v1/traces",
    "http://collector.example:4318/v1/traces",
    "http://secret@localhost:4318/v1/traces",
    "http://localhost:4318/v1/traces?secret=value",
    "http://localhost:4318/v1/traces#fragment",
    "http://localhost:bad/v1/traces",
    "http://localhost:0/v1/traces",
])
def test_otlp_export_rejects_nonlocal_or_credential_bearing_endpoint(endpoint):
    with pytest.raises(argparse.ArgumentTypeError):
        verification.local_endpoint(endpoint)


def test_cli_preserves_existing_evidence_before_running(monkeypatch, tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("existing evidence", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["verify_worker_scenarios", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        verification.main()
    assert error.value.code == 2
    assert output.read_text(encoding="utf-8") == "existing evidence"
