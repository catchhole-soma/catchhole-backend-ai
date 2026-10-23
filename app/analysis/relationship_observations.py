"""Experimental, source-grounded observations; never confirmed character facts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

RELATIONSHIP_OBSERVATION_VERSION = "relationship-observations:v1"
MAX_RELATIONSHIP_OBSERVATIONS = 12
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class RelationshipEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: ShortText | None
    mention: ShortText


class RelationshipObservationWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: RelationshipEndpoint
    target: RelationshipEndpoint
    kind: Literal["FRIEND", "COMPANION", "ALLY", "HOSTILE", "MENTOR", "FAMILY", "ROMANTIC", "OTHER"]
    direction: Literal["SOURCE_TO_TARGET", "MUTUAL", "UNKNOWN"]
    basis: Literal["EXPLICIT", "CONTEXTUAL", "UNCERTAIN", "NEGATED", "FIGURATIVE"]
    temporal_scope: Literal["PRESENT", "PAST", "HYPOTHETICAL", "UNKNOWN"]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    evidence_quotes: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        min_length=1, max_length=3,
    )


class LocatedRelationshipEvidence(BaseModel):
    quote: str
    start_offset: int
    end_offset: int


class RelationshipObservation(RelationshipObservationWire):
    source_chunk_id: UUID
    evidence_spans: list[LocatedRelationshipEvidence]


class RelationshipObservationRejection(BaseModel):
    index: int | None
    reason: Literal[
        "MISSING_ARRAY", "INVALID_ARRAY", "LIMIT_EXCEEDED", "INVALID_ITEM",
        "SELF_OR_AMBIGUOUS_ENDPOINTS", "EVIDENCE_NOT_FOUND", "EVIDENCE_NOT_UNIQUE",
        "ENDPOINT_NOT_IN_EVIDENCE", "DUPLICATE",
    ]


def ground_relationship_observations(
    raw: object, chunk_text: str, source_chunk_id: UUID,
) -> tuple[list[RelationshipObservation], list[RelationshipObservationRejection]]:
    """Isolate observation defects without retrying or discarding valid fact candidates.

    This validates source location, not truth, identity or mutual friendship. All accepted
    items remain unconfirmed observations. Shared JSON/provider failures still affect extraction.
    """
    observations = []
    rejected = []

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
        spans = []
        for quote in value.evidence_quotes:
            start = chunk_text.find(quote)
            if start < 0:
                reject(index, "EVIDENCE_NOT_FOUND")
                break
            if chunk_text.find(quote, start + 1) >= 0:
                reject(index, "EVIDENCE_NOT_UNIQUE")
                break
            spans.append(LocatedRelationshipEvidence(
                quote=quote, start_offset=start, end_offset=start + len(quote),
            ))
        else:
            if not all(any(endpoint.mention in span.quote for span in spans)
                       for endpoint in (value.source, value.target)):
                reject(index, "ENDPOINT_NOT_IN_EVIDENCE")
                continue
            fingerprint = value.model_dump_json()
            if fingerprint in seen:
                reject(index, "DUPLICATE")
                continue
            seen.add(fingerprint)
            observations.append(RelationshipObservation(
                **value.model_dump(), source_chunk_id=source_chunk_id, evidence_spans=spans,
            ))
    return observations, rejected
