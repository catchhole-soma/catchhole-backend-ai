"""One independent comparison call over a frozen episode relationship context.

Only request-local references enter the prompt. The backend remains responsible
for ownership, optimistic concurrency, idempotency, and durable episode history.
"""

import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.relationship_extractor import DEFAULT_RELATIONSHIP_MODEL, _unique_object
from app.analysis.relationship_observations import RelationshipInputTooLargeError
from app.llm.protocols import LlmCallOptions, LlmResponseSchema, TextGenerationClient
from app.usage.metering import estimate_text_input_tokens

RELATIONSHIP_COMPARISON_VERSION = "relationship-comparison:v1"
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "relationship_comparison.md"
Operation = Literal["ADD", "MERGE", "UPDATE", "END", "EXCLUDE", "HOLD"]
Axis = Literal["SOCIAL", "ATTITUDE", "INTERACTION"]
Kind = Literal[
    "FRIENDSHIP", "COMPANIONSHIP", "COOPERATION", "MENTORSHIP", "ROMANCE",
    "HOSTILITY", "TRUST", "DISTRUST", "AFFECTION", "ACQUAINTANCE", "OTHER",
]
Direction = Literal["DIRECTED", "MUTUAL", "UNDIRECTED"]
TemporalScope = Literal["PRESENT", "PAST", "HYPOTHETICAL", "UNKNOWN"]
NodeRef = Annotated[str, StringConstraints(pattern=r"^N[1-9][0-9]*$", max_length=12)]
RelationshipRef = Annotated[str, StringConstraints(pattern=r"^R[1-9][0-9]*$", max_length=12)]
ObservationRef = Annotated[str, StringConstraints(pattern=r"^[OH][1-9][0-9]*$", max_length=12)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
MAX_OBSERVATIONS = 384
_MUTATIONS = frozenset({"ADD", "MERGE", "UPDATE", "END"})
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
_LOCAL_REF = re.compile(r"(?<![A-Za-z0-9_])[NOHR][1-9][0-9]*(?![A-Za-z0-9_])")


class RelationshipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observationRefs: list[ObservationRef] = Field(min_length=1, max_length=MAX_OBSERVATIONS * 2)
    operation: Operation
    relationshipRef: RelationshipRef | None
    sourceNodeRef: NodeRef | None
    targetNodeRef: NodeRef | None
    axis: Axis | None
    kind: Kind | None
    label: Label | None
    summary: Description | None
    direction: Direction | None
    temporalScope: TemporalScope
    reason: Description | None


class RelationshipComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decisions: list[RelationshipDecision] = Field(max_length=512)


RELATIONSHIP_COMPARISON_RESPONSE_SCHEMA = LlmResponseSchema(
    name="relationship_comparison", schema=RelationshipComparisonResponse.model_json_schema(), strict=True,
)


def _invalid_input():
    raise OrderedInputContextError("Invalid frozen relationship context.")


def _ref(value, prefix):
    if not isinstance(value, str) or re.fullmatch(rf"{prefix}[1-9][0-9]*", value) is None or len(value) > 12:
        _invalid_input()
    return value


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _invalid_input()
    return value


def _rows(value, maximum):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(row, dict) for row in value):
        _invalid_input()
    return value


def _episode(value, cutoff):
    if type(value) is not int or not 1 <= value <= cutoff:
        _invalid_input()
    return value


def _slot(row):
    source, target = row["sourceNodeRef"], row["targetNodeRef"]
    if row["direction"] != "DIRECTED":
        source, target = sorted((source, target))
    return source, target, row["axis"], row["kind"], row["direction"]


def _relationship_comparison_prompt_data(context: dict) -> dict:
    """Whitelist public context fields; never pass contextToken/storage IDs to LLM."""
    if not isinstance(context, dict) or type(context.get("formatVersion")) is not int or context["formatVersion"] != 1:
        _invalid_input()
    cutoff = context.get("sourceEpisodeNo")
    if type(cutoff) is not int or cutoff < 1:
        _invalid_input()
    node_rows = _rows(context.get("nodes"), MAX_OBSERVATIONS * 4)
    nodes = []
    for row in node_rows:
        if type(row.get("profileRegistered")) is not bool:
            _invalid_input()
        nodes.append({"ref": _ref(row.get("ref"), "N"), "name": _text(row.get("name"), 100),
                      "profileRegistered": row["profileRegistered"]})
    node_refs = {row["ref"] for row in nodes}
    if len(node_refs) != len(nodes):
        _invalid_input()
    relationships = []
    for row in _rows(context.get("relationships"), MAX_OBSERVATIONS * 4):
        item = {key: row.get(key) for key in (
            "ref", "sourceNodeRef", "targetNodeRef", "axis", "kind", "label", "summary", "direction", "status",
            "knownFromEpisodeNo", "lastChangedEpisodeNo",
        )}
        _ref(item["ref"], "R")
        if (item["sourceNodeRef"] not in node_refs or item["targetNodeRef"] not in node_refs
                or item["sourceNodeRef"] == item["targetNodeRef"] or item["status"] not in {"ACTIVE", "ENDED"}):
            _invalid_input()
        # Reuse strict decision field validation without interpreting the state as a new decision.
        try:
            RelationshipDecision.model_validate({
                "observationRefs": ["O1"], "operation": "ADD", "relationshipRef": None,
                **{key: item[key] for key in ("sourceNodeRef", "targetNodeRef", "axis", "kind", "label", "summary", "direction")},
                "temporalScope": "PRESENT", "reason": None,
            })
        except ValueError:
            _invalid_input()
        if any(item[key] is None for key in ("axis", "kind", "label", "summary", "direction")):
            _invalid_input()
        first = _episode(item["knownFromEpisodeNo"], cutoff)
        last = _episode(item["lastChangedEpisodeNo"], cutoff)
        if last < first:
            _invalid_input()
        relationships.append(item)
    if (len({row["ref"] for row in relationships}) != len(relationships)
            or len({_slot(row) for row in relationships}) != len(relationships)):
        _invalid_input()

    def observations(key, prefix):
        items = []
        for row in _rows(context.get(key, []), MAX_OBSERVATIONS):
            item = {"ref": _ref(row.get("ref"), prefix), "summary": _text(row.get("summary"), 300)}
            for side in ("source", "target"):
                endpoint = row.get(side)
                if not isinstance(endpoint, dict):
                    _invalid_input()
                name = endpoint.get("name")
                item[side] = {"name": None if name is None else _text(name, 100),
                              "mention": _text(endpoint.get("mention"), 100)}
                refs = row.get(f"{side}NodeRefs")
                if not isinstance(refs, list) or any(ref not in node_refs for ref in refs) or len(refs) != len(set(refs)):
                    _invalid_input()
                item[f"{side}NodeRefs"] = refs
            evidence = []
            for span in _rows(row.get("evidence"), 12):
                quote = _text(span.get("quote"), 480)
                start, end = span.get("startOffset"), span.get("endOffset")
                if type(start) is not int or type(end) is not int or start < 0 or end <= start or end - start != len(quote):
                    _invalid_input()
                evidence.append({"quote": quote, "startOffset": start, "endOffset": end})
            if not evidence or sum(len(span["quote"]) for span in evidence) > 4000:
                _invalid_input()
            if any(left["endOffset"] > right["startOffset"] for left, right in pairwise(evidence)):
                _invalid_input()
            item["evidence"] = evidence
            # Held observations carry their original chapter, never become today's text.
            if prefix == "H":
                item["sourceEpisodeNo"] = _episode(row.get("sourceEpisodeNo"), cutoff)
            items.append(item)
        if len({row["ref"] for row in items}) != len(items):
            _invalid_input()
        return items

    return {"sourceEpisodeNo": cutoff, "nodes": nodes, "relationships": relationships,
            "observations": observations("observations", "O"),
            "heldObservations": observations("heldObservations", "H")}


def relationship_comparison_prompt_data(context: dict) -> dict:
    """Reject malformed envelopes without echoing source or backend values."""
    try:
        return _relationship_comparison_prompt_data(context)
    except (ValueError, TypeError, KeyError):
        raise OrderedInputContextError("Invalid frozen relationship context.") from None


def validate_relationship_decisions(result: RelationshipComparisonResponse, context: dict) -> list[dict]:
    """Validate coverage, identities, chronology and existing slots before any write."""
    new = context["observations"]
    obs = {row["ref"]: row for row in [*new, *context["heldObservations"]]}
    expected = {row["ref"] for row in new}
    order = {ref: index for index, ref in enumerate(obs)}
    relationships = {row["ref"]: row for row in context["relationships"]}
    slots = {_slot(row) for row in relationships.values()}
    consumed, changed, used_targets = {}, set(), set()
    validated = []
    for decision in result.decisions:
        row = decision.model_dump()
        refs = row["observationRefs"]
        if any(ref not in obs for ref in refs) or len(set(refs)) != len(refs):
            raise ValueError("Invalid observation coverage")
        indexes = [order[ref] for ref in refs]
        new_indexes = [order[ref] for ref in refs if ref in expected]
        if indexes != sorted(indexes) or not new_indexes:
            raise ValueError("Decisions require ordered references and new evidence")
        mutates = row["operation"] in _MUTATIONS
        for ref in refs:
            if ref in consumed and not (consumed[ref] and mutates):
                raise ValueError("An observation cannot be both held/excluded and applied")
            consumed[ref] = mutates
        for key in ("label", "summary", "reason"):
            if row[key] is not None and (_UUID.search(row[key]) or _LOCAL_REF.search(row[key])):
                raise ValueError("Display text contains internal references")
        target_fields = ("relationshipRef", "sourceNodeRef", "targetNodeRef", "axis", "kind", "label", "summary", "direction")
        if row["operation"] not in _MUTATIONS:
            if any(row[key] is not None for key in target_fields) or not row["reason"]:
                raise ValueError("Hold/exclude must not propose a graph mutation")
            validated.append(row)
            continue
        if row["temporalScope"] != "PRESENT":
            raise ValueError("Non-present observation cannot mutate current state")
        if any(row[key] is None for key in target_fields[1:]):
            raise ValueError("Mutation is missing relationship content")
        pair = (row["sourceNodeRef"], row["targetNodeRef"])
        if pair[0] == pair[1]:
            raise ValueError("Self relationship is invalid")
        for ref in refs:
            source, target = obs[ref]["sourceNodeRefs"], obs[ref]["targetNodeRefs"]
            if len(source) != 1 or len(target) != 1 or pair not in {(source[0], target[0]), (target[0], source[0])}:
                raise ValueError("Relationship endpoints are not unambiguously grounded")
        slot = _slot(row)
        if slot in changed:
            raise ValueError("A relationship slot must have one grouped decision")
        changed.add(slot)
        if row["operation"] == "ADD":
            if row["relationshipRef"] is not None or slot in slots:
                raise ValueError("ADD must target a new relationship slot")
        else:
            target = relationships.get(row["relationshipRef"])
            if (target is None or _slot(target) != slot
                    or (target["sourceNodeRef"], target["targetNodeRef"]) != pair):
                raise ValueError("Existing relationship reference does not match")
            if row["relationshipRef"] in used_targets:
                raise ValueError("An existing relationship cannot be changed twice")
            used_targets.add(row["relationshipRef"])
            if row["operation"] in {"MERGE", "END"} and target["status"] != "ACTIVE":
                raise ValueError("MERGE/END require an active relationship")
            if row["operation"] == "MERGE" and (row["label"], row["summary"]) != (target["label"], target["summary"]):
                raise ValueError("MERGE only adds evidence and must preserve current content")
        validated.append(row)
    if set(consumed).intersection(expected) != expected:
        raise ValueError("Every new observation must be covered")
    return validated


class RelationshipComparator:
    """One explicit Terra/medium request, no repair calls and no silent context trimming."""

    def __init__(self, llm_client: TextGenerationClient, *, model: str = DEFAULT_RELATIONSHIP_MODEL,
                 max_output_tokens: int = 16000, max_input_tokens: int = 64000,
                 prompt_path: Path = DEFAULT_PROMPT_PATH):
        if (not isinstance(model, str) or not model.strip() or type(max_output_tokens) is not int
                or not 1 <= max_output_tokens <= 128000 or type(max_input_tokens) is not int or max_input_tokens < 1):
            raise ValueError("Invalid relationship comparison configuration")
        self.llm_client, self.model = llm_client, model
        self.max_output_tokens, self.max_input_tokens = max_output_tokens, max_input_tokens
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    async def compare(self, context: dict) -> list[dict]:
        payload = relationship_comparison_prompt_data(context)
        if not payload["observations"]:
            return []
        user_prompt = json.dumps(payload, ensure_ascii=False)
        if estimate_text_input_tokens(self.system_prompt, user_prompt, self.model,
                                      RELATIONSHIP_COMPARISON_RESPONSE_SCHEMA) > self.max_input_tokens:
            raise RelationshipInputTooLargeError("Relationship comparison input exceeds its token budget.")
        response = await self.llm_client.create_text_response(
            system_prompt=self.system_prompt, user_prompt=user_prompt, model=self.model,
            max_output_tokens=self.max_output_tokens, prompt_cache_key=RELATIONSHIP_COMPARISON_VERSION,
            response_schema=RELATIONSHIP_COMPARISON_RESPONSE_SCHEMA,
            call_options=LlmCallOptions(reasoning_effort="medium"),
        )
        try:
            raw = json.loads(response.text, object_pairs_hook=_unique_object)
            result = RelationshipComparisonResponse.model_validate(raw)
            return validate_relationship_decisions(result, payload)
        except (ValueError, TypeError):
            raise ComparisonValidationError("Invalid relationship comparison response.") from None
