"""Render only: no Docker daemon operations, credentials, or business requests."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("ai-worker", "ai-character-comparison-worker", "ai-world-comparison-worker")
SYNTHETIC = {
    "AI_IMAGE": "ghcr.io/catchhole-soma/catchhole-backend-ai:sha-aaaaaaa",
    "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@db.invalid/synthetic",
    "AWS_REGION": "ap-northeast-2",
    "AWS_S3_BUCKET": "synthetic",
    "LLM_API_KEY": "synthetic",
    "SPRING_INTERNAL_API_BASE_URL": "http://10.0.1.10:8080",
    "SPRING_INTERNAL_API_KEY": "synthetic",
}


@pytest.fixture(scope="module")
def compose_cli():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is not installed.")
    version = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, check=False, timeout=10,
    )
    if version.returncode:
        pytest.skip("Docker Compose CLI is not available.")
    return docker


def render(compose_cli, tmp_path, values):
    environment_file = tmp_path / "worker.env"
    environment_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8",
    )
    # Match the deployment tool: unrelated shell variables cannot override env files.
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR", "LANG"}
    }
    result = subprocess.run(
        [
            compose_cli, "compose", "--env-file", str(environment_file),
            "-f", str(ROOT / "deploy/compose.worker.prod.yml"), "config", "--format", "json",
        ],
        env=environment, capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, "Synthetic Compose configuration did not render."
    return json.loads(result.stdout)["services"]


@pytest.mark.parametrize(("flags", "expected"), [
    ({}, ("false", "false", "false")),
    ({
        "AI_WORKER_TRACING_ENABLED": "false",
        "AI_ANALYSIS_TRACING_ENABLED": "false",
        "AI_CHARACTER_COMPARISON_TRACING_ENABLED": "true",
        "AI_WORLD_COMPARISON_TRACING_ENABLED": "false",
    }, ("false", "true", "false")),
    ({"AI_WORKER_TRACING_ENABLED": "true"}, ("true", "true", "true")),
    ({
        "AI_WORKER_TRACING_ENABLED": "true",
        "AI_ANALYSIS_TRACING_ENABLED": "false",
        "AI_CHARACTER_COMPARISON_TRACING_ENABLED": "false",
        "AI_WORLD_COMPARISON_TRACING_ENABLED": "false",
    }, ("false", "false", "false")),
    ({
        "AI_WORKER_TRACING_ENABLED": "true",
        "AI_ANALYSIS_TRACING_ENABLED": "",
    }, ("true", "true", "true")),
])
def test_tracing_rollout_flags_preserve_worker_capacity(
    compose_cli, tmp_path, monkeypatch, flags, expected,
):
    monkeypatch.setenv("AI_WORKER_TRACING_ENABLED", "untrusted-shell-override")
    values = {
        **SYNTHETIC,
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://10.0.2.10:4318/v1/traces",
        **flags,
    }
    services = render(compose_cli, tmp_path, values)
    assert tuple(services[name]["environment"]["AI_WORKER_TRACING_ENABLED"] for name in SERVICES) == expected
    assert services["ai-worker"]["deploy"]["replicas"] == 5
    for name in SERVICES:
        env = services[name]["environment"]
        assert env["OTEL_SERVICE_NAME"] == "catchhole-ai-worker"
        assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == values["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
        assert env["AI_WORKER_SHUTDOWN_GRACE_SECONDS"] == "180"
        assert env["AI_WORKER_CONCURRENCY"] == ("10" if name == "ai-worker" else "1")
        assert env["LLM_MAX_CONCURRENT_REQUESTS"] == ("10" if name == "ai-worker" else "1")
        assert services[name]["stop_grace_period"] == "3m30s"
        assert all(port["target"] == 9102 for port in services[name]["ports"])


def option(command, flag):
    assert command.count(flag) == 1
    return command[command.index(flag) + 1]


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, (0, "3", "4", "5", "validation-prod")),
    ({
        "VALIDATION_WORKER_PROCESS_COUNT": "1",
        "VALIDATION_WORKER_CONCURRENCY": "2",
        "VALIDATION_WORKER_LLM_CONCURRENCY": "2",
        "VALIDATION_WORKER_POLL_SECONDS": "3",
        "VALIDATION_WORKER_ID": "validation-a",
        # Analysis-wide observability flags must not reach the validation runner.
        "AI_WORKER_TRACING_ENABLED": "true",
        "AI_WORKER_METRICS_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://10.0.2.10:4318/v1/traces",
    }, (1, "2", "2", "3", "validation-a")),
])
def test_validation_worker_is_opt_in_and_reuses_api_credentials(
    compose_cli, tmp_path, overrides, expected,
):
    values = {
        **SYNTHETIC,
        "LLM_API_KEY": "synthetic-provider-key",
        "SPRING_INTERNAL_API_KEY": "synthetic-internal-key",
        **overrides,
    }
    services = render(compose_cli, tmp_path, values)
    replicas, concurrency, llm_concurrency, poll_seconds, worker_id = expected
    validation = services["ai-validation-worker"]
    assert validation["deploy"]["replicas"] == replicas
    command = validation["command"]
    assert command[:3] == ["python", "scripts/run_validation_worker.py", "--enable"]
    assert option(command, "--backend-url") == "http://10.0.1.10:8080"
    assert option(command, "--internal-key-env") == "SPRING_INTERNAL_API_KEY"
    assert option(command, "--model-key-env") == "LLM_API_KEY"
    assert option(command, "--poll-seconds") == poll_seconds
    assert option(command, "--worker-id") == worker_id
    assert "--once" not in command
    env = validation["environment"]
    assert env["AI_WORKER_CONCURRENCY"] == concurrency
    assert env["LLM_MAX_CONCURRENT_REQUESTS"] == llm_concurrency
    assert env["AI_WORKER_METRICS_ENABLED"] == "false"
    assert env["AI_WORKER_TRACING_ENABLED"] == "false"
    assert env["AI_WORKER_SHUTDOWN_GRACE_SECONDS"] == "180"
    assert env["SPRING_INTERNAL_API_KEY"] == "synthetic-internal-key"
    assert env["LLM_API_KEY"] == "synthetic-provider-key"
    assert env["SPRING_INTERNAL_API_BASE_URL"] == "http://10.0.1.10:8080"
    assert "ports" not in validation
    assert validation["stop_grace_period"] == "3m30s"
    # The analysis services keep their capacity, credentials, metrics ports, and tracing flags.
    assert services["ai-worker"]["deploy"]["replicas"] == 5
    tracing = values.get("AI_WORKER_TRACING_ENABLED", "false")
    for name in SERVICES:
        env = services[name]["environment"]
        assert env["AI_WORKER_TRACING_ENABLED"] == tracing
        assert env["AI_WORKER_METRICS_ENABLED"] == "true"
        assert env["AI_WORKER_CONCURRENCY"] == ("10" if name == "ai-worker" else "1")
        assert env["LLM_MAX_CONCURRENT_REQUESTS"] == ("10" if name == "ai-worker" else "1")
        assert env["SPRING_INTERNAL_API_KEY"] == "synthetic-internal-key"
        assert env["LLM_API_KEY"] == "synthetic-provider-key"
        assert all(port["target"] == 9102 for port in services[name]["ports"])
