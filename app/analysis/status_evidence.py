"""Resolve STATUS evidence references to immutable slices of the current chunk."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.analysis.schemas import character_setting_provider_json_schema
from app.chunking.chunk_splitter import split_paragraphs
from app.llm.protocols import LlmResponseSchema

# Keep closing quotation marks/brackets with the preceding sentence. A punctuation
# mark inside a number or word is not a boundary unless whitespace follows it.
_SENTENCE_END = re.compile(r"""[.!?。？！]+[\"'”’»）)\]}」』】]*(?=\s|$)""")
_REFERENCE_DEFINITION = "_StatusEvidenceReference"


class StatusEvidenceReferenceError(ValueError):
    """A codec failure containing only a fixed reason code and safe field paths."""

    def __init__(self, reason_code: str, field_locs: tuple[str, ...]) -> None:
        self.reason_code = reason_code
        self.field_locs = field_locs
        super().__init__(reason_code)


@dataclass(frozen=True)
class StatusEvidenceUnit:
    ref: str
    text: str = field(repr=False)
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class StatusEvidenceDocument:
    """Request-local evidence vocabulary; all offsets are relative to this chunk.

    Each nonblank source line is divided at explicit sentence boundaries. Text is
    sliced from the original chunk without newline or whitespace normalization.
    Several transitions inside one sentence still share an evidence unit.
    """

    chunk_text: str = field(repr=False)
    units: tuple[StatusEvidenceUnit, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.chunk_text, str):
            raise StatusEvidenceReferenceError(
                "STATUS_EVIDENCE_INVALID_DOCUMENT", ("chunk_text",)
            )
        units: list[StatusEvidenceUnit] = []
        for paragraph in split_paragraphs(self.chunk_text):
            start = 0
            boundaries = [match.end() for match in _SENTENCE_END.finditer(paragraph.text)]
            if not boundaries or boundaries[-1] != len(paragraph.text):
                boundaries.append(len(paragraph.text))
            for end in boundaries:
                # Exclude only whitespace between units; keep every character
                # inside the chosen source interval, including repeated spaces.
                left, right = start, end
                while left < right and paragraph.text[left].isspace():
                    left += 1
                while right > left and paragraph.text[right - 1].isspace():
                    right -= 1
                if left < right:
                    source_start = paragraph.start_offset + left
                    source_end = paragraph.start_offset + right
                    units.append(StatusEvidenceUnit(
                        ref=f"E{len(units) + 1}",
                        text=self.chunk_text[source_start:source_end],
                        start_offset=source_start,
                        end_offset=source_end,
                    ))
                start = end
        object.__setattr__(self, "units", tuple(units))

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(unit.ref for unit in self.units)

    def render(self) -> str:
        """Render data, never source-derived instructions or backend identifiers."""
        return json.dumps(
            [{"ref": unit.ref, "text": unit.text} for unit in self.units],
            ensure_ascii=False,
            indent=2,
        )

    @property
    def response_schema(self) -> LlmResponseSchema:
        # An empty document must be skipped by the caller before a provider call;
        # do not manufacture an invalid empty enum or an invented evidence ref.
        if not self.units:
            raise StatusEvidenceReferenceError(
                "STATUS_EVIDENCE_EMPTY_DOCUMENT", ("evidence_refs",)
            )
        schema = deepcopy(character_setting_provider_json_schema())
        _replace_evidence_schema(schema)
        # Share the request-local vocabulary across all six candidate variants.
        # Repeating a large enum in each variant needlessly consumes schema limits.
        schema.setdefault("$defs", {})[_REFERENCE_DEFINITION] = {
            "type": "string",
            "enum": list(self.references),
        }
        # The quote/offset wire definition is no longer reachable from this schema.
        schema.get("$defs", {}).pop("_ProviderEvidenceSpan", None)
        return LlmResponseSchema(
            name="character_status_extraction",
            schema=schema,
            strict=True,
        )

    def resolve_payload(self, payload: object) -> dict[str, Any]:
        """Replace only evidence_refs; remaining fields use the existing validator.

        Resolution is atomic and does not modify the provider payload, including
        when a later candidate has an invalid reference. Reference order is kept.
        """
        if not isinstance(payload, dict) or set(payload) != {"candidates"}:
            raise StatusEvidenceReferenceError(
                "STATUS_EVIDENCE_INVALID_PAYLOAD", ("candidates",)
            )
        candidates = payload["candidates"]
        if not isinstance(candidates, list):
            raise StatusEvidenceReferenceError(
                "STATUS_EVIDENCE_INVALID_PAYLOAD", ("candidates",)
            )
        by_ref = {unit.ref: unit for unit in self.units}
        resolved: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(candidates):
            candidate_loc = f"candidates.{candidate_index}"
            refs_loc = f"{candidate_loc}.evidence_refs"
            if not isinstance(candidate, dict):
                raise StatusEvidenceReferenceError(
                    "STATUS_EVIDENCE_INVALID_PAYLOAD", (candidate_loc,)
                )
            if "evidence_spans" in candidate:
                raise StatusEvidenceReferenceError(
                    "STATUS_EVIDENCE_INVALID_PAYLOAD", (f"{candidate_loc}.evidence_spans",)
                )
            refs = candidate.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                raise StatusEvidenceReferenceError(
                    "STATUS_EVIDENCE_INVALID_REFS", (refs_loc,)
                )
            spans: list[dict[str, Any]] = []
            seen_refs: set[str] = set()
            for ref_index, ref in enumerate(refs):
                ref_loc = f"{refs_loc}.{ref_index}"
                if not isinstance(ref, str):
                    raise StatusEvidenceReferenceError(
                        "STATUS_EVIDENCE_INVALID_REFS", (ref_loc,)
                    )
                if ref not in by_ref:
                    raise StatusEvidenceReferenceError(
                        "STATUS_EVIDENCE_UNKNOWN_REF", (ref_loc,)
                    )
                if ref in seen_refs:
                    raise StatusEvidenceReferenceError(
                        "STATUS_EVIDENCE_DUPLICATE_REF", (ref_loc,)
                    )
                seen_refs.add(ref)
                unit = by_ref[ref]
                spans.append({
                    "quote": unit.text,
                    "start_offset": unit.start_offset,
                    "end_offset": unit.end_offset,
                })
            result = deepcopy(candidate)
            del result["evidence_refs"]
            result["evidence_spans"] = spans
            resolved.append(result)
        return {"candidates": resolved}


def _replace_evidence_schema(node: object) -> None:
    if isinstance(node, list):
        for child in node:
            _replace_evidence_schema(child)
        return
    if not isinstance(node, dict):
        return
    properties = node.get("properties")
    if isinstance(properties, dict) and "evidence_spans" in properties:
        del properties["evidence_spans"]
        properties["evidence_refs"] = {
            "type": "array",
            "items": {"$ref": f"#/$defs/{_REFERENCE_DEFINITION}"},
            "minItems": 1,
        }
        node["required"] = [
            "evidence_refs" if name == "evidence_spans" else name
            for name in node["required"]
        ]
    for child in node.values():
        _replace_evidence_schema(child)
