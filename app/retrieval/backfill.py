"""Persistent, bounded operational retrieval indexing: run/status/preflight/resume.

No dotenv loading. A campaign's scope, index specification and total input admission
budget are immutable. SQLite contains private cursor IDs, never source text or keys.
Each sweep covers every in-scope work's current episodes and, since 2026-11-03, the current
revision of each non-archived setting book (V114 tables), under the same campaign budget.
"""

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID

import httpx
import boto3
from botocore.config import Config as S3Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.core.config import Settings
from app.embeddings.client import OpenAIEmbeddingsClient
from app.retrieval.cli import exclusive_outbox
from app.retrieval.contracts import IndexBusy, IndexSpec, Limits, Scope, SourceChanged
from app.retrieval.provider import IndexProviderFailure, OperationalEmbeddingProvider
from app.retrieval.service import RetrievalIndexService
from app.retrieval.store import RetrievalStore, SettingBookRetrievalStore
from app.storage.s3 import S3TextObjectStorage
from app.usage.metering import _estimate_embedding_token_upper_bound

ZERO_UUID = UUID(int=0)
NORMAL_STATES = {
    "DISABLED",
    "READY",
    "EMPTY",
    "PROGRESS",
    "TIME_LIMIT",
    "CHUNK_LIMIT",
    "BUDGET_EXHAUSTED",
    "INDEX_BUSY",
    "PREFLIGHT_READY",
    "RESUMED",
    "NOT_STARTED",
}
# 2026-11-03: setting books join the sweep. The per-work book cursor and book counters are added in place, so a
# state file written by the episode-only code keeps its campaign identity, admitted budget, uncertainty marker and
# in-flight episode cursors. Every new column has a default: the previous image can still run on a migrated file.
STATE_MIGRATIONS = (
    ("work_cursors", "book_after", f"TEXT NOT NULL DEFAULT '{ZERO_UUID}'"),
    ("campaigns", "book_seen", "INTEGER NOT NULL DEFAULT 0"),
    ("campaigns", "book_ready", "INTEGER NOT NULL DEFAULT 0"),
    ("campaigns", "book_excluded", "INTEGER NOT NULL DEFAULT 0"),
)


def _boolean(value):
    if value.lower() not in {"true", "false"}:
        raise ValueError("Expected true or false")
    return value.lower() == "true"


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: str
    max_input_tokens: int
    work_ids: tuple[UUID, ...] = ()
    state_path: str = "/var/lib/catchhole-chat/backfill.sqlite3"
    outbox_path: str = "/var/lib/catchhole-chat/retrieval.sqlite"
    actor: str = ""
    page_size: int = 100
    max_chunks: int = 1000
    max_seconds: int = 300
    batch_size: int = 8
    max_retries: int = 1

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", self.campaign_id):
            raise ValueError("Explicit stable campaign ID is required")
        if not 1 <= self.max_input_tokens <= 10**12:
            raise ValueError("Explicit finite campaign token budget is required")
        if not self.actor.strip() or len(self.actor) > 100:
            raise ValueError("Explicit operational actor is required")
        if not 1 <= self.page_size <= 100 or not 1 <= self.max_seconds <= 300:
            raise ValueError("Invalid campaign page or time limit")
        if (
            not Path(self.state_path).is_absolute()
            or not Path(self.outbox_path).is_absolute()
            or Path(self.state_path).resolve() == Path(self.outbox_path).resolve()
        ):
            raise ValueError("Separate absolute persistent state and outbox paths are required")
        Limits(self.max_chunks, 1000000, self.batch_size, self.max_retries, self.max_seconds)

    @classmethod
    def from_env(cls, env):
        raw_ids = env.get("RETRIEVAL_BACKFILL_WORK_IDS", "").strip()
        scope = env.get("RETRIEVAL_BACKFILL_SCOPE", "").strip()
        if (raw_ids and scope not in {"", "work-ids"}) or (not raw_ids and scope != "active-works"):
            raise ValueError("Explicit active-works scope or work IDs are required")
        work_ids = tuple(sorted({UUID(v.strip()) for v in raw_ids.split(",") if v.strip()}))
        if raw_ids and not work_ids:
            raise ValueError("Empty work IDs")
        return cls(
            campaign_id=env.get("RETRIEVAL_BACKFILL_ID", ""),
            max_input_tokens=int(env.get("RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS", "0")),
            work_ids=work_ids,
            state_path=env.get("RETRIEVAL_BACKFILL_STATE_PATH", cls.state_path),
            outbox_path=env.get("RETRIEVAL_BACKFILL_OUTBOX_PATH", cls.outbox_path),
            actor=env.get("RETRIEVAL_BACKFILL_ACTOR", ""),
            page_size=int(env.get("RETRIEVAL_BACKFILL_PAGE_SIZE", "100")),
            max_chunks=int(env.get("RETRIEVAL_BACKFILL_MAX_CHUNKS", "1000")),
            max_seconds=int(env.get("RETRIEVAL_BACKFILL_MAX_SECONDS", "300")),
            batch_size=int(env.get("RETRIEVAL_BACKFILL_BATCH_SIZE", "8")),
            max_retries=int(env.get("RETRIEVAL_BACKFILL_MAX_RETRIES", "1")),
        )

    def identity(self, spec):
        return json.dumps(
            {
                "schemaVersion": 1,
                "spec": asdict(spec),
                "maxInputTokens": self.max_input_tokens,
                "scope": "work-ids" if self.work_ids else "active-works",
                "workIds": [str(v) for v in self.work_ids],
                "actor": self.actor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@contextmanager
def exclusive_campaign(path):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(destination) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IndexBusy() from None
        yield
    finally:
        os.close(fd)


class CampaignState:
    def __init__(self, config, spec, *, create=True):
        self.config = config
        path = Path(config.state_path)
        if not create and not path.exists():
            raise FileNotFoundError()
        if create:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, identity TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0,
                inflight INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'NOT_STARTED',
                reason TEXT, sweep INTEGER NOT NULL DEFAULT 0, scanning INTEGER NOT NULL DEFAULT 0,
                seen INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0, no_progress INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS work_cursors (
                campaign TEXT NOT NULL, work TEXT NOT NULL, episode_to INTEGER NOT NULL,
                snapshot_at TEXT NOT NULL, after_no INTEGER NOT NULL DEFAULT 0,
                after_id TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (campaign, work));
        """)
        try:
            self._migrate()
            with self.db:
                old = self.db.execute(
                    "SELECT identity FROM campaigns WHERE id=?", (config.campaign_id,)
                ).fetchone()
                if old is None:
                    if not create:
                        raise FileNotFoundError()
                    self.db.execute(
                        "INSERT INTO campaigns (id,identity) VALUES (?,?)",
                        (config.campaign_id, config.identity(spec)),
                    )
                elif old[0] != config.identity(spec):
                    raise ValueError("Campaign scope, spec and budget are immutable")
        except BaseException:
            self.db.close()
            raise

    def _migrate(self):
        with self.db:
            for table, name, definition in STATE_MIGRATIONS:
                present = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if name not in present:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self):
        self.db.close()

    def row(self):
        return self.db.execute(
            "SELECT * FROM campaigns WHERE id=?", (self.config.campaign_id,)
        ).fetchone()

    def status(self, **extra):
        row = self.row()
        snapshot = self.db.execute(
            "SELECT max(snapshot_at) FROM work_cursors WHERE campaign=?",
            (self.config.campaign_id,),
        ).fetchone()[0]
        return {
            "schemaVersion": 1,
            "state": row["state"],
            "reason": row["reason"],
            "admittedInputTokenUpperBound": row["admitted"],
            "maxInputTokens": self.config.max_input_tokens,
            "remainingInputTokenAllowance": self.config.max_input_tokens - row["admitted"],
            "uncertainAttempt": bool(row["inflight"]),
            "sweep": row["sweep"],
            "sweepInProgress": bool(row["scanning"]),
            "snapshotAt": snapshot,
            # Totals cover both kinds; the setting-book share is reported separately.
            "checkedSources": row["seen"],
            "readySources": row["ready"],
            "excludedOrUnavailableSources": row["excluded"],
            "checkedSettingBooks": row["book_seen"],
            "readySettingBooks": row["book_ready"],
            "excludedOrUnavailableSettingBooks": row["book_excluded"],
            # SETTING_BOOK: the current revision of each non-archived setting book only.
            "supportedSourceTypes": ["CURRENT_EPISODE", "SETTING_BOOK"],
            "unsupportedSourceTypes": ["PAST_EPISODE_VERSION"],
            "userTokenDebit": False,
            **extra,
        }

    def outcome(self, state, reason=None, *, progress=False):
        with self.db:
            self.db.execute(
                "UPDATE campaigns SET state=?,reason=?,no_progress=CASE WHEN ? THEN 0 ELSE no_progress END WHERE id=?",
                (state, reason, progress, self.config.campaign_id),
            )

    def no_progress(self, reason):
        with self.db:
            self.db.execute(
                "UPDATE campaigns SET no_progress=no_progress+1 WHERE id=?",
                (self.config.campaign_id,),
            )
        self.outcome("BLOCKED" if self.row()["no_progress"] >= 3 else "PROGRESS", reason)

    def resume(self):
        with self.db:
            self.db.execute(
                "UPDATE campaigns SET state='RESUMED',reason=NULL,inflight=0,no_progress=0 WHERE id=?",
                (self.config.campaign_id,),
            )

    def reserve(self, tokens):
        # Commit before any HTTP request. A killed process permanently spends admission
        # allowance and leaves an uncertainty marker. Restart cannot refund or replay it.
        with self.db:
            row = self.row()
            if row["inflight"]:
                raise IndexProviderFailure("UNKNOWN_USAGE", False)
            if tokens > self.config.max_input_tokens - row["admitted"]:
                raise IndexProviderFailure("BUDGET_EXHAUSTED", False)
            self.db.execute(
                "UPDATE campaigns SET admitted=admitted+?,inflight=1 WHERE id=?",
                (tokens, self.config.campaign_id),
            )

    def resolve_attempt(self):
        with self.db:
            self.db.execute(
                "UPDATE campaigns SET inflight=0 WHERE id=?", (self.config.campaign_id,)
            )

    def start_sweep(self, snapshot):
        with self.db:
            self.db.execute("DELETE FROM work_cursors WHERE campaign=?", (self.config.campaign_id,))
            self.db.executemany(
                "INSERT INTO work_cursors (campaign,work,episode_to,snapshot_at,after_id) VALUES (?,?,?,?,?)",
                [
                    (self.config.campaign_id, str(work), upper, at, str(ZERO_UUID))
                    for work, upper, at in snapshot
                ],
            )
            self.db.execute(
                "UPDATE campaigns SET sweep=sweep+1,scanning=1,seen=0,ready=0,excluded=0,"
                "book_seen=0,book_ready=0,book_excluded=0 WHERE id=?",
                (self.config.campaign_id,),
            )

    def next_work(self):
        return self.db.execute(
            "SELECT * FROM work_cursors WHERE campaign=? AND done=0 ORDER BY work LIMIT 1",
            (self.config.campaign_id,),
        ).fetchone()

    def advance(self, source, *, ready):
        with self.db:
            self.db.execute(
                "UPDATE work_cursors SET after_no=?,after_id=? WHERE campaign=? AND work=?",
                (
                    source.episode_no,
                    str(source.episode_id),
                    self.config.campaign_id,
                    str(source.work_id),
                ),
            )
            self.db.execute(
                "UPDATE campaigns SET seen=seen+1,ready=ready+?,excluded=excluded+?,no_progress=0 WHERE id=?",
                (int(ready), int(not ready), self.config.campaign_id),
            )

    def advance_book(self, source, *, ready):
        # The episode cursor is untouched; books follow the work's episodes keyset by setting-book ID.
        with self.db:
            self.db.execute(
                "UPDATE work_cursors SET book_after=? WHERE campaign=? AND work=?",
                (str(source.setting_book_id), self.config.campaign_id, str(source.work_id)),
            )
            self.db.execute(
                "UPDATE campaigns SET seen=seen+1,ready=ready+?,excluded=excluded+?,book_seen=book_seen+1,"
                "book_ready=book_ready+?,book_excluded=book_excluded+?,no_progress=0 WHERE id=?",
                (int(ready), int(not ready), int(ready), int(not ready), self.config.campaign_id),
            )

    def work_done(self, work_id):
        with self.db:
            self.db.execute(
                "UPDATE work_cursors SET done=1 WHERE campaign=? AND work=?",
                (self.config.campaign_id, str(work_id)),
            )

    def finish_sweep(self):
        row = self.row()
        self.outcome(
            "PARTIAL" if row["excluded"] else "READY" if row["seen"] else "EMPTY",
            progress=True,
        )
        with self.db:
            self.db.execute(
                "UPDATE campaigns SET scanning=0 WHERE id=?", (self.config.campaign_id,)
            )


class CampaignProvider:
    def __init__(self, delegate, state):
        self.delegate, self.state = delegate, state
        self.attempt_usage_unknown = False

    @property
    def attempts(self):
        return self.delegate.attempts

    @property
    def input_tokens(self):
        return self.delegate.input_tokens

    @property
    def unknown_usage(self):
        return self.delegate.unknown_usage

    async def embed(self, source, chunks, before_call):
        outbox = getattr(self.delegate, "outbox", None)
        if outbox is not None and outbox.count():
            raise IndexProviderFailure("REPORTS_PENDING", False)
        self.state.reserve(_estimate_embedding_token_upper_bound([c.chunk_text for c in chunks]))
        attempts_before = self.attempts
        unknown_before = self.unknown_usage
        try:
            result = await self.delegate.embed(source, chunks, before_call)
            self.attempt_usage_unknown = self.unknown_usage != unknown_before
            return result
        except ChatTransportError as exc:
            # operation/start/reserve can fail before invoke() reaches the provider.
            # A lost Spring response cannot have invoked it; keep the spent admission
            # allowance, but do not turn a known zero-call failure into unknown billing.
            if self.attempts != attempts_before or self.unknown_usage != unknown_before:
                raise
            self.state.resolve_attempt()
            retryable = exc.status is None or exc.status in {408, 429} or 500 <= exc.status < 600
            # Retry on a later timer, with the campaign's consecutive-failure bound.
            # Do not multiply reservations through service-level provider retries.
            raise IndexProviderFailure(
                "LEDGER_RETRYABLE" if retryable else "LEDGER_REJECTED", False
            ) from None
        except (IndexProviderFailure, SourceChanged):
            if self.unknown_usage == unknown_before:
                self.state.resolve_attempt()
            raise

    def after_save(self):
        # Keep the durable uncertainty marker across the provider-response/DB-save
        # window. A crash or DB error here requires explicit operator recovery.
        if not self.attempt_usage_unknown:
            self.state.resolve_attempt()

    def discard_result(self):
        # Source validation positively rejected the obsolete result; no old vector
        # may be saved. Its admitted allowance remains permanently spent.
        if not self.attempt_usage_unknown:
            self.state.resolve_attempt()


class CampaignRunner:
    """One bounded sweep step. Per work: current episodes first, then current setting-book revisions.

    Both kinds share the per-work advisory lock, the campaign budget/uncertainty marker, the provider,
    batching/retries/limits/deadline and every result branch below.
    """

    def __init__(self, config, state, service, provider, *, book_service):
        self.config, self.state, self.service = config, state, service
        self.book_service = book_service
        self.store = service.store
        self.provider = CampaignProvider(provider, state)

    def _status(self, **extra):
        return self.state.status(
            embeddedChunks=self.embedded, providerAttempts=self.provider.attempts, **extra
        )

    async def run(self, deadline):
        state, cfg = self.state, self.config
        if state.row()["inflight"]:
            state.outcome("BLOCKED", "UNKNOWN_USAGE")
        if state.row()["state"] in {"BLOCKED", "BUDGET_EXHAUSTED"}:
            return state.status(providerAttempts=0)
        if not state.row()["scanning"]:
            snapshot = await asyncio.to_thread(self.store.backfill_snapshot, cfg.work_ids)
            state.start_sweep(snapshot)
        self.embedded, self.checked, self.selected = 0, 0, 0
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            cursor = state.next_work()
            if cursor is None:
                state.finish_sweep()
                return self._status()
            work_id = UUID(cursor["work"])
            with self.store.exclusive(work_id):
                service, advance = self.service, state.advance
                sources = await asyncio.to_thread(
                    self.store.backfill_sources,
                    work_id,
                    cursor["episode_to"],
                    cursor["snapshot_at"],
                    cursor["after_no"],
                    UUID(cursor["after_id"]),
                    cfg.page_size,
                )
                if not sources:
                    # Episodes done: the work's current setting-book revisions, same lock and rules.
                    service, advance = self.book_service, state.advance_book
                    sources = await asyncio.to_thread(
                        service.store.backfill_sources,
                        work_id,
                        UUID(cursor["book_after"]),
                        cfg.page_size,
                    )
                if not sources:
                    state.work_done(work_id)
                    continue
                for source in sources:
                    if loop.time() >= deadline or self.selected >= cfg.max_chunks:
                        break
                    stopped = await self._index(service, advance, work_id, source, deadline)
                    if stopped is not None:
                        return stopped
            if self.selected >= cfg.max_chunks:
                state.outcome("CHUNK_LIMIT")
                return self._status()
        state.outcome("TIME_LIMIT")
        if not self.embedded and not self.checked:
            state.no_progress("TIME_LIMIT")
        return self._status()

    async def _index(self, service, advance, work_id, source, deadline):
        """Index one source. None once the cursor advanced past it, otherwise this run's final status."""
        state, cfg = self.state, self.config
        if source.state != "AVAILABLE":
            advance(source, ready=False)
            self.checked += 1
            return None
        current = await asyncio.to_thread(service.store.inspect, source, service.spec)
        if current["state"] == "READY":
            advance(source, ready=True)
            self.checked += 1
            return None
        report = await service.run(
            # The scope's single ID is the store's source ID (episode or setting-book revision).
            Scope(work_id, (service.store.source_id(source),), max_sources=1),
            self.provider,
            enabled=True,
            source_snapshot=source,
            deadline=deadline,
            limits=Limits(
                cfg.max_chunks - self.selected,
                1000000,
                cfg.batch_size,
                cfg.max_retries,
                cfg.max_seconds,
            ),
        )
        produced = report["embeddedChunks"]
        self.embedded += produced
        self.selected += produced + report["failedChunks"] + report["changedChunks"]
        if produced:
            state.outcome("PROGRESS", progress=True)
        if state.row()["inflight"]:
            state.outcome("BLOCKED", "UNKNOWN_USAGE")
            return self._status()
        if report["state"] == "BUDGET_EXHAUSTED":
            state.outcome("BUDGET_EXHAUSTED")
            return self._status()
        if report["state"] == "REPORTS_PENDING":
            state.outcome("PROGRESS", "REPORTS_PENDING")
            return self._status(state="REPORTS_PENDING")
        if report["state"] == "LEDGER_RETRYABLE":
            state.no_progress("LEDGER_FAILURE")
            return self._status()
        if report["state"] in {"CHUNK_LIMIT", "TIME_LIMIT"}:
            state.outcome(report["state"])
            if not produced:
                state.no_progress(report["state"])
            return self._status()
        if report["sourceFailures"] or report["changedChunks"]:
            codes = {r["code"] for r in report["sourceFailures"]}
            if codes <= {"SOURCE_CHANGED"}:
                state.no_progress("SOURCE_CHANGED")
            elif (advance == state.advance_book and codes == {"SOURCE_UNREADABLE"}
                  and not report["changedChunks"]):
                # A setting book over the 2,000,000-byte cap (uploads allow 10 MiB) or otherwise unreadable is
                # excluded, so one book does not stop every work's episodes and books (owner decision 2026-11-03).
                # The sweep ends PARTIAL; it is never marked READY.
                advance(source, ready=False)
                self.checked += 1
                return None
            else:
                state.outcome("BLOCKED", "SOURCE_UNREADABLE")
            return self._status()
        if report["failedChunks"] or report["state"] not in {"COMPLETED", "PARTIAL"}:
            state.outcome(
                "BLOCKED",
                report["state"] if report["state"] != "PARTIAL" else "PROVIDER_RETRIES_EXHAUSTED",
            )
            return self._status()
        current = await asyncio.to_thread(service.store.inspect, source, service.spec)
        if current["state"] != "READY":
            state.no_progress("SOURCE_CHANGED")
            return self._status()
        advance(source, ready=True)
        self.checked += 1
        return None


async def operations_ready(client):
    """Authenticated, side-effect-free Java capability check, with no raw errors."""
    try:
        response = await client.http.get(
            client.base_url + "/api/internal/v1/chat-runs/operations/readiness",
            headers={"X-Internal-Api-Key": client.api_key},
            timeout=10,
            follow_redirects=False,
        )
        envelope = response.json()
        if (
            response.status_code != 200
            or envelope.get("success") is not True
            or envelope.get("data", {}).get("operationsEnabled") is not True
            or envelope.get("data", {}).get("capabilityConfigured") is not True
        ):
            raise ValueError()
    except (httpx.RequestError, ValueError, TypeError, AttributeError):
        raise ChatTransportError(code="OPERATIONS_NOT_READY") from None


def _engine(settings):
    if not settings.database_url.startswith("postgresql"):
        raise ValueError("PostgreSQL DATABASE_URL is required")
    return create_engine(
        settings.database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": f"-c timezone={settings.tz} -c statement_timeout=5000 -c lock_timeout=5000",
        },
    )


def _storage(settings, *, connect):
    if not settings.aws_s3_bucket.strip() or not settings.aws_region.strip():
        raise ValueError("Explicit S3 bucket and region are required")
    storage = S3TextObjectStorage.from_settings(settings)
    if not connect:
        return storage
    options = {"region_name": storage.region}
    if storage.access_key_id and storage.secret_access_key:
        options.update(
            aws_access_key_id=storage.access_key_id, aws_secret_access_key=storage.secret_access_key
        )
        if storage.session_token:
            options["aws_session_token"] = storage.session_token
    return replace(
        storage,
        client=boto3.client(
            "s3",
            **options,
            config=S3Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}),
        ),
    )


async def preflight(config, settings, spec):
    # Read-only SQLite validation intentionally does not take the runner's flock:
    # deployment may preflight a new image while the previous timer run finishes.
    path = Path(config.state_path)
    if path.exists():
        if path.is_symlink():
            raise ValueError("Campaign state must not be a symlink")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute(
                "SELECT identity FROM campaigns WHERE id=?", (config.campaign_id,)
            ).fetchone()
            if row is not None and row[0] != config.identity(spec):
                raise ValueError("Campaign scope, spec and budget are immutable")
        finally:
            db.close()
    if not settings.embedding_generation_enabled:
        return {"state": "DISABLED", "providerAttempts": 0, "userTokenDebit": False}
    if not settings.spring_internal_api_key or not settings.llm_api_key:
        raise ValueError("Internal API and provider credentials are required")
    _storage(settings, connect=False)
    async with httpx.AsyncClient() as http:
        client = ChatExecutionClient(
            settings.spring_internal_api_base_url, settings.spring_internal_api_key, http
        )
        await operations_ready(client)
    engine = _engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM retrieval_sources LIMIT 0"))
            connection.execute(text("SELECT 1 FROM retrieval_chunks LIMIT 0"))
            connection.execute(text("SELECT 1 FROM retrieval_setting_book_sources LIMIT 0"))
            connection.execute(text("SELECT 1 FROM retrieval_setting_book_chunks LIMIT 0"))
            _check_database_permissions(connection)
        return {"state": "PREFLIGHT_READY", "providerAttempts": 0, "userTokenDebit": False}
    finally:
        engine.dispose()


def _check_database_permissions(connection):
    # Inspect ACLs without modifying data, taking row locks or invoking a provider.
    # Each privilege is checked separately: PostgreSQL's comma-separated privilege
    # argument means ANY privilege, whereas this writer requires ALL of them.
    permitted = connection.scalar(
        text("""
        SELECT current_setting('transaction_read_only') = 'off'
           AND has_any_column_privilege(current_user, 'works', 'UPDATE')
           AND bool_and(has_table_privilege(current_user, relation, privilege))
        FROM (VALUES
            ('works', 'SELECT'),
            ('members', 'SELECT'),
            ('episodes', 'SELECT'),
            ('episode_source_purge_requests', 'SELECT'),
            ('retrieval_sources', 'SELECT'),
            ('retrieval_sources', 'INSERT'),
            ('retrieval_sources', 'UPDATE'),
            ('retrieval_chunks', 'SELECT'),
            ('retrieval_chunks', 'INSERT'),
            ('retrieval_chunks', 'UPDATE'),
            ('retrieval_chunks', 'DELETE'),
            ('upload_files', 'SELECT'),
            ('upload_batches', 'SELECT'),
            ('setting_book_revisions', 'SELECT'),
            ('retrieval_setting_book_sources', 'SELECT'),
            ('retrieval_setting_book_sources', 'INSERT'),
            ('retrieval_setting_book_sources', 'UPDATE'),
            ('retrieval_setting_book_chunks', 'SELECT'),
            ('retrieval_setting_book_chunks', 'INSERT'),
            ('retrieval_setting_book_chunks', 'UPDATE'),
            ('retrieval_setting_book_chunks', 'DELETE')
        ) AS required(relation, privilege)
        """)
    )
    if permitted is not True:
        raise ValueError("Backfill database writer permissions are required")


def _pending_reports(path):
    destination = Path(path)
    if not destination.exists():
        return 0
    if destination.is_symlink():
        raise ValueError("Outbox must not be a symlink")
    db = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        # Expired rows still count until normal recovery removes them. Status does
        # not send reports or mutate the outbox to satisfy an activation check.
        return db.execute("SELECT count(*) FROM reports").fetchone()[0]
    finally:
        db.close()


async def main(mode, env=None):
    env = os.environ if env is None else env
    enabled = _boolean(env.get("RETRIEVAL_BACKFILL_ENABLED", "false"))
    if not enabled and mode == "preflight":
        return {"state": "DISABLED", "providerAttempts": 0, "userTokenDebit": False}
    if not enabled and mode in {"run", "preflight"}:
        # Existing usage reports still need delivery when the campaign is switched off.
        outbox_path = env.get("RETRIEVAL_BACKFILL_OUTBOX_PATH", CampaignConfig.outbox_path)
        if not Path(outbox_path).exists():
            return {"state": "DISABLED", "providerAttempts": 0, "userTokenDebit": False}
        settings = Settings(_env_file=None)
        if not settings.spring_internal_api_key:
            raise ValueError("Internal API credentials required for report recovery")
        with exclusive_outbox(outbox_path) as outbox:
            async with httpx.AsyncClient() as http:
                client = ChatExecutionClient(
                    settings.spring_internal_api_base_url, settings.spring_internal_api_key, http
                )
                async with asyncio.timeout(30):
                    await outbox.flush(client)
                return {
                    "state": "REPORTS_PENDING" if outbox.count() else "DISABLED",
                    "pendingReports": outbox.count(),
                    "providerAttempts": 0,
                    "userTokenDebit": False,
                }
    config = CampaignConfig.from_env(env)
    settings = Settings(_env_file=None)
    spec = IndexSpec(
        settings.embedding_model, settings.embedding_version, settings.embedding_dimensions
    )
    if mode == "preflight":
        return await preflight(config, settings, spec)
    with exclusive_campaign(config.state_path):
        if mode == "status" and not Path(config.state_path).exists():
            return {"state": "NOT_STARTED", "providerAttempts": 0, "userTokenDebit": False}
        state = CampaignState(config, spec, create=mode == "run")
        try:
            if mode == "status":
                pending = _pending_reports(config.outbox_path)
                return state.status(
                    state="REPORTS_PENDING" if pending else state.row()["state"],
                    pendingReports=pending,
                )
            if mode == "resume":
                state.resume()
                return state.status()
            if not settings.spring_internal_api_key:
                raise ValueError("Internal API credentials are required")
            deadline = asyncio.get_running_loop().time() + config.max_seconds
            with exclusive_outbox(config.outbox_path) as outbox:
                async with httpx.AsyncClient() as http:
                    client = ChatExecutionClient(
                        settings.spring_internal_api_base_url,
                        settings.spring_internal_api_key,
                        http,
                    )
                    async with asyncio.timeout_at(deadline):
                        await outbox.flush(client)
                        if outbox.count():
                            return state.status(
                                state="REPORTS_PENDING",
                                pendingReports=outbox.count(),
                                providerAttempts=0,
                            )
                        if not settings.embedding_generation_enabled:
                            return state.status(state="DISABLED", providerAttempts=0)
                        if not settings.llm_api_key:
                            raise ValueError("Embedding provider credentials are required")
                        await operations_ready(client)
                    engine = _engine(settings)
                    try:
                        # One pool (2 connections) for both kinds: the work lock plus short transactions.
                        sessions = sessionmaker(engine, expire_on_commit=False)
                        store = RetrievalStore(sessions)
                        if state.row()["inflight"] or state.row()["state"] in {
                            "BLOCKED",
                            "BUDGET_EXHAUSTED",
                        }:
                            if state.row()["inflight"]:
                                state.outcome("BLOCKED", "UNKNOWN_USAGE")
                            return state.status(providerAttempts=0)
                        delegate = OpenAIEmbeddingsClient.from_settings(settings)
                        try:
                            storage = _storage(settings, connect=True)
                            service = RetrievalIndexService(store, storage, spec)
                            book_service = RetrievalIndexService(
                                SettingBookRetrievalStore(sessions), storage, spec
                            )
                            provider = OperationalEmbeddingProvider(
                                client, delegate, outbox, spec, config.actor
                            )
                            result = await CampaignRunner(
                                config, state, service, provider, book_service=book_service
                            ).run(deadline)
                            result["pendingReports"] = outbox.count()
                            if outbox.count():
                                result["state"] = "REPORTS_PENDING"
                            return result
                        finally:
                            await delegate.aclose()
                    finally:
                        engine.dispose()
        finally:
            state.close()


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "status", "preflight", "resume"))
    args = parser.parse_args()
    try:
        result = asyncio.run(main(args.mode))
    except IndexBusy:
        result = {"state": "INDEX_BUSY"}
    except ChatTransportError:
        result = {"state": "LEDGER_OR_READINESS_FAILURE"}
    except TimeoutError:
        result = {"state": "TIME_LIMIT"}
    except KeyboardInterrupt:
        result = {"state": "INTERRUPTED"}
    except Exception:
        # URLs, source IDs/text, credentials and raw exceptions never reach stdout.
        result = {"state": "CONFIGURATION_OR_EXECUTION_FAILURE"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] in NORMAL_STATES else 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
