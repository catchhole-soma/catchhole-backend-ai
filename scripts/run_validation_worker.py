"""Explicit opt-in validation runner. Reads configuration without loading a local .env.

Continuous mode isolates every claim (GH340): an IncompleteInspection from one job (EXECUTION_STOPPED,
INPUT_CHANGED, SOURCE_CHANGED, SOURCE_PURGED, ...) or any other exception from claim/process is logged
with fixed codes, the exception type and job/report ids only, and the loop claims again. Consecutive
failures back off exponentially (poll seconds x 2^n, capped at 60 s) and reset after a clean iteration.
Cancellation, KeyboardInterrupt and a closing worker end the loop. --once handles at most one claim and
re-raises its failure after logging, so the process exits non-zero.
"""
import argparse
import asyncio
import logging
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.embeddings.client import OpenAIEmbeddingsClient
from app.llm.openai_client import OpenAIResponsesClient
from app.retrieval.contracts import IndexSpec
from app.validation.client import ValidationExecutionClient
from app.validation.contracts import IncompleteInspection, safe_code
from app.worker.episode_validation_worker import EpisodeValidationWorker

logger = logging.getLogger("app.validation.runner")
MAX_BACKOFF_SECONDS = 60.0


def _closing(worker):
    return getattr(worker, "_close_task", None) is not None


def _backoff_seconds(base, failures):
    # The exponent is bounded so a long outage never overflows the float conversion.
    return min(base * 2 ** min(failures, 30), max(MAX_BACKOFF_SECONDS, base))


def _origin(exc):
    """Innermost `module.function:line` (app code first); never the message or a file path."""
    innermost = app = None
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        innermost = f"{frame.f_globals.get('__name__', '?')}.{frame.f_code.co_name}:{tb.tb_lineno}"
        if innermost.startswith("app."):
            app = innermost
        tb = tb.tb_next
    return app or innermost or "-"


def _log_failure(log, exc, claim):
    job = getattr(claim, "job_id", None) or "-"
    report = getattr(getattr(claim, "scope", None), "reportId", None) or "-"
    if isinstance(exc, IncompleteInspection):
        log.warning("validation job incomplete code=%s cause=%s job=%s report=%s",
                    safe_code(exc.code, "-"), exc.cause or "-", job, report)
    else:
        # Exception text may carry response bodies, provider text or URLs; only the type is logged.
        log.error("validation job failed error=%s origin=%s job=%s report=%s",
                  type(exc).__name__, _origin(exc), job, report)


async def run_loop(worker, *, once, poll_seconds, sleeper=asyncio.sleep, should_stop=None, log=logger):
    """Claim/process until stopped; one job's failure never ends continuous execution."""
    base = poll_seconds if poll_seconds is not None and poll_seconds > 0 else 1.0
    failures, logged_backoff = 0, 0.0
    while not _closing(worker) and not (should_stop is not None and should_stop()):
        claim = None
        try:
            claim = await worker.claim_next()  # Validates every required explicit server role/branch budget.
            if claim is not None:
                await worker.process_claimed(claim)
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Deploy shutdown: the job is not failed here; Spring lease expiry/reclaim recovers it.
            raise
        except Exception as exc:  # One claim's failure must keep the poller alive.
            if _closing(worker):
                log.info("validation runner stopping: worker is closing")
                return
            _log_failure(log, exc, claim)
            if once:
                raise
            failures += 1
        else:
            failures = 0
        if once:
            return
        if failures:
            delay = _backoff_seconds(base, failures)
            if delay > logged_backoff:
                log.warning("validation runner backoff failures=%d sleep_seconds=%g", failures, delay)
                logged_backoff = delay
        else:
            delay, logged_backoff = base, 0.0
        await sleeper(delay)


async def run(args):
    if not args.enable:
        raise ValueError("Validation runtime is OFF; pass --enable with explicit server budgets.")
    if not args.once and (args.poll_seconds is None or args.poll_seconds <= 0):
        raise ValueError("An explicit positive --poll-seconds is required for continuous execution.")
    internal_key, model_key = os.environ[args.internal_key_env], os.environ[args.model_key_env]
    spec = IndexSpec(version=args.index_version)
    engine, embedding = None, None
    sessions = None
    if args.readonly_url_env:
        # The DB account must also have SELECT-only grants. No validation writes use this connection.
        engine = create_engine(os.environ[args.readonly_url_env],
                               connect_args={"options": "-c default_transaction_read_only=on"})
        sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    if args.query_embedding:
        if sessions is None:
            raise ValueError("Query embedding requires explicitly configured read-only retrieval sessions.")
        embedding = OpenAIEmbeddingsClient(model_key, spec.model, spec.dimensions, spec.version,
                                           args.embedding_url)
    raw = OpenAIResponsesClient(model_key, "gpt-6.1-terra", args.responses_url)
    client = ValidationExecutionClient(args.backend_url, internal_key)
    worker = EpisodeValidationWorker(client, raw, enabled=True, worker_id=args.worker_id,
        readonly_sessions=sessions, embedding_client=embedding, index_spec=spec)
    try:
        await run_loop(worker, once=args.once, poll_seconds=args.poll_seconds)
    finally:
        await worker.aclose()
        await raw.http_client.aclose()
        if embedding:
            await embedding.http_client.aclose()
        if engine:
            engine.dispose()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--enable", action="store_true")
    result.add_argument("--once", action="store_true")
    result.add_argument("--poll-seconds", type=float)
    result.add_argument("--backend-url", required=True)
    result.add_argument("--worker-id", required=True)
    result.add_argument("--internal-key-env", default="INTERNAL_API_KEY")
    result.add_argument("--model-key-env", default="LLM_API_KEY")
    result.add_argument("--responses-url", default="https://api.openai.com/v1/responses")
    result.add_argument("--readonly-url-env")
    result.add_argument("--query-embedding", action="store_true")
    result.add_argument("--embedding-url", default="https://api.openai.com/v1/embeddings")
    result.add_argument("--index-version", default="v1")
    return result


if __name__ == "__main__":
    # Fixed-code branch diagnostics only (GH331); HTTP client loggers stay at WARNING.
    logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] %(levelname)s %(name)s %(message)s")
    logging.getLogger("app.validation").setLevel(logging.INFO)
    asyncio.run(run(parser().parse_args()))
