"""Map exact supporting passages back to the original, server-owned citations.

This verifies quote location and coverage, not whether a quote entails a claim.
The caller supplies the same trusted passages that the model actually received.
An evidence reference anchors one passage; it never authorizes searching a
different source for a matching sentence.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class SupportQuote(Protocol):
    evidence_ref: str
    quote: str


@dataclass(frozen=True)
class ResolvedSupport:
    """An exact source quote and the original citations covering its full span."""

    quote: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class _Reference:
    ref: str
    start: int
    end: int


@dataclass(frozen=True)
class _Passage:
    text: str
    references: tuple[_Reference, ...]


def _index_passages(passages: Sequence[Mapping[str, Any]]) -> dict[str, _Passage]:
    indexed = {}
    for raw in passages:
        if not isinstance(raw, Mapping):
            raise ValueError("GROUNDING_INVALID_PASSAGE")
        text = raw.get("text")
        references = raw.get("references")
        if not isinstance(text, str) or not isinstance(references, (list, tuple)):
            raise ValueError("GROUNDING_INVALID_PASSAGE")
        parsed = []
        for reference in references:
            if not isinstance(reference, Mapping):
                raise ValueError("GROUNDING_INVALID_PASSAGE")
            ref = reference.get("evidenceRef")
            start, end = reference.get("start"), reference.get("end")
            if (
                not isinstance(ref, str)
                or not ref.strip()
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(text)
                or ref in indexed
                or any(item.ref == ref for item in parsed)
            ):
                raise ValueError("GROUNDING_INVALID_PASSAGE")
            parsed.append(_Reference(ref, start, end))
        if not parsed:
            raise ValueError("GROUNDING_INVALID_PASSAGE")
        passage = _Passage(text, tuple(parsed))
        indexed.update((reference.ref, passage) for reference in parsed)
    return indexed


def _cover_span(passage: _Passage, start: int, end: int, anchor: str) -> list[str]:
    containing = [r for r in passage.references if r.start <= start and r.end >= end]
    if containing:
        # A quote inside the overlap needs only one citation. Preserve a valid
        # anchor; otherwise choose the narrowest original chunk, stably.
        selected = min(containing, key=lambda r: (r.ref != anchor, r.end - r.start))
        return [selected.ref]

    chosen = []
    cursor = start
    while cursor < end:
        available = [r for r in passage.references if r.start <= cursor < r.end]
        if not available:
            # Even an exact passage match is invalid if its citation spans have
            # a hole. Touching spans are allowed; uncovered whitespace is not.
            raise ValueError("GROUNDING_UNCOVERED_QUOTE")
        selected = max(available, key=lambda r: (r.end, r.ref == anchor))
        chosen.append(selected.ref)
        cursor = selected.end
    return chosen


_LINE_BREAK = re.compile(r"[ \t]*(?:\r\n|\r|\n)[ \t\r\n]*")


def _line_break_view(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Only render line breaks as spaces, retaining every original source offset.

    Horizontal whitespace elsewhere, punctuation and non-whitespace characters are
    unchanged. In particular this cannot join separate words or fix a paraphrase.
    """
    characters, spans, cursor = [], [], 0
    for match in _LINE_BREAK.finditer(text):
        characters.extend(text[cursor : match.start()])
        spans.extend((i, i + 1) for i in range(cursor, match.start()))
        characters.append(" ")
        spans.append((match.start(), match.end()))
        cursor = match.end()
    characters.extend(text[cursor:])
    spans.extend((i, i + 1) for i in range(cursor, len(text)))
    return "".join(characters), spans


def _quote_span(text: str, quote: str) -> tuple[int, int]:
    start = text.find(quote)
    if start >= 0:
        if text.find(quote, start + 1) >= 0:
            raise ValueError("GROUNDING_AMBIGUOUS_QUOTE")
        return start, start + len(quote)
    viewed, spans = _line_break_view(text)
    viewed_quote, _ = _line_break_view(quote)
    start = viewed.find(viewed_quote)
    if start < 0:
        raise ValueError("GROUNDING_QUOTE_NOT_FOUND")
    if viewed.find(viewed_quote, start + 1) >= 0:
        raise ValueError("GROUNDING_AMBIGUOUS_QUOTE")
    return spans[start][0], spans[start + len(viewed_quote) - 1][1]


def resolve_support_quotes(
    supports: Sequence[SupportQuote], passages: Sequence[Mapping[str, Any]]
) -> list[ResolvedSupport]:
    """Locate quotes inside their anchored passage and recover exact source text.

    ``supports`` accepts the schema's EvidenceSupport objects. ``passages`` is the
    existing answer payload's text/references array, with Unicode code point offsets.
    The supplied evidence_ref may identify a neighboring chunk in a merged passage:
    the exact quote determines which original chunks are actually cited. Quotes must
    occur once in that passage. A line break rendered as a space may be recovered
    only when it maps to a unique original span; the returned quote is always the
    exact original text. Equal text in another passage cannot be substituted,
    but does not invalidate an otherwise unambiguous, explicitly anchored source.

    No model-authored text, IDs, or rejected values enter exception messages. The
    returned supports retain their order. The caller must still
    revalidate the corresponding full original chunks before publishing.
    """
    if not supports:
        raise ValueError("GROUNDING_SUPPORT_REQUIRED")
    indexed = _index_passages(passages)
    resolved = []
    for support in supports:
        if support.evidence_ref not in indexed:
            raise ValueError("GROUNDING_UNKNOWN_REFERENCE")
        quote = support.quote
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("GROUNDING_QUOTE_NOT_FOUND")
        passage = indexed[support.evidence_ref]
        start, end = _quote_span(passage.text, quote)
        refs = _cover_span(passage, start, end, support.evidence_ref)
        resolved.append(ResolvedSupport(passage.text[start:end], tuple(refs)))
    return resolved


def resolve_support_references(
    supports: Sequence[SupportQuote], passages: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Return deduplicated original references; use resolve_support_quotes to store quotes."""
    return list(
        dict.fromkeys(
            ref
            for support in resolve_support_quotes(supports, passages)
            for ref in support.evidence_refs
        )
    )
