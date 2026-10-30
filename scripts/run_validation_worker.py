"""Explicit opt-in validation runner. Reads configuration without loading a local .env."""
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
from app.worker.episode_validation_worker import EpisodeValidationWorker


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
        while True:
            claim = await worker.claim_next()  # Validates every required explicit server role/branch budget.
            if claim is not None:
                await worker.process_claimed(claim)
            if args.once:
                return
            await asyncio.sleep(args.poll_seconds)
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
