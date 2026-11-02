"""Run-level frozen RAG reads: L receipts, the E-ref registry and one VECTOR search per distinct query.

Grid v4 removes keyword, direct, neighbor and raw/book reads. Evidence comes from the frozen retrieval index
only, and the Backend verifies every cited quote against the actual source at publication, so the Worker no
longer re-reads each chunk through READ_SOURCE.
"""

import asyncio
from collections import Counter
from dataclasses import asdict

from app.validation.contracts import IncompleteInspection, LookupReceipt

VECTOR_SCOPE = "ELIGIBLE_EPISODES"


class ValidationCorpus:
    def __init__(self, frozen, budget, *, search=None, embedding_client=None, available=False, stats=None):
        """`available` is the start-of-job offer decision (readiness complete and an embedding client present)."""
        self.frozen, self.budget = frozen, budget
        self.search, self.embedding_client = search, embedding_client
        self.sources = {source.ref: source for source in frozen.sources}
        self.episodes = tuple(source for source in frozen.sources if source.kind == "EPISODE")
        self.available = bool(available) and search is not None and embedding_client is not None
        self.stats = stats if stats is not None else Counter()
        self.evidence = {}
        self.receipts = []
        self._results = {}
        # One lock around embedding, search and proof capture: the query embedding's proof is per-job state,
        # and a second identical query waiting here finds the first one's result instead of searching again.
        self._lock = asyncio.Lock()

    @property
    def search_offered(self):
        """VECTOR can be offered to the judge: no prior episodes (empty, NOT_APPLICABLE) or a ready index."""
        return not self.episodes or self.available

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
        value = {"scope_source_refs": tuple(source.ref for source in self.episodes), "executed_methods": ("VECTOR",),
                 "vector_scope": VECTOR_SCOPE, "vector_status": status}
        if found is not None:
            # Counts may arrive as Decimal from SQL sum(); receipts are JSON (bundle digest, publication).
            value.update({key: tuple(item) if isinstance(item, (list, tuple)) else int(item)
                          for key, item in asdict(found).items()})
        return value

    async def vector(self, intent):
        """One VECTOR receipt for `intent`; a query already searched in this run is reused (new L, `reused_from`).

        Without prior episodes there is nothing to search: an empty NOT_APPLICABLE success, no lookup and no
        embedding. With episodes but no ready index the search fails with VECTOR_NOT_READY.
        """
        if not self.episodes:
            return self.mint(intent, "SUCCEEDED", readiness=self._readiness("NOT_APPLICABLE"))
        if not self.available:
            self.mint(intent, "FAILED", failure_code="VECTOR_NOT_READY", readiness=self._readiness("NOT_READY"))
            raise IncompleteInspection("VECTOR_NOT_READY")
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
        if (found is None or found.source_count != len(self.episodes)
                or found.vector_ready_sources < found.source_count or "VECTOR_INDEX_PARTIAL" in found.limitations):
            exc = IncompleteInspection("VECTOR_INDEX_PARTIAL")
            exc.readiness = self._readiness("PARTIAL", found)
            raise exc
        readiness = self._readiness("READY", found)
        proof = getattr(self.embedding_client, "last_receipt", None)
        if proof is not None:
            readiness["query_embedding"] = dict(proof)
        by_episode = {source.metadata.get("episodeId"): source for source in self.episodes}
        refs = []
        for chunk in result.chunks:
            source = by_episode.get(str(chunk.episode_id))
            if source is None:
                raise IncompleteInspection("SOURCE_OUTSIDE_MANIFEST")
            if chunk.source_hash != source.content_hash:
                raise IncompleteInspection("SOURCE_CHANGED")
            refs.append(self.register(source, chunk.start_offset, chunk.end_offset, chunk.text, "VECTOR"))
        return tuple(dict.fromkeys(refs)), bool(result.limited), readiness
