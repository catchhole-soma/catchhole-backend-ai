"""Additive wire parity and opt-in runtime configuration without provider or DB access."""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine

from app.chatbot import http_app
from app.chatbot.hybrid import HybridQaHandler
from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.qa_config import ChatQaSettings
from app.schemas.chat_execution import ChatResult
from app.schemas.chat_hybrid import RETRIEVAL_SOURCE_LIMIT, HybridPlan
from app.schemas.chat_retrieval import RetrievalReceipt, RetrievalReference


@pytest.mark.parametrize("limit", [1, 6, 40])
def test_model_call_limit_accepts_supported_environment_bounds(monkeypatch, limit):
    monkeypatch.setenv("CHAT_RETRIEVAL_MAX_MODEL_CALLS", str(limit))
    assert HybridSettings().max_model_calls == limit


@pytest.mark.parametrize("limit", [0, 41])
def test_model_call_limit_rejects_environment_override_outside_bounds(monkeypatch, limit):
    # 상한은 원문 탐색(CHAT_RETRIEVAL_AGENT)을 켤 때 올릴 수 있게 40이다. 기본값은 6 그대로다.
    monkeypatch.setenv("CHAT_RETRIEVAL_MAX_MODEL_CALLS", str(limit))
    with pytest.raises(ValueError, match="max_model_calls"):
        HybridSettings()
    monkeypatch.delenv("CHAT_RETRIEVAL_MAX_MODEL_CALLS")
    assert HybridSettings().max_model_calls == 6


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


# ---- 원문 탐색(#319): 인용 상한과 설정 기본값 ----


def numbered_references(count):
    fixture = json.loads(
        (Path(__file__).parents[1] / "docs/contracts/chat-hybrid-v1.json").read_text()
    )
    reference = fixture["retrievalRequest"]["sources"][0]
    return [dict(reference, chunkId=str(UUID(int=number))) for number in range(1, count + 1)]


def test_retrieval_source_caps_accept_sixty_and_reject_sixty_one():
    assert RETRIEVAL_SOURCE_LIMIT == 60
    sixty, sixty_one = numbered_references(60), numbered_references(61)
    assert (
        len(RetrievalReceipt.model_validate({"schemaVersion": 1, "sources": sixty}).sources) == 60
    )
    with pytest.raises(ValueError):
        RetrievalReceipt.model_validate({"schemaVersion": 1, "sources": sixty_one})
    result = dict(outcome="ANSWER", text="합성 답", sources=[], limitations=[])
    assert (
        len(ChatResult.model_validate({**result, "retrievalSources": sixty}).retrieval_sources)
        == 60
    )
    with pytest.raises(ValueError):
        ChatResult.model_validate({**result, "retrievalSources": sixty_one})


def clear_chat_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("CHAT_RETRIEVAL_", "CHAT_QA_")):
            monkeypatch.delenv(name)


def test_exploration_is_off_by_default_and_existing_defaults_are_unchanged(monkeypatch):
    clear_chat_environment(monkeypatch)
    settings = HybridSettings()
    assert settings.agent is False
    existing = settings.model_dump(include=EXISTING_HYBRID_DEFAULTS.keys())
    assert existing == EXISTING_HYBRID_DEFAULTS
    # 스위치만 켜면 새 설정은 측정에 쓴 조합이 된다.
    monkeypatch.setenv("CHAT_RETRIEVAL_AGENT", "true")
    enabled = HybridSettings()
    assert enabled.agent is True
    assert enabled.model_dump(include=EXISTING_HYBRID_DEFAULTS.keys()) == EXISTING_HYBRID_DEFAULTS
    assert enabled.model_dump(include=AGENT_DEFAULTS.keys()) == AGENT_DEFAULTS
    # 새 설정은 agent와 agent_ 접두사뿐이고, 걷어 낸 회차 요약 색인 설정은 남아 있지 않다.
    added = (
        set(HybridSettings.model_fields)
        - set(EXISTING_HYBRID_DEFAULTS)
        - {
            "enabled",
            "database_url",
        }
    )
    assert added == {"agent", *AGENT_DEFAULTS}
    assert not any("outline" in name for name in HybridSettings.model_fields)


EXISTING_HYBRID_DEFAULTS = dict(
    embedding_version="v1",
    keyword_top_k=16,
    vector_top_k=16,
    neighbor_radius=1,
    focus_radius=3,
    ordered_top_k=4,
    max_neighbors=8,
    max_evidence=12,
    per_episode=4,
    evidence_tokens=9000,
    max_rounds=3,
    max_model_calls=6,
    max_query_embeddings=2,
)
AGENT_DEFAULTS = dict(
    agent_max_steps=4,
    agent_max_searches=4,
    agent_max_read_episodes=5,
    agent_pool_tokens=80000,
    agent_step_tokens=14000,
    agent_search_share=0.6,
    agent_keyword_top_k=6,
    agent_vector_top_k=4,
    agent_step_output_tokens=3000,
    agent_answer_reserve_seconds=75,
    agent_explore_seconds=45,
    agent_max_answers=3,
    agent_items=True,
    agent_relax_plan_range=True,
    agent_verify_registered=True,
    agent_extract=True,
    agent_extract_group_tokens=10000,
    agent_extract_max_groups=6,
    agent_extract_output_tokens=8000,
)


@pytest.mark.parametrize(
    "name,accepted,rejected",
    [
        ("MAX_EVIDENCE", "60", "61"),
        ("MAX_NEIGHBORS", "60", "61"),
        ("EVIDENCE_TOKENS", "60000", "60001"),
        ("MAX_QUERY_EMBEDDINGS", "64", "65"),
        ("NEIGHBOR_RADIUS", "2", "3"),  # 저장소의 이웃 반경 계약(0~3) 안에 두려고 넓히지 않았다
        ("AGENT_POOL_TOKENS", "150000", "150001"),
        ("AGENT_MAX_STEPS", "8", "9"),
        ("AGENT_MAX_READ_EPISODES", "5", "6"),
        ("AGENT_EXTRACT_MAX_GROUPS", "12", "13"),
        ("AGENT_EXPLORE_SECONDS", "0", "241"),
    ],
)
def test_widened_bounds_accept_the_new_ceiling_and_reject_beyond(
    monkeypatch, name, accepted, rejected
):
    clear_chat_environment(monkeypatch)
    monkeypatch.setenv(f"CHAT_RETRIEVAL_{name}", accepted)
    HybridSettings()
    monkeypatch.setenv(f"CHAT_RETRIEVAL_{name}", rejected)
    with pytest.raises(ValueError):
        HybridSettings()


def test_qa_input_bounds_are_wider_but_defaults_stay(monkeypatch):
    clear_chat_environment(monkeypatch)
    defaults = ChatQaSettings()
    assert (defaults.max_input_tokens, defaults.max_context_characters) == (32000, 32000)
    assert defaults.provider_timeout_seconds == 90
    monkeypatch.setenv("CHAT_QA_MAX_INPUT_TOKENS", "160000")
    monkeypatch.setenv("CHAT_QA_MAX_CONTEXT_CHARACTERS", "400000")
    monkeypatch.setenv("CHAT_QA_PROVIDER_TIMEOUT_SECONDS", "150")
    raised = ChatQaSettings()
    assert (raised.max_input_tokens, raised.max_context_characters) == (160000, 400000)
    assert raised.provider_timeout_seconds == 150
    for name, value in [
        ("MAX_INPUT_TOKENS", "200001"),
        ("MAX_CONTEXT_CHARACTERS", "400001"),
        ("PROVIDER_TIMEOUT_SECONDS", "301"),
        ("PROVIDER_TIMEOUT_SECONDS", "9"),
    ]:
        clear_chat_environment(monkeypatch)
        monkeypatch.setenv(f"CHAT_QA_{name}", value)
        with pytest.raises(ValueError):
            ChatQaSettings()


@pytest.mark.parametrize("configured,expected", [(None, 90), ("150", 150)])
def test_provider_timeout_setting_reaches_the_provider_http_client(
    monkeypatch, tmp_path, configured, expected
):
    clear_chat_environment(monkeypatch)
    environment(monkeypatch, tmp_path)
    monkeypatch.setenv("CHAT_QA_ENABLED", "true")
    if configured:
        monkeypatch.setenv("CHAT_QA_PROVIDER_TIMEOUT_SECONDS", configured)
    seen = {}

    def provider(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(http_app, "OpenAIResponsesClient", provider)
    app = http_app.create_app(api_key="synthetic-internal-key")

    async def run():
        async with app.router.lifespan_context(app):
            timeout = seen["http_client"].timeout
            assert (timeout.read, timeout.write, timeout.connect) == (expected, expected, 10)

    asyncio.run(run())
