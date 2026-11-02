"""Pair-scoped generation with bounded repair and explicit incomplete coverage.

The additive Spring capability, not wire formatVersion, enables this path. No
model exception body, manuscript, or rejected response enters failure metadata.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.relationship_comparator import (
    MAX_OBSERVATIONS,
    Axis,
    Description,
    Kind,
    Label,
    ObservationRef,
    Operation,
    RelationshipComparator,
    RelationshipRef,
    TemporalScope,
    _comparison_error,
    _invalid_input,
    _project_comparison,
    relationship_comparison_prompt_data,
)
from app.analysis.relationship_extractor import _unique_object
from app.llm.protocols import LlmCallOptions, LlmResponseSchema
from app.schemas.worker import FAILURE_DIAGNOSTIC_REASONS
from app.usage.metering import estimate_text_input_tokens

VERSION = "relationship-comparison:pair-groups-v2"
PROMPT = Path(__file__).resolve().parents[1] / "llm/prompts/relationship_comparison_grouped.md"
MAX_BATCHES = 16  # Each initial batch has at most one repair request: 32 total.
MAX_GROUPS_PER_BATCH = 24  # 384 independent pairs still fit the 16-batch ceiling.
PLANNED_OUTPUT_TOKENS_PER_GROUP = 512
FAILURE_REASONS = frozenset(
    reason for reason in FAILURE_DIAGNOSTIC_REASONS
    if reason.startswith("RELATIONSHIP_") and reason != "RELATIONSHIP_CONTEXT_INVALID"
)
GroupRef = Annotated[str, StringConstraints(pattern=r"^G[1-9][0-9]*$", max_length=12)]


class GroupDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observationRefs: list[ObservationRef] = Field(min_length=1, max_length=MAX_OBSERVATIONS * 2)
    operation: Operation
    relationshipRef: RelationshipRef | None
    axis: Axis | None
    kind: Kind | None
    label: Label | None
    summary: Description | None
    direction: Literal["LEFT_TO_RIGHT", "RIGHT_TO_LEFT", "MUTUAL", "UNDIRECTED"] | None
    temporalScope: TemporalScope
    reason: Description | None


class GroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groupRef: GroupRef
    decisions: list[GroupDecision] = Field(max_length=512)


class GroupedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[GroupResponse] = Field(max_length=MAX_OBSERVATIONS)


RESPONSE_SCHEMA = LlmResponseSchema(
    name="relationship_pair_groups", schema=GroupedResponse.model_json_schema(), strict=True,
)


@dataclass(frozen=True)
class RelationshipComparisonOutcome:
    decisions: list[dict]
    failures: list[dict]


def _pair(observation):
    source, target = observation["sourceNodeRefs"], observation["targetNodeRefs"]
    if len(source) == len(target) == 1 and source != target:
        return tuple(sorted((source[0], target[0])))
    return None


def _hold(observation):
    return {
        "observationRefs": [observation["ref"]], "operation": "HOLD",
        "relationshipRef": None, "sourceNodeRef": None, "targetNodeRef": None,
        "axis": None, "kind": None, "label": None, "summary": None, "direction": None,
        "temporalScope": "UNKNOWN", "reason": "관계의 두 인물을 유일하게 확인할 수 없어 보류했습니다.",
    }


def _failure(group, reason):
    if reason not in FAILURE_REASONS:
        _invalid_input()
    return {"observationRefs": [row["ref"] for row in group["context"]["observations"]],
            "reasonCode": reason}


def validate_partial_context(context):
    """Validate capability/blocked identities before any provider call."""
    if context.get("supportsPartialResults") is not True:
        _invalid_input()
    nodes = {row["ref"] for row in context["nodes"]}
    blocked, rows = set(), context.get("blockedPairs", [])
    if not isinstance(rows, list) or len(rows) > MAX_OBSERVATIONS * 4:
        _invalid_input()
    for row in rows:
        if not isinstance(row, dict):
            _invalid_input()
        source, target = row.get("sourceNodeRef"), row.get("targetNodeRef")
        episode = row.get("firstFailedEpisodeNo")
        if (not isinstance(source, str) or not isinstance(target, str)
                or source not in nodes or target not in nodes or source == target
                or type(episode) is not int or not 1 <= episode < context["sourceEpisodeNo"]):
            _invalid_input()
        pair = tuple(sorted((source, target)))
        if pair in blocked:
            _invalid_input()
        blocked.add(pair)
    return blocked


def _groups(payload):
    pairs, holds = {}, []
    for observation in payload["observations"]:
        pair = _pair(observation)
        if pair is None:
            holds.append(_hold(observation))
        else:
            pairs.setdefault(pair, []).append(observation)
    groups = []
    for index, (pair, observations) in enumerate(pairs.items(), 1):
        related = [*observations, *(row for row in payload["heldObservations"] if _pair(row) == pair)]
        groups.append({"groupRef": f"G{index}", "pair": pair,
                       "context": _project_comparison(payload, related)})
    return groups, holds


def _request(groups, reasons=None):
    return {"groups": [
        {"groupRef": group["groupRef"], "leftNodeRef": group["pair"][0],
         "rightNodeRef": group["pair"][1], **group["context"],
         "validationFeedback": (reasons or {}).get(group["groupRef"])}
        for group in groups
    ]}


def _expand(row, group):
    """Restore immutable identity fields from an explicitly selected owned target."""
    row = row.model_dump()
    if any(row[key] is not None and re.search(r"(?<![A-Za-z0-9_])G[1-9][0-9]*(?![A-Za-z0-9_])", row[key])
           for key in ("label", "summary", "reason")):
        raise _comparison_error("RELATIONSHIP_DISPLAY_REFERENCE_LEAK")
    operation = row["operation"]
    source = target = None
    if operation == "ADD":
        if row["relationshipRef"] is not None:
            raise _comparison_error("RELATIONSHIP_ADD_SLOT_EXISTS")
        source, target = group["pair"]
        if row["direction"] == "RIGHT_TO_LEFT":
            source, target = target, source
        if row["direction"] in {"LEFT_TO_RIGHT", "RIGHT_TO_LEFT"}:
            row["direction"] = "DIRECTED"
    elif operation in {"MERGE", "UPDATE", "END"}:
        existing = next((item for item in group["context"]["relationships"]
                         if item["ref"] == row["relationshipRef"]), None)
        if existing is None or any(row[key] is not None for key in ("axis", "kind", "direction")):
            raise _comparison_error("RELATIONSHIP_EXISTING_REF_MISMATCH")
        source, target = existing["sourceNodeRef"], existing["targetNodeRef"]
        for key in ("axis", "kind", "direction"):
            row[key] = existing[key]
        if operation == "MERGE":
            if row["label"] is not None or row["summary"] is not None:
                raise _comparison_error("RELATIONSHIP_MERGE_CONTENT_CHANGED")
            row["label"], row["summary"] = existing["label"], existing["summary"]
    return {**row, "sourceNodeRef": source, "targetNodeRef": target}


class GroupedRelationshipComparator:
    def __init__(self, comparator: RelationshipComparator):
        self.comparator = comparator
        self.system_prompt = PROMPT.read_text(encoding="utf-8")

    def _fits(self, groups):
        # Input fit alone can pack 140 short pairs whose valid two-slot output
        # already exceeds 16k tokens. Plan room for several decisions per pair
        # and leave at least a fifth of output tokens for reasoning/variation. This is
        # an estimate, not a guarantee: keep one pair's full O/H indivisible and
        # retain the normal failure boundary for any actual output truncation.
        output_group_limit = max(1, min(
            MAX_GROUPS_PER_BATCH,
            self.comparator.max_output_tokens * 4 // 5 // PLANNED_OUTPUT_TOKENS_PER_GROUP,
        ))
        if len(groups) > output_group_limit:
            return False
        # Reserve a full allowlisted diagnostic list per group. A repair sends
        # exactly one fixed reason, so it always fits the preflighted batch.
        reserve = {group["groupRef"]: sorted(FAILURE_REASONS) for group in groups}
        return estimate_text_input_tokens(
            self.system_prompt, json.dumps(_request(groups, reserve), ensure_ascii=False),
            self.comparator.model, RESPONSE_SCHEMA,
        ) <= self.comparator.max_input_tokens

    def _plan(self, groups):
        batches, pending, failures = [], [], []
        for group in groups:
            if not self._fits([group]):
                failures.append(_failure(group, "RELATIONSHIP_INPUT_TOO_LARGE"))
                continue
            if pending and not self._fits([*pending, group]):
                batches.append(pending)
                pending = []
            pending.append(group)
        if pending:
            batches.append(pending)
        # An explicit failed pair preserves omitted work; never silently truncate
        # its observations or claim they were semantically excluded.
        for batch in batches[MAX_BATCHES:]:
            failures.extend(_failure(group, "RELATIONSHIP_INPUT_TOO_LARGE") for group in batch)
        return batches[:MAX_BATCHES], failures

    async def compare(self, context):
        payload = relationship_comparison_prompt_data(context)
        blocked = validate_partial_context(context)
        groups, holds = _groups(payload)
        failures = [_failure(group, "RELATIONSHIP_PREDECESSOR_INCOMPLETE")
                    for group in groups if group["pair"] in blocked]
        available = [group for group in groups if group["pair"] not in blocked]
        batches, oversized = self._plan(available)
        failures.extend(oversized)
        accepted = {}
        for batch in batches:
            valid, invalid = await self._call(batch)
            accepted.update(valid)
            if invalid:
                retry = [group for group in batch if group["groupRef"] in invalid]
                repaired, remaining = await self._call(retry, invalid)
                accepted.update(repaired)
                failures.extend(_failure(group, remaining[group["groupRef"]])
                                for group in retry if group["groupRef"] in remaining)
        decisions = list(holds)
        for group in groups:
            rows = accepted.get(group["groupRef"])
            if rows is None:
                continue
            if len(decisions) + len(rows) > 512:
                failures.append(_failure(group, "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"))
            else:
                decisions.extend(rows)
        failed = {ref for failure in failures for ref in failure["observationRefs"]}
        successful = {**payload, "observations": [row for row in payload["observations"] if row["ref"] not in failed]}
        # All preserved decisions are checked together against the original frozen
        # context. Distinct pairs cannot share mutable slots or observation refs.
        decisions = self.comparator._validate_response({"decisions": decisions}, successful)
        failures.sort(key=lambda row: next(i for i, item in enumerate(payload["observations"])
                                          if item["ref"] == row["observationRefs"][0]))
        return RelationshipComparisonOutcome(decisions, failures)

    async def _call(self, groups, reasons=None):
        response = await self.comparator.llm_client.create_text_response(
            system_prompt=self.system_prompt, user_prompt=json.dumps(_request(groups, reasons), ensure_ascii=False),
            model=self.comparator.model, max_output_tokens=self.comparator.max_output_tokens,
            prompt_cache_key=VERSION, response_schema=RESPONSE_SCHEMA,
            call_options=LlmCallOptions(reasoning_effort="medium"),
        )
        # Provider, transport, lease, quota and unexpected exceptions are not
        # caught here. Only actual returned JSON/schema/domain failures repair.
        try:
            raw = json.loads(response.text, object_pairs_hook=_unique_object)
        except (ValueError, TypeError):
            return {}, {group["groupRef"]: "RELATIONSHIP_RESPONSE_JSON_INVALID" for group in groups}
        expected = {group["groupRef"]: group for group in groups}
        if (not isinstance(raw, dict) or set(raw) != {"groups"} or not isinstance(raw["groups"], list)
                or len(raw["groups"]) > MAX_OBSERVATIONS):
            return {}, {ref: "RELATIONSHIP_RESPONSE_SCHEMA_INVALID" for ref in expected}
        indexed, invalid = {}, {}
        for item in raw["groups"]:
            if not isinstance(item, dict) or not isinstance(item.get("groupRef"), str) or item["groupRef"] not in expected:
                return {}, {ref: "RELATIONSHIP_OBSERVATION_REFS_INVALID" for ref in expected}
            ref = item["groupRef"]
            if ref in indexed:
                invalid[ref] = "RELATIONSHIP_OBSERVATION_REFS_INVALID"
            indexed[ref] = item
        valid = {}
        for ref, group in expected.items():
            if ref in invalid:
                continue
            if ref not in indexed:
                invalid[ref] = "RELATIONSHIP_OBSERVATION_COVERAGE_MISSING"
                continue
            try:
                result = GroupResponse.model_validate(indexed[ref])
            except (ValueError, TypeError):
                invalid[ref] = "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"
                continue
            try:
                rows = [_expand(row, group) for row in result.decisions]
                valid[ref] = self.comparator._validate_response({"decisions": rows}, group["context"])
            except ComparisonValidationError as exc:
                diagnostic = getattr(exc, "failure_diagnostics", None)
                reason = diagnostic.reason_code if diagnostic is not None else "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"
                invalid[ref] = reason if reason in FAILURE_REASONS else "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"
        return valid, invalid
