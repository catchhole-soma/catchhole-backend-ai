"""Source-grounded observations from the independent relationship call.

The model selects numbered source spans. Only the server copies source text and
computes Unicode offsets. Grounding does not establish identity or truth.
"""

import re
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.analysis.exceptions import ComparisonValidationError
from app.llm.responses import LlmTextResponse


class RelationshipInputTooLargeError(ComparisonValidationError):
    """Complete relationship input exceeds a fixed budget; never truncate it."""


RELATIONSHIP_OBSERVATION_VERSION = "relationship-source-spans:v1"
MAX_RELATIONSHIP_OBSERVATIONS = 32
MAX_RELATIONSHIP_SOURCE_SPAN_CHARS = 480
MAX_RELATIONSHIP_EVIDENCE_SPANS = 12
MAX_RELATIONSHIP_EVIDENCE_CHARS = 4000
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
SourceSpanRef = Annotated[str, StringConstraints(pattern=r"^S[1-9][0-9]*$", max_length=12)]


class RelationshipSourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    ref: SourceSpanRef
    text: str
    start_offset: int
    end_offset: int


def relationship_source_spans(text: str) -> list[RelationshipSourceSpan]:
    """Partition all source characters without stripping or Unicode normalization.

    Prefer newline boundaries, then sentence/word boundaries for unusually long
    lines. A hard code-point boundary handles unbroken text. Every span is bounded
    and every original character belongs to exactly one stable request-local ref.
    """
    spans = []
    start = 0
    while start < len(text):
        cap = min(start + MAX_RELATIONSHIP_SOURCE_SPAN_CHARS, len(text))
        newline = text.find("\n", start, cap)
        if newline >= 0:
            end = newline + 1
        elif cap == len(text):
            end = cap
        else:
            boundaries = list(re.finditer(r"[.!?。！？][\"'’”)]*\s+|\s+", text[start:cap]))
            end = start + boundaries[-1].end() if boundaries else cap
        spans.append(RelationshipSourceSpan(
            ref=f"S{len(spans) + 1}", text=text[start:end], start_offset=start, end_offset=end,
        ))
        start = end
    return spans


class RelationshipEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: ShortText | None
    mention: ShortText


class RelationshipObservationWire(BaseModel):
    """The endpoints are participants; order alone does not imply direction."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: RelationshipEndpoint
    target: RelationshipEndpoint
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    evidence_span_ids: list[SourceSpanRef] = Field(
        min_length=1, max_length=MAX_RELATIONSHIP_EVIDENCE_SPANS,
    )


class RelationshipProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observations: list[RelationshipObservationWire] = Field(max_length=MAX_RELATIONSHIP_OBSERVATIONS)


class LocatedRelationshipEvidence(BaseModel):
    quote: str
    start_offset: int
    end_offset: int


class RelationshipObservation(RelationshipObservationWire):
    source_chunk_id: UUID
    evidence_quotes: list[str]
    evidence_spans: list[LocatedRelationshipEvidence]


class RelationshipObservationRejection(BaseModel):
    index: int | None
    reason: Literal[
        "MISSING_ARRAY", "INVALID_ARRAY", "LIMIT_EXCEEDED", "INVALID_ITEM",
        "SELF_OR_AMBIGUOUS_ENDPOINTS", "UNKNOWN_SOURCE_SPAN", "INVALID_SPAN_ORDER",
        "EVIDENCE_LIMIT_EXCEEDED", "EMPTY_SOURCE_SPAN", "ENDPOINT_NOT_IN_EVIDENCE", "NAME_NOT_IN_EVIDENCE", "DUPLICATE",
    ]


@dataclass(frozen=True)
class RelationshipExtractionResult:
    source_chunk_id: UUID
    raw_observations: list
    observations: list[RelationshipObservation]
    rejections: list[RelationshipObservationRejection]
    response: LlmTextResponse


_KOREAN_PARTICLES = frozenset({
    "은", "는", "이", "가", "을", "를", "의", "에", "에게", "한테", "께",
    "와", "과", "도", "만", "보다", "처럼", "으로", "로", "에서", "에게서",
    "한테서", "부터", "까지", "에게는", "에게도", "에서는", "에서도", "라고", "이라",
    "이라고", "라는", "이라는", "랑", "이랑", "야", "아", "이야", "이여",
    "이다", "입니다", "이었다", "였다", "이라고는", "라고는",
})


def _has_endpoint_mention(mention: str, span: LocatedRelationshipEvidence, text: str) -> bool:
    """Reject word substrings, permitting exact Korean particle suffixes."""
    for match in re.finditer(re.escape(mention), span.quote):
        start = span.start_offset + match.start()
        end = span.start_offset + match.end()
        if start and (text[start - 1].isalnum() or text[start - 1] == "_"):
            continue
        suffix = re.match(r"\w*", text[end:]).group()
        if not suffix or (suffix in _KOREAN_PARTICLES and end + len(suffix) <= span.end_offset):
            return True
    return False


def ground_relationship_observations(
    raw: object, chunk_text: str, source_chunk_id: UUID,
) -> tuple[list[RelationshipObservation], list[RelationshipObservationRejection]]:
    """Reject defective items independently and reconstruct exact selected evidence."""
    observations = []
    rejected = []
    spans_by_ref = {span.ref: span for span in relationship_source_spans(chunk_text)}

    def reject(index, reason):
        rejected.append(RelationshipObservationRejection(index=index, reason=reason))

    if not isinstance(raw, list):
        reject(None, "MISSING_ARRAY" if raw is None else "INVALID_ARRAY")
        return observations, rejected
    seen = set()
    for index, item in enumerate(raw):
        if index >= MAX_RELATIONSHIP_OBSERVATIONS:
            reject(index, "LIMIT_EXCEEDED")
            continue
        try:
            value = RelationshipObservationWire.model_validate(item)
        except ValidationError:
            reject(index, "INVALID_ITEM")
            continue
        if (value.source.mention == value.target.mention
                or (value.source.name and value.source.name == value.target.name)):
            reject(index, "SELF_OR_AMBIGUOUS_ENDPOINTS")
            continue
        selected = [spans_by_ref.get(ref) for ref in value.evidence_span_ids]
        if any(span is None for span in selected):
            reject(index, "UNKNOWN_SOURCE_SPAN")
            continue
        if any(not span.text.strip() for span in selected):
            reject(index, "EMPTY_SOURCE_SPAN")
            continue
        offsets = [span.start_offset for span in selected]
        if offsets != sorted(set(offsets)):
            reject(index, "INVALID_SPAN_ORDER")
            continue
        if sum(len(span.text) for span in selected) > MAX_RELATIONSHIP_EVIDENCE_CHARS:
            reject(index, "EVIDENCE_LIMIT_EXCEEDED")
            continue
        spans = [LocatedRelationshipEvidence(
            quote=span.text, start_offset=span.start_offset, end_offset=span.end_offset,
        ) for span in selected]
        if not all(any(_has_endpoint_mention(endpoint.mention, span, chunk_text) for span in spans)
                   for endpoint in (value.source, value.target)):
            reject(index, "ENDPOINT_NOT_IN_EVIDENCE")
            continue
        if not all(endpoint.name is None or any(
            _has_endpoint_mention(endpoint.name, span, chunk_text) for span in spans
        ) for endpoint in (value.source, value.target)):
            reject(index, "NAME_NOT_IN_EVIDENCE")
            continue
        fingerprint = value.model_dump_json()
        if fingerprint in seen:
            reject(index, "DUPLICATE")
            continue
        seen.add(fingerprint)
        observations.append(RelationshipObservation(
            **value.model_dump(), source_chunk_id=source_chunk_id,
            evidence_quotes=[span.quote for span in spans], evidence_spans=spans,
        ))
    return observations, rejected
