"""GH344 validation runner's read-only engine against an explicitly disposable localhost PostgreSQL only.

Set GH344_TEST_DATABASE_URL (postgresql+psycopg://...@localhost or 127.0.0.1). Only SHOW, SELECT 1 and
pg_sleep run; no table is read or written. Without the variable the test is skipped.
"""

import os
import time

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from scripts import run_validation_worker as runner

pytestmark = pytest.mark.integration


def test_statement_timeout_cancels_a_stuck_read_and_returns_the_only_connection(monkeypatch):
    """A blocking read left behind by a cancelled asyncio wait is ended by the database, not by asyncio."""
    raw = os.getenv("GH344_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Explicit disposable localhost GH344_TEST_DATABASE_URL required")
    if make_url(raw).host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Only a disposable localhost PostgreSQL is allowed")
    monkeypatch.setattr(runner, "READONLY_STATEMENT_TIMEOUT_MS", 200)
    engine = runner.readonly_engine(raw, 1)  # pool_size=1, max_overflow=0: one stuck read could block all.
    sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        with sessions() as session:
            assert session.scalar(text("SHOW transaction_read_only")) == "on"
            assert session.scalar(text("SHOW statement_timeout")) == "200ms"
            assert session.scalar(text("SHOW idle_in_transaction_session_timeout")) == "10s"
        started = time.monotonic()
        with pytest.raises(OperationalError) as failure, sessions() as session:
            session.execute(text("SELECT pg_sleep(30)"))
        assert getattr(failure.value.orig, "sqlstate", None) == "57014"  # query_canceled
        assert time.monotonic() - started < 5
        assert engine.pool.checkedout() == 0
        with sessions() as session:  # The single pooled connection is usable again.
            assert session.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()
