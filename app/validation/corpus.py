"""Read-only frozen corpus: indexed episode reads plus unindexed raw/book fallback."""

import asyncio
from dataclasses import asdict

from app.validation.contracts import IncompleteInspection, split_segments


class ValidationCorpus:
    def __init__(self, data, frozen, budget, *, search=None, embedding_client=None):
        self.data, self.frozen, self.budget = data, frozen, budget
        self.search, self.embedding_client = search, embedding_client
        self.sources = {source.ref: source for source in frozen.sources}
        self.evidence = {}
        self.last_readiness = None

    async def _io(self, action, *args):
        self.budget.consume("lookup")
        try:
            return await asyncio.wait_for(action(*args), self.budget.remaining())
        except TimeoutError:
            raise IncompleteInspection("BUDGET_EXHAUSTED") from None

    async def _read(self, source, start=None, end=None):
        value = await self._io(self.data.read_source, source, start, end)
        if self.last_readiness is not None:
            self.last_readiness["read_source_refs"] = tuple(
                dict.fromkeys((*self.last_readiness.get("read_source_refs", ()), source.ref))
            )
        return value

    def register(self, source, start, end, quote, method):
        if (
            source.ref not in self.sources
            or self.sources[source.ref] != source
            or not 0 <= start < end
            or len(quote) != end - start
        ):
            raise IncompleteInspection("INVALID_SOURCE_EVIDENCE")
        same = next(
            (
                ref
                for ref, value in self.evidence.items()
                if (value["source_ref"], value["start"], value["end"], value["quote"])
                == (source.ref, start, end, quote)
            ),
            None,
        )
        ref = same or f"E{max((int(key[1:]) for key in self.evidence), default=0) + 1}"
        self.evidence[ref] = {
            "ref": ref,
            "source_ref": source.ref,
            "source_kind": source.kind,
            "start": start,
            "end": end,
            "quote": quote,
            "method": method,
            "source_context": {
                key: source.metadata[key]
                for key in ("episodeNo", "revisionNo", "referenceTimeKind", "referenceEpisodeNo")
                if key in source.metadata
            },
        }
        return ref

    async def lookup(self, intent):
        limit = self.budget.limits.search_limit
        rows, limited = [], False
        episodes = [source for source in self.sources.values() if source.kind == "EPISODE"]
        self.last_readiness = {
            "scope_source_refs": tuple(self.sources),
            "read_source_refs": (),
            "executed_methods": (),
            "book_vector_index_required": False,
        }
        if intent.operation == "VECTOR":
            self.last_readiness.update(
                vector_scope="ELIGIBLE_EPISODES",
                vector_status="NOT_CHECKED" if episodes else "NOT_APPLICABLE",
                book_strategy="RAW_TEXT_AND_KEYWORD",
            )
        if intent.operation == "DIRECT":
            if intent.source_ref not in self.sources:
                raise IncompleteInspection("UNKNOWN_SOURCE_REFERENCE")
            source = self.sources[intent.source_ref]
            read = await self._read(source, intent.start, intent.end)
            rows.append((source, read.start, read.end, read.text, "DIRECT"))
        elif intent.operation in {"KEYWORD", "VECTOR"}:
            if not intent.query or not intent.query.strip() or len(intent.query) > 1000:
                raise IncompleteInspection("INVALID_SEARCH_REQUEST")
            indexed_refs = set()
            if self.search is not None and (episodes or intent.operation == "KEYWORD"):
                result, indexed_refs = await self._io(
                    self.search.search, self.frozen, intent, self.embedding_client, limit
                )
                readiness = getattr(result, "readiness", None)
                if readiness is not None:
                    self.last_readiness.update(asdict(readiness))
                if intent.operation == "VECTOR" and (
                    readiness is None
                    or readiness.source_count != len(episodes)
                    or readiness.vector_ready_sources < readiness.source_count
                    or "VECTOR_INDEX_PARTIAL" in readiness.limitations
                ):
                    self.last_readiness["vector_status"] = "PARTIAL"
                    raise IncompleteInspection("VECTOR_INDEX_PARTIAL")
                if intent.operation == "VECTOR":
                    self.last_readiness["vector_status"] = "READY"
                    proof = getattr(self.embedding_client, "last_receipt", None)
                    if proof is not None:
                        self.last_readiness["query_embedding"] = dict(proof)
                self.last_readiness["executed_methods"] = (intent.operation,)
                limited = result.limited
                by_episode = {
                    source.metadata.get("episodeId"): source for source in self.sources.values()
                }
                for chunk in result.chunks:
                    source = by_episode.get(str(chunk.episode_id))
                    if source is None:
                        raise IncompleteInspection("SOURCE_OUTSIDE_MANIFEST")
                    read = await self._read(source, chunk.start_offset, chunk.end_offset)
                    if read.text != chunk.text:
                        raise IncompleteInspection("SOURCE_CHANGED")
                    rows.append((source, read.start, read.end, read.text, intent.operation))
            elif intent.operation == "VECTOR" and episodes:
                raise IncompleteInspection("VECTOR_NOT_READY")
            if intent.operation == "KEYWORD" or any(
                source.kind == "SETTING_BOOK" for source in self.sources.values()
            ):
                # Actual raw reading is in code, never a whole-manuscript LLM scan.
                # Include prior material that has no Fact linkage and no retrieval index.
                terms = list(dict.fromkeys(intent.query.casefold().split()))
                ranked = []
                for source in self.sources.values():
                    if source.ref in indexed_refs:
                        continue
                    if intent.operation == "VECTOR" and source.kind != "SETTING_BOOK":
                        continue
                    read = await self._read(source)
                    self.last_readiness["executed_methods"] = tuple(
                        dict.fromkeys((*self.last_readiness["executed_methods"], "KEYWORD"))
                    )
                    for segment in split_segments(
                        read.text, core_chars=self.budget.limits.segment_chars, context_chars=0
                    ):
                        self.budget.remaining()
                        text = read.text[segment.start : segment.end]
                        score = sum(term in text.casefold() for term in terms)
                        if score:
                            ranked.append((score, source, segment.start, segment.end, text))
                ranked.sort(key=lambda row: (-row[0], row[1].ref, row[2]))
                limited |= len(ranked) > limit
                rows.extend(
                    (source, start, end, text, "KEYWORD")
                    for _, source, start, end, text in ranked[:limit]
                )
        elif intent.operation == "NEIGHBORS":
            if not intent.evidence_refs or any(
                ref not in self.evidence for ref in intent.evidence_refs
            ):
                raise IncompleteInspection("UNKNOWN_EVIDENCE_REFERENCE")
            for ref in intent.evidence_refs:
                seed = self.evidence[ref]
                source = self.sources[seed["source_ref"]]
                read = await self._read(source)
                start = max(0, seed["start"] - self.budget.limits.context_chars)
                end = min(read.total_code_points, seed["end"] + self.budget.limits.context_chars)
                rows.append((source, start, end, read.text[start:end], "NEIGHBORS"))
        else:
            raise IncompleteInspection("INVALID_SEARCH_OPERATION")
        refs = []
        if intent.operation in {"DIRECT", "NEIGHBORS"}:
            self.last_readiness["executed_methods"] = (intent.operation,)
        if intent.operation == "KEYWORD":
            terms = list(dict.fromkeys(intent.query.casefold().split()))
            rows.sort(
                key=lambda row: (
                    -sum(term in row[3].casefold() for term in terms),
                    row[0].ref,
                    row[1],
                )
            )
        # All candidates were authorized before ranking/limiting; model references are minted here.
        limited |= len(rows) > limit
        for source, start, end, text, method in rows[:limit]:
            refs.append(self.register(source, start, end, text, method))
        return tuple(dict.fromkeys(refs)), limited

    async def revalidate(self):
        reads = {}
        for value in self.evidence.values():
            source_ref = value["source_ref"]
            if source_ref not in reads:
                reads[source_ref] = await self._read(self.sources[source_ref])
            read = reads[source_ref]
            if read.text[value["start"] : value["end"]] != value["quote"]:
                raise IncompleteInspection("SOURCE_CHANGED")
