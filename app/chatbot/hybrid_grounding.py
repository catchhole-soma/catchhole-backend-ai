"""Map exact supporting passages back to the original, server-owned citations.

This verifies quote location and coverage, not whether a quote entails a claim.
The caller supplies the same trusted passages that the model actually received.
An evidence reference anchors one passage; it never authorizes searching a
different source for a matching sentence.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class SupportQuote(Protocol):
    evidence_ref: str
    quote: str


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


def resolve_support_references(
    supports: Sequence[SupportQuote], passages: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Resolve support quotes to the fewest original refs covering every character.

    ``supports`` accepts the schema's EvidenceSupport objects. ``passages`` is the
    existing answer payload's text/references array, with Unicode code point offsets.
    The supplied evidence_ref may identify a neighboring chunk in a merged passage:
    the exact quote determines which original chunks are actually cited. Quotes must
    occur once in that passage. Equal text in another passage cannot be substituted,
    but does not invalidate an otherwise unambiguous, explicitly anchored source.

    No model-authored text, IDs, or rejected values enter exception messages. The
    returned refs retain support order and are deduplicated. The caller must still
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
        start = passage.text.find(quote)
        if start < 0:
            raise ValueError("GROUNDING_QUOTE_NOT_FOUND")
        if passage.text.find(quote, start + 1) >= 0:
            raise ValueError("GROUNDING_AMBIGUOUS_QUOTE")
        refs = _cover_span(passage, start, start + len(quote), support.evidence_ref)
        resolved.extend(ref for ref in refs if ref not in resolved)
    return resolved
