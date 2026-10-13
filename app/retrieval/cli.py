"""python -m app.retrieval.cli --help. No .env loading and no paid calls in dry-run."""

import argparse
import asyncio
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.chatbot.outbox import Outbox
from app.core.config import Settings
from app.embeddings.client import OpenAIEmbeddingsClient
from app.retrieval.contracts import IndexBusy, IndexSpec, Limits, Scope
from app.retrieval.provider import OperationalEmbeddingProvider
from app.retrieval.service import RetrievalIndexService
from app.retrieval.store import RetrievalStore
from app.storage.s3 import S3TextObjectStorage


@contextmanager
def exclusive_outbox(path):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(destination) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IndexBusy() from None
        box = Outbox(str(destination))
        try:
            yield box
        finally:
            box.close()
    finally:
        os.close(fd)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("status", "dry-run", "prepare", "execute", "recover-reports"))
    p.add_argument("--work-id", type=UUID)
    p.add_argument("--episode-id", type=UUID, action="append", default=[])
    p.add_argument("--episode-from", type=int, default=1)
    p.add_argument("--episode-to", type=int, default=2147483647)
    p.add_argument("--max-sources", type=int, default=20)
    p.add_argument("--max-chunks", type=int, default=100)
    p.add_argument("--max-input-tokens", type=int, default=100000)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--max-retries", type=int, default=1)
    p.add_argument("--max-seconds", type=int, default=900)
    p.add_argument("--usd-per-million", type=Decimal)
    p.add_argument("--max-cost-usd", type=Decimal)
    p.add_argument("--actor")
    p.add_argument(
        "--outbox", help="Persistent private SQLite path, required for execute/recover-reports"
    )
    return p


async def main(args):
    cfg = Settings(_env_file=None)
    if args.mode in {"execute", "recover-reports"}:
        if not args.outbox or not cfg.spring_internal_api_key:
            raise ValueError("Persistent outbox and internal API credentials are required")
        with exclusive_outbox(args.outbox) as outbox:
            async with httpx.AsyncClient() as http:
                client = ChatExecutionClient(
                    cfg.spring_internal_api_base_url, cfg.spring_internal_api_key, http
                )
                # Delivery is always first, including generation OFF and report-only recovery.
                await outbox.flush(client)
                if args.mode == "recover-reports" or outbox.count():
                    return {
                        "state": "REPORTS_PENDING" if outbox.count() else "REPORTS_DELIVERED",
                        "pendingReports": outbox.count(),
                    }
                if not cfg.embedding_generation_enabled:
                    return {"state": "DISABLED", "providerAttempts": 0, "userTokenDebit": False}
                if not args.actor or not cfg.llm_api_key:
                    raise ValueError("Explicit actor and provider credentials are required")
                delegate = OpenAIEmbeddingsClient.from_settings(cfg)
                try:
                    return await _scoped(args, cfg, (client, delegate, outbox))
                finally:
                    await delegate.aclose()
    return await _scoped(args, cfg)


async def _scoped(args, cfg, dependencies=None):
    if args.work_id is None or not cfg.database_url.startswith("postgresql"):
        raise ValueError("Explicit work scope and PostgreSQL DATABASE_URL are required")
    scope = Scope(
        args.work_id, tuple(args.episode_id), args.episode_from, args.episode_to, args.max_sources
    )
    spec = IndexSpec(cfg.embedding_model, cfg.embedding_version, cfg.embedding_dimensions)
    limits = Limits(
        args.max_chunks,
        args.max_input_tokens,
        args.batch_size,
        args.max_retries,
        args.max_seconds,
        args.usd_per_million,
        args.max_cost_usd,
    )
    engine = create_engine(
        cfg.database_url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"options": f"-c timezone={cfg.tz}"},
    )
    store = RetrievalStore(sessionmaker(engine, expire_on_commit=False))
    service = RetrievalIndexService(store, S3TextObjectStorage.from_settings(cfg), spec)
    try:
        if args.mode == "status":
            return await asyncio.to_thread(service.readiness, scope)
        if args.mode == "dry-run":
            return await asyncio.to_thread(service.dry_run, scope, limits)
        with store.exclusive(scope.work_id):
            if args.mode == "prepare":
                return await asyncio.to_thread(service.prepare, scope)
            client, delegate, outbox = dependencies
            provider = OperationalEmbeddingProvider(client, delegate, outbox, spec, args.actor)
            report = await service.run(scope, provider, enabled=True, limits=limits)
            report["pendingReports"] = outbox.count()
            return report
    finally:
        engine.dispose()


def entrypoint():
    args = parser().parse_args()
    try:
        result = asyncio.run(main(args))
    except IndexBusy:
        result = {"state": "INDEX_BUSY"}
    except ChatTransportError:
        result = {"state": "LEDGER_FAILURE"}
    except KeyboardInterrupt:
        result = {
            "state": "INTERRUPTED",
            "resume": "Run recover-reports, then execute the same scope",
        }
    except Exception:
        # Never print connection URLs, source keys/text or raw provider exceptions.
        result = {"state": "CONFIGURATION_OR_EXECUTION_FAILURE"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {None, "COMPLETED", "REPORTS_DELIVERED", "DISABLED"} else 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
