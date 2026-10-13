"""Provider-independent passage composition with original citation references preserved."""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from hashlib import sha256
import math

import tiktoken

from app.retrieval.search_contracts import ChunkEvidence


@dataclass(frozen=True)
class EvidencePassage:
    text: str = field(repr=False)
    start_offset: int
    end_offset: int
    chunks: tuple[ChunkEvidence, ...]
    token_count: int


@dataclass(frozen=True)
class EvidenceBundle:
    passages: tuple[EvidencePassage, ...]
    chunks: tuple[ChunkEvidence, ...]
    token_count: int
    limited: bool
    rejected_count: int = 0


def _valid(chunk):
    return (
        chunk.start_offset >= 0
        and chunk.end_offset > chunk.start_offset
        and len(chunk.text) == chunk.end_offset - chunk.start_offset
        and sha256(chunk.text.encode()).hexdigest() == chunk.chunk_hash
        and math.isfinite(chunk.score)
        and chunk.methods
        and all(m in {"DIRECT", "KEYWORD", "VECTOR", "NEIGHBOR"} for m in chunk.methods)
    )


def _rank(chunk):
    priority = 0 if "DIRECT" in chunk.methods else 2 if chunk.methods == ("NEIGHBOR",) else 1
    return priority, -chunk.score, chunk.episode_no, chunk.chunk_index, str(chunk.chunk_id)


def compose_evidence(chunks, *, max_items=12, max_tokens=6000, per_episode=4, count_tokens=None):
    """Select at most max_items ORIGINAL references, then merge exact touching slices.

    Diversity is round-robin by episode after direct refs, bounded by per_episode.
    Text is never silently cut; skipped references and invalid/contradicting slices
    make the bundle limited. max_tokens covers passage text (caller budgets prompt overhead).
    """
    if not 1 <= max_items <= 100 or not 1 <= per_episode <= max_items or max_tokens < 1:
        raise ValueError("Invalid evidence limits")
    if count_tokens is None:
        encoding = tiktoken.get_encoding("cl100k_base")

        def count_tokens(value):
            return len(encoding.encode(value, disallowed_special=()))

    unique, rejected = {}, 0
    conflicting = set()
    for chunk in chunks:
        if not _valid(chunk) or chunk.chunk_id in conflicting:
            rejected += 1
            continue
        old = unique.get(chunk.chunk_id)
        if old is not None and old.fingerprint != chunk.fingerprint:
            rejected += 2
            del unique[chunk.chunk_id]
            conflicting.add(chunk.chunk_id)
            continue
        unique[chunk.chunk_id] = (
            chunk
            if old is None
            else replace(
                old,
                methods=tuple(dict.fromkeys(old.methods + chunk.methods)),
                score=max(old.score, chunk.score),
            )
        )
    # Two retrieval routes may carry different chunk IDs for the same exact quote.
    # Distinct positions remain distinct, even when their text happens to be equal.
    quotes = {}
    for chunk in sorted(unique.values(), key=_rank):
        key = (chunk.source_fingerprint, chunk.start_offset, chunk.end_offset, chunk.chunk_hash)
        old = quotes.get(key)
        quotes[key] = (
            chunk
            if old is None
            else replace(
                old,
                methods=tuple(dict.fromkeys(old.methods + chunk.methods)),
                score=max(old.score, chunk.score),
            )
        )
    ordered = sorted(quotes.values(), key=_rank)
    direct = [c for c in ordered if "DIRECT" in c.methods]
    groups = defaultdict(list)
    for chunk in ordered:
        if "DIRECT" not in chunk.methods:
            groups[chunk.episode_id].append(chunk)
    # Each episode contributes one candidate per round, so one dense chapter cannot monopolize.
    candidates = list(direct)
    while groups:
        for episode in list(groups):
            candidates.append(groups[episode].pop(0))
            if not groups[episode]:
                del groups[episode]
    chosen, per_source = [], defaultdict(int)
    passages, total, limited = (), 0, rejected > 0
    for chunk in candidates:
        if (
            len(chosen) >= max_items
            or per_source[chunk.episode_id] >= per_episode
            or len(chunk.text) > 4000
        ):
            limited = True
            continue
        try:
            proposed = _merge_passages([*chosen, chunk], count_tokens)
        except ValueError:
            rejected += 1
            limited = True
            continue
        tokens = sum(p.token_count for p in proposed)
        if tokens > max_tokens:
            limited = True
            continue
        passages, total = proposed, tokens
        chosen.append(chunk)
        per_source[chunk.episode_id] += 1
    return EvidenceBundle(tuple(passages), tuple(chosen), total, limited, rejected)


def _merge_passages(chunks, count_tokens):
    grouped = defaultdict(list)
    for chunk in chunks:
        grouped[chunk.source_fingerprint].append(chunk)
    passages = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda c: (c.start_offset, c.end_offset, str(c.chunk_id)))
        current = None
        for chunk in ordered:
            if current is None or chunk.start_offset > current.end_offset:
                if current is not None:
                    passages.append(current)
                current = EvidencePassage(
                    chunk.text,
                    chunk.start_offset,
                    chunk.end_offset,
                    (chunk,),
                    count_tokens(chunk.text),
                )
                continue
            overlap_end = min(current.end_offset, chunk.end_offset)
            overlap = overlap_end - chunk.start_offset
            if (
                current.text[
                    chunk.start_offset - current.start_offset : overlap_end - current.start_offset
                ]
                != chunk.text[:overlap]
            ):
                raise ValueError("Conflicting source slices")
            merged = current.text + chunk.text[overlap:]
            current = EvidencePassage(
                merged,
                current.start_offset,
                max(current.end_offset, chunk.end_offset),
                current.chunks + (chunk,),
                count_tokens(merged),
            )
        if current is not None:
            passages.append(current)
    return tuple(passages)
