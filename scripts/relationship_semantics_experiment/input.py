"""Structural input contract for the offline relationship semantics experiment.

The projection visits only named execution fields. Oracle values are never read,
and unknown fields are rejected instead of silently losing unsupported data.
References, time consistency and domain meaning belong to the B/C validators.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

PositiveInt = Annotated[StrictInt, Field(gt=0)]


class InputModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, alias_generator=to_camel
    )

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)

    @field_validator("*")
    @classmethod
    def reject_blank_strings(cls, value):
        # Do not strip the stored text: source/quote content must round-trip exactly.
        strings = value if isinstance(value, tuple) else (value,)
        if any(isinstance(item, str) and not item.strip() for item in strings):
            raise ValueError("string values must not be blank")
        return value


class Node(InputModel):
    node_ref: StrictStr
    name: StrictStr
    work_id: StrictStr


class Source(InputModel):
    source_ref: StrictStr
    work_id: StrictStr
    episode_no: PositiveInt
    text: StrictStr
    quote: StrictStr


class Claim(InputModel):
    claim_ref: StrictStr
    source_ref: StrictStr
    subject_candidate_refs: tuple[StrictStr, ...]
    object_candidate_refs: tuple[StrictStr, ...]
    statement_mode: Literal["ASSERTED", "UNCERTAIN", "HYPOTHETICAL"]
    curation_status: Literal["CONFIRMED", "HOLD", "RETRACTED"]
    temporal_scope: Literal["PRESENT", "PAST", "HYPOTHETICAL", "UNKNOWN"]
    narrative_context_id: StrictStr


class Slot(InputModel):
    work_id: StrictStr
    source_node_ref: StrictStr
    target_node_ref: StrictStr
    axis: Literal["SOCIAL", "ATTITUDE", "INTERACTION"]
    kind: StrictStr
    direction: Literal["DIRECTED", "MUTUAL", "UNDIRECTED"]


class Fact(InputModel):
    fact_ref: StrictStr
    slot: Slot
    status: Literal["ACTIVE", "ENDED"]
    fact_validity: Literal["VALID", "RETRACTED"]
    known_from_episode_no: PositiveInt
    last_changed_episode_no: PositiveInt
    support_claim_refs: tuple[StrictStr, ...]
    end_claim_refs: tuple[StrictStr, ...]
    correction_source_refs: tuple[StrictStr, ...]


class Suppression(InputModel):
    suppression_ref: StrictStr
    slot: Slot
    reason: Literal["USER_DELETE"]
    applies_to: Literal["ALL_EPISODES"]
    recorded_revision: PositiveInt


class Query(InputModel):
    query_ref: StrictStr
    mode: Literal["READER_KNOWLEDGE", "STORY_TIME"]
    cutoff_episode_no: PositiveInt
    story_context_id: StrictStr | None
    axis: Literal["SOCIAL", "ATTITUDE", "INTERACTION"] | None = None
    node_ref: StrictStr | None = None
    perspective: Literal["INCOMING", "OUTGOING"] | None = None


class Case(InputModel):
    case_id: StrictStr
    work_id: StrictStr
    snapshot_through_episode_no: PositiveInt
    snapshot_revision: PositiveInt
    nodes: tuple[Node, ...]
    sources: tuple[Source, ...]
    claims: tuple[Claim, ...]
    facts: tuple[Fact, ...]
    suppressions: tuple[Suppression, ...]
    queries: tuple[Query, ...]
    title: StrictStr | None = None
    purpose: StrictStr | None = None
    product_question_id: StrictStr | None = None


class Dataset(InputModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    synthetic: Literal[True] = True
    cases: Annotated[tuple[Case, ...], Field(min_length=1)]

    @field_validator("synthetic", mode="before")
    @classmethod
    def require_synthetic_boolean(cls, value):
        # Literal[True] alone also accepts integer 1; this boundary does not.
        if type(value) is not bool or value is not True:
            raise ValueError("synthetic must be the boolean true")
        return value

    @model_validator(mode="after")
    def require_unique_cases(self):
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate caseId")
        return self


# These are the only non-execution fields accepted by the document loader.
# Their values are intentionally not inspected, copied or validated.
_DATASET_METADATA = frozenset({"status", "oracle", "outputSemantics", "productIntegration"})
_QUERY_METADATA = frozenset(
    {"expected", "consumerExamples", "expectedText", "title", "purpose", "description", "question"}
)
_CHILD_OBJECTS = {(Fact, "slot"): Slot, (Suppression, "slot"): Slot}
_CHILD_ARRAYS = {
    (Dataset, "cases"): Case,
    (Case, "nodes"): Node,
    (Case, "sources"): Source,
    (Case, "claims"): Claim,
    (Case, "facts"): Fact,
    (Case, "suppressions"): Suppression,
    (Case, "queries"): Query,
}


def _project(model: type[InputModel], value: Mapping) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{model.__name__} must be an object")  # noqa: TRY004 - input error contract
    aliases = {field.alias for field in model.model_fields.values()}
    ignored = _DATASET_METADATA if model is Dataset else _QUERY_METADATA if model is Query else ()
    if set(value) - aliases - set(ignored):
        raise ValueError(f"unsupported field in {model.__name__}")
    result = {}
    for alias in aliases:
        if alias not in value:
            continue
        child = value[alias]
        if child_model := _CHILD_OBJECTS.get((model, alias)):
            child = _project(child_model, child)
        elif child_model := _CHILD_ARRAYS.get((model, alias)):
            if not isinstance(child, (list, tuple)):
                raise ValueError(f"{model.__name__}.{alias} must be an array")
            child = [_project(child_model, item) for item in child]
        result[alias] = child
    return result


def load_document(document: Mapping) -> Dataset:
    """Read supported camelCase JSON fields without accessing oracle values."""
    if not isinstance(document, Mapping):
        raise ValueError("dataset must be an object")  # noqa: TRY004 - input error contract
    if "schemaVersion" not in document or "synthetic" not in document:
        raise ValueError("schemaVersion and synthetic are required")
    return Dataset.model_validate(_project(Dataset, document))


def load_path(path: Path) -> Dataset:
    return load_document(json.loads(Path(path).read_text(encoding="utf-8")))


def input_document(document: Mapping) -> dict:
    """Return reusable execution JSON; no expected or consumer text is retained."""
    return load_document(document).to_dict()
