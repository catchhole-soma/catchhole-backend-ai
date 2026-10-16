"""Initial host setup: no live hosts, credentials, database writes or provider calls."""

import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

from dotenv import dotenv_values
import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "chat_runtime_bootstrap", ROOT / "deploy/bootstrap-chat-runtime.py"
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
IMAGE = "ghcr.io/catchhole-soma/catchhole-backend-ai:sha-" + "a" * 40
ADDRESS = "10.0.0.2"
READER = "postgresql+psycopg://reader:private-test-password@10.0.0.3/catchhole"


def env_values(content):
    return dict(dotenv_values(stream=StringIO(content), interpolate=False))


def change(path, **values):
    original = path.read_text() if path.exists() else ""
    bootstrap.atomic_private_write(path, bootstrap.updated_env(original, values))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    obj = bootstrap.Bootstrap(tmp_path, release=ROOT / "deploy", image=IMAGE)
    worker = {
        "ai-worker": {
            "environment": {
                "DATABASE_POOL_SIZE": "3",
                "DATABASE_POOL_MAX_OVERFLOW": "0",
                "SPRING_INTERNAL_API_BASE_URL": "http://10.0.0.1:8080",
                "LLM_API_KEY": "private-provider-key",
            },
            "deploy": {"replicas": 5},
        },
        "ai-character-comparison-worker": {
            "environment": {"DATABASE_POOL_SIZE": "1", "DATABASE_POOL_MAX_OVERFLOW": "0"}
        },
        "ai-world-comparison-worker": {
            "environment": {"DATABASE_POOL_SIZE": "1", "DATABASE_POOL_MAX_OVERFLOW": "0"}
        },
    }
    db_calls, restarts = [], []
    defaults = {
        "api": {
            "DATABASE_POOL_MAXIMUM_SIZE": "10",
            "CHAT_ENABLED": "false",
            "CHAT_OPERATIONS_ENABLED": "false",
            "INTERNAL_API_KEY": "private-internal-key",
        },
        "chatbot": {"CHAT_QA_MODEL": "gpt-6-luna", "CHAT_RETRIEVAL_EMBEDDING_VERSION": "v1"},
        "retrieval": {"DATABASE_URL": "postgresql+psycopg://admin:private@10.0.0.3/catchhole"},
    }

    def configuration(kind, *, candidate=None):
        if kind == "worker":
            return worker
        path = tmp_path / (kind + ".env")
        text = candidate if candidate is not None else path.read_text() if path.exists() else ""
        env = {**defaults[kind], **env_values(text)}
        if kind == "chatbot" and env.get("CHAT_BIND_ADDRESS") == "REPLACE_WITH_AI_PRIVATE_IP":
            raise bootstrap.BootstrapError("COMPOSE_CONFIGURATION_INVALID")
        service = {"api": "backend", "retrieval": "retrieval-index", "chatbot": "chatbot"}[kind]
        return {
            service: {
                "environment": env,
                "ports": [{"host_ip": env.get("CHAT_BIND_ADDRESS", ADDRESS)}],
            }
        }

    def database_prepare(env, connections, existing_url):
        db_calls.append((env, connections, existing_url))
        return existing_url or READER

    def readiness(env):
        return {
            "operationsEnabled": bootstrap.is_enabled(env.get("CHAT_OPERATIONS_ENABLED")),
            "publicChatEnabled": bootstrap.is_enabled(env.get("CHAT_ENABLED")),
            "capabilityConfigured": bool(env.get("CHAT_CAPABILITY_SECRET")),
        }

    monkeypatch.setattr(obj, "configuration", configuration)
    monkeypatch.setattr(obj, "database_prepare", database_prepare)
    monkeypatch.setattr(obj, "restart_api", lambda: restarts.append("restart"))
    monkeypatch.setattr(obj, "wait_api", lambda: None)
    monkeypatch.setattr(bootstrap, "api_readiness", readiness)
    monkeypatch.setattr(bootstrap, "available_memory", lambda: 2 * 1024**3)
    monkeypatch.setattr(bootstrap, "healthy", lambda *_: True)
    return SimpleNamespace(obj=obj, worker=worker, db_calls=db_calls, restarts=restarts)


def test_inspect_returns_only_safe_network_and_capacity_metadata(runtime):
    result = runtime.obj.inspect()
    assert result == {
        "state": "INSPECTED",
        "apiPrivateIp": "10.0.0.1",
        "apiPort": 8080,
        "workerDatabaseConnections": 17,
        "analysisReplicas": 5,
    }
    assert "private-provider-key" not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    ["http://8.8.8.8:8080", "http://name:8080", "http://u:p@10.0.0.1", "http://10.0.0.1?secret=x"],
)
def test_inspect_rejects_nonprivate_or_authenticated_api_urls(runtime, url):
    runtime.worker["ai-worker"]["environment"]["SPRING_INTERNAL_API_BASE_URL"] = url
    with pytest.raises(bootstrap.BootstrapError):
        runtime.obj.inspect()


@pytest.mark.parametrize("existing_template", [False, True])
def test_first_ai_setup_handles_missing_files_and_example_placeholder(runtime, existing_template):
    obj = runtime.obj
    if existing_template:
        for kind in ("chatbot", "retrieval"):
            (obj.directory / f"{kind}.env").write_text(
                (ROOT / "deploy" / f"{kind}.env.example").read_text()
            )
    result = obj.prepare_ai(ADDRESS, 10)
    chat = env_values((obj.directory / "chatbot.env").read_text())
    retrieval = env_values((obj.directory / "retrieval.env").read_text())
    assert result["requiredDatabaseConnections"] == 32
    assert chat["CHAT_BIND_ADDRESS"] == ADDRESS
    assert chat["CHAT_QA_ENABLED"] == chat["CHAT_RETRIEVAL_ENABLED"] == "true"
    assert chat["CHAT_RETRIEVAL_DATABASE_URL"] == READER
    assert retrieval["RETRIEVAL_BACKFILL_ENABLED"] == "true"
    assert retrieval["RETRIEVAL_EMBEDDING_GENERATION_ENABLED"] == "true"
    assert retrieval["RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS"] == "50000000"
    assert retrieval["RETRIEVAL_BACKFILL_ID"] == "retrieval-v1-rollout"
    assert retrieval["RETRIEVAL_BACKFILL_SCOPE"] == "active-works"
    assert retrieval["AI_RUNTIME_DB_CONNECTION_BUDGET"] == "32"
    for path in (obj.private, obj.private / "state.json", obj.directory / "chatbot.env"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert READER not in json.dumps(result)


def test_custom_models_allowlist_dsn_campaign_and_budget_survive(runtime):
    obj = runtime.obj
    custom_reader = READER.replace("private-test-password", "REPLACE-is-a-valid-password")
    change(
        obj.directory / "chatbot.env",
        CHAT_QA_MODEL="custom-model",
        CHAT_RETRIEVAL_DATABASE_URL=custom_reader,
        CHAT_BIND_ADDRESS=ADDRESS,
    )
    change(
        obj.directory / "retrieval.env",
        RETRIEVAL_BACKFILL_ID="custom-campaign",
        RETRIEVAL_BACKFILL_SCOPE="",
        RETRIEVAL_BACKFILL_WORK_IDS="00000000-0000-4000-8000-000000000001",
        RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS="12345",
        RETRIEVAL_BACKFILL_ACTOR="custom-actor",
        AI_RUNTIME_DB_CONNECTION_BUDGET="100",
        EMBEDDING_VERSION="v2",
    )
    obj.prepare_ai(ADDRESS, 10)
    chat = env_values((obj.directory / "chatbot.env").read_text())
    retrieval = env_values((obj.directory / "retrieval.env").read_text())
    assert chat["CHAT_QA_MODEL"] == "custom-model"
    assert chat["CHAT_RETRIEVAL_DATABASE_URL"] == custom_reader
    assert runtime.db_calls[-1][2] == custom_reader
    assert retrieval["RETRIEVAL_BACKFILL_SCOPE"] == ""
    assert retrieval["RETRIEVAL_BACKFILL_ID"] == "custom-campaign"
    assert retrieval["RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS"] == "12345"
    assert retrieval["AI_RUNTIME_DB_CONNECTION_BUDGET"] == "100"
    assert retrieval["EMBEDDING_VERSION"] == "v2"


def test_second_ai_setup_preserves_manual_off(runtime):
    obj = runtime.obj
    obj.prepare_ai(ADDRESS, 10)
    change(obj.directory / "chatbot.env", CHAT_QA_ENABLED="false", CHAT_RETRIEVAL_ENABLED="false")
    change(
        obj.directory / "retrieval.env",
        RETRIEVAL_BACKFILL_ENABLED="false",
        RETRIEVAL_EMBEDDING_GENERATION_ENABLED="false",
    )
    obj.prepare_ai(ADDRESS, 10)
    chat = env_values((obj.directory / "chatbot.env").read_text())
    retrieval = env_values((obj.directory / "retrieval.env").read_text())
    assert chat["CHAT_QA_ENABLED"] == chat["CHAT_RETRIEVAL_ENABLED"] == "false"
    assert (
        retrieval["RETRIEVAL_BACKFILL_ENABLED"]
        == retrieval["RETRIEVAL_EMBEDDING_GENERATION_ENABLED"]
        == "false"
    )
    assert runtime.db_calls[-1][1] == 28


def test_second_ai_setup_keeps_environment_files_byte_identical(runtime):
    obj = runtime.obj
    obj.prepare_ai(ADDRESS, 10)
    paths = [obj.directory / "chatbot.env", obj.directory / "retrieval.env"]
    before = [path.read_bytes() for path in paths]
    result = obj.prepare_ai(ADDRESS, 10)
    assert [path.read_bytes() for path in paths] == before
    assert result["changedFields"] == []


@pytest.mark.parametrize(
    "raw", ['"10.0.0.2" # private bind', "10.0.0.2 # private bind", "${AI_PRIVATE_IP}"]
)
def test_custom_bind_uses_compose_resolved_port_not_raw_assignment(runtime, monkeypatch, raw):
    obj = runtime.obj
    original = obj.configuration
    (obj.directory / "chatbot.env").write_text(f"CHAT_BIND_ADDRESS={raw}\n")

    def configuration(kind, **kwargs):
        result = original(kind, **kwargs)
        if kind == "chatbot":
            result["chatbot"]["ports"] = [{"host_ip": ADDRESS}]
        return result

    monkeypatch.setattr(obj, "configuration", configuration)
    obj.prepare_ai(ADDRESS, 10)
    assert f"CHAT_BIND_ADDRESS={raw}\n" in (obj.directory / "chatbot.env").read_text()


def test_ai_crash_between_env_files_does_not_reenable_manual_off(runtime, monkeypatch):
    obj = runtime.obj
    original = bootstrap.atomic_private_write

    def fail_retrieval(path, content):
        if path.name == "retrieval.env":
            raise OSError("simulated crash")
        original(path, content)

    monkeypatch.setattr(bootstrap, "atomic_private_write", fail_retrieval)
    with pytest.raises(OSError):
        obj.prepare_ai(ADDRESS, 10)
    monkeypatch.setattr(bootstrap, "atomic_private_write", original)
    change(obj.directory / "chatbot.env", CHAT_QA_ENABLED="false")
    obj.prepare_ai(ADDRESS, 10)
    assert env_values((obj.directory / "chatbot.env").read_text())["CHAT_QA_ENABLED"] == "false"


def test_failed_database_keeps_existing_env_and_durable_reader_intent(runtime, monkeypatch):
    obj = runtime.obj
    change(obj.directory / "chatbot.env", CHAT_QA_ENABLED="false")
    before = (obj.directory / "chatbot.env").read_bytes()

    def fail(*_):
        bootstrap.atomic_private_write(obj.private / "reader-intent.json", '{"private":"preserve"}')
        raise bootstrap.BootstrapError("DATABASE_CAPACITY_INSUFFICIENT")

    monkeypatch.setattr(obj, "database_prepare", fail)
    with pytest.raises(bootstrap.BootstrapError, match="DATABASE_CAPACITY_INSUFFICIENT"):
        obj.prepare_ai(ADDRESS, 10)
    assert (obj.directory / "chatbot.env").read_bytes() == before
    assert (obj.private / "reader-intent.json").is_file()
    assert not (obj.directory / "retrieval.env").exists()


def test_low_memory_stops_before_database_or_environment_changes(runtime, monkeypatch):
    monkeypatch.setattr(bootstrap, "available_memory", lambda: 768 * 1024**2 - 1)
    with pytest.raises(bootstrap.BootstrapError, match="AI_MEMORY_BELOW_768_MIB"):
        runtime.obj.prepare_ai(ADDRESS, 10)
    assert not runtime.db_calls
    assert not runtime.obj.private.exists()


def test_custom_pool_capacity_is_computed_and_small_existing_budget_rejected(runtime):
    runtime.worker["ai-worker"]["deploy"]["replicas"] = 3
    runtime.worker["ai-worker"]["environment"]["DATABASE_POOL_MAX_OVERFLOW"] = "2"
    change(runtime.obj.directory / "retrieval.env", AI_RUNTIME_DB_CONNECTION_BUDGET="32")
    with pytest.raises(bootstrap.BootstrapError, match="EXISTING_DATABASE_BUDGET_INSUFFICIENT"):
        runtime.obj.prepare_ai(ADDRESS, 20)
    assert not runtime.db_calls


def test_prepare_api_generates_secret_once_and_preserves_unrelated_environment(runtime):
    obj = runtime.obj
    path = obj.directory / "api.env"
    path.write_text("# Existing production configuration\nBACKEND_IMAGE=existing-sha\n")
    result = obj.prepare_api(ADDRESS)
    values = env_values(path.read_text())
    secret = values["CHAT_CAPABILITY_SECRET"]
    assert len(secret) == 64
    assert values["CHAT_ENABLED"] == "false"
    assert values["CHAT_OPERATIONS_ENABLED"] == "true"
    assert values["CHAT_PYTHON_URL"] == "http://10.0.0.2:8082"
    assert values["BACKEND_IMAGE"] == "existing-sha"
    assert "# Existing production configuration" in path.read_text()
    assert result == {"state": "API_PREPARED", "apiPoolSize": 10, "activationPending": True}
    obj.prepare_api(ADDRESS)
    assert env_values(path.read_text())["CHAT_CAPABILITY_SECRET"] == secret
    assert len(runtime.restarts) == 1
    assert secret not in json.dumps(result)


def test_api_existing_complete_configuration_needs_no_recreation(runtime):
    obj = runtime.obj
    change(
        obj.directory / "api.env",
        CHAT_ENABLED="true",
        CHAT_OPERATIONS_ENABLED="true",
        CHAT_CAPABILITY_SECRET="custom" * 10,
        CHAT_PYTHON_URL="http://10.0.0.2:8082",
    )
    result = obj.prepare_api(ADDRESS)
    assert not result["activationPending"]
    assert not runtime.restarts
    assert (
        env_values((obj.directory / "api.env").read_text())["CHAT_CAPABILITY_SECRET"]
        == "custom" * 10
    )


def test_api_health_failure_retries_exact_persisted_secret(runtime, monkeypatch):
    obj = runtime.obj
    original = obj.restart_api
    monkeypatch.setattr(
        obj,
        "restart_api",
        lambda: (_ for _ in ()).throw(bootstrap.BootstrapError("API_HEALTH_NOT_READY")),
    )
    with pytest.raises(bootstrap.BootstrapError, match="API_HEALTH_NOT_READY"):
        obj.prepare_api(ADDRESS)
    secret = env_values((obj.directory / "api.env").read_text())["CHAT_CAPABILITY_SECRET"]
    assert obj.state["api"]["restartRequired"]
    monkeypatch.setattr(obj, "restart_api", original)
    obj.prepare_api(ADDRESS)
    assert env_values((obj.directory / "api.env").read_text())["CHAT_CAPABILITY_SECRET"] == secret
    assert "pendingChanges" not in obj.state["api"]


def test_prepared_api_manual_operations_off_is_preserved(runtime):
    obj = runtime.obj
    obj.prepare_api(ADDRESS)
    change(obj.directory / "api.env", CHAT_OPERATIONS_ENABLED="false")
    obj.prepare_api(ADDRESS)
    assert env_values((obj.directory / "api.env").read_text())["CHAT_OPERATIONS_ENABLED"] == "false"
    assert len(runtime.restarts) == 1


def test_activation_is_once_and_later_manual_public_off_is_respected(runtime):
    obj = runtime.obj
    obj.prepare_api(ADDRESS)
    assert obj.activate_api(ADDRESS)["state"] == "API_ACTIVATED"
    change(obj.directory / "api.env", CHAT_ENABLED="false")
    assert obj.activate_api(ADDRESS) == {
        "state": "API_ACTIVATION_SKIPPED",
        "publicChatEnabled": False,
    }
    assert len(runtime.restarts) == 2


@pytest.mark.parametrize("fault", ["health", "url", "operations"])
def test_activation_failure_leaves_public_gate_off(runtime, monkeypatch, fault):
    obj = runtime.obj
    obj.prepare_api(ADDRESS)
    if fault == "health":
        monkeypatch.setattr(bootstrap, "healthy", lambda *_: False)
    elif fault == "url":
        change(obj.directory / "api.env", CHAT_PYTHON_URL="http://10.0.0.99:8082")
    else:
        monkeypatch.setattr(
            bootstrap,
            "api_readiness",
            lambda *_: (_ for _ in ()).throw(bootstrap.BootstrapError("API_OPERATIONS_DISABLED")),
        )
    with pytest.raises(bootstrap.BootstrapError):
        obj.activate_api(ADDRESS)
    assert env_values((obj.directory / "api.env").read_text())["CHAT_ENABLED"] == "false"
    assert obj.state["api"]["activationPending"]


def test_activation_crash_then_operator_off_cannot_reenable(runtime, monkeypatch):
    obj = runtime.obj
    obj.prepare_api(ADDRESS)
    monkeypatch.setattr(
        obj,
        "restart_api",
        lambda: (_ for _ in ()).throw(bootstrap.BootstrapError("API_HEALTH_NOT_READY")),
    )
    with pytest.raises(bootstrap.BootstrapError):
        obj.activate_api(ADDRESS)
    change(obj.directory / "api.env", CHAT_ENABLED="false")
    assert obj.activate_api(ADDRESS)["state"] == "API_ACTIVATION_SKIPPED"
    assert not obj.state["api"]["activationPending"]


def test_operator_already_enabled_api_is_not_recreated(runtime):
    obj = runtime.obj
    obj.prepare_api(ADDRESS)
    change(obj.directory / "api.env", CHAT_ENABLED="true")
    assert obj.activate_api(ADDRESS)["state"] == "API_ALREADY_ENABLED"
    assert len(runtime.restarts) == 1


def test_deployment_lock_reuses_inode_and_private_state(tmp_path):
    obj = bootstrap.Bootstrap(tmp_path)
    with obj.locked(api=True):
        obj.save_state()
    inode = (tmp_path / ".api-deploy.lock").stat().st_ino
    with obj.locked(api=True):
        obj.save_state()
    assert (tmp_path / ".api-deploy.lock").stat().st_ino == inode
    assert stat.S_IMODE((tmp_path / ".api-deploy.lock").stat().st_mode) == 0o600


def test_symlink_environment_is_never_replaced(tmp_path):
    original = tmp_path / "original"
    original.write_text("existing")
    link = tmp_path / "api.env"
    link.symlink_to(original)
    with pytest.raises(bootstrap.BootstrapError, match="SYMLINK_NOT_ALLOWED"):
        bootstrap.atomic_private_write(link, "changed")
    assert original.read_text() == "existing"


@pytest.mark.parametrize(
    "code",
    [
        "READ_ROLE_WRITE_PRIVILEGE",
        "DATABASE_CAPACITY_INSUFFICIENT",
        "DB_BOOTSTRAP_PERMISSION_DENIED",
        "RETRIEVAL_VECTOR_SCHEMA_INVALID",
    ],
)
def test_database_module_fixed_failure_is_preserved_without_stderr(monkeypatch, code):
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            2,
            json.dumps({"schemaVersion": 1, "state": "FAILED", "code": code}),
            "postgresql://private-secret",
        ),
    )
    with pytest.raises(bootstrap.BootstrapError, match="^" + code + "$"):
        bootstrap.command(["ignored"], database_result=True)


@pytest.mark.parametrize(
    "result",
    [
        (2, '{"schemaVersion":1,"state":"FAILED","code":"private-secret"}'),
        (2, "postgresql://private-secret"),
        (0, '{"schemaVersion":1,"state":"FAILED"}'),
        (2, '{"schemaVersion":2,"state":"FAILED","code":"READ_ROLE_WRITE_PRIVILEGE"}'),
    ],
)
def test_database_unknown_or_malformed_result_is_redacted(monkeypatch, result):
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], *result, "private-secret"),
    )
    with pytest.raises(bootstrap.BootstrapError, match="^DATABASE_BOOTSTRAP_RESULT_INVALID$"):
        bootstrap.command(["ignored"], database_result=True)


def test_database_container_uses_private_literal_envfile_and_host_uid(tmp_path, monkeypatch):
    obj = bootstrap.Bootstrap(tmp_path, image=IMAGE)
    calls = []
    password = 'private-$dollar\\quote"'

    def command(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["docker", "run"]:
            path = Path(args[args.index("--env-file") + 1])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert f"DATABASE_URL={password}\n" in path.read_text()
            assert "CHAT_BOOTSTRAP_REQUIRED_CONNECTIONS=32\n" in path.read_text()
            assert password not in json.dumps(args)
            assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
            assert kwargs["database_result"] is True
            bootstrap.atomic_private_write(
                obj.private / "reader.json", json.dumps({"databaseUrl": READER})
            )
        return ""

    monkeypatch.setattr(bootstrap, "command", command)
    with obj.locked():
        assert obj.database_prepare({"DATABASE_URL": password}, 32, "") == READER
        # The container can normalize URL escaping without changing credentials.
        existing = READER.replace("private-test-password", "%70rivate-test-password")
        assert obj.database_prepare({"DATABASE_URL": password}, 32, existing) == existing
    assert calls[0][0] == ["docker", "pull", IMAGE]
    assert calls[0][1]["timeout"] == 600
    assert not list(obj.private.glob(".database-env-*"))


@pytest.mark.parametrize(
    "operations,capability,code",
    [(False, True, "API_OPERATIONS_DISABLED"), (True, False, "CAPABILITY_SECRET_INVALID")],
)
def test_actual_authenticated_api_flags_are_required(monkeypatch, operations, capability, code):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, *_):
            return json.dumps(
                {
                    "success": True,
                    "data": {
                        "operationsEnabled": operations,
                        "capabilityConfigured": capability,
                        "publicChatEnabled": False,
                    },
                }
            ).encode()

    def request(req, *, timeout):
        assert req.get_header("X-internal-api-key") == "test-internal-key"
        return Response()

    monkeypatch.setattr(bootstrap, "build_opener", lambda *_: SimpleNamespace(open=request))
    with pytest.raises(bootstrap.BootstrapError, match=code):
        bootstrap.api_readiness({"INTERNAL_API_KEY": "test-internal-key"})


def test_cli_never_prints_raw_failure_or_secrets(monkeypatch, capsys):
    monkeypatch.setattr(
        bootstrap.Bootstrap, "inspect", lambda *_: (_ for _ in ()).throw(RuntimeError(READER))
    )
    assert bootstrap.main(["inspect"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "state": "FAILED",
        "code": "BOOTSTRAP_CONFIGURATION_FAILURE",
    }


@pytest.mark.skipif(
    os.environ.get("GH258_COMPOSE_BOOTSTRAP_TEST") != "1",
    reason="opt-in local Docker Compose config only",
)
@pytest.mark.parametrize("layout", ["missing", "template", "inline-comment", "quoted", "variable"])
def test_real_compose_bootstrap_candidates_and_credential_roundtrip(tmp_path, monkeypatch, layout):
    """No containers started: exercise Docker Compose's real dotenv/port parser."""
    obj = bootstrap.Bootstrap(tmp_path, release=ROOT / "deploy", image=IMAGE)
    provider_key = "synthetic-$provider-$$-key"
    internal_key = "synthetic-${literal}-internal-$$-key"
    (tmp_path / "worker.env").write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+psycopg://admin:synthetic@10.0.0.3/catchhole",
                "AWS_REGION=ap-northeast-2",
                "AWS_S3_BUCKET=synthetic-test-bucket",
                f"LLM_API_KEY='{provider_key}'",
                f"SPRING_INTERNAL_API_KEY='{internal_key}'",
                "SPRING_INTERNAL_API_BASE_URL=http://10.0.0.1:8080",
            ]
        )
        + "\n"
    )
    if layout != "missing":
        for kind in ("chatbot", "retrieval"):
            (tmp_path / f"{kind}.env").write_text(
                (ROOT / "deploy" / f"{kind}.env.example").read_text()
            )
    if layout in {"inline-comment", "quoted", "variable"}:
        path = tmp_path / "chatbot.env"
        value = {
            "inline-comment": ADDRESS + " # private bind",
            "quoted": f'"{ADDRESS}" # private bind',
            "variable": "${AI_PRIVATE_IP}",
        }[layout]
        path.write_text(
            f"AI_PRIVATE_IP={ADDRESS}\n"
            + path.read_text().replace("REPLACE_WITH_AI_PRIVATE_IP", value)
        )
    special_dsn = 'postgresql+psycopg://reader:p$word\\slash"quote@10.0.0.3/catchhole'
    monkeypatch.setattr(obj, "database_prepare", lambda *_: special_dsn)
    monkeypatch.setattr(bootstrap, "available_memory", lambda: 2 * 1024**3)
    result = obj.prepare_ai(ADDRESS, 10)
    assert result["requiredDatabaseConnections"] == 32
    configuration = obj.configuration("chatbot")["chatbot"]
    assert configuration["ports"][0]["host_ip"] == ADDRESS
    assert configuration["environment"]["CHAT_RETRIEVAL_DATABASE_URL"] == special_dsn
    assert configuration["environment"]["LLM_API_KEY"] == provider_key
    assert configuration["environment"]["SPRING_INTERNAL_API_KEY"] == internal_key
    retrieval = obj.configuration("retrieval")["retrieval-index"]["environment"]
    assert retrieval["EMBEDDING_GENERATION_ENABLED"] == "true"
    assert retrieval["RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS"] == "50000000"
