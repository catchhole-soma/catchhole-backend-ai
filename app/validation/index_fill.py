"""Fill only the gaps of a validation run's frozen RAG scope before judging (owner decision 2026-11-02).

"RAG 색인은 검수를 접수할 때 그 작품의 빈 곳(앞 회차·설정집)만 채운다." The scope is the frozen prior episodes and
the setting-book revisions the server marked `currentRevision` at intake (`search.indexed_sources`). Works that
never submit a validation are never touched.

Per source, one aggregate query (no vectors loaded) decides whether anything is missing:

- a complete source (header = frozen key/version/hash/policy, every chunk a valid READY vector of the index spec)
  is skipped without reading its text;
- otherwise the full text is read through the frozen data API (READ_SOURCE: server-verified manifest and hash),
  cut with the same retrieval splitter, and `store.prepare` keeps an intact chunk set or replaces a changed one
  (a changed source is re-chunked, never mixed), and only chunks that are not READY are embedded.

Embeddings go through the run's metered INDEX_EMBEDDING role (lease-fenced, per-run caps, service-paid). Provider
failures, role caps, writer-database errors and source changes end the fill with a FAILED outcome and the run
continues: judging then sees a not-ready index, and only questions that need a search end INCOMPLETE with
VECTOR_INDEX_FILL_FAILED. Lease loss and input invalidation still stop the run. Retries and lease recovery are
safe: a later run skips every chunk an earlier attempt already saved.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.retrieval.chunking import RetrievalSplitter
from app.retrieval.contracts import BookSource, Source, SourceChanged
from app.retrieval.store import RetrievalStore, SettingBookRetrievalStore
from app.validation.contracts import IncompleteInspection
from app.validation.search import EPISODE, indexed_sources

logger = logging.getLogger(__name__)

INDEX_FILL_BATCH_SIZE = 32  # chunks per embedding call: <= 32 x 800 tokens, far below the provider's request cap
FILL_FAILED_CODE = "VECTOR_INDEX_FILL_FAILED"
# Ledger/HTTP codes that end only the fill; every other IncompleteInspection (gate stop, lease or input
# change, data access) is a run-level failure and is re-raised.
_FILL_LOCAL_CODES = {
    "VALIDATION_EXECUTION_LIMIT_EXCEEDED": "INDEX_FILL_LIMIT_EXCEEDED",
    "AI_TOKEN_QUOTA_EXHAUSTED": "INDEX_FILL_QUOTA_EXHAUSTED",
    "EXECUTION_TRANSPORT_FAILED": "INDEX_FILL_LEDGER_FAILED",
    "VALIDATION_PUBLICATION_CONFLICT": "INDEX_FILL_LEDGER_FAILED",
    "AI_TOKEN_RESERVATION_CONFLICT": "INDEX_FILL_LEDGER_FAILED",
    "EMBEDDING_CONFIGURATION_INVALID": "INDEX_FILL_PROVIDER_FAILED",
    "SOURCE_UNREADABLE": "INDEX_FILL_SOURCE_UNREADABLE",
    "INVALID_SOURCE_CONTRACT": "INDEX_FILL_SOURCE_UNREADABLE",
}


@dataclass(frozen=True)
class IndexFillOutcome:
    """`status`: NOT_NEEDED (scope empty or complete), COMPLETED, FAILED (with `cause`) or UNAVAILABLE."""

    status: str
    cause: str | None = None
    counts: dict = field(default_factory=dict)

    @property
    def failed(self):
        return self.status == "FAILED"


class _FillFailed(Exception):
    def __init__(self, cause):
        super().__init__(cause)
        self.cause = cause


class ValidationIndexFill:
    """One fill per run. `sessions` is a writer session factory; `embedding` the metered INDEX_EMBEDDING client."""

    def __init__(self, sessions, embedding, spec, *, gate=None, batch_size=INDEX_FILL_BATCH_SIZE, splitter=None,
                 blocking_runner=None, episode_store=None, book_store=None):
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise ValueError("Index fill batch size must be between 1 and 64")
        self.sessions, self.embedding, self.spec, self.batch_size = sessions, embedding, spec, batch_size
        self.gate = gate
        self.splitter = splitter or RetrievalSplitter()
        self.run_blocking = blocking_runner or asyncio.to_thread
        self.stores = {EPISODE: episode_store or RetrievalStore(sessions),
                       "SETTING_BOOK": book_store or SettingBookRetrievalStore(sessions)}

    @staticmethod
    def source(frozen, document):
        """The retrieval source identity of one frozen manifest row; the store re-checks it under the work lock."""
        meta, work = document.metadata, frozen.context.work_id
        if document.kind == EPISODE:
            return Source(work, UUID(meta["episodeId"]), meta["episodeNo"], meta.get("contentS3Key"),
                          meta.get("contentS3Version"), document.content_hash, "AVAILABLE")
        return BookSource(work, UUID(meta["settingBookId"]), UUID(meta["revisionId"]), meta["revisionNo"],
                          meta.get("contentS3Key"), meta.get("contentS3Version"), document.content_hash)

    def _complete(self, store, sources):
        """{source_id: complete} for one kind in one aggregate read; vectors are never loaded."""
        if not sources:
            return {}
        owner = store.owner
        header, chunks = store.source_model.__tablename__, store.chunk_model.__tablename__
        with self.sessions() as session:
            rows = session.execute(
                text(f"""
                SELECT s.*, count(c.id) AS stored,
                  count(c.id) FILTER (WHERE c.status='READY' AND c.embedding IS NOT NULL
                    AND c.embedding_model=:model AND c.embedding_version=:version
                    AND c.start_offset >= 0 AND c.end_offset > c.start_offset
                    AND char_length(c.chunk_text)=c.end_offset-c.start_offset
                    AND encode(sha256(convert_to(c.chunk_text,'UTF8')),'hex')=c.chunk_hash) AS ready
                FROM {header} s LEFT JOIN {chunks} c ON c.{owner}=s.{owner}
                WHERE s.{owner}=ANY(:ids) GROUP BY s.{owner}"""),
                {"ids": [store.source_id(source) for source in sources], "model": self.spec.model,
                 "version": self.spec.version},
            ).mappings().all()
        found = {row[owner]: row for row in rows}
        result = {}
        for source in sources:
            row = found.get(store.source_id(source))
            result[store.source_id(source)] = bool(
                row is not None and store._matches(SimpleNamespace(**row), source, self.spec)
                and row["chunk_count"] > 0 and row["stored"] == row["ready"] == row["chunk_count"])
        return result

    def gaps(self, frozen):
        """Frozen RAG sources with a missing, stale or not fully embedded index, in manifest order."""
        documents = indexed_sources(frozen.sources)
        by_kind = {kind: [(document, self.source(frozen, document)) for document in documents if document.kind == kind]
                   for kind in self.stores}
        complete = {kind: self._complete(self.stores[kind], [source for _, source in rows])
                    for kind, rows in by_kind.items()}
        missing = {id(document) for kind, rows in by_kind.items() for document, source in rows
                   if not complete[kind][self.stores[kind].source_id(source)]}
        return documents, [document for document in documents if id(document) in missing]

    async def fill(self, frozen, data):
        """Fill the gaps of `frozen`'s scope. Returns an outcome; run-level failures are raised."""
        gate = self.gate
        counts = {"scopeSources": 0, "gapSources": 0, "filledSources": 0, "embeddedChunks": 0,
                  "keptReadyChunks": 0, "embeddingCalls": 0}
        try:
            documents, gaps = await self.run_blocking(self.gaps, frozen)
        except SQLAlchemyError:
            return self._outcome("FAILED", "INDEX_FILL_DATABASE_FAILED", counts)
        counts.update(scopeSources=len(documents), gapSources=len(gaps))
        if not gaps:
            return self._outcome("NOT_NEEDED", None, counts)
        if self.embedding is None:
            return self._outcome("UNAVAILABLE", "INDEX_FILL_UNAVAILABLE", counts)
        try:
            for document in gaps:
                if gate is not None:
                    gate.require()
                await self._fill_source(frozen, data, document, counts, gate)
                counts["filledSources"] += 1
        except _FillFailed as failure:
            return self._outcome("FAILED", failure.cause, counts)
        return self._outcome("COMPLETED", None, counts)

    async def _fill_source(self, frozen, data, document, counts, gate):
        store, source = self.stores[document.kind], self.source(frozen, document)
        try:
            read = await data.read_source(document)
        except IncompleteInspection as exc:
            if exc.code in _FILL_LOCAL_CODES:
                raise _FillFailed(_FILL_LOCAL_CODES[exc.code]) from None
            raise
        if read.start != 0 or read.end != read.total_code_points:
            raise _FillFailed("INDEX_FILL_SOURCE_UNREADABLE")
        try:
            slices = await self.run_blocking(self.splitter.split, read.text)
            chunks = await self.run_blocking(store.prepare, source, self.spec, slices)
        except SourceChanged:
            raise _FillFailed("INDEX_FILL_SOURCE_CHANGED") from None
        except SQLAlchemyError:
            raise _FillFailed("INDEX_FILL_DATABASE_FAILED") from None
        missing = [chunk for chunk in chunks if store.chunk_state(chunk, self.spec) != "READY"]
        counts["keptReadyChunks"] += len(chunks) - len(missing)
        for offset in range(0, len(missing), self.batch_size):
            if gate is not None:
                gate.require()
            batch = missing[offset:offset + self.batch_size]
            try:
                await self.run_blocking(store.validate, source, self.spec, batch)
                await self.run_blocking(store.attempted, source, self.spec, batch, None)
            except SourceChanged:
                raise _FillFailed("INDEX_FILL_SOURCE_CHANGED") from None
            except SQLAlchemyError:
                raise _FillFailed("INDEX_FILL_DATABASE_FAILED") from None
            counts["embeddingCalls"] += 1
            try:
                vectors = await self.embedding.embed(document, batch)
            except IncompleteInspection as exc:
                if exc.code not in _FILL_LOCAL_CODES:
                    raise
                await self._mark_failed(store, source, batch, _FILL_LOCAL_CODES[exc.code])
                raise _FillFailed(_FILL_LOCAL_CODES[exc.code]) from None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - provider text/URLs never leave this boundary; only a fixed cause.
                await self._mark_failed(store, source, batch, "INDEX_FILL_PROVIDER_FAILED")
                raise _FillFailed("INDEX_FILL_PROVIDER_FAILED") from None
            try:
                await self.run_blocking(store.save, source, self.spec, batch, vectors)
            except SourceChanged:
                raise _FillFailed("INDEX_FILL_SOURCE_CHANGED") from None
            except SQLAlchemyError:
                raise _FillFailed("INDEX_FILL_DATABASE_FAILED") from None
            counts["embeddedChunks"] += len(batch)

    async def _mark_failed(self, store, source, batch, cause):
        try:
            await self.run_blocking(store.failed, source, self.spec, batch, cause[:40])
        except (SourceChanged, SQLAlchemyError):
            pass  # FAILED is diagnostic only: a later fill treats MISSING and FAILED chunks alike.

    @staticmethod
    def _outcome(status, cause, counts):
        logger.info("validation index fill status=%s cause=%s counts=%s", status, cause or "-",
                    dict(sorted(counts.items())))
        return IndexFillOutcome(status, cause, dict(counts))
