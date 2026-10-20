"""No Docker, credentials, network, or paid calls: host rollout boundaries."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "runtime_deploy", ROOT / "deploy/manage-ai-runtime.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    obj = deploy.Runtime(tmp_path, tmp_path / ("a" * 40))
    common = {
        "SPRING_INTERNAL_API_KEY": "private-test-key",
        "LLM_API_KEY": "provider-test-key",
        "SPRING_INTERNAL_API_BASE_URL": "http://10.0.0.1:8080",
        "DATABASE_URL": "postgresql+psycopg://admin:test@10.0.0.3/catchhole",
        "AWS_REGION": "ap-northeast-2",
        "AWS_S3_BUCKET": "synthetic-bucket",
    }
    configs = {
        "worker": {
            "ai-worker": {
                "environment": {
                    **common,
                    "DATABASE_POOL_SIZE": "3",
                    "DATABASE_POOL_MAX_OVERFLOW": "0",
                },
                "deploy": {"replicas": 5},
            },
            "ai-character-comparison-worker": {
                "environment": {"DATABASE_POOL_SIZE": "1", "DATABASE_POOL_MAX_OVERFLOW": "0"}
            },
            "ai-world-comparison-worker": {
                "environment": {"DATABASE_POOL_SIZE": "1", "DATABASE_POOL_MAX_OVERFLOW": "0"}
            },
        },
        "chatbot": {
            "chatbot": {
                "environment": {
                    **common,
                    "CHAT_QA_ENABLED": "true",
                    "CHAT_RETRIEVAL_ENABLED": "true",
                    "CHAT_RETRIEVAL_DATABASE_URL": "postgresql+psycopg://readonly:test@localhost/db",
                    "CHAT_RETRIEVAL_EMBEDDING_VERSION": "v1",
                },
                "ports": [{"host_ip": "10.0.0.2"}],
            }
        },
        "retrieval": {
            "retrieval-index": {
                "environment": {
                    **common,
                    "EMBEDDING_VERSION": "v1",
                    "EMBEDDING_GENERATION_ENABLED": "true",
                    "RETRIEVAL_BACKFILL_ENABLED": "true",
                    "AI_RUNTIME_API_DB_POOL_SIZE": "10",
                    "AI_RUNTIME_DB_CONNECTION_BUDGET": "32",
                }
            }
        },
    }
    monkeypatch.setattr(obj, "config", lambda kind: configs[kind])
    return obj, configs


def test_total_connection_budget_counts_both_new_processes(runtime):
    obj, configs = runtime
    assert obj.validate()["databaseConnections"] == 31
    configs["retrieval"]["retrieval-index"]["environment"]["AI_RUNTIME_DB_CONNECTION_BUDGET"] = "27"
    with pytest.raises(deploy.DeploymentError, match="DATABASE_CONNECTION_BUDGET"):
        obj.validate()


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "::", "8.8.8.8", "bad"])
def test_remote_api_requires_private_bind(runtime, address):
    obj, configs = runtime
    configs["chatbot"]["chatbot"]["ports"][0]["host_ip"] = address
    with pytest.raises(deploy.DeploymentError, match="CHAT_PRIVATE_BIND"):
        obj.validate()


def test_false_qa_is_not_treated_as_a_ready_chatbot(runtime):
    obj, configs = runtime
    configs["chatbot"]["chatbot"]["environment"]["CHAT_QA_ENABLED"] = "false"
    with pytest.raises(deploy.DeploymentError, match="CHAT_QA_ENABLED"):
        obj.validate()


def test_false_index_generation_cannot_claim_automatic_backfill(runtime):
    obj, configs = runtime
    configs["retrieval"]["retrieval-index"]["environment"]["EMBEDDING_GENERATION_ENABLED"] = "false"
    with pytest.raises(deploy.DeploymentError, match="EMBEDDING_GENERATION_ENABLED"):
        obj.validate()


def test_wrong_internal_scope_credentials_fail_closed(runtime):
    obj, configs = runtime
    configs["retrieval"]["retrieval-index"]["environment"]["SPRING_INTERNAL_API_KEY"] = (
        "wrong-secret"
    )
    with pytest.raises(deploy.DeploymentError, match="INCONSISTENT_SPRING_INTERNAL_API_KEY"):
        obj.validate()


@pytest.mark.parametrize(
    "kind,service,key",
    [
        ("chatbot", "chatbot", "LLM_API_KEY"),
        *[
            ("retrieval", "retrieval-index", key)
            for key in ("LLM_API_KEY", "DATABASE_URL", "AWS_REGION", "AWS_S3_BUCKET")
        ],
    ],
)
def test_stale_common_settings_stop_before_service_changes(runtime, monkeypatch, kind, service, key):
    obj, configs = runtime
    configs[kind][service]["environment"][key] = "synthetic-stale-setting"
    calls = []
    monkeypatch.setattr(obj, "compose", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(deploy.DeploymentError, match="^INCONSISTENT_" + key + "$"):
        obj.deploy()
    assert not calls


def test_new_embedding_version_requires_chat_consumer_match(runtime):
    obj, configs = runtime
    configs["retrieval"]["retrieval-index"]["environment"]["EMBEDDING_VERSION"] = "v2"
    with pytest.raises(deploy.DeploymentError, match="EMBEDDING_VERSION_MISMATCH"):
        obj.validate()


def test_disabled_indexing_keeps_report_status_available(runtime):
    obj, configs = runtime
    configs["retrieval"]["retrieval-index"]["environment"]["RETRIEVAL_BACKFILL_ENABLED"] = "false"
    assert obj.validate()["databaseConnections"] == 29


def test_failed_readiness_does_not_mutate_running_services(runtime, monkeypatch):
    obj, _ = runtime
    calls = []

    def compose(kind, *args, **kwargs):
        calls.append((kind, args))
        if "app.chatbot.readiness" in args:
            raise deploy.DeploymentError("COMMAND_FAILED")
        return ""

    monkeypatch.setattr(obj, "compose", compose)
    with pytest.raises(deploy.DeploymentError):
        obj.deploy()
    assert not any("up" in args for _, args in calls)


def test_deploy_uses_configured_replica_count_and_does_not_wait_for_paid_backfill(
    runtime, monkeypatch
):
    obj, configs = runtime
    configs["worker"]["ai-worker"]["deploy"]["replicas"] = 4
    calls = []

    def compose(kind, *args, **kwargs):
        calls.append((kind, args))
        if "app.chatbot.readiness" in args:
            return json.dumps({"state": "READY"})
        if args[-1:] == ("preflight",):
            return json.dumps({"state": "PREFLIGHT_READY"})
        if args[:1] == ("ps",):
            return "id\n" * (4 if args[-1] == "ai-worker" else 1)
        return ""

    monkeypatch.setattr(obj, "compose", compose)
    result = obj.deploy()
    assert result["state"] == "DEPLOYED"
    assert result["backfillComplete"] is False
    assert not any(args[-1:] == ("run",) for _, args in calls)
    assert (
        "retrieval",
        (
            "run",
            "--rm",
            "--no-deps",
            "retrieval-index",
            "python",
            "-m",
            "app.retrieval.backfill",
            "preflight",
        ),
    ) in calls


def test_compose_credentials_never_leak_on_error(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, "password-secret", "postgresql://secret@db"
        ),
    )
    with pytest.raises(deploy.DeploymentError) as exc:
        deploy.command(["docker", "compose"])
    assert str(exc.value) == "COMMAND_FAILED"


def test_shell_environment_cannot_override_file_scope_budget_or_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS", "99999999999999")
    monkeypatch.setenv("SPRING_INTERNAL_API_KEY", "shell-secret")
    obj = deploy.Runtime(tmp_path, tmp_path / ("b" * 40))
    assert "RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS" not in obj.environment
    assert "SPRING_INTERNAL_API_KEY" not in obj.environment
    assert obj.environment["AI_IMAGE"].endswith(":sha-bbbbbbb")


def test_worker_tracing_is_optional_with_existing_environment_files(runtime):
    obj, _ = runtime
    assert obj.validate()["tracingWorkerServices"] == []
    assert not deploy.validate_worker_tracing({
        "AI_WORKER_TRACING_ENABLED": "false",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
    })


def test_existing_single_comparison_worker_can_be_enabled_alone(runtime):
    obj, configs = runtime
    configs["worker"]["ai-character-comparison-worker"]["environment"].update({
        "AI_WORKER_TRACING_ENABLED": "true",
        "OTEL_SERVICE_NAME": "catchhole-ai-worker",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://10.0.2.10:4318/v1/traces",
    })
    result = obj.validate()
    assert result["tracingWorkerServices"] == ["ai-character-comparison-worker"]
    assert result["analysisReplicas"] == 5
    assert result["databaseConnections"] == 31


@pytest.mark.parametrize("address", ["10.0.2.10", "172.31.8.12", "192.168.1.20"])
def test_all_worker_kinds_accept_explicit_private_otlp(runtime, address):
    obj, configs = runtime
    for service in configs["worker"].values():
        service["environment"].update({
            "AI_WORKER_TRACING_ENABLED": "true",
            "OTEL_SERVICE_NAME": "catchhole-ai-worker",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://{address}:4318/v1/traces",
        })
    assert obj.validate()["tracingWorkerServices"] == [
        "ai-worker", "ai-character-comparison-worker", "ai-world-comparison-worker",
    ]


@pytest.mark.parametrize("endpoint", [
    "",
    "http://127.0.0.1:4318/v1/traces",
    "http://0.0.0.0:4318/v1/traces",
    "http://8.8.8.8:4318/v1/traces",
    "http://169.254.169.254:4318/v1/traces",
    "http://192.0.2.10:4318/v1/traces",
    "http://collector.internal:4318/v1/traces",
    "http://[::1]:4318/v1/traces",
    "https://10.0.2.10:4318/v1/traces",
    "http://10.0.2.10:4317/v1/traces",
    "http://10.0.2.10:4318/",
    "http://synthetic-secret@10.0.2.10:4318/v1/traces",
    "http://10.0.2.10:4318/v1/traces?token=synthetic-secret",
    "http://10.0.2.10:4318/v1/traces#synthetic-secret",
    "http://10.0.2.10:bad/v1/traces",
])
def test_invalid_tracing_endpoint_blocks_preflight_before_runtime_operations(
    runtime, monkeypatch, endpoint,
):
    obj, configs = runtime
    configs["worker"]["ai-world-comparison-worker"]["environment"].update({
        "AI_WORKER_TRACING_ENABLED": "true",
        "OTEL_SERVICE_NAME": "catchhole-ai-worker",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
    })

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid tracing config reached pull/readiness/deployment.")

    monkeypatch.setattr(obj, "compose", forbidden)
    with pytest.raises(deploy.DeploymentError) as error:
        obj.preflight()
    assert str(error.value) == "WORKER_TRACING_PRIVATE_OTLP_ENDPOINT_REQUIRED"
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("flag", ["1", "yes", "", "typo"])
def test_tracing_opt_in_requires_explicit_boolean(flag):
    with pytest.raises(deploy.DeploymentError, match="WORKER_TRACING_BOOLEAN_REQUIRED"):
        deploy.validate_worker_tracing({"AI_WORKER_TRACING_ENABLED": flag})


def test_tracing_service_name_is_bounded():
    with pytest.raises(deploy.DeploymentError, match="WORKER_TRACING_SERVICE_NAME_REQUIRED"):
        deploy.validate_worker_tracing({
            "AI_WORKER_TRACING_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://10.0.2.10:4318/v1/traces",
            "OTEL_SERVICE_NAME": "private source / user-provided title",
        })


def test_validate_cli_only_renders_and_validates_configuration(runtime, monkeypatch, capsys):
    obj, _ = runtime
    monkeypatch.setattr(deploy, "Runtime", lambda *args: obj)
    monkeypatch.setattr("sys.argv", ["manage-ai-runtime.py", "validate"])

    def forbidden(*args, **kwargs):
        raise AssertionError("Validation attempted a readiness check or mutation.")

    monkeypatch.setattr(obj, "preflight", forbidden)
    monkeypatch.setattr(obj, "deploy", forbidden)
    monkeypatch.setattr(obj, "backfill", forbidden)
    assert deploy.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["tracingWorkerServices"] == []


def test_scheduler_script_parses_and_does_not_restart_live_backfill():
    path = ROOT / "deploy/install-chat-scheduler.sh"
    subprocess.run(["bash", "-n", str(path)], check=True)
    content = path.read_text()
    assert "systemctl start --no-block catchhole-retrieval-backfill.service" in content
    assert "systemctl restart" not in content
    assert "OnCalendar=*:0/5" in content


@pytest.mark.parametrize("state", ["INDEX_BUSY", "DISABLED", "REPORTS_PENDING"])
def test_zero_exit_incomplete_preflight_is_not_ready(state):
    with pytest.raises(deploy.DeploymentError, match="RUNTIME_PREFLIGHT_NOT_READY"):
        deploy.Runtime.verify_ready(json.dumps({"state": state}), "PREFLIGHT_READY")


def test_blocked_runner_status_is_preserved_without_credentials(runtime, monkeypatch):
    obj, _ = runtime

    def compose(kind, *args, **kwargs):
        assert kwargs["allowed_codes"] == (0, 2)
        return json.dumps(
            {
                "state": "BLOCKED",
                "reason": "UNKNOWN_USAGE",
                "remainingInputTokenAllowance": 500,
                "private": "must-not-emit",
            }
        )

    monkeypatch.setattr(obj, "compose", compose)
    assert obj.backfill("status") == {
        "state": "BLOCKED",
        "reason": "UNKNOWN_USAGE",
        "remainingInputTokenAllowance": 500,
    }


def test_deploy_probe_connection_has_reserved_headroom(runtime):
    obj, configs = runtime
    configs["retrieval"]["retrieval-index"]["environment"]["AI_RUNTIME_DB_CONNECTION_BUDGET"] = "31"
    with pytest.raises(deploy.DeploymentError, match="DATABASE_CONNECTION_BUDGET"):
        obj.validate()


@pytest.mark.parametrize(
    "key,value", [("DATABASE_POOL_SIZE", "0"), ("DATABASE_POOL_MAX_OVERFLOW", "-1")]
)
def test_unbounded_pool_configuration_cannot_underreport_budget(runtime, key, value):
    obj, configs = runtime
    configs["worker"]["ai-worker"]["environment"][key] = value
    with pytest.raises(deploy.DeploymentError, match="DATABASE_CONNECTION_BUDGET"):
        obj.validate()


def test_status_preserves_snapshot_and_supported_scope(runtime, monkeypatch):
    obj, _ = runtime
    status = {
        "state": "READY",
        "snapshotAt": "2026-10-16T01:00:00+00:00",
        "supportedSourceTypes": ["CURRENT_EPISODE"],
        "unsupportedSourceTypes": ["SETTING_BOOK", "PAST_EPISODE_VERSION"],
    }
    monkeypatch.setattr(obj, "compose", lambda *a, **k: json.dumps(status))
    assert obj.backfill("status") == {**status, "reason": None}
