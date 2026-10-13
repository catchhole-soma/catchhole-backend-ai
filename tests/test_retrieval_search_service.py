import asyncio
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from app.retrieval.contracts import IndexSpec
from app.retrieval.search import RetrievalSearchService
from app.retrieval.search_contracts import SearchReadiness, SearchScope
from tests.test_retrieval_search_evidence import chunk


def service_fixture(*, vectors=1):
    scope = SearchScope(42, uuid4(), 3, 8)
    found = chunk()
    events = []

    @contextmanager
    def sessions():
        events.append("session-open")
        try:
            yield object()
        finally:
            events.append("session-close")

    class Repository:
        def __init__(self, session, trusted_scope, spec):
            assert trusted_scope == scope
            self.scope, self.spec = trusted_scope, spec

        def readiness(self):
            return SearchReadiness(
                1, 1, vectors, 1, vectors, () if vectors else ("VECTOR_INDEX_PARTIAL",)
            )

        def vector(self, vector, *, limit, excluded_chunk_ids):
            events.append(("vector", vector, limit))
            return [found]

        def revalidate(self, chunks):
            return chunks

    class Embedding:
        model = IndexSpec().model
        version = IndexSpec().version

        async def create_embedding(self, text):
            assert events[-1] == "session-close"
            events.append(("embedding", text))
            return [1.0] * 1536

    return (
        RetrievalSearchService(sessions, repository_factory=Repository),
        scope,
        Embedding(),
        events,
        found,
    )


def test_existing_vector_service_uses_only_bound_current_corpus_adapter_and_injected_embedding():
    service, scope, embedding, events, found = service_fixture()
    result = asyncio.run(service.vector(scope, "민수 흑염", embedding, limit=4))
    assert result.chunks == (found,)
    assert result.readiness.vector_chunks == 1 and not result.limited
    assert sum(event == ("embedding", "민수 흑염") for event in events) == 1
    assert ("vector", [1.0] * 1536, 4) in events


def test_missing_vectors_preserve_text_readiness_without_calling_embedding():
    service, scope, embedding, events, _ = service_fixture(vectors=0)
    result = asyncio.run(service.vector(scope, "장면", embedding))
    assert not result.chunks
    assert result.readiness.text_ready_sources == 1
    assert result.readiness.limitations == ("VECTOR_INDEX_PARTIAL",)
    assert not any(isinstance(event, tuple) and event[0] == "embedding" for event in events)


@pytest.mark.parametrize("query,limit", [(" ", 2), ("a" * 1001, 2), ("ok", 0), ("ok", 101)])
def test_invalid_vector_request_never_opens_database_or_calls_provider(query, limit):
    service, scope, embedding, events, _ = service_fixture()
    with pytest.raises(ValueError):
        asyncio.run(service.vector(scope, query, embedding, limit))
    assert not events


def test_mismatched_model_is_rejected_before_any_io():
    service, scope, embedding, events, _ = service_fixture()
    embedding.version = "other"
    with pytest.raises(ValueError):
        asyncio.run(service.vector(scope, "query", embedding))
    assert not events


def test_falsey_embedding_adapter_cannot_activate_legacy_default_provider():
    service, scope, embedding, events, _ = service_fixture()

    class FalseyEmbedding(type(embedding)):
        def __bool__(self):
            return False

    with pytest.raises(ValueError):
        asyncio.run(service.vector(scope, "query", FalseyEmbedding()))
    assert not events


def test_scope_cannot_be_constructed_with_invalid_server_range():
    scope = SearchScope(42, uuid4())
    with pytest.raises(ValueError):
        replace(scope, episode_to=0)
    with pytest.raises(ValueError):
        replace(scope, member_id=0)
