"""Intake index fill (owner decision 2026-11-02) without a database: orchestration, failure policy, wire identity.

The real stores and SQL are exercised in tests/test_validation_index_fill_postgres.py.
"""

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.retrieval.contracts import IndexSpec, SourceChanged
from app.validation.contracts import IncompleteInspection
from app.validation.corpus import ValidationCorpus
from app.validation.data import FrozenInput, SourceDocument, SourceRead
from app.validation.index_fill import IndexFillOutcome, ValidationIndexFill
from app.validation.search import FrozenChunk, FrozenSearchResult, indexed_sources
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    FakeEmbedding,
    FakeSearch,
    ScriptedModel,
    SyntheticData,
    budget,
    decided,
    draft,
)

SPEC = IndexSpec()


def contract():
    return json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json").read_text())["execution"]


# --- service: fill before judging ------------------------------------------------------------------------


class GatedSearch(FakeSearch):
    """Not ready until the fill says so; records when readiness was read."""

    def __init__(self, data, events):
        super().__init__(data, ready=False)
        self.events = events

    async def readiness(self, frozen):
        self.events.append(("readiness", self.ready))
        return self._readiness()


class StubFill:
    def __init__(self, outcome=None, *, error=None, delay=0.0, events=None, search=None):
        self.outcome, self.error, self.delay = outcome, error, delay
        self.events, self.search, self.calls, self.cancelled = events if events is not None else [], search, 0, False

    async def fill(self, frozen, data):
        self.calls += 1
        self.events.append(("fill-start",))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        if self.search is not None and self.outcome.status in {"COMPLETED", "NOT_NEEDED"}:
            self.search.ready = True
        self.events.append(("fill-end", self.outcome.status))
        return self.outcome


def suspected(payload, n):
    # The contradicting stored value is the finding's basis quote (judge rule guard, 2026-11-03).
    return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"])


def inspect(data, model, *, search, fill, **limits):
    service = SettingValidationService(data, model, budget(**limits), ROLES, search=search,
                                       embedding_client=FakeEmbedding(), index_fill=fill)
    return asyncio.run(service.inspect())


def test_judging_waits_for_the_fill_and_then_searches_the_filled_index():
    data, events = SyntheticData(), []
    search = GatedSearch(data, events)
    fill = StubFill(IndexFillOutcome("COMPLETED", counts={"embeddedChunks": 3}), delay=0.05, events=events,
                    search=search)
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=search, fill=fill)
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    # The fill started before planning ended; readiness was read once, after the fill, and the index was ready.
    assert events.index(("fill-start",)) < events.index(("fill-end", "COMPLETED")) < events.index(("readiness", True))
    assert [event for event in events if event[0] == "readiness"] == [("readiness", True)]
    assert result.normalizations["INDEX_FILL_COMPLETED"] == 1 and result.normalizations["INDEX_FILL_EMBEDDED_CHUNKS"] == 3
    assert result.normalizations["VECTOR_OFFERED"] == 1 and search.searches


@pytest.mark.parametrize("cause", ["INDEX_FILL_PROVIDER_FAILED", "INDEX_FILL_LIMIT_EXCEEDED"])
def test_a_failed_fill_keeps_judging_and_only_the_search_question_ends_incomplete(cause):
    data = SyntheticData()
    search = FakeSearch(data, ready=False)
    model = ScriptedModel({"TSEG1": [draft(), draft("gate", "그는 성문 앞에서 기다렸다.", prop="성문 대기")]},
                          lambda payload, n: suspected(payload, n) if payload["question_ref"] == "Q1"
                          else decided(payload))
    result = inspect(data, model, search=search, fill=StubFill(IndexFillOutcome("FAILED", cause)))
    assert result.status == "INCOMPLETE" and result.failure_code == "SETTING_QUESTIONS_INCOMPLETE"
    assert result.question_failures["Q1"]["code"] == "VECTOR_INDEX_FILL_FAILED"
    assert result.question_failures["Q1"]["cause"] == cause
    assert result.decisions["Q2"].outcome == "NO_CONFLICT"  # the rest of the run is still judged
    failed = [row for row in result.receipts if row.status == "FAILED"]
    assert [row.failure_code for row in failed] == ["VECTOR_INDEX_FILL_FAILED"]
    assert result.normalizations["INDEX_FILL_FAILED"] == 1 and result.normalizations["VECTOR_OFFERED"] == 0


def test_without_a_fill_the_unready_index_still_reports_vector_not_ready():
    data = SyntheticData()
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=FakeSearch(data, ready=False), fill=None)
    assert result.question_failures["Q1"]["code"] == "VECTOR_NOT_READY"
    assert "INDEX_FILL_FAILED" not in result.normalizations


def test_a_fill_without_writer_or_role_is_not_ready_with_its_cause():
    data = SyntheticData()
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=FakeSearch(data, ready=False),
                     fill=StubFill(IndexFillOutcome("UNAVAILABLE", "INDEX_FILL_UNAVAILABLE")))
    assert (result.question_failures["Q1"]["code"], result.question_failures["Q1"]["cause"]) == (
        "VECTOR_NOT_READY", "INDEX_FILL_UNAVAILABLE")


def test_a_run_level_fill_failure_stops_the_run_like_any_input_change():
    data = SyntheticData()
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=FakeSearch(data), fill=StubFill(error=IncompleteInspection("INPUT_CHANGED")))
    assert result.status == "INCOMPLETE" and result.failure_code == "INPUT_CHANGED"
    assert model.judge_calls() == []


def test_an_unexpected_fill_error_ends_only_the_fill():
    data = SyntheticData()
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=FakeSearch(data, ready=False), fill=StubFill(error=KeyError("bug")))
    assert result.question_failures["Q1"] == {"code": "VECTOR_INDEX_FILL_FAILED", "cause": "INDEX_FILL_UNEXPECTED",
                                              "call": 1}


def test_a_stuck_fill_times_out_is_cancelled_and_judging_continues(monkeypatch):
    from app.validation import service

    monkeypatch.setattr(service, "INDEX_FILL_TIMEOUT_SECONDS", 0.05)
    data = SyntheticData()
    fill = StubFill(IndexFillOutcome("COMPLETED"), delay=30)
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=FakeSearch(data, ready=False), fill=fill)
    assert fill.cancelled
    assert result.question_failures["Q1"]["cause"] == "INDEX_FILL_TIMEOUT"


def test_a_planner_failure_cancels_the_running_fill():
    data = SyntheticData()
    fill = StubFill(IndexFillOutcome("COMPLETED"), delay=30)

    class Broken(ScriptedModel):
        async def create_text_response(self, **kwargs):
            raise RuntimeError("provider down")

    result = inspect(data, Broken(), search=FakeSearch(data), fill=fill)
    assert result.status == "INCOMPLETE" and result.failure_code == "MODEL_CALL_FAILED" and fill.cancelled


def test_no_rag_scope_never_starts_a_fill():
    data = SyntheticData(prior={})
    fill = StubFill(IndexFillOutcome("COMPLETED"))
    model = ScriptedModel({"TSEG1": [draft()]}, suspected)
    result = inspect(data, model, search=None, fill=fill)
    assert result.status == "COMPLETED" and fill.calls == 0


# --- the fill itself (fake stores; SQL in the PostgreSQL test) --------------------------------------------


class FakeStore:
    owner = "episode_id"

    def __init__(self, ready_indexes=()):
        self.ready_indexes = set(ready_indexes)
        self.events, self.saved, self.failed_rows = [], [], []

    def source_id(self, source):
        return getattr(source, self.owner)

    def prepare(self, source, spec, slices):
        self.events.append("prepare")
        return [SimpleNamespace(id=uuid4(), chunk_index=s.index, chunk_text=s.text, chunk_hash=s.content_hash,
                                status="READY" if s.index in self.ready_indexes else "MISSING") for s in slices]

    @staticmethod
    def chunk_state(chunk, spec):
        return "READY" if chunk.status == "READY" else "MISSING"

    def validate(self, source, spec, chunks):
        self.events.append("validate")

    def attempted(self, source, spec, chunks, operation_id):
        assert operation_id is None  # a validation fill has no operational run
        self.events.append("attempted")

    def save(self, source, spec, chunks, vectors):
        self.saved.extend(chunk.chunk_index for chunk in chunks)

    def failed(self, source, spec, chunks, code):
        self.failed_rows.append((code, [chunk.chunk_index for chunk in chunks]))


class FakeBookStore(FakeStore):
    owner = "revision_id"


class FakeIndexEmbedding:
    def __init__(self, error=None):
        self.error, self.batches = error, []

    async def embed(self, document, chunks):
        self.batches.append((document.ref, [chunk.chunk_index for chunk in chunks]))
        if self.error is not None:
            raise self.error
        return [[1.0] + [0.0] * 1535 for _ in chunks]


LONG = "민수는 왼손을 잃었다. " * 400  # several retrieval chunks


def frozen_input(*, book_current=True):
    work = uuid4()
    context = SimpleNamespace(work_id=work)
    episode = SourceDocument("R1", "EPISODE", sha256(LONG.encode()).hexdigest(), "v1",
                             {"episodeId": str(uuid4()), "episodeNo": 1, "contentS3Key": "synthetic/r1",
                              "contentS3Version": "v1"})
    book = SourceDocument("R2", "SETTING_BOOK", sha256("설정".encode()).hexdigest(), "v1",
                          {"settingBookId": str(uuid4()), "revisionId": str(uuid4()), "revisionNo": 2,
                           "referenceTimeKind": "WORK_START", "currentRevision": book_current,
                           "contentS3Key": "synthetic/book", "contentS3Version": "v1"})
    old = SourceDocument("R3", "SETTING_BOOK", "c" * 64, "v1",
                         {"settingBookId": book.metadata["settingBookId"], "revisionId": str(uuid4()),
                          "revisionNo": 1, "referenceTimeKind": "WORK_START", "currentRevision": False})
    return SimpleNamespace(context=context, sources=(episode, book, old)), {"R1": LONG, "R2": "설정"}


class FakeData:
    def __init__(self, texts, error=None):
        self.texts, self.error, self.reads = texts, error, []

    async def read_source(self, document, start=None, end=None):
        self.reads.append(document.ref)
        if self.error is not None:
            raise self.error
        text = self.texts[document.ref]
        return SourceRead(document, text, 0, len(text), len(text))


def filler(monkeypatch, *, complete=None, embedding=None, gate=None, episode_store=None, book_store=None):
    episode_store, book_store = episode_store or FakeStore(), book_store or FakeBookStore()
    fill = ValidationIndexFill(lambda: None, embedding if embedding is not None else FakeIndexEmbedding(), SPEC,
                               gate=gate, batch_size=2, episode_store=episode_store, book_store=book_store,
                               blocking_runner=lambda action, *args: asyncio.sleep(0, action(*args)))
    complete = complete or {}
    monkeypatch.setattr(fill, "_complete", lambda store, sources: {
        store.source_id(source): complete.get(store.source_id(source), False) for source in sources})
    return fill, episode_store, book_store


def test_scope_is_prior_episodes_and_current_setting_book_revisions_only():
    frozen, _ = frozen_input()
    assert [source.ref for source in indexed_sources(frozen.sources)] == ["R1", "R2"]
    frozen, _ = frozen_input(book_current=False)
    assert [source.ref for source in indexed_sources(frozen.sources)] == ["R1"]


def test_complete_sources_are_skipped_without_reading_or_embedding(monkeypatch):
    frozen, texts = frozen_input()
    episode_id, revision_id = (UUID(frozen.sources[0].metadata["episodeId"]),
                               UUID(frozen.sources[1].metadata["revisionId"]))
    fill, store, _ = filler(monkeypatch, complete={episode_id: True, revision_id: True})
    data = FakeData(texts)
    outcome = asyncio.run(fill.fill(frozen, data))
    assert outcome.status == "NOT_NEEDED" and outcome.counts["scopeSources"] == 2
    assert data.reads == [] and fill.embedding.batches == [] and store.events == []


def test_only_chunks_that_are_not_ready_are_embedded_in_batches(monkeypatch):
    frozen, texts = frozen_input()
    revision_id = UUID(frozen.sources[1].metadata["revisionId"])
    store = FakeStore(ready_indexes={0, 2})
    fill, store, _ = filler(monkeypatch, complete={revision_id: True}, episode_store=store)
    data = FakeData(texts)
    outcome = asyncio.run(fill.fill(frozen, data))
    assert outcome.status == "COMPLETED" and data.reads == ["R1"]  # the complete book is not read
    chunks = len(fill.splitter.split(LONG))
    missing = [index for index in range(chunks) if index not in {0, 2}]
    assert sorted(store.saved) == missing and 0 not in store.saved and 2 not in store.saved
    assert all(len(batch) <= 2 for _, batch in fill.embedding.batches)
    assert outcome.counts["keptReadyChunks"] == 2 and outcome.counts["embeddedChunks"] == len(missing)


def test_a_setting_book_without_embeddings_is_filled_through_the_book_store(monkeypatch):
    frozen, texts = frozen_input()
    episode_id = UUID(frozen.sources[0].metadata["episodeId"])
    fill, store, book = filler(monkeypatch, complete={episode_id: True})
    outcome = asyncio.run(fill.fill(frozen, FakeData(texts)))
    assert outcome.status == "COMPLETED" and book.saved == [0] and store.saved == []
    assert fill.embedding.batches == [("R2", [0])]


@pytest.mark.parametrize(("error", "cause"), [
    (RuntimeError("provider 503"), "INDEX_FILL_PROVIDER_FAILED"),
    (IncompleteInspection("VALIDATION_EXECUTION_LIMIT_EXCEEDED"), "INDEX_FILL_LIMIT_EXCEEDED"),
    (IncompleteInspection("EXECUTION_TRANSPORT_FAILED"), "INDEX_FILL_LEDGER_FAILED"),
])
def test_a_provider_or_cap_failure_ends_the_fill_and_marks_the_batch_failed(monkeypatch, error, cause):
    frozen, texts = frozen_input()
    fill, store, _ = filler(monkeypatch, embedding=FakeIndexEmbedding(error))
    outcome = asyncio.run(fill.fill(frozen, FakeData(texts)))
    assert outcome == IndexFillOutcome("FAILED", cause, outcome.counts)
    assert store.failed_rows == [(cause, [0, 1])] and store.saved == []


@pytest.mark.parametrize("code", ["EXECUTION_HTTP_REJECTED", "EXECUTION_STOPPED", "USAGE_OWNER_INVALID"])
def test_lease_loss_during_a_fill_is_a_run_level_failure(monkeypatch, code):
    frozen, texts = frozen_input()
    fill, _, _ = filler(monkeypatch, embedding=FakeIndexEmbedding(IncompleteInspection(code)))
    with pytest.raises(IncompleteInspection, match=code):
        asyncio.run(fill.fill(frozen, FakeData(texts)))


@pytest.mark.parametrize(("error", "outcome"), [
    (IncompleteInspection("SOURCE_UNREADABLE"), "INDEX_FILL_SOURCE_UNREADABLE"),
    (IncompleteInspection("INPUT_CHANGED"), None),
    (IncompleteInspection("DATA_READ_FAILED"), None),
])
def test_source_reads_keep_the_run_level_codes(monkeypatch, error, outcome):
    frozen, texts = frozen_input()
    fill, _, _ = filler(monkeypatch)
    if outcome is None:
        with pytest.raises(IncompleteInspection, match=error.code):
            asyncio.run(fill.fill(frozen, FakeData(texts, error)))
    else:
        assert asyncio.run(fill.fill(frozen, FakeData(texts, error))).cause == outcome


def test_a_source_that_changed_under_the_work_lock_ends_the_fill(monkeypatch):
    frozen, texts = frozen_input()

    class Changed(FakeStore):
        def prepare(self, source, spec, slices):
            raise SourceChanged()

    fill, _, _ = filler(monkeypatch, episode_store=Changed())
    assert asyncio.run(fill.fill(frozen, FakeData(texts))).cause == "INDEX_FILL_SOURCE_CHANGED"


def test_a_stopped_gate_stops_the_fill_before_the_next_batch(monkeypatch):
    from app.validation.client import ExecutionGate

    frozen, texts = frozen_input()
    gate = ExecutionGate()

    class Stopping(FakeIndexEmbedding):
        async def embed(self, document, chunks):
            gate.stop()
            return await super().embed(document, chunks)

    fill, store, _ = filler(monkeypatch, embedding=Stopping(), gate=gate)
    with pytest.raises(IncompleteInspection, match="EXECUTION_STOPPED"):
        asyncio.run(fill.fill(frozen, FakeData(texts)))
    assert store.saved == [0, 1]  # the in-flight batch is saved; nothing after the stop is embedded


def test_without_an_embedding_client_gaps_are_reported_but_never_filled(monkeypatch):
    frozen, texts = frozen_input()
    fill = ValidationIndexFill(lambda: None, None, SPEC, episode_store=FakeStore(), book_store=FakeBookStore())
    monkeypatch.setattr(fill, "_complete", lambda store, sources: {store.source_id(s): False for s in sources})
    data = FakeData(texts)
    outcome = asyncio.run(fill.fill(frozen, data))
    assert (outcome.status, outcome.cause, outcome.counts["gapSources"]) == ("UNAVAILABLE", "INDEX_FILL_UNAVAILABLE", 2)
    assert data.reads == []


# --- corpus: setting-book chunks are evidence of their SETTING_BOOK source -------------------------------


class BookHits:
    def __init__(self, chunks, readiness):
        self.chunks, self.value = chunks, readiness

    async def readiness(self, frozen):
        return self.value

    async def search(self, frozen, query, embedding_client, limit):
        await embedding_client.create_embedding(query)
        return FrozenSearchResult(tuple(self.chunks), self.value)


def test_vector_evidence_can_come_from_a_current_setting_book_and_is_labelled_as_one():
    from app.retrieval.search_contracts import SearchReadiness
    from app.validation.contracts import BudgetState, LookupIntent

    frozen, _ = frozen_input()
    _, book, old = frozen.sources
    frozen = FrozenInput(SimpleNamespace(work_id=uuid4()), SourceDocument("T1", "EPISODE", "d" * 64, None, {}),
                         frozen.sources, "c" * 64, 0)
    chunk = FrozenChunk("SETTING_BOOK", UUID(book.metadata["revisionId"]), uuid4(), 0, "설정", 0, 2,
                        sha256("설정".encode()).hexdigest(), book.content_hash, 0.9)
    ready = SearchReadiness(2, 2, 2, 5, 5)
    corpus = ValidationCorpus(frozen, BudgetState(budget()), search=BookHits([chunk], ready),
                              embedding_client=FakeEmbedding(), available=True)
    intent = LookupIntent(ref="Q1-VECTOR-1", kind="COMPARISON_CONTEXT", operation="VECTOR", purpose="검색",
                          query="설정")
    receipt = asyncio.run(corpus.vector(intent))
    (ref,) = receipt.evidence_refs
    row = corpus.evidence[ref]
    assert (row["source_ref"], row["source_kind"]) == ("R2", "SETTING_BOOK")
    assert row["source_context"] == {"revisionNo": 2, "referenceTimeKind": "WORK_START"}
    assert receipt.readiness["scope_source_refs"] == ("R1", "R2")

    stale = FrozenChunk("SETTING_BOOK", UUID(old.metadata["revisionId"]), uuid4(), 0, "옛", 0, 1, "e" * 64,
                        old.content_hash, 0.9)
    corpus = ValidationCorpus(frozen, BudgetState(budget()), search=BookHits([stale], ready),
                              embedding_client=FakeEmbedding(), available=True)
    with pytest.raises(IncompleteInspection, match="SOURCE_OUTSIDE_MANIFEST"):
        asyncio.run(corpus.vector(intent))


def test_the_judge_prompt_labels_setting_book_evidence_as_a_setting_book():
    from app.validation.models import JUDGE_SYSTEM

    assert "SETTING_BOOK이면 작가가 올린 설정집" in JUDGE_SYSTEM and "설정집은 회차가 아니다" in JUDGE_SYSTEM
    assert "이전 회차 원문과 설정집에서 의미 검색" in JUDGE_SYSTEM


# --- wire identity of one index embedding reservation -----------------------------------------------------


def test_index_input_matches_the_shared_contract_and_chunk_digest_vector():
    from app.validation.execution import index_input

    wire = contract()["indexFill"]
    vector = wire["chunkDigestVector"]
    assert sha256("\n".join(vector["chunkHashes"]).encode()).hexdigest() == vector["sha256"]
    example = wire["reservation"]["indexInput"]
    claim = SimpleNamespace(scope=SimpleNamespace(inputDigest=example["inputDigest"]),
                            wire={"manifestHash": example["manifestHash"]})
    document = SimpleNamespace(ref=example["sourceRef"], content_hash=example["sourceHash"])
    chunks = [SimpleNamespace(chunk_hash=value) for value in vector["chunkHashes"]]
    assert index_input(claim, SPEC, document, chunks) == example
    assert wire["batchChunks"] == 32 and wire["role"] == "INDEX_EMBEDDING"


def test_only_an_index_fill_reservation_carries_index_input():
    from app.validation.client import ExecutionGate, ExecutionScope, ValidationClaim
    from app.validation.execution import ValidationLedger

    class Recording:
        def __init__(self):
            self.bodies = []

        async def leased(self, claim, operation, **body):
            self.bodies.append(body)

    claim = ValidationClaim(uuid4(), uuid4(), ExecutionScope(contractVersion=1, reportId=uuid4(), generation=1,
                                                             inputDigest="a" * 64), {})
    client = Recording()
    ledger = ValidationLedger(client, claim, ExecutionGate())
    ledger.index_input = {"sourceRef": "R1"}

    async def run():
        for purpose in ("VALIDATION_JUDGE", "VALIDATION_INDEX_EMBEDDING"):
            await ledger.reserve_ai_tokens(uuid4(), claim.job_id, purpose, 1, "m", 10, claim.lease_token)

    asyncio.run(run())
    judge, index = client.bodies
    assert "indexInput" not in judge and judge["role"] == "JUDGE"
    assert index["indexInput"] == {"sourceRef": "R1"} and index["role"] == "INDEX_EMBEDDING"
    assert index["queryInput"] is None


INDEX_MODEL = {"model": "text-embedding-3-small", "version": "v1", "dimensions": 1536}


@pytest.mark.parametrize(("model", "cap", "expected"), [
    (None, None, None),
    (INDEX_MODEL, {"maxCalls": 4, "maxReservedTokens": 1000}, "client"),
    ({**INDEX_MODEL, "version": "v2"}, {"maxCalls": 4, "maxReservedTokens": 1000}, "error"),
    (INDEX_MODEL, None, "error"),
])
def test_the_index_embedding_role_is_optional_but_must_match_the_index(model, cap, expected):
    from app.validation.execution import ValidationIndexEmbedding, metered_index_embedding

    wire = {"models": {"INDEX_EMBEDDING": model} if model else {},
            "budgets": {"roles": {"INDEX_EMBEDDING": cap} if cap else {}}, "manifestHash": "c" * 64}
    claim = SimpleNamespace(job_id=uuid4(), lease_token=uuid4(), wire=wire,
                            scope=SimpleNamespace(inputDigest="a" * 64))
    raw = SimpleNamespace(model="text-embedding-3-small", version="v1", dimensions=1536)
    if expected == "error":
        with pytest.raises(IncompleteInspection, match="EMBEDDING_CONFIGURATION_INVALID"):
            metered_index_embedding(raw, SimpleNamespace(), claim, SPEC)
        return
    client = metered_index_embedding(raw, SimpleNamespace(), claim, SPEC)
    assert (client is None) == (expected is None)
    if client is not None:
        assert isinstance(client, ValidationIndexEmbedding) and client.metered.purpose == "VALIDATION_INDEX_EMBEDDING"
        assert client.metered.max_retries == 1


def test_index_embedding_rejects_a_response_that_does_not_fit_the_index():
    from app.validation.execution import ValidationIndexEmbedding

    class Raw:
        model, version, dimensions = "text-embedding-3-small", "v1", 1536

        async def create_embeddings(self, inputs):
            return SimpleNamespace(model="text-embedding-3-small", embeddings=[[0.0] * 1536 for _ in inputs],
                                   input_token_count=3, raw_response={})

    class Ledger:
        index_input = None

        async def reserve_ai_tokens(self, **kwargs):
            pass

        async def settle_ai_tokens(self, *args, **kwargs):
            pass

    claim = SimpleNamespace(job_id=uuid4(), lease_token=uuid4(), wire={"manifestHash": "c" * 64},
                            scope=SimpleNamespace(inputDigest="a" * 64))
    client = ValidationIndexEmbedding(Raw(), Ledger(), claim, SPEC)
    document = SimpleNamespace(ref="R1", content_hash="a" * 64)
    with pytest.raises(IncompleteInspection, match="EMBEDDING_CONFIGURATION_INVALID"):  # all-zero vectors
        asyncio.run(client.embed(document, [SimpleNamespace(chunk_text="본문", chunk_hash="d" * 64)]))
    assert client.ledger.index_input is None  # the shared proof never leaks to the next reservation
