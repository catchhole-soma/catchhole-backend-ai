"""Persistent, bounded operational retrieval indexing: run/status/preflight/resume.

No dotenv loading. A campaign's scope, index specification and total input admission
budget are immutable. SQLite contains private cursor IDs, never source text or keys.
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
from app.retrieval.store import RetrievalStore
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
            "checkedSources": row["seen"],
            "readySources": row["ready"],
            "excludedOrUnavailableSources": row["excluded"],
            "supportedSourceTypes": ["CURRENT_EPISODE"],
            "unsupportedSourceTypes": ["SETTING_BOOK", "PAST_EPISODE_VERSION"],
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
                "UPDATE campaigns SET sweep=sweep+1,scanning=1,seen=0,ready=0,excluded=0 WHERE id=?",
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

    def work_done(self, work_id):
        with self.db:
            self.db.execute(
                "UPDATE work_cursors SET done=1 WHERE campaign=? AND work=?",
                (self.config.campaign_id, str(work_id)),
            )

    def finish_sweep(self):
        row = self.row()
        self.outcome("PARTIAL" if row["excluded"] else "READY" if row["seen"] else "EMPTY")
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
        unknown_before = self.unknown_usage
        try:
            result = await self.delegate.embed(source, chunks, before_call)
            self.attempt_usage_unknown = self.unknown_usage != unknown_before
            return result
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
    def __init__(self, config, state, service, provider):
        self.config, self.state, self.service = config, state, service
        self.store = service.store
        self.provider = CampaignProvider(provider, state)

    async def run(self, deadline):
        state, cfg = self.state, self.config
        if state.row()["inflight"]:
            state.outcome("BLOCKED", "UNKNOWN_USAGE")
        if state.row()["state"] in {"BLOCKED", "BUDGET_EXHAUSTED"}:
            return state.status(providerAttempts=0)
        if not state.row()["scanning"]:
            snapshot = await asyncio.to_thread(self.store.backfill_snapshot, cfg.work_ids)
            state.start_sweep(snapshot)
        embedded, checked, selected = 0, 0, 0
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            cursor = state.next_work()
            if cursor is None:
                state.finish_sweep()
                return state.status(
                    embeddedChunks=embedded, providerAttempts=self.provider.attempts
                )
            work_id = UUID(cursor["work"])
            with self.store.exclusive(work_id):
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
                    state.work_done(work_id)
                    continue
                for source in sources:
                    if loop.time() >= deadline or selected >= cfg.max_chunks:
                        break
                    if source.state != "AVAILABLE":
                        state.advance(source, ready=False)
                        checked += 1
                        continue
                    current = await asyncio.to_thread(self.store.inspect, source, self.service.spec)
                    if current["state"] == "READY":
                        state.advance(source, ready=True)
                        checked += 1
                        continue
                    report = await self.service.run(
                        Scope(work_id, (source.episode_id,), max_sources=1),
                        self.provider,
                        enabled=True,
                        source_snapshot=source,
                        deadline=deadline,
                        limits=Limits(
                            cfg.max_chunks - selected,
                            1000000,
                            cfg.batch_size,
                            cfg.max_retries,
                            cfg.max_seconds,
                        ),
                    )
                    produced = report["embeddedChunks"]
                    embedded += produced
                    selected += produced + report["failedChunks"] + report["changedChunks"]
                    if produced:
                        state.outcome("PROGRESS", progress=True)
                    if state.row()["inflight"]:
                        state.outcome("BLOCKED", "UNKNOWN_USAGE")
                        return state.status(
                            embeddedChunks=embedded, providerAttempts=self.provider.attempts
                        )
                    if report["state"] == "BUDGET_EXHAUSTED":
                        state.outcome("BUDGET_EXHAUSTED")
                        return state.status(
                            embeddedChunks=embedded, providerAttempts=self.provider.attempts
                        )
                    if report["state"] == "REPORTS_PENDING":
                        state.outcome("PROGRESS", "REPORTS_PENDING")
                        return state.status(
                            state="REPORTS_PENDING",
                            embeddedChunks=embedded,
                            providerAttempts=self.provider.attempts,
                        )
                    if report["state"] in {"CHUNK_LIMIT", "TIME_LIMIT"}:
                        state.outcome(report["state"])
                        if not produced:
                            state.no_progress(report["state"])
                        return state.status(
                            embeddedChunks=embedded, providerAttempts=self.provider.attempts
                        )
                    if report["sourceFailures"] or report["changedChunks"]:
                        codes = {r["code"] for r in report["sourceFailures"]}
                        if codes <= {"SOURCE_CHANGED"}:
                            state.no_progress("SOURCE_CHANGED")
                        else:
                            state.outcome("BLOCKED", "SOURCE_UNREADABLE")
                        return state.status(
                            embeddedChunks=embedded, providerAttempts=self.provider.attempts
                        )
                    if report["failedChunks"] or report["state"] not in {"COMPLETED", "PARTIAL"}:
                        state.outcome(
                            "BLOCKED",
                            report["state"]
                            if report["state"] != "PARTIAL"
                            else "PROVIDER_RETRIES_EXHAUSTED",
                        )
                        return state.status(
                            embeddedChunks=embedded, providerAttempts=self.provider.attempts
                        )
                    current = await asyncio.to_thread(self.store.inspect, source, self.service.spec)
                    if current["state"] != "READY":
                        state.no_progress("SOURCE_CHANGED")
                        return state.status(
                            embeddedChunks=embedded, providerAttempts=self.provider.attempts
                        )
                    state.advance(source, ready=True)
                    checked += 1
            if selected >= cfg.max_chunks:
                state.outcome("CHUNK_LIMIT")
                return state.status(
                    embeddedChunks=embedded, providerAttempts=self.provider.attempts
                )
        state.outcome("TIME_LIMIT")
        if not embedded and not checked:
            state.no_progress("TIME_LIMIT")
        return state.status(embeddedChunks=embedded, providerAttempts=self.provider.attempts)


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
        return {"state": "PREFLIGHT_READY", "providerAttempts": 0, "userTokenDebit": False}
    finally:
        engine.dispose()


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
                return state.status()
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
                        store = RetrievalStore(sessionmaker(engine, expire_on_commit=False))
                        if state.row()["inflight"] or state.row()["state"] in {
                            "BLOCKED",
                            "BUDGET_EXHAUSTED",
                        }:
                            if state.row()["inflight"]:
                                state.outcome("BLOCKED", "UNKNOWN_USAGE")
                            return state.status(providerAttempts=0)
                        delegate = OpenAIEmbeddingsClient.from_settings(settings)
                        try:
                            service = RetrievalIndexService(
                                store, _storage(settings, connect=True), spec
                            )
                            provider = OperationalEmbeddingProvider(
                                client, delegate, outbox, spec, config.actor
                            )
                            result = await CampaignRunner(config, state, service, provider).run(
                                deadline
                            )
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
