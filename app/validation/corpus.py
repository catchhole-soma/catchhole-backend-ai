"""Run-level frozen RAG reads: L receipts, the E-ref registry and one VECTOR search per distinct query.

Grid v4 removes keyword, direct, neighbor and raw/book reads. Evidence comes from the frozen retrieval index
only: prior episodes and the current setting-book revisions (filled at intake), and the Backend verifies every
cited quote against the actual source at publication, so the Worker no longer re-reads each chunk.
"""

import asyncio
from collections import Counter
from dataclasses import asdict

from app.validation.contracts import IncompleteInspection, LookupReceipt
from app.validation.search import EPISODE, indexed_sources

# Frozen prior episodes plus the setting-book revisions that were current at intake.
VECTOR_SCOPE = "ELIGIBLE_EPISODES_AND_CURRENT_SETTING_BOOKS"
NOT_READY_CODE = "VECTOR_NOT_READY"


def _chunk_source(chunk):
    """(kind, id) of a ranked chunk; an episode-only `ChunkEvidence` (local experiment search) is an episode."""
    if hasattr(chunk, "source_kind"):
        return chunk.source_kind, str(chunk.source_id)
    return EPISODE, str(chunk.episode_id)


class ValidationCorpus:
    def __init__(self, frozen, budget, *, search=None, embedding_client=None, available=False, stats=None,
                 unavailable=(NOT_READY_CODE, None)):
        """`available` is the before-judging offer decision (readiness complete and an embedding client present).

        `unavailable` is the `(code, cause)` a needed search fails with when it is not offered: VECTOR_NOT_READY
        when the index could not be filled at all, VECTOR_INDEX_FILL_FAILED when the intake fill failed.
        """
        self.frozen, self.budget = frozen, budget
        self.search, self.embedding_client = search, embedding_client
        self.sources = {source.ref: source for source in frozen.sources}
        self.indexed = indexed_sources(frozen.sources)
        self.available = bool(available) and search is not None and embedding_client is not None
        self.unavailable = unavailable
        self.stats = stats if stats is not None else Counter()
        self.evidence = {}
        self.receipts = []
        self._results = {}
        # One lock around embedding, search and proof capture: the query embedding's proof is per-job state,
        # and a second identical query waiting here finds the first one's result instead of searching again.
        self._lock = asyncio.Lock()

    @property
    def search_offered(self):
        """VECTOR can be offered to the judge: an empty RAG scope (NOT_APPLICABLE) or a ready index."""
        return not self.indexed or self.available

    def mint(self, intent, status, evidence_refs=(), limited=False, *, failure_code=None, reused_from=None,
             readiness=None):
        """Append the next L receipt. Synchronous: no await between numbering and appending."""
        receipt = LookupReceipt(f"L{len(self.receipts) + 1}", intent, status, tuple(evidence_refs), limited,
                                failure_code, reused_from, readiness)
        self.receipts.append(receipt)
        return receipt

    def register(self, source, start, end, quote, method):
        if (source.ref not in self.sources or self.sources[source.ref] != source
                or not 0 <= start < end or len(quote) != end - start):
            raise IncompleteInspection("INVALID_SOURCE_EVIDENCE")
        same = next((ref for ref, value in self.evidence.items()
                     if (value["source_ref"], value["start"], value["end"], value["quote"])
                     == (source.ref, start, end, quote)), None)
        ref = same or f"E{len(self.evidence) + 1}"
        self.evidence[ref] = {
            "ref": ref, "source_ref": source.ref, "source_kind": source.kind, "start": start, "end": end,
            "quote": quote, "method": method,
            "source_context": {key: source.metadata[key]
                               for key in ("episodeNo", "revisionNo", "referenceTimeKind", "referenceEpisodeNo")
                               if key in source.metadata},
        }
        return ref

    def _reuse(self, intent, original):
        self.stats["RAG_CACHE_HITS"] += 1
        return self.mint(intent, "SUCCEEDED", original.evidence_refs, original.limited,
                         reused_from=original.ref, readiness=original.readiness)

    def _readiness(self, status, found=None):
        value = {"scope_source_refs": tuple(source.ref for source in self.indexed), "executed_methods": ("VECTOR",),
                 "vector_scope": VECTOR_SCOPE, "vector_status": status}
        if found is not None:
            # Counts may arrive as Decimal from SQL sum(); receipts are JSON (bundle digest, publication).
            value.update({key: tuple(item) if isinstance(item, (list, tuple)) else int(item)
                          for key, item in asdict(found).items()})
        return value

    async def vector(self, intent):
        """One VECTOR receipt for `intent`; a query already searched in this run is reused (new L, `reused_from`).

        Without prior episodes or current setting books there is nothing to search: an empty NOT_APPLICABLE
        success, no lookup and no embedding. With a scope but no ready index the search fails with the
        `unavailable` code (VECTOR_NOT_READY, or VECTOR_INDEX_FILL_FAILED after a failed intake fill).
        """
        if not self.indexed:
            return self.mint(intent, "SUCCEEDED", readiness=self._readiness("NOT_APPLICABLE"))
        if not self.available:
            code, cause = self.unavailable
            self.mint(intent, "FAILED", failure_code=code, readiness=self._readiness("NOT_READY"))
            raise IncompleteInspection(code, cause)
        key = intent.query.strip()
        if key in self._results:
            return self._reuse(intent, self._results[key])
        async with self._lock:
            if key in self._results:
                return self._reuse(intent, self._results[key])
            try:
                refs, limited, readiness = await self._search(intent.query)
            except IncompleteInspection as exc:
                self.mint(intent, "FAILED", failure_code=exc.code, readiness=getattr(exc, "readiness", None))
                raise
            receipt = self.mint(intent, "SUCCEEDED", refs, limited, readiness=readiness)
            self._results[key] = receipt
            return receipt

    async def _search(self, query):
        self.budget.consume("lookup")
        self.stats["VECTOR_SEARCHES"] += 1
        try:
            result = await asyncio.wait_for(
                self.search.search(self.frozen, query, self.embedding_client, self.budget.limits.search_limit),
                self.budget.remaining())
        except TimeoutError:
            raise IncompleteInspection("BUDGET_EXHAUSTED") from None
        found = result.readiness
        if (found is None or found.source_count != len(self.indexed)
                or found.vector_ready_sources < found.source_count or "VECTOR_INDEX_PARTIAL" in found.limitations):
            exc = IncompleteInspection("VECTOR_INDEX_PARTIAL")
            exc.readiness = self._readiness("PARTIAL", found)
            raise exc
        readiness = self._readiness("READY", found)
        proof = getattr(self.embedding_client, "last_receipt", None)
        if proof is not None:
            readiness["query_embedding"] = dict(proof)
        by_source = {(source.kind, source.metadata.get("episodeId" if source.kind == EPISODE else "revisionId")): source
                     for source in self.indexed}
        refs = []
        for chunk in result.chunks:
            source = by_source.get(_chunk_source(chunk))
            if source is None:
                raise IncompleteInspection("SOURCE_OUTSIDE_MANIFEST")
            if chunk.source_hash != source.content_hash:
                raise IncompleteInspection("SOURCE_CHANGED")
            refs.append(self.register(source, chunk.start_offset, chunk.end_offset, chunk.text, "VECTOR"))
        return tuple(dict.fromkeys(refs)), bool(result.limited), readiness
