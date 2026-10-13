import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.chatbot.outbox import Outbox
from app.retrieval.cli import main, parser
from app.schemas.chat_execution import Dispatch
from app.domain.enums import AnalysisJobCheckpointStage
from app.worker.analysis_job_worker import AnalysisJobWorker


@pytest.mark.parametrize("enabled,fail", [(False, False), (True, False), (True, True)])
def test_new_and_resumed_analysis_prepares_without_replacing_analysis_chunks(
    monkeypatch, enabled, fail
):
    monkeypatch.setattr(
        "app.worker.analysis_job_worker.get_settings", lambda: Settings(_env_file=None)
    )
    scopes = []

    def prepare(scope):
        scopes.append(scope)
        if fail:
            raise RuntimeError("private-source-content")

    chunks = [SimpleNamespace(id=uuid4())]

    class Blocking:
        async def run(self, fn, *a, **kw):
            return fn(*a, **kw)

    worker = AnalysisJobWorker(
        spring_client=SimpleNamespace(),
        episode_chunk_service=SimpleNamespace(get_episode_chunks=lambda _: chunks),
        blocking_io_executor=Blocking(),
        retrieval_preparation_enabled=enabled,
        retrieval_preparer=SimpleNamespace(prepare=prepare),
    )
    payload = SimpleNamespace(work_id=uuid4(), episode=SimpleNamespace(episode_id=uuid4()))
    result, metrics = asyncio.run(
        worker._run_chunk_stage(payload, AnalysisJobCheckpointStage.CHUNKS_READY)
    )
    assert result is chunks
    assert len(scopes) == int(enabled)
    if enabled:
        assert scopes[0].work_id == payload.work_id
        assert scopes[0].episode_ids == (payload.episode.episode_id,)
    assert metrics["embeddedChunkCount"] == 0


def test_generation_off_does_not_construct_provider_but_flushes_reports_first(
    monkeypatch, tmp_path
):
    cfg = Settings(
        _env_file=None, spring_internal_api_key="test", embedding_generation_enabled=False
    )
    monkeypatch.setattr("app.retrieval.cli.Settings", lambda **kw: cfg)
    flushed = AsyncMock()
    monkeypatch.setattr("app.retrieval.cli.Outbox.flush", flushed)
    monkeypatch.setattr(
        "app.retrieval.cli.OpenAIEmbeddingsClient.from_settings",
        lambda _: pytest.fail("paid adapter created"),
    )
    args = parser().parse_args(["execute", "--outbox", str(tmp_path / "reports.db")])
    assert asyncio.run(main(args))["state"] == "DISABLED"
    flushed.assert_awaited_once()


def test_report_only_recovery_does_not_read_db_s3_or_provider(monkeypatch, tmp_path):
    cfg = Settings(
        _env_file=None, spring_internal_api_key="test", embedding_generation_enabled=True
    )
    monkeypatch.setattr("app.retrieval.cli.Settings", lambda **kw: cfg)
    monkeypatch.setattr("app.retrieval.cli.Outbox.flush", AsyncMock())
    monkeypatch.setattr(
        "app.retrieval.cli.create_engine", lambda *a, **kw: pytest.fail("DB created")
    )
    args = parser().parse_args(["recover-reports", "--outbox", str(tmp_path / "reports.db")])
    assert asyncio.run(main(args))["state"] == "REPORTS_DELIVERED"


def test_explicit_episode_ids_cannot_be_silently_cut_into_a_source_page():
    from app.retrieval.contracts import Scope

    with pytest.raises(ValueError, match="Invalid episode IDs"):
        Scope(uuid4(), (uuid4(), uuid4()), max_sources=1)


@pytest.mark.parametrize("enabled", [False, True])
def test_startup_recovery_deadline_retains_reports_without_starting_indexing(
    monkeypatch, tmp_path, enabled
):
    path = tmp_path / "reports.db"
    expires = datetime.now(UTC) + timedelta(minutes=5)
    dispatch = Dispatch(schemaVersion=1, attemptId=uuid4(), capability="a" * 64, deadline=expires)
    with_box = Outbox(str(path))
    with_box.put("pending", dispatch, uuid4(), "usage", {"test": True}, expires.timestamp())
    with_box.close()
    cfg = Settings(
        _env_file=None, spring_internal_api_key="test", embedding_generation_enabled=enabled
    )
    monkeypatch.setattr("app.retrieval.cli.Settings", lambda **kw: cfg)
    monkeypatch.setattr(
        "app.retrieval.cli.OpenAIEmbeddingsClient.from_settings",
        lambda _: pytest.fail("provider created after recovery exhausted the budget"),
    )
    monkeypatch.setattr(
        "app.retrieval.cli.create_engine", lambda *a, **kw: pytest.fail("DB created")
    )

    async def execute():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked_request(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr("app.retrieval.cli.ChatExecutionClient.request", blocked_request)
        args = parser().parse_args(["execute", "--max-seconds", "1", "--outbox", str(path)])
        result = await asyncio.wait_for(main(args), timeout=2)
        assert started.is_set() and cancelled.is_set()
        assert result == {
            "state": "TIME_LIMIT",
            "providerAttempts": 0,
            "userTokenDebit": False,
            "pendingReports": 1,
        }
        recovered = Outbox(str(path))
        try:
            assert recovered.count() == 1
            client = SimpleNamespace(request=AsyncMock())
            await recovered.flush(client)
            client.request.assert_awaited_once()
            assert recovered.count() == 0
        finally:
            recovered.close()

    asyncio.run(execute())


def test_cli_passes_deadline_from_before_recovery_to_index_service(monkeypatch, tmp_path):
    cfg = Settings(
        _env_file=None,
        database_url="postgresql://unused.invalid/test",
        spring_internal_api_key="test",
        llm_api_key="test",
        embedding_generation_enabled=True,
    )
    monkeypatch.setattr("app.retrieval.cli.Settings", lambda **kw: cfg)
    delegate = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        "app.retrieval.cli.OpenAIEmbeddingsClient.from_settings", lambda _: delegate
    )
    monkeypatch.setattr(
        "app.retrieval.cli.create_engine", lambda *a, **kw: SimpleNamespace(dispose=lambda: None)
    )
    monkeypatch.setattr("app.retrieval.cli.sessionmaker", lambda *a, **kw: None)
    monkeypatch.setattr(
        "app.retrieval.cli.RetrievalStore",
        lambda _: SimpleNamespace(exclusive=lambda _: nullcontext()),
    )
    monkeypatch.setattr("app.retrieval.cli.S3TextObjectStorage.from_settings", lambda _: None)

    async def execute():
        loop = asyncio.get_running_loop()
        started = loop.time()
        recovery_started = []

        async def recover(*args):
            recovery_started.append(loop.time())
            await asyncio.sleep(0.05)

        async def run(self, scope, provider, *, enabled, limits, deadline):
            assert limits.max_seconds == 1
            assert started + 1 <= deadline <= recovery_started[0] + 1
            assert 0 < deadline - loop.time() < 0.98
            return {"state": "COMPLETED"}

        monkeypatch.setattr("app.retrieval.cli.Outbox.flush", recover)
        monkeypatch.setattr("app.retrieval.cli.RetrievalIndexService.run", run)
        args = parser().parse_args(
            [
                "execute",
                "--max-seconds",
                "1",
                "--work-id",
                str(uuid4()),
                "--actor",
                "test",
                "--outbox",
                str(tmp_path / "reports.db"),
            ]
        )
        assert (await main(args))["state"] == "COMPLETED"
        delegate.aclose.assert_awaited_once()

    asyncio.run(execute())
