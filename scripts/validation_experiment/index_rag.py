"""Option A: re-embed the prior episodes locally with the production retrieval index code (paid, tiny).

`RetrievalIndexService.prepare/run` chunk and embed exactly as the production backfill does; only the storage is
the local manuscripts folder and the provider calls the embeddings API directly (no operational ledger).
"""

import math
from uuid import UUID

from sqlalchemy.orm import sessionmaker

from app.retrieval.contracts import IndexSpec, Limits, Scope
from app.retrieval.provider import IndexProviderFailure
from app.retrieval.service import RetrievalIndexService
from app.retrieval.store import RetrievalStore
from scripts.validation_experiment.common import (
    HarnessError,
    file_sha256,
    manuscript_path,
    read_manuscript,
)


class LocalTextStorage:
    """`get_text(key)` for the fixture's storage keys, served from the local manuscripts folder."""

    def __init__(self, fixture, manuscripts):
        self.paths = {row["contentS3Key"]: manuscript_path(manuscripts, row["episodeNo"]) for row in fixture["episodes"]}

    def get_text(self, key, version_id=None, *, max_bytes=None):
        if key not in self.paths:
            raise HarnessError("Unknown storage key")
        return read_manuscript(self.paths[key])


class DirectEmbeddingProvider:
    """The index service's provider protocol over an embeddings client; counts attempts and measured tokens."""

    def __init__(self, client, spec):
        self.client, self.spec = client, spec
        self.attempts = self.input_tokens = self.unknown_usage = 0

    async def embed(self, source, chunks, before_call):
        # `before_call` records an operational ledger run id (FK to chat_runs); the local experiment has no
        # operational ledger, so attempts are only counted here.
        self.attempts += 1
        try:
            result = await self.client.create_embeddings([chunk.chunk_text for chunk in chunks])
        except Exception as exc:
            self.unknown_usage += 1
            raise IndexProviderFailure("PROVIDER_CONTRACT", False) from exc
        if result.input_token_count is None:
            self.unknown_usage += 1
        else:
            self.input_tokens += result.input_token_count
        if (result.model != self.spec.model or len(result.embeddings) != len(chunks)
                or any(len(vector) != self.spec.dimensions or not all(math.isfinite(x) for x in vector)
                       for vector in result.embeddings)):
            raise IndexProviderFailure("PROVIDER_CONTRACT", False)
        return result.embeddings


def check_hashes(fixture, manuscripts):
    """Local file bytes must hash to the episode content_hash the database (and the frozen manifest) carries."""
    mismatched = [row["episodeNo"] for row in fixture["episodes"]
                  if file_sha256(manuscript_path(manuscripts, row["episodeNo"])) != row["contentHash"]]
    if mismatched:
        raise HarnessError(f"Local manuscripts differ from the exported hashes for episodes {mismatched}; rebuild "
                           "the fixture with --use-local-hashes to index the local text instead")


async def index(engine, fixture, manuscripts, embedding_client, *, spec=None):
    spec = spec or IndexSpec()
    check_hashes(fixture, manuscripts)
    sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    service = RetrievalIndexService(RetrievalStore(sessions), LocalTextStorage(fixture, manuscripts), spec)
    scope = Scope(UUID(fixture["work"]["workId"]), episode_from=1, episode_to=fixture["target"]["episodeNo"] - 1,
                  max_sources=100)
    prepared = service.prepare(scope)
    provider = DirectEmbeddingProvider(embedding_client, spec)
    report = await service.run(scope, provider, enabled=True,
                               limits=Limits(max_chunks=1000, max_input_tokens=1_000_000, batch_size=16,
                                             max_seconds=1800, max_retries=0))
    return {"prepared": prepared, "run": report, "readiness": service.readiness(scope)}
