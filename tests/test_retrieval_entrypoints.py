import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.retrieval.cli import main, parser
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
