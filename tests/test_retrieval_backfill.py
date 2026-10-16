import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.config import Settings
from app.retrieval.backfill import (
    CampaignConfig,
    CampaignProvider,
    CampaignRunner,
    CampaignState,
    exclusive_campaign,
    main,
    operations_ready,
)
from app.retrieval.contracts import IndexBusy, IndexSpec
from app.retrieval.provider import IndexProviderFailure
from app.retrieval.service import RetrievalIndexService
from tests.test_retrieval_service import MemoryStore, Provider, TEXT

SHORT = "A synthetic scene."


class Corpus:
    chunk_state = staticmethod(MemoryStore.chunk_state)

    def __init__(self):
        self.items = {}
        self.texts = {}
        self.birth = {}
        self.prepared_fingerprints = {}
        self.clock = datetime.now(UTC)
        self.page_calls = []
        self.snapshot_calls = 0

    def add(self, work, number, value=SHORT, *, episode_id=None):
        memory = MemoryStore()
        memory.source = replace(
            memory.source,
            work_id=work,
            episode_no=number,
            episode_id=episode_id or uuid4(),
            content_hash=sha256(value.encode()).hexdigest(),
        )
        self.items[memory.source.episode_id] = memory
        self.texts[memory.source.episode_id] = value
        self.birth[memory.source.episode_id] = self.clock
        return memory

    def backfill_snapshot(self, work_ids):
        self.snapshot_calls += 1
        works = sorted({m.source.work_id for m in self.items.values()})
        return [
            (
                work,
                max(m.source.episode_no for m in self.items.values() if m.source.work_id == work),
                self.clock.isoformat(),
            )
            for work in works
            if not work_ids or work in work_ids
        ]

    def backfill_sources(self, work, upper, at, after_no, after_id, limit):
        selected = sorted(
            [
                m.source
                for m in self.items.values()
                if m.source.work_id == work
                and m.source.episode_no <= upper
                and self.birth[m.source.episode_id] <= datetime.fromisoformat(at)
                and (m.source.episode_no, m.source.episode_id) > (after_no, after_id)
                and m.source.state != "ARCHIVED"
            ],
            key=lambda s: (s.episode_no, s.episode_id),
        )[:limit]
        self.page_calls.append((work, after_no, after_id, len(selected)))
        return selected

    def exclusive(self, _):
        return nullcontext()

    def get_text(self, key, **kwargs):
        # Service only gets the per-source storage adapter below.
        raise AssertionError()

    def inspect(self, source, spec):
        memory = self.items[source.episode_id]
        if memory.source.fingerprint != source.fingerprint:
            return {"state": "SOURCE_CHANGED"}
        if self.prepared_fingerprints.get(source.episode_id) != source.fingerprint:
            return {"state": "UNPREPARED"}
        return {
            "state": "READY"
            if all(self.chunk_state(c, spec) == "READY" for c in memory.chunks)
            else "PARTIAL"
        }

    def prepare(self, source, spec, slices):
        memory = self.items[source.episode_id]
        if self.prepared_fingerprints.get(source.episode_id) != source.fingerprint:
            memory.chunks = []
        result = memory.prepare(source, spec, slices)
        self.prepared_fingerprints[source.episode_id] = source.fingerprint
        return result

    def check_source(self, source):
        self.items[source.episode_id].check_source(source)
        self.current_source = source

    def __getattr__(self, method):
        def invoke(source, *args):
            return getattr(self.items[source.episode_id], method)(source, *args)

        return invoke

    def service(self):
        return RetrievalIndexService(
            self,
            SimpleNamespace(get_text=lambda *a, **kw: self.texts[self.current_source.episode_id]),
            IndexSpec(),
        )


def config(tmp_path, **changes):
    return replace(
        CampaignConfig(
            "synthetic-campaign",
            1000000,
            state_path=str(tmp_path / "state.sqlite"),
            outbox_path=str(tmp_path / "outbox.sqlite"),
            actor="synthetic-operator",
        ),
        **changes,
    )


def invoke(cfg, corpus, provider=None, *, resume=False):
    with exclusive_campaign(cfg.state_path):
        state = CampaignState(cfg, IndexSpec())
        try:
            if resume:
                state.resume()

            async def run():
                return await CampaignRunner(
                    cfg, state, corpus.service(), provider or Provider()
                ).run(asyncio.get_running_loop().time() + cfg.max_seconds)

            return asyncio.run(run())
        finally:
            state.close()


def test_more_than_400_episodes_multiwork_pages_restart_and_ready_skip(tmp_path):
    corpus = Corpus()
    for work, count in [(UUID(int=1), 405), (UUID(int=2), 12)]:
        for number in range(1, count + 1):
            corpus.add(work, number)
    cfg = config(tmp_path, max_chunks=127, page_size=100)
    total_attempts = 0
    for _ in range(4):
        report = invoke(cfg, corpus)
        total_attempts += report["providerAttempts"]
    assert report["state"] == "READY"
    assert total_attempts == report["checkedSources"] == report["readySources"] == 417
    assert len(corpus.page_calls) > 4 and max(p[3] for p in corpus.page_calls) == 100
    assert corpus.snapshot_calls == 1
    next_sweep = invoke(cfg, corpus)
    assert next_sweep["state"] == "READY" and next_sweep["providerAttempts"] == 0
    assert next_sweep["admittedInputTokenUpperBound"] == report["admittedInputTokenUpperBound"]
    assert corpus.snapshot_calls == 2
    assert all(str(work) not in json.dumps(report) for work in (UUID(int=1), UUID(int=2)))


def test_keyset_retains_same_episode_number_and_new_sources_wait_next_snapshot(tmp_path):
    corpus, work = Corpus(), uuid4()
    for identifier in [UUID(int=3), UUID(int=1), UUID(int=2)]:
        corpus.add(work, 1, episode_id=identifier)
    cfg = config(tmp_path, max_chunks=1, page_size=1)
    assert invoke(cfg, corpus)["embeddedChunks"] == 1
    corpus.clock += timedelta(seconds=1)
    corpus.add(work, 1, episode_id=UUID(int=4))
    corpus.add(work, 2)
    invoke(cfg, corpus)
    invoke(cfg, corpus)
    done = invoke(cfg, corpus)
    assert done["state"] == "READY" and done["checkedSources"] == 3
    assert invoke(cfg, corpus)["embeddedChunks"] == 1
    assert invoke(cfg, corpus)["embeddedChunks"] == 1
    assert invoke(cfg, corpus)["checkedSources"] == 5


def test_mid_source_chunk_limit_never_advances_page_and_resume_skips_saved_chunks(tmp_path):
    corpus = Corpus()
    item = corpus.add(uuid4(), 1, TEXT)
    cfg = config(tmp_path, max_chunks=1, page_size=100)
    first = invoke(cfg, corpus)
    assert first["state"] == "CHUNK_LIMIT" and first["checkedSources"] == 0
    state = CampaignState(cfg, IndexSpec())
    assert state.next_work()["after_no"] == 0
    state.close()
    total = first["providerAttempts"]
    for _ in range(100):
        result = invoke(cfg, corpus)
        total += result["providerAttempts"]
        if result["state"] == "READY":
            break
    assert result["state"] == "READY" and result["checkedSources"] == 1
    assert total == len(item.chunks) > 1


def test_total_budget_is_durable_and_never_resets_each_invocation(tmp_path):
    corpus = Corpus()
    for number in range(1, 7):
        corpus.add(UUID(int=1), number)
    per_call = len(SHORT.encode()) + 256
    cfg = config(tmp_path, max_input_tokens=per_call * 3 + 1, max_chunks=2)
    assert invoke(cfg, corpus)["providerAttempts"] == 2
    second = invoke(cfg, corpus)
    assert second["state"] == "BUDGET_EXHAUSTED" and second["providerAttempts"] == 1
    for resume in [False, False, True]:
        report = invoke(cfg, corpus, resume=resume)
        assert report["state"] == "BUDGET_EXHAUSTED" and report["providerAttempts"] == 0
        assert report["admittedInputTokenUpperBound"] == per_call * 3
        assert report["remainingInputTokenAllowance"] == 1


@pytest.mark.parametrize(
    "change", [{"max_input_tokens": 2000}, {"work_ids": (UUID(int=1),)}, {"actor": "another"}]
)
def test_campaign_identity_cannot_change_on_restart(tmp_path, change):
    cfg = config(tmp_path)
    state = CampaignState(cfg, IndexSpec())
    state.close()
    with pytest.raises(ValueError, match="immutable"):
        CampaignState(replace(cfg, **change), IndexSpec())
    with pytest.raises(ValueError, match="immutable"):
        CampaignState(cfg, IndexSpec(version="v2"))


def test_private_state_and_exclusive_lock(tmp_path):
    cfg = config(tmp_path)
    with exclusive_campaign(cfg.state_path):
        with pytest.raises(IndexBusy):
            with exclusive_campaign(cfg.state_path):
                pytest.fail("two simultaneous runners")
        state = CampaignState(cfg, IndexSpec())
        state.close()
    assert (tmp_path / "state.sqlite").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state.sqlite.lock").stat().st_mode & 0o777 == 0o600


def test_crash_after_reservation_blocks_paid_replay_and_resume_never_refunds(tmp_path):
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    cfg = config(tmp_path)
    state = CampaignState(cfg, IndexSpec())
    state.reserve(777)
    state.close()  # killed process after durable reserve, before final state update
    result = invoke(cfg, corpus)
    assert result["state"] == "BLOCKED" and result["reason"] == "UNKNOWN_USAGE"
    assert result["providerAttempts"] == 0 and result["admittedInputTokenUpperBound"] == 777
    resumed = invoke(cfg, corpus, resume=True)
    assert resumed["state"] == "READY"
    assert resumed["admittedInputTokenUpperBound"] == 777 + len(SHORT.encode()) + 256


def test_unknown_usage_stops_retries_and_blocks_next_timer(tmp_path):
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    cfg = config(tmp_path)

    def unknown(provider):
        provider.unknown_usage += 1
        raise IndexProviderFailure("PROVIDER_CONTRACT", False)

    result = invoke(cfg, corpus, Provider(unknown))
    assert result["state"] == "BLOCKED" and result["reason"] == "UNKNOWN_USAGE"
    assert result["providerAttempts"] == 1
    assert invoke(cfg, corpus)["providerAttempts"] == 0


def test_known_provider_failure_blocks_until_explicit_resume(tmp_path):
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    cfg = config(tmp_path, max_retries=0)

    def fail(_):
        raise IndexProviderFailure("PROVIDER_RETRYABLE", True)

    first = invoke(cfg, corpus, Provider(fail))
    assert first["state"] == "BLOCKED" and first["reason"] == "PROVIDER_RETRIES_EXHAUSTED"
    assert not first["uncertainAttempt"] and first["checkedSources"] == 0
    assert invoke(cfg, corpus)["providerAttempts"] == 0
    resumed = invoke(cfg, corpus, resume=True)
    assert resumed["state"] == "READY" and resumed["providerAttempts"] == 1
    assert resumed["admittedInputTokenUpperBound"] == first["admittedInputTokenUpperBound"] * 2


def test_changed_source_behind_cursor_is_reindexed_next_sweep(tmp_path):
    corpus = Corpus()
    item = corpus.add(uuid4(), 1)
    cfg = config(tmp_path)
    first = invoke(cfg, corpus)
    assert first["state"] == "READY"
    updated = "Updated synthetic source."
    item.source = replace(item.source, content_hash=sha256(updated.encode()).hexdigest())
    corpus.texts[item.source.episode_id] = updated
    next_sweep = invoke(cfg, corpus)
    assert next_sweep["state"] == "READY" and next_sweep["providerAttempts"] == 1
    assert next_sweep["admittedInputTokenUpperBound"] > first["admittedInputTokenUpperBound"]


def test_source_changes_during_provider_do_not_advance_cursor_or_restore_vectors(tmp_path):
    corpus = Corpus()
    item = corpus.add(uuid4(), 1)
    cfg = config(tmp_path)

    def change(_):
        item.source = replace(item.source, content_hash="b" * 64)

    first = invoke(cfg, corpus, Provider(change))
    assert first["state"] == "PROGRESS" and first["reason"] == "SOURCE_CHANGED"
    assert first["checkedSources"] == 0
    assert all(c.status != "READY" for c in item.chunks)
    for _ in range(2):
        report = invoke(cfg, corpus)
    assert report["state"] == "BLOCKED" and report["providerAttempts"] == 0


def test_unavailable_sources_produce_partial_not_ready(tmp_path):
    corpus = Corpus()
    item = corpus.add(uuid4(), 1)
    item.source = replace(item.source, state="PURGE_PENDING")
    report = invoke(config(tmp_path), corpus)
    assert report["state"] == "PARTIAL" and report["readySources"] == 0
    assert report["excludedOrUnavailableSources"] == 1 and report["providerAttempts"] == 0


def test_disabled_without_state_or_outbox_does_not_construct_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.backfill.Settings", lambda **kw: pytest.fail("settings loaded")
    )
    env = {"RETRIEVAL_BACKFILL_OUTBOX_PATH": str(tmp_path / "absent.sqlite")}
    assert asyncio.run(main("run", env))["state"] == "DISABLED"
    assert not list(tmp_path.iterdir())


def test_explicit_scope_and_total_budget_required(tmp_path):
    base = {
        "RETRIEVAL_BACKFILL_ID": "stable",
        "RETRIEVAL_BACKFILL_ACTOR": "actor",
        "RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS": "1000",
    }
    with pytest.raises(ValueError):
        CampaignConfig.from_env(base)
    valid = CampaignConfig.from_env({**base, "RETRIEVAL_BACKFILL_SCOPE": "active-works"})
    assert valid.work_ids == () and valid.max_input_tokens == 1000
    with pytest.raises(ValueError):
        CampaignConfig.from_env(
            {
                **base,
                "RETRIEVAL_BACKFILL_SCOPE": "active-works",
                "RETRIEVAL_BACKFILL_WORK_IDS": str(uuid4()),
            }
        )
    with pytest.raises(ValueError):
        config(tmp_path, max_seconds=301)


@pytest.mark.parametrize(
    "status,data,allowed",
    [
        (
            200,
            {"operationsEnabled": True, "capabilityConfigured": True, "publicChatEnabled": False},
            True,
        ),
        (200, {"operationsEnabled": True, "capabilityConfigured": False}, False),
        (200, {"operationsEnabled": True}, False),
        (503, {}, False),
        (200, {"operationsEnabled": False}, False),
        (200, {}, False),
    ],
)
def test_readiness_is_authenticated_get_without_side_effect(status, data, allowed):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, json={"success": status == 200, "data": data})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client = SimpleNamespace(
                http=http, base_url="https://internal.invalid", api_key="test-secret"
            )
            if allowed:
                await operations_ready(client)
            else:
                from app.chatbot.client import ChatTransportError

                with pytest.raises(ChatTransportError):
                    await operations_ready(client)

    asyncio.run(run())
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].headers["X-Internal-Api-Key"] == "test-secret"
    assert str(calls[0].url).endswith("/operations/readiness")


def test_disabled_recovers_report_outbox_before_returning(monkeypatch, tmp_path):
    from app.chatbot.outbox import Outbox
    from app.schemas.chat_execution import Dispatch

    path = tmp_path / "outbox.sqlite"
    box = Outbox(str(path))
    expires = datetime.now(UTC) + timedelta(minutes=5)
    dispatch = Dispatch(schemaVersion=1, attemptId=uuid4(), capability="a" * 64, deadline=expires)
    box.put("pending", dispatch, uuid4(), "finish", {"test": True}, expires.timestamp())
    box.close()
    settings = Settings(_env_file=None, spring_internal_api_key="test")
    monkeypatch.setattr("app.retrieval.backfill.Settings", lambda **kw: settings)
    requests = AsyncMock()
    monkeypatch.setattr("app.retrieval.backfill.ChatExecutionClient.request", requests)
    monkeypatch.setattr(
        "app.retrieval.backfill.OpenAIEmbeddingsClient.from_settings",
        lambda _: pytest.fail("provider created"),
    )
    result = asyncio.run(main("run", {"RETRIEVAL_BACKFILL_OUTBOX_PATH": str(path)}))
    assert result["state"] == "DISABLED" and result["pendingReports"] == 0
    requests.assert_awaited_once()


def test_cancelled_attempt_remains_uncertain_and_admission_is_not_refunded(tmp_path):
    state = CampaignState(config(tmp_path), IndexSpec())
    started = asyncio.Event()

    async def cancelled(*args):
        started.set()
        raise asyncio.CancelledError()

    delegate = SimpleNamespace(attempts=0, input_tokens=0, unknown_usage=0, embed=cancelled)
    provider = CampaignProvider(delegate, state)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await provider.embed(None, [SimpleNamespace(chunk_text=SHORT)], None)

    try:
        asyncio.run(run())
        assert started.is_set() and state.status()["uncertainAttempt"]
        assert state.status()["admittedInputTokenUpperBound"] == len(SHORT.encode()) + 256
    finally:
        state.close()


def test_provider_success_before_database_save_retains_durable_uncertainty(tmp_path):
    cfg = config(tmp_path)
    state = CampaignState(cfg, IndexSpec())
    provider = CampaignProvider(Provider(), state)

    async def no_op(_):
        pass

    asyncio.run(provider.embed(None, [SimpleNamespace(chunk_text=SHORT, token_count=4)], no_op))
    assert state.status()["uncertainAttempt"]
    state.close()  # simulate death after paid success, before PostgreSQL save
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    report = invoke(cfg, corpus)
    assert report["state"] == "BLOCKED" and report["reason"] == "UNKNOWN_USAGE"
    assert report["providerAttempts"] == 0


def test_database_save_failure_keeps_uncertainty_until_operator_resume(tmp_path):
    from sqlalchemy.exc import OperationalError

    corpus = Corpus()
    item = corpus.add(uuid4(), 1)
    original = item.save

    def fail(*args):
        raise OperationalError("", {}, Exception("private source detail"))

    item.save = fail
    cfg = config(tmp_path)
    first = invoke(cfg, corpus)
    assert first["state"] == "BLOCKED" and first["reason"] == "UNKNOWN_USAGE"
    assert first["providerAttempts"] == 1 and first["checkedSources"] == 0
    assert "private source" not in json.dumps(first)
    item.save = original
    assert invoke(cfg, corpus)["providerAttempts"] == 0
    resumed = invoke(cfg, corpus, resume=True)
    assert resumed["state"] == "READY" and resumed["providerAttempts"] == 1
    assert resumed["admittedInputTokenUpperBound"] == 2 * first["admittedInputTokenUpperBound"]


def test_preflight_is_readonly_and_can_run_while_campaign_lock_is_held(tmp_path, monkeypatch):
    from app.retrieval.backfill import preflight

    cfg = config(tmp_path)
    state = CampaignState(cfg, IndexSpec())
    state.reserve(777)
    state.close()
    before = (tmp_path / "state.sqlite").read_bytes()
    settings = Settings(
        _env_file=None,
        spring_internal_api_key="test",
        llm_api_key="test",
        embedding_generation_enabled=True,
        aws_s3_bucket="synthetic-bucket",
    )
    checked = AsyncMock()
    monkeypatch.setattr("app.retrieval.backfill.operations_ready", checked)
    queries = []
    engine = SimpleNamespace(
        connect=lambda: nullcontext(
            SimpleNamespace(
                execute=lambda sql: queries.append(str(sql)),
                scalar=lambda sql: queries.append(str(sql)) or True,
            )
        ),
        dispose=lambda: None,
    )
    monkeypatch.setattr("app.retrieval.backfill._engine", lambda _: engine)
    monkeypatch.setattr(
        "app.retrieval.backfill.OpenAIEmbeddingsClient.from_settings",
        lambda _: pytest.fail("provider created"),
    )
    monkeypatch.setattr(
        "app.retrieval.backfill.boto3.client", lambda *a, **kw: pytest.fail("S3 connected")
    )
    with exclusive_campaign(cfg.state_path):
        report = asyncio.run(preflight(cfg, settings, IndexSpec()))
    assert report["state"] == "PREFLIGHT_READY" and len(queries) == 3
    assert all(query.strip().startswith("SELECT") for query in queries)
    assert "transaction_read_only" in queries[-1] and "has_table_privilege" in queries[-1]
    assert before == (tmp_path / "state.sqlite").read_bytes()
    checked.assert_awaited_once()
    with pytest.raises(ValueError, match="immutable"):
        asyncio.run(preflight(replace(cfg, max_input_tokens=1), settings, IndexSpec()))


def test_preflight_does_not_create_a_new_campaign_or_outbox(tmp_path, monkeypatch):
    from app.retrieval.backfill import preflight

    settings = Settings(
        _env_file=None,
        spring_internal_api_key="test",
        llm_api_key="test",
        embedding_generation_enabled=True,
        aws_s3_bucket="synthetic-bucket",
    )
    monkeypatch.setattr("app.retrieval.backfill.operations_ready", AsyncMock())
    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(execute=lambda _: None, scalar=lambda _: True)),
        dispose=lambda: None,
    )
    monkeypatch.setattr("app.retrieval.backfill._engine", lambda _: engine)
    assert (
        asyncio.run(preflight(config(tmp_path), settings, IndexSpec()))["state"]
        == "PREFLIGHT_READY"
    )
    assert not list(tmp_path.iterdir())


def test_preflight_rejects_missing_writer_permissions_without_creating_state(tmp_path, monkeypatch):
    from app.retrieval.backfill import preflight

    settings = Settings(
        _env_file=None,
        spring_internal_api_key="test",
        llm_api_key="test",
        embedding_generation_enabled=True,
        aws_s3_bucket="synthetic-bucket",
    )
    monkeypatch.setattr("app.retrieval.backfill.operations_ready", AsyncMock())
    disposed = []
    engine = SimpleNamespace(
        connect=lambda: nullcontext(
            SimpleNamespace(execute=lambda _: None, scalar=lambda _: False)
        ),
        dispose=lambda: disposed.append(True),
    )
    monkeypatch.setattr("app.retrieval.backfill._engine", lambda _: engine)
    with pytest.raises(ValueError, match="writer permissions"):
        asyncio.run(preflight(config(tmp_path), settings, IndexSpec()))
    assert disposed == [True]
    assert not list(tmp_path.iterdir())


def test_pending_reports_stop_next_batch_then_resume_after_delivery(tmp_path):
    corpus = Corpus()
    item = corpus.add(uuid4(), 1, TEXT)
    pending = {"count": 0}

    def response(_):
        pending["count"] = 1

    provider = Provider(response)
    provider.outbox = SimpleNamespace(count=lambda: pending["count"])
    cfg = config(tmp_path, batch_size=1)
    first = invoke(cfg, corpus, provider)
    assert first["state"] == "REPORTS_PENDING" and first["providerAttempts"] == 1
    assert first["checkedSources"] == 0 and not first["uncertainAttempt"]
    pending["count"] = 0
    resumed = invoke(cfg, corpus)
    assert resumed["state"] == "READY" and resumed["embeddedChunks"] == len(item.chunks) - 1


@pytest.fixture
def ledger_failure_case(tmp_path):
    from app.chatbot.client import ChatTransportError
    from app.chatbot.outbox import Outbox
    from app.retrieval.provider import OperationalEmbeddingProvider
    from app.schemas.chat_execution import Dispatch

    case = SimpleNamespace(stage="operation", status=503, failing=True, ledger_calls=[])

    def check(stage):
        case.ledger_calls.append(stage)
        if case.failing and case.stage == stage:
            raise ChatTransportError(case.status)

    class Ledger:
        async def operation(self, operation_id, work_id, actor, policy, maximum):
            check("operation")
            self.work_id, self.maximum = work_id, maximum
            return Dispatch(
                schemaVersion=1,
                attemptId=operation_id,
                capability="a" * 64,
                deadline=datetime.now(UTC) + timedelta(minutes=2),
            )

        async def start(self, dispatch, runner):
            check("start")
            return SimpleNamespace(
                operational=True,
                work_id=self.work_id,
                question_id=None,
                token_budget=self.maximum,
                max_calls=1,
            )

        async def reserve(self, *args):
            check("reserve")

        async def request(self, *args):
            pass  # Synthetic report delivery; no external HTTP.

    class Embedding:
        async def create_embeddings(self, values):
            assert not case.failing, "A rejected ledger request must not invoke the provider"
            return SimpleNamespace(
                model=IndexSpec().model,
                input_token_count=7,
                embeddings=[[1.0] * IndexSpec().dimensions for _ in values],
            )

    case.outbox = Outbox(str(tmp_path / "outbox.sqlite"))
    case.provider = OperationalEmbeddingProvider(
        Ledger(), Embedding(), case.outbox, IndexSpec(), "synthetic-actor"
    )
    try:
        yield case
    finally:
        case.outbox.close()


@pytest.mark.parametrize("stage", ["operation", "start", "reserve"])
@pytest.mark.parametrize("status", [None, 503, 429])
def test_pre_provider_transient_ledger_failure_recovers_on_next_run_without_refund(
    tmp_path, ledger_failure_case, stage, status
):
    case = ledger_failure_case
    case.stage, case.status = stage, status
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    cfg = config(tmp_path)
    first = invoke(cfg, corpus, case.provider)
    assert first["state"] == "PROGRESS" and first["reason"] == "LEDGER_FAILURE"
    assert first["providerAttempts"] == 0 and not first["uncertainAttempt"]
    assert case.ledger_calls.count(stage) == 1  # No immediate service-level retry.
    case.failing = False
    second = invoke(cfg, corpus, case.provider)
    assert second["state"] == "READY" and second["providerAttempts"] == 1
    assert second["admittedInputTokenUpperBound"] == 2 * first["admittedInputTokenUpperBound"]


def test_pre_provider_transient_ledger_failures_block_after_three_timer_runs(
    tmp_path, ledger_failure_case
):
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    cfg, case = config(tmp_path), ledger_failure_case
    reports = [invoke(cfg, corpus, case.provider) for _ in range(3)]
    assert [report["state"] for report in reports] == ["PROGRESS", "PROGRESS", "BLOCKED"]
    assert reports[-1]["reason"] == "LEDGER_FAILURE"
    assert all(
        not report["uncertainAttempt"] and report["providerAttempts"] == 0 for report in reports
    )
    assert (
        reports[-1]["admittedInputTokenUpperBound"]
        == 3 * reports[0]["admittedInputTokenUpperBound"]
    )
    case.failing = False
    assert invoke(cfg, corpus, case.provider)["state"] == "BLOCKED"
    assert case.ledger_calls == ["operation"] * 3


@pytest.mark.parametrize("stage", ["operation", "start", "reserve"])
@pytest.mark.parametrize("status", [401, 403, 409])
def test_pre_provider_permanent_ledger_rejection_blocks_without_unknown_billing(
    tmp_path, ledger_failure_case, stage, status
):
    case = ledger_failure_case
    case.stage, case.status = stage, status
    corpus = Corpus()
    corpus.add(uuid4(), 1)
    cfg = config(tmp_path)
    first = invoke(cfg, corpus, case.provider)
    assert first["state"] == "BLOCKED" and first["reason"] == "LEDGER_REJECTED"
    assert first["providerAttempts"] == 0 and not first["uncertainAttempt"]
    before = list(case.ledger_calls)
    case.failing = False
    assert invoke(cfg, corpus, case.provider)["state"] == "BLOCKED"
    assert case.ledger_calls == before


def test_ledger_transport_error_after_provider_started_keeps_uncertainty(tmp_path):
    from app.chatbot.client import ChatTransportError

    corpus = Corpus()
    corpus.add(uuid4(), 1)

    def fail(_):
        raise ChatTransportError(503)

    report = invoke(config(tmp_path), corpus, Provider(fail))
    assert report["state"] == "BLOCKED" and report["reason"] == "UNKNOWN_USAGE"
    assert report["providerAttempts"] == 1 and report["uncertainAttempt"]


def test_completed_empty_sweep_resets_old_failure_count_before_new_source(tmp_path):
    cfg, corpus = config(tmp_path), Corpus()
    old = corpus.add(uuid4(), 1)
    old.source = replace(old.source, content_hash="b" * 64)
    assert invoke(cfg, corpus)["state"] == "PROGRESS"
    assert invoke(cfg, corpus)["state"] == "PROGRESS"
    old.source = replace(old.source, state="ARCHIVED")
    assert invoke(cfg, corpus)["state"] == "EMPTY"
    new = corpus.add(uuid4(), 1)
    new.source = replace(new.source, content_hash="c" * 64)
    assert invoke(cfg, corpus)["state"] == "PROGRESS"
    assert invoke(cfg, corpus)["state"] == "PROGRESS"
    assert invoke(cfg, corpus)["state"] == "BLOCKED"


def test_successful_vectors_with_unknown_usage_block_following_paid_calls(tmp_path):
    corpus = Corpus()
    item = corpus.add(uuid4(), 1)

    def response(provider):
        provider.unknown_usage += 1

    cfg = config(tmp_path)
    first = invoke(cfg, corpus, Provider(response))
    assert first["state"] == "BLOCKED" and first["reason"] == "UNKNOWN_USAGE"
    assert first["providerAttempts"] == 1 and first["uncertainAttempt"]
    assert all(chunk.status == "READY" for chunk in item.chunks)
    resumed = invoke(cfg, corpus, resume=True)
    assert resumed["state"] == "READY" and resumed["providerAttempts"] == 0


def test_status_reads_pending_outbox_without_delivery_or_mutation(tmp_path, monkeypatch):
    from app.chatbot.outbox import Outbox
    from app.schemas.chat_execution import Dispatch

    cfg = config(tmp_path)
    state = CampaignState(cfg, IndexSpec())
    state.start_sweep([])
    state.finish_sweep()
    state.close()
    outbox = Outbox(cfg.outbox_path)
    expires = datetime.now(UTC) + timedelta(minutes=5)
    dispatch = Dispatch(schemaVersion=1, attemptId=uuid4(), capability="a" * 64, deadline=expires)
    outbox.put("pending", dispatch, uuid4(), "finish", {"test": True}, expires.timestamp())
    outbox.close()
    before = (tmp_path / "outbox.sqlite").read_bytes()
    monkeypatch.setattr("app.retrieval.backfill.CampaignConfig.from_env", lambda _: cfg)
    monkeypatch.setattr("app.retrieval.backfill.Settings", lambda **kw: Settings(_env_file=None))
    monkeypatch.setattr("app.retrieval.backfill.Outbox", None, raising=False)
    monkeypatch.setattr(
        "app.retrieval.backfill.ChatExecutionClient.request",
        AsyncMock(side_effect=AssertionError("delivery during status")),
    )
    report = asyncio.run(main("status", {}))
    assert report["state"] == "REPORTS_PENDING" and report["pendingReports"] == 1
    assert report["sweep"] == 1 and report["checkedSources"] == report["readySources"] == 0
    assert not report["sweepInProgress"] and not report["uncertainAttempt"]
    assert before == (tmp_path / "outbox.sqlite").read_bytes()
