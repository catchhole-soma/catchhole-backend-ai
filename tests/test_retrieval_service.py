import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.retrieval.contracts import IndexSpec, Limits, Scope, Source, SourceChanged
from app.retrieval.provider import IndexProviderFailure
from app.retrieval.service import RetrievalIndexService
from app.retrieval.store import RetrievalStore

TEXT = "아리아는 반지의 추적 마법을 알아챘다. 그녀는 그것을 강물에 던졌다.\n" * 100


class MemoryStore:
    chunk_state = staticmethod(RetrievalStore.chunk_state)

    def __init__(self):
        self.source = Source(
            uuid4(), uuid4(), 1, "synthetic", "v1", sha256(TEXT.encode()).hexdigest(), "AVAILABLE"
        )
        self.chunks = []
        self.spec = None
        self.operations = []
        self.writes = 0

    def sources(self, scope):
        return [self.source], False

    def check_source(self, source):
        if source.fingerprint != self.source.fingerprint or self.source.state != "AVAILABLE":
            raise SourceChanged()

    def existing(self, source, spec):
        return deepcopy(self.chunks) if spec == self.spec else []

    def prepare(self, source, spec, slices):
        self.check_source(source)
        if self.chunks and spec == self.spec:
            return deepcopy(self.chunks)
        self.writes += 1
        self.spec = spec
        self.chunks = [
            SimpleNamespace(
                id=uuid4(),
                chunk_index=s.index,
                chunk_text=s.text,
                chunk_hash=s.content_hash,
                start_offset=s.start,
                end_offset=s.end,
                token_count=s.tokens,
                status="MISSING",
                embedding=None,
                embedding_model=None,
                embedding_version=None,
            )
            for s in slices
        ]
        return deepcopy(self.chunks)

    def validate(self, source, spec, chunks):
        self.check_source(source)
        if not {c.id for c in chunks} <= {c.id for c in self.chunks}:
            raise SourceChanged()

    def attempted(self, source, spec, chunks, operation_id):
        self.validate(source, spec, chunks)
        self.operations.append(operation_id)

    def save(self, source, spec, chunks, vectors):
        self.validate(source, spec, chunks)
        for c in self.chunks:
            if c.id in {c.id for c in chunks}:
                c.status, c.embedding = "READY", [1.0] * 1536
                c.embedding_model, c.embedding_version = spec.model, spec.version

    def failed(self, source, spec, chunks, code):
        self.validate(source, spec, chunks)
        for c in self.chunks:
            if c.id in {c.id for c in chunks}:
                c.status = "FAILED"


class Storage:
    def get_text(self, key, **kwargs):
        assert kwargs["version_id"] == "v1" and kwargs["max_bytes"] == 2000000
        return TEXT


class Provider:
    def __init__(self, action=None):
        self.action, self.attempts, self.input_tokens, self.unknown_usage = action, 0, 0, 0

    async def embed(self, source, chunks, before_call):
        await before_call(uuid4())
        self.attempts += 1
        if self.action:
            self.action(self)
        self.input_tokens += sum(c.token_count for c in chunks)
        return [[1.0] * 1536 for c in chunks]


@pytest.fixture
def fixture():
    store = MemoryStore()
    return store, RetrievalIndexService(store, Storage(), IndexSpec()), Scope(store.source.work_id)


def run(service, scope, provider=None, limits=Limits(), enabled=True):
    return asyncio.run(service.run(scope, provider or Provider(), enabled=enabled, limits=limits))


def test_dry_run_counts_original_tokens_without_writes_or_provider(fixture):
    store, service, scope = fixture
    report = service.dry_run(scope, Limits(usd_per_million=Decimal("0.02")))
    assert report["knownInputTokens"] > 0 and report["targetMissingChunks"] > 1
    assert Decimal(report["estimatedCostUsd"]) > 0
    assert store.writes == 0 and not store.operations
    assert report["userTokenDebit"] is False


def test_disabled_does_not_read_source_or_construct_provider(fixture):
    store, service, scope = fixture
    assert run(service, scope, enabled=False)["state"] == "DISABLED"
    assert store.writes == 0 and not store.operations


def test_completed_vectors_are_skipped_after_restart(fixture):
    store, service, scope = fixture
    first = run(service, scope)
    ids = [c.id for c in store.chunks]
    second = run(service, scope)
    assert first["embeddedChunks"] == len(ids) > 0
    assert second["providerAttempts"] == 0
    assert second["skippedReadyChunks"] == len(ids)
    assert ids == [c.id for c in store.chunks]
    assert service.dry_run(scope)["targetMissingChunks"] == 0


def test_chunk_cap_retains_missing_work_for_resume(fixture):
    store, service, scope = fixture
    first = run(service, scope, limits=Limits(max_chunks=1, batch_size=8))
    assert first["state"] == "CHUNK_LIMIT" and first["embeddedChunks"] == 1
    second = run(service, scope)
    assert second["skippedReadyChunks"] == 1
    assert second["embeddedChunks"] == len(store.chunks) - 1


@pytest.mark.parametrize(
    "limits",
    [
        Limits(max_input_tokens=1),
        Limits(usd_per_million=Decimal("1"), max_cost_usd=Decimal("0.000001")),
    ],
)
def test_budget_stops_before_paid_call(fixture, limits):
    store, service, scope = fixture
    report = run(service, scope, limits=limits)
    assert report["state"] == "BUDGET_EXHAUSTED"
    assert report["providerAttempts"] == 0 and not store.operations


@pytest.mark.parametrize("change", ["edit", "archive", "purge", "rechunk"])
def test_inflight_result_cannot_restore_changed_or_purged_source(fixture, change):
    store, service, scope = fixture

    def mutate(provider):
        if change == "edit":
            store.source = replace(store.source, content_hash="b" * 64)
        elif change == "rechunk":
            store.chunks = []
        else:
            store.source = replace(
                store.source, state="ARCHIVED" if change == "archive" else "PURGE_PENDING"
            )

    report = run(service, scope, Provider(mutate))
    assert report["embeddedChunks"] == 0 and report["changedChunks"] > 0
    assert not any(c.status == "READY" for c in store.chunks)


def test_recoverable_failure_retries_one_actual_attempt_at_a_time(fixture, monkeypatch):
    store, service, scope = fixture

    async def no_sleep(_):
        pass

    monkeypatch.setattr("app.retrieval.service.asyncio.sleep", no_sleep)

    def transient(provider):
        if provider.attempts == 1:
            raise IndexProviderFailure("PROVIDER_RETRYABLE", True)

    report = run(service, scope, Provider(transient))
    assert report["state"] == "COMPLETED"
    assert report["providerAttempts"] == len(store.operations) == 2
    assert report["attemptedInputTokens"] == 2 * report["measuredInputTokens"]


def test_auth_failure_stops_without_retry_and_resume_retries_failed_chunks(fixture):
    store, service, scope = fixture

    def fail(_):
        raise IndexProviderFailure("PROVIDER_AUTH", False)

    first = run(service, scope, Provider(fail))
    assert first["state"] == "PROVIDER_AUTH" and first["providerAttempts"] == 1
    assert all(c.status == "FAILED" for c in store.chunks)
    assert run(service, scope)["embeddedChunks"] == len(store.chunks)


def test_unreadable_source_is_unknown_not_zero_cost(fixture):
    store, service, scope = fixture
    service.storage.get_text = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("private source")
    )
    report = service.dry_run(scope, Limits(usd_per_million=Decimal("0.02")))
    assert report["unknownSourceCount"] == 1 and report["estimatedCostUsd"] is None
    assert report["sources"][0]["inputTokens"] is None
    assert "private source" not in str(report)


def test_database_failure_after_paid_response_is_visible_and_resumable(fixture):
    store, service, scope = fixture
    original = store.save
    store.save = lambda *a: (_ for _ in ()).throw(OperationalError("", {}, Exception()))
    report = run(service, scope)
    assert report["state"] == "DATABASE_FAILURE" and report["providerAttempts"] == 1
    store.save = original
    assert run(service, scope)["embeddedChunks"] > 0
