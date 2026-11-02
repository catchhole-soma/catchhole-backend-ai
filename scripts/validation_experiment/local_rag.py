"""Local stand-in for the Worker's frozen RAG: the production search service on the experiment database.

`LocalRagSearch` offers `readiness(frozen)` and `search(frozen, query, embedding_client, limit)` like
`FrozenRetrievalSearchService`, scoped to the work's episodes before the target (no report fence exists locally).
"""

from uuid import UUID

from sqlalchemy.orm import sessionmaker

from app.retrieval.contracts import IndexSpec
from app.retrieval.search import RetrievalSearchService
from app.retrieval.search_contracts import SearchScope, SearchScopeUnavailable
from app.usage.metering import MeteredEmbeddingClient
from app.validation.contracts import IncompleteInspection


class LocalRagSearch:
    def __init__(self, engine, fixture, spec=None):
        self.spec = spec or IndexSpec()
        self.service = RetrievalSearchService(sessionmaker(engine, autoflush=False, expire_on_commit=False), self.spec)
        self.scope = SearchScope(fixture["work"]["memberId"], UUID(fixture["work"]["workId"]), 1,
                                 fixture["target"]["episodeNo"] - 1)

    async def readiness(self, frozen):
        return await self.service.readiness(self.scope)

    async def search(self, frozen, query, embedding_client, limit):
        try:
            return await self.service.vector(self.scope, query, embedding_client, limit=limit)
        except IncompleteInspection:
            raise
        except SearchScopeUnavailable:
            raise IncompleteInspection("INPUT_CHANGED") from None
        except Exception:  # noqa: BLE001 - the database/embedding boundary keeps fixed codes only.
            raise IncompleteInspection("SEARCH_FAILED") from None


class LocalQueryEmbedding:
    """Metered query embedding against the local ledger (role QUERY_EMBEDDING); `last_receipt` is synthetic."""

    def __init__(self, raw, ledger, job_id, lease, spec=None, request_semaphore=None):
        spec = spec or IndexSpec()
        self.model, self.version, self.dimensions = spec.model, spec.version, spec.dimensions
        self.metered = MeteredEmbeddingClient(raw, ledger, job_id, spec.model, lease, request_semaphore=request_semaphore,
                                              max_retries=0, purpose="VALIDATION_QUERY_EMBEDDING")
        self.last_receipt = None
        self.calls = 0

    async def create_embedding(self, query):
        from hashlib import sha256

        self.calls += 1
        response = await self.metered.create_embeddings([query])
        self.last_receipt = {"queryHash": sha256(query.encode()).hexdigest(), "indexModel": self.model,
                             "indexVersion": self.version, "indexDimensions": self.dimensions, "local": True}
        return response.embeddings[0]
