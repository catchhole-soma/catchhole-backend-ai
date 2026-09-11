"""Validate ordered STATUS observations and adapt them to the existing wire format."""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import Field, StrictStr, StringConstraints, ValidationError

from app.analysis.schemas import (
    _ProviderBooleanValueJson,
    _ProviderNumberValueJson,
    _ProviderStringValueJson,
    _StrictProviderModel,
    _to_openai_strict_json_schema,
)
from app.analysis.status_evidence import StatusEvidenceDocument, StatusEvidenceReferenceError
from app.llm.protocols import LlmResponseSchema

_Value = TypeVar("_Value")
_Kind = Literal["START", "CONTINUE", "CHANGE", "END", "PAST", "HYPOTHETICAL"]
_Origin = Literal["STARTS_IN_CURRENT_CHUNK", "PREEXISTING_OR_UNSPECIFIED"]
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_REFERENCE = "_StatusObservationEvidenceReference"
_NONCURRENT = {"PAST", "HYPOTHETICAL"}


class _Observation(_StrictProviderModel, Generic[_Value]):
    kind: _Kind
    attribute_value: str | None
    value_json: _Value
    evidence_refs: list[StrictStr] = Field(min_length=1)
    confidence: float | None = Field(ge=0, le=1)


class _State(_StrictProviderModel, Generic[_Value]):
    entity_name: _Name
    raw_entity_mention: Annotated[str, StringConstraints(max_length=100)] | None
    attribute_name: _Name
    origin: _Origin
    observations: list[_Observation[_Value]] = Field(min_length=1)


class _NumberState(_State[_ProviderNumberValueJson]):
    value_type: Literal["NUMBER"]


class _BooleanState(_State[_ProviderBooleanValueJson]):
    value_type: Literal["BOOLEAN"]


class _StringState(_State[_ProviderStringValueJson]):
    value_type: Literal["STRING"]


class _StatusJsonValue(_StrictProviderModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _JsonState(_State[_StatusJsonValue]):
    value_type: Literal["JSON"]


class _Response(_StrictProviderModel):
    states: list[Annotated[
        _NumberState | _BooleanState | _StringState | _JsonState,
        Field(discriminator="value_type"),
    ]]


@dataclass(frozen=True)
class ResolvedStatusObservations:
    provider_payload: dict[str, Any]
    kinds: tuple[str, ...]
    state_indexes: tuple[int, ...]


class StatusObservationConflictError(StatusEvidenceReferenceError):
    """A validated transition conflict with only immutable kind/reference hints."""

    def __init__(
        self,
        field_locs: tuple[str, str],
        observations: tuple[tuple[_Kind, tuple[str, ...]], tuple[_Kind, tuple[str, ...]]],
    ) -> None:
        self._observations = tuple((kind, tuple(refs)) for kind, refs in observations)
        super().__init__("STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT", field_locs)

    @property
    def observations(self) -> tuple[tuple[_Kind, tuple[str, ...]], ...]:
        return self._observations


def build_status_observation_schema(document: StatusEvidenceDocument) -> LlmResponseSchema:
    """Use literal E references, a STATUS name and existing scalar value models."""
    if not document.references:
        raise StatusEvidenceReferenceError("STATUS_EVIDENCE_EMPTY_DOCUMENT", ("evidence_refs",))
    schema = _to_openai_strict_json_schema(_Response.model_json_schema())
    for definition in schema["$defs"].values():
        properties = definition.get("properties", {})
        if "evidence_refs" in properties:
            properties["evidence_refs"]["items"] = {"$ref": f"#/$defs/{_REFERENCE}"}
    schema["$defs"][_REFERENCE] = {"type": "string", "enum": list(document.references)}
    return LlmResponseSchema(name="character_status_observations", schema=schema, strict=True)


def _error(reason: str, loc: str) -> StatusEvidenceReferenceError:
    return StatusEvidenceReferenceError(f"STATUS_OBSERVATION_{reason}", (loc,))


def _safe_validation_locs(exc: ValidationError) -> tuple[str, ...]:
    allowed = {"states", "observations", "entity_name", "raw_entity_mention", "attribute_name",
               "origin", "value_type", "kind", "attribute_value", "value_json", "evidence_refs",
               "confidence", "extra_json", "value", "name"}
    variants = {"NUMBER", "BOOLEAN", "STRING", "JSON"}
    paths = []
    for error in exc.errors(include_input=False, include_context=False):
        parts = [str(part) if isinstance(part, int) or part in allowed else "unexpected_field"
                 for part in error["loc"] if part not in variants]
        paths.append(".".join(parts) or "states")
    return tuple(dict.fromkeys(paths))


def resolve_status_observations(
    payload: object, document: StatusEvidenceDocument,
) -> ResolvedStatusObservations:
    """Resolve atomically without editing caller input, values, names or evidence.

    START/CONTINUE/CHANGE and END determine only the reserved ``active`` property.
    PAST/HYPOTHETICAL preserve their noncurrent text and omit that property. Current
    observations are checked in source order; distinct states are then interleaved
    by the existing last-evidence anchor, with stable ordering at equal anchors.
    """
    try:
        validated = _Response.model_validate(payload)
    except ValidationError as exc:
        raise StatusEvidenceReferenceError(
            "STATUS_OBSERVATION_INVALID_PAYLOAD", _safe_validation_locs(exc),
        ) from None

    candidates: list[dict[str, Any]] = []
    locations: list[str] = []
    kinds: list[str] = []
    state_indexes: list[int] = []
    groups: list[list[int]] = []
    identities = set()
    for state_index, (state, checked) in enumerate(zip(payload["states"], validated.states, strict=True)):
        state_loc = f"states.{state_index}"
        if not checked.attribute_name.startswith("status.") or len(checked.attribute_name) == 7:
            raise _error("INVALID_STATUS_KEY", f"{state_loc}.attribute_name")
        identity = (checked.entity_name, checked.raw_entity_mention, checked.attribute_name, checked.value_type)
        if identity in identities:
            raise _error("DUPLICATE_STATE", state_loc)
        identities.add(identity)
        indices = []
        for observation_index, observation in enumerate(state["observations"]):
            loc = f"{state_loc}.observations.{observation_index}"
            value = deepcopy(observation["value_json"])
            if state["value_type"] == "JSON":
                value = {"extra_json": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}
            extra = json.loads(value["extra_json"]) if value["extra_json"] is not None else {}
            if "active" in extra:
                raise _error("ACTIVE_FORBIDDEN", f"{loc}.value_json.extra_json")
            if observation["kind"] not in _NONCURRENT:
                extra["active"] = observation["kind"] != "END"
                value["extra_json"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
            candidates.append({
                "candidate_kind": "SETTING", "entity_type": "CHARACTER",
                "entity_name": state["entity_name"], "raw_entity_mention": state["raw_entity_mention"],
                "attribute_name": state["attribute_name"], "value_type": state["value_type"],
                "attribute_value": observation["attribute_value"], "value_json": value,
                "confidence": observation["confidence"], "evidence_refs": deepcopy(observation["evidence_refs"]),
            })
            indices.append(len(candidates) - 1)
            locations.append(loc)
            kinds.append(observation["kind"])
            state_indexes.append(state_index)
        groups.append(indices)

    try:
        resolved = document.resolve_payload({"candidates": candidates})["candidates"]
    except StatusEvidenceReferenceError as exc:
        # Keep the codec reason but point retry feedback at the actual STATUS wire.
        mapped = []
        for loc in exc.field_locs:
            parts = loc.split(".")
            if len(parts) > 1 and parts[0] == "candidates" and parts[1].isdigit():
                mapped.append(".".join([locations[int(parts[1])], *parts[2:]]))
            else:
                mapped.append("states")
        raise StatusEvidenceReferenceError(exc.reason_code, tuple(mapped)) from None

    anchors = [max(span["start_offset"] for span in candidate["evidence_spans"])
               for candidate in resolved]
    first_anchors = [min(span["start_offset"] for span in candidate["evidence_spans"])
                     for candidate in resolved]
    for state_index, (state, indices) in enumerate(zip(payload["states"], groups, strict=True)):
        last_active = None
        first_current = True
        previous_anchor = None
        for observation, index in zip(state["observations"], indices, strict=True):
            anchor, kind = anchors[index], observation["kind"]
            if kind == "END" and last_active is not None and first_anchors[index] <= anchors[last_active]:
                # Both observations have passed the response and literal-E codecs.
                raise StatusObservationConflictError(
                    (f"{locations[last_active]}.evidence_refs", f"{locations[index]}.evidence_refs"),
                    ((kinds[last_active], tuple(candidates[last_active]["evidence_refs"])),
                     ("END", tuple(candidates[index]["evidence_refs"]))),
                )
            if previous_anchor is not None and anchor < previous_anchor:
                raise _error("OBSERVATION_ORDER_INVALID", f"{locations[index]}.evidence_refs")
            previous_anchor = anchor
            if kind in _NONCURRENT:
                continue
            if first_current and state["origin"] == "STARTS_IN_CURRENT_CHUNK" and kind != "START":
                raise _error("CURRENT_START_REQUIRED", f"{locations[index]}.kind")
            first_current = False
            if kind in {"START", "CONTINUE", "CHANGE"}:
                last_active = index
        if first_current and state["origin"] == "STARTS_IN_CURRENT_CHUNK":
            raise _error("CURRENT_START_REQUIRED", f"states.{state_index}.origin")

    ordered = sorted(range(len(resolved)), key=anchors.__getitem__)
    return ResolvedStatusObservations(
        provider_payload={"candidates": [resolved[index] for index in ordered]},
        kinds=tuple(kinds[index] for index in ordered),
        state_indexes=tuple(state_indexes[index] for index in ordered),
    )
