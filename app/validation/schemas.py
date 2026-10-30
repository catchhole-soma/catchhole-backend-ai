"""Per-call strict response schemas that only offer actually readable references (GH331).

The wire shape of every role output is unchanged; only the allowed values are narrowed. OpenAI strict
mode cannot make the root object conditional, so FOLLOW_UP/NO_CONFLICT field rules stay in the prompt
and in the validators. Enum lists are capped so a large catalogue never produces an oversized schema;
the validators remain the authority either way.
"""

from copy import deepcopy

from app.validation.contracts import TARGET_REF, QuestionBatch, RelatedSelection, ValidationDecision
from app.validation.models import _strict_schema

_ENUM_LIMIT = 100
_ENUM_BUDGET = 500
_KINDS = ["COMPARISON_CONTEXT", "CHANGES_EXCEPTIONS", "NEW_CLUE"]


class _Enums:
    """Hands out enum constraints in priority order until the per-schema budget is used."""

    def __init__(self):
        self.remaining = _ENUM_BUDGET

    def string(self, values):
        values = list(dict.fromkeys(values))
        if not values or len(values) > _ENUM_LIMIT or len(values) > self.remaining:
            return {"type": "string"}
        self.remaining -= len(values)
        return {"type": "string", "enum": values}

    def array(self, values, *, min_items=0):
        result = {"type": "array", "items": self.string(values)}
        if min_items:
            result["minItems"] = min_items
        return result


def _position(minimum_key):
    return {"anyOf": [{"type": "integer", minimum_key: 0}, {"type": "null"}]}


def _intent_variant(operations, *, query, source_ref, positions, evidence_refs):
    properties = {
        "ref": {"type": "string"},
        "kind": {"type": "string", "enum": list(_KINDS)},
        "operation": {"type": "string", "enum": list(operations)},
        "purpose": {"type": "string", "minLength": 1},
        "query": {"type": "string", "minLength": 1} if query else {"type": "null"},
        "source_ref": source_ref,
        "start": _position("minimum") if positions else {"type": "null"},
        "end": _position("exclusiveMinimum") if positions else {"type": "null"},
        "evidence_refs": evidence_refs,
    }
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def lookup_intent_schema(enums, *, entry_refs, source_refs, evidence_refs):
    """One anyOf branch per operation: DIRECT needs an R ref, NEIGHBORS an E ref, BASELINE B refs."""
    variants = []
    if entry_refs:
        variants.append(_intent_variant(["BASELINE"], query=False, source_ref={"type": "null"},
                                        positions=False, evidence_refs=enums.array(entry_refs)))
    variants.append(_intent_variant(
        ["TARGET_CONTEXT"], query=False,
        source_ref={"anyOf": [{"type": "string", "enum": [TARGET_REF]}, {"type": "null"}]},
        positions=True, evidence_refs={"type": "array", "items": {"type": "string"}}))
    if source_refs:
        variants.append(_intent_variant(["DIRECT"], query=False, source_ref=enums.string(source_refs),
                                        positions=True,
                                        evidence_refs={"type": "array", "items": {"type": "string"}}))
    variants.append(_intent_variant(["KEYWORD", "VECTOR"], query=True, source_ref={"type": "null"},
                                    positions=False,
                                    evidence_refs={"type": "array", "items": {"type": "string"}}))
    if evidence_refs:
        variants.append(_intent_variant(["NEIGHBORS"], query=False, source_ref={"type": "null"},
                                        positions=False,
                                        evidence_refs=enums.array(evidence_refs, min_items=1)))
    return {"anyOf": variants}


def _target_span(schema):
    schema["$defs"]["Span"]["properties"]["source_ref"] = {"type": "string", "enum": [TARGET_REF]}


def question_batch_schema(*, segment_refs, subject_refs, rule_refs, clue_refs, entry_refs, source_refs,
                          evidence_refs=()):
    enums = _Enums()
    schema = deepcopy(_strict_schema(QuestionBatch.model_json_schema()))
    defs = schema["$defs"]
    _target_span(schema)
    defs["SegmentCoverage"]["properties"]["segment_ref"] = enums.string(segment_refs)
    defs["ClueCoverage"] = deepcopy(defs["RuleCoverage"])
    defs["RuleCoverage"]["properties"]["entry_ref"] = enums.string(rule_refs)
    defs["ClueCoverage"]["properties"]["entry_ref"] = enums.string(clue_refs)
    schema["properties"]["clue_checks"]["items"] = {"$ref": "#/$defs/ClueCoverage"}
    draft = defs["QuestionDraft"]["properties"]
    draft["lookup_intents"]["items"] = lookup_intent_schema(
        enums, entry_refs=entry_refs, source_refs=source_refs, evidence_refs=evidence_refs)
    draft["subject_refs"] = enums.array(subject_refs)
    defs.pop("LookupIntent")
    return schema


def selection_schema(*, entry_refs):
    schema = deepcopy(_strict_schema(RelatedSelection.model_json_schema()))
    schema["$defs"]["EntrySelection"]["properties"]["entry_ref"] = _Enums().string(entry_refs)
    return schema


def decision_schema(*, known_refs, receipt_refs, question_refs, fact_refs, evidence_refs, entry_refs,
                    source_refs):
    enums = _Enums()
    schema = deepcopy(_strict_schema(ValidationDecision.model_json_schema()))
    defs, properties = schema["$defs"], schema["properties"]
    _target_span(schema)
    defs["Assessment"]["properties"]["receipt_refs"] = enums.array(receipt_refs, min_items=1)
    defs["Assessment"]["properties"]["evidence_refs"] = enums.array(known_refs, min_items=1)
    properties["evidence_refs"] = enums.array(known_refs)
    properties["affected_question_refs"] = enums.array(question_refs)
    defs["Disagreement"]["properties"]["fact_ref"] = enums.string(fact_refs)
    defs["Disagreement"]["properties"]["evidence_ref"] = enums.string(evidence_refs)
    properties["follow_ups"]["items"] = lookup_intent_schema(
        enums, entry_refs=entry_refs, source_refs=source_refs, evidence_refs=evidence_refs)
    defs.pop("LookupIntent")
    return schema
