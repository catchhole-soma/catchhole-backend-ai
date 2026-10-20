"""Render only: no Docker daemon operations, credentials, or business requests."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("ai-worker", "ai-character-comparison-worker", "ai-world-comparison-worker")


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
        "AI_IMAGE": "ghcr.io/catchhole-soma/catchhole-backend-ai:sha-aaaaaaa",
        "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@db.invalid/synthetic",
        "AWS_REGION": "ap-northeast-2",
        "AWS_S3_BUCKET": "synthetic",
        "LLM_API_KEY": "synthetic",
        "SPRING_INTERNAL_API_BASE_URL": "http://10.0.1.10:8080",
        "SPRING_INTERNAL_API_KEY": "synthetic",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://10.0.2.10:4318/v1/traces",
        **flags,
    }
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
    services = json.loads(result.stdout)["services"]
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
