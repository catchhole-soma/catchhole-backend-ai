"""Deployment readiness checks real boundaries without model calls or personal data."""

import asyncio
from contextlib import contextmanager
import json
import socket

import httpx
import pytest

from app.chatbot import readiness
from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.qa_config import ChatQaSettings


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    for settings in (ChatQaSettings, HybridSettings):
        prefix = settings.model_config["env_prefix"]
        for name in settings.model_fields:
            monkeypatch.delenv(prefix + name.upper(), raising=False)
    monkeypatch.setenv("CHAT_QA_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-model-key")
    monkeypatch.setenv("SPRING_INTERNAL_API_KEY", "synthetic-internal-key")
    monkeypatch.setenv("SPRING_INTERNAL_API_BASE_URL", "http://spring.test:8080")

    def reject(*args, **kwargs):
        raise AssertionError("Readiness tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def successful_response(request, *, operations=False, public=False, capability=True):
    if request.url.path == "/actuator/health":
        assert "X-Internal-Api-Key" not in request.headers
        return httpx.Response(200, json={"status": "UP"})
    assert request.url.path == "/api/internal/v1/chat-runs/operations/readiness"
    assert request.headers["X-Internal-Api-Key"] == "synthetic-internal-key"
    return httpx.Response(
        200,
        json={
            "success": True,
            "data": {
                "operationsEnabled": operations,
                "publicChatEnabled": public,
                "capabilityConfigured": capability,
            },
        },
    )


def run(respond=successful_response):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            return await readiness.check_readiness(http=http)

    return asyncio.run(check())


def test_both_gates_may_be_off_during_preflight(monkeypatch):
    monkeypatch.setattr(readiness, "create_engine", lambda *a, **kw: pytest.fail("DB disabled"))
    assert run() == {
        "schemaVersion": 1,
        "state": "READY",
        "checks": {"qa": "READY", "spring": "READY", "retrieval": "DISABLED"},
        "operationsEnabled": False,
        "publicChatEnabled": False,
    }


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("CHAT_QA_ENABLED", "false", "QA_DISABLED"),
        ("CHAT_QA_ENABLED", "invalid", "CONFIGURATION_INVALID"),
        ("CHAT_QA_MODEL", " ", "MODEL_CONFIGURATION_INVALID"),
        ("LLM_API_KEY", "", "MODEL_CONFIGURATION_INVALID"),
        ("SPRING_INTERNAL_API_KEY", "short", "INTERNAL_KEY_INVALID"),
        ("SPRING_INTERNAL_API_BASE_URL", "", "SPRING_URL_INVALID"),
        ("SPRING_INTERNAL_API_BASE_URL", "https://user:secret@spring.test", "SPRING_URL_INVALID"),
        ("SPRING_INTERNAL_API_BASE_URL", "https://spring.test?key=secret", "SPRING_URL_INVALID"),
        ("SPRING_INTERNAL_API_BASE_URL", "file:///secret", "SPRING_URL_INVALID"),
    ],
)
def test_invalid_configuration_stops_before_network(monkeypatch, name, value, code):
    monkeypatch.setenv(name, value)
    result = run(lambda request: pytest.fail("Configuration must be validated first"))
    assert result == {"schemaVersion": 1, "state": "NOT_READY", "code": code}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(503, json={"status": "DOWN"}), "SPRING_UNHEALTHY"),
        (httpx.Response(200, json={"status": "DOWN"}), "SPRING_UNHEALTHY"),
        (httpx.Response(200, json=["UP"]), "SPRING_UNHEALTHY"),
        (httpx.Response(200, text="secret raw failure"), "SPRING_UNHEALTHY"),
        (httpx.Response(302, headers={"Location": "https://provider.test"}), "SPRING_UNHEALTHY"),
    ],
)
def test_health_must_be_up_without_redirects(response, code):
    requests = []

    def respond(request):
        requests.append(request)
        return response

    assert run(respond)["code"] == code
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401, text="secret auth details"), "SPRING_AUTH_FAILED"),
        (httpx.Response(403), "SPRING_AUTH_FAILED"),
        (httpx.Response(503), "SPRING_READINESS_FAILED"),
        (httpx.Response(404), "SPRING_READINESS_FAILED"),
        (httpx.Response(200, json={"success": True, "data": {}}), "SPRING_READINESS_INVALID"),
        (httpx.Response(200, text="secret exception"), "SPRING_READINESS_INVALID"),
        (
            httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "operationsEnabled": "false",
                        "publicChatEnabled": False,
                        "capabilityConfigured": True,
                    },
                },
            ),
            "SPRING_READINESS_INVALID",
        ),
    ],
)
def test_health_alone_is_insufficient(response, code):
    result = run(
        lambda request: (
            successful_response(request) if request.url.path == "/actuator/health" else response
        )
    )
    assert result["code"] == code
    assert "secret" not in json.dumps(result)


def test_capability_must_be_provisioned_even_before_public_enablement():
    assert run(lambda req: successful_response(req, capability=False))["code"] == (
        "CAPABILITY_NOT_CONFIGURED"
    )


def test_spring_transport_failure_does_not_leak_url_or_keys():
    def respond(request):
        raise httpx.ConnectError("secret URL and internal key", request=request)

    assert run(respond) == {
        "schemaVersion": 1,
        "state": "NOT_READY",
        "code": "SPRING_UNREACHABLE",
    }


class ProbeEngine:
    def __init__(self, *, readonly="on", dimensions=1536, failure=None):
        self.readonly, self.dimensions, self.failure = readonly, dimensions, failure
        self.queries = []
        self.disposed = False

    @contextmanager
    def connect(self):
        yield self

    def scalar(self, sql, params=None):
        self.queries.append((str(sql), params))
        if str(sql).startswith("SHOW"):
            return self.readonly
        return self.dimensions

    def execute(self, sql, params):
        self.queries.append((str(sql), params))
        if self.failure:
            raise self.failure

    def dispose(self):
        self.disposed = True


def enable_retrieval(monkeypatch, engine):
    monkeypatch.setenv("CHAT_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv(
        "CHAT_RETRIEVAL_DATABASE_URL", "postgresql+psycopg://reader:secret@database.test/test"
    )
    monkeypatch.setenv("CHAT_RETRIEVAL_EMBEDDING_VERSION", "v42")

    def factory(url, **kwargs):
        assert url.drivername == "postgresql+psycopg"
        assert kwargs["poolclass"] is readiness.NullPool
        assert kwargs["hide_parameters"] is True
        assert kwargs["connect_args"]["connect_timeout"] == 5
        assert "default_transaction_read_only=on" in kwargs["connect_args"]["options"]
        assert "statement_timeout=5000" in kwargs["connect_args"]["options"]
        return engine

    monkeypatch.setattr(readiness, "create_engine", factory)


def test_retrieval_checks_actual_query_without_reading_corpus(monkeypatch):
    engine = ProbeEngine()
    enable_retrieval(monkeypatch, engine)
    result = run()
    assert result["checks"]["retrieval"] == "READY"
    assert engine.disposed
    sql, params = engine.queries[-1]
    for table in (
        "retrieval_chunks",
        "retrieval_sources",
        "episodes",
        "works",
        "members",
        "episode_source_purge_requests",
        "retrieval_current_chunks",
    ):
        assert table in sql
    assert sql.endswith("LIMIT 0")
    assert params["model"] == "text-embedding-3-small"
    assert params["version"] == "v42"
    assert len(json.loads(params["vector"])) == 1536


@pytest.mark.parametrize(
    ("engine", "code"),
    [
        (ProbeEngine(readonly="off"), "RETRIEVAL_TRANSACTION_NOT_READ_ONLY"),
        (ProbeEngine(dimensions=3072), "RETRIEVAL_VECTOR_SCHEMA_INVALID"),
        (ProbeEngine(failure=RuntimeError("secret DB URL")), "RETRIEVAL_DATABASE_NOT_READY"),
    ],
)
def test_database_failures_are_safe_and_connections_closed(monkeypatch, engine, code):
    enable_retrieval(monkeypatch, engine)
    assert run() == {"schemaVersion": 1, "state": "NOT_READY", "code": code}
    assert engine.disposed


def test_invalid_database_scheme_is_rejected_before_network(monkeypatch):
    monkeypatch.setenv("CHAT_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("CHAT_RETRIEVAL_DATABASE_URL", "sqlite:///secret.sqlite")
    result = run(lambda req: pytest.fail("Invalid DSN must fail before HTTP"))
    assert result["code"] == "RETRIEVAL_DATABASE_URL_INVALID"


def test_entrypoint_prints_only_safe_json_and_sets_failure_exit_status(monkeypatch, capsys):
    monkeypatch.setenv("LLM_API_KEY", "")
    assert readiness.entrypoint() == 2
    assert json.loads(capsys.readouterr().out)["code"] == "MODEL_CONFIGURATION_INVALID"
