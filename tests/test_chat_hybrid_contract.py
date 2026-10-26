"""Additive wire parity and opt-in runtime configuration without provider or DB access."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.chatbot import http_app
from app.chatbot.hybrid import HybridQaHandler
from app.chatbot.hybrid_config import HybridSettings
from app.schemas.chat_execution import ChatResult
from app.schemas.chat_hybrid import HybridPlan
from app.schemas.chat_retrieval import RetrievalReceipt, RetrievalReference


@pytest.mark.parametrize("limit", [1, 6])
def test_model_call_limit_accepts_supported_environment_bounds(monkeypatch, limit):
    monkeypatch.setenv("CHAT_RETRIEVAL_MAX_MODEL_CALLS", str(limit))
    assert HybridSettings().max_model_calls == limit


@pytest.mark.parametrize("limit", [7, 12])
def test_model_call_limit_rejects_environment_override_above_six(monkeypatch, limit):
    monkeypatch.setenv("CHAT_RETRIEVAL_MAX_MODEL_CALLS", str(limit))
    with pytest.raises(ValueError, match="max_model_calls"):
        HybridSettings()


def test_shared_current_source_wire_fixture():
    fixture = json.loads(
        (Path(__file__).parents[1] / "docs/contracts/chat-hybrid-v1.json").read_text()
    )
    refs = [RetrievalReference.model_validate(v) for v in fixture["retrievalRequest"]["sources"]]
    assert RetrievalReceipt.model_validate(fixture["retrievalResponse"]).sources == refs
    result = ChatResult.model_validate(fixture["result"])
    assert result.retrieval_sources == refs
    assert result.model_dump(by_alias=True, mode="json") == fixture["result"]
    old = result.model_copy(update={"retrieval_sources": []}).model_dump(by_alias=True, mode="json")
    assert "retrievalSources" not in old


@pytest.mark.parametrize(
    "field,value", [("sourceVersion", "a" * 101), ("endOffset", 26), ("quote", " ")]
)
def test_malformed_receipt_is_rejected(field, value):
    fixture = json.loads(
        (Path(__file__).parents[1] / "docs/contracts/chat-hybrid-v1.json").read_text()
    )
    reference = fixture["retrievalRequest"]["sources"][0]
    reference[field] = value
    with pytest.raises(ValueError):
        RetrievalReference.model_validate(reference)


def test_duplicate_subjects_and_noninteger_ranges_fail_inside_planning_schema():
    plan = dict(
        disposition="PROCEED",
        registeredCurrentRequired=False,
        rdbMode="CURRENT",
        sourceNeed="NONE",
        subjects=[
            {"domain": "CHARACTER", "name": "아리아"},
            {"domain": "CHARACTER", "name": " 아리아 "},
        ],
        searchTerms=[],
        includeEvidence=False,
        episodeFrom=None,
        episodeTo=None,
    )
    with pytest.raises(ValueError):
        HybridPlan.model_validate(plan)
    plan["subjects"].pop()
    for value in (True, "1", 1.5):
        plan["episodeFrom"] = value
        with pytest.raises(ValueError):
            HybridPlan.model_validate(plan)


def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SPRING_INTERNAL_API_BASE_URL", "http://spring.test")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-never-used")


def test_disabled_retrieval_never_constructs_database_or_embedding(monkeypatch, tmp_path):
    environment(monkeypatch, tmp_path)
    monkeypatch.setenv("CHAT_QA_ENABLED", "true")
    monkeypatch.setenv("CHAT_RETRIEVAL_ENABLED", "false")

    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled retrieval must not allocate resources")

    monkeypatch.setattr(http_app, "create_engine", forbidden)
    monkeypatch.setattr(http_app, "OpenAIEmbeddingsClient", forbidden)
    app = http_app.create_app(api_key="synthetic-internal-key")

    async def run():
        async with app.router.lifespan_context(app):
            assert not isinstance(app.state.runtime.handler, HybridQaHandler)

    asyncio.run(run())


@pytest.mark.parametrize(
    "qa,dsn", [("false", ""), ("true", "secret-malformed-dsn"), ("true", "sqlite:///secret")]
)
def test_hybrid_requires_explicit_qa_and_postgres(monkeypatch, tmp_path, qa, dsn):
    environment(monkeypatch, tmp_path)
    monkeypatch.setenv("CHAT_QA_ENABLED", qa)
    monkeypatch.setenv("CHAT_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("CHAT_RETRIEVAL_DATABASE_URL", dsn)
    app = http_app.create_app(api_key="synthetic-internal-key")

    async def run():
        with pytest.raises(RuntimeError) as caught:
            async with app.router.lifespan_context(app):
                pytest.fail("Invalid runtime was enabled")
        assert "secret" not in str(caught.value)

    asyncio.run(run())


def test_hybrid_constructs_bounded_read_only_pool_and_disposes_it(monkeypatch, tmp_path):
    environment(monkeypatch, tmp_path)
    monkeypatch.setenv("CHAT_QA_ENABLED", "true")
    monkeypatch.setenv("CHAT_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv(
        "CHAT_RETRIEVAL_DATABASE_URL", "postgresql+psycopg://test:synthetic@localhost/test"
    )
    engine = create_engine("sqlite://")  # local stub; production factory arguments are inspected.
    called, disposed = [], []

    def factory(url, **kwargs):
        called.append(kwargs)
        return engine

    original_dispose = engine.dispose

    def dispose():
        disposed.append(True)
        original_dispose()

    monkeypatch.setattr(http_app, "create_engine", factory)
    monkeypatch.setattr(engine, "dispose", dispose)
    app = http_app.create_app(api_key="synthetic-internal-key")

    async def run():
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.runtime.handler, HybridQaHandler)
            assert app.state.runtime.registry is app.state.runtime.handler.metrics.registry

    asyncio.run(run())
    assert called[0]["pool_size"] == 2 and called[0]["max_overflow"] == 0
    assert called[0]["hide_parameters"] is True
    assert "default_transaction_read_only=on" in called[0]["connect_args"]["options"]
    assert "statement_timeout=5000" in called[0]["connect_args"]["options"]
    assert disposed == [True]
