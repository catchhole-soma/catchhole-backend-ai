"""Strict response schemas that only offer readable references of the right kind (GH331).

The wire shape of every role output is unchanged; only the allowed values are narrowed. OpenAI strict
mode cannot make the root object conditional, so FOLLOW_UP/NO_CONFLICT field rules stay in the prompt
and in the validators, which remain the authority either way.

PLANNER and JUDGE run many times with the same system prompt, and the provider caches that identical
prefix only while the schema is identical too. Their schemas therefore constrain references by kind
(`B*`, `R*`, `E*`, `L*`, ...) with fixed patterns instead of per-call lists. COVERAGE and BOOK_COVERAGE list
the exact selected rules/clues (S3), capped so a large catalogue never produces an oversized schema.
SUBJECT_RESOLUTION (GH349) lists the report's catalogue subject refs or null; it is built once per report so
its prefix stays cacheable like JUDGE.
"""

import re
from copy import deepcopy

from app.validation.contracts import (
    TARGET_REF,
    QuestionBatch,
    SubjectResolution,
    ValidationDecision,
)
from app.validation.models import _strict_schema

_ENUM_LIMIT = 100
_ENUM_BUDGET = 500
_KINDS = ["COMPARISON_CONTEXT", "CHANGES_EXCEPTIONS", "NEW_CLUE"]
_REF = {"T1": TARGET_REF, "B": r"B[1-9][0-9]*", "R": r"R[1-9][0-9]*", "E": r"E[1-9][0-9]*",
        "S": r"S[1-9][0-9]*", "Q": r"Q[1-9][0-9]*", "L": r"L[1-9][0-9]*", "SEG": r"TSEG[1-9][0-9]*",
        "CL": r"CL[1-9][0-9]*"}
_ANY = {"type": "string"}


def ref_kind(*kinds, values=()):
    """A fixed per-kind pattern; plain string if a supplied real ref does not have that shape."""
    body = "|".join(_REF[kind] for kind in kinds)
    if any(not re.fullmatch(body, value) for value in values):
        return dict(_ANY)
    return {"type": "string", "pattern": f"^({body})$"}


class _Enums:
    """Hands out enum constraints in priority order until the per-schema budget is used."""

    def __init__(self):
        self.remaining = _ENUM_BUDGET

    def string(self, values):
        values = list(dict.fromkeys(values))
        if not values or len(values) > _ENUM_LIMIT or len(values) > self.remaining:
            return dict(_ANY)
        self.remaining -= len(values)
        return {"type": "string", "enum": values}


def _array(items, *, min_items=0):
    result = {"type": "array", "items": items}
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


def lookup_intent_schema(*, entry_ref, source_ref, evidence_ref, search_operations=("KEYWORD", "VECTOR")):
    """One anyOf branch per operation: BASELINE B refs, DIRECT an R ref, NEIGHBORS E refs (S6).

    A branch whose reference schema is ``None`` is not offered (no entries, sources or evidence).
    """
    unused = _array(dict(_ANY))
    variants = []
    if entry_ref is not None:
        variants.append(_intent_variant(["BASELINE"], query=False, source_ref={"type": "null"},
                                        positions=False, evidence_refs=_array(entry_ref)))
    variants.append(_intent_variant(
        ["TARGET_CONTEXT"], query=False,
        source_ref={"anyOf": [{"type": "string", "enum": [TARGET_REF]}, {"type": "null"}]},
        positions=True, evidence_refs=unused))
    if source_ref is not None:
        variants.append(_intent_variant(["DIRECT"], query=False, source_ref=source_ref, positions=True,
                                        evidence_refs=unused))
    variants.append(_intent_variant(list(search_operations), query=True, source_ref={"type": "null"},
                                    positions=False, evidence_refs=unused))
    if evidence_ref is not None:
        variants.append(_intent_variant(["NEIGHBORS"], query=False, source_ref={"type": "null"},
                                        positions=False, evidence_refs=_array(evidence_ref, min_items=1)))
    return {"anyOf": variants}


def _target_span(schema):
    schema["$defs"]["Span"]["properties"]["source_ref"] = {"type": "string", "enum": [TARGET_REF]}


def question_batch_schema(*, segment_refs, subject_refs, rule_refs, clue_refs, entry_refs, source_refs,
                          evidence_refs=(), search_operations=("KEYWORD", "VECTOR"), cacheable=False):
    """PLANNER uses ``cacheable=True`` (kind patterns); COVERAGE lists the exact selected rules/clues."""
    schema = deepcopy(_strict_schema(QuestionBatch.model_json_schema()))
    defs = schema["$defs"]
    _target_span(schema)
    defs["ClueCoverage"] = deepcopy(defs["RuleCoverage"])
    schema["properties"]["clue_checks"]["items"] = {"$ref": "#/$defs/ClueCoverage"}
    draft = defs["QuestionDraft"]["properties"]
    if cacheable:
        segment, rule, clue = (ref_kind("SEG", values=segment_refs), ref_kind("B", values=rule_refs),
                               ref_kind("CL", values=clue_refs))
        subject = ref_kind("S", values=subject_refs)
        entry = ref_kind("B", values=entry_refs) if entry_refs else None
        source = ref_kind("R", values=source_refs) if source_refs else None
        evidence = ref_kind("E", values=evidence_refs) if evidence_refs else None
    else:
        enums = _Enums()
        segment, rule, clue = enums.string(segment_refs), enums.string(rule_refs), enums.string(clue_refs)
        subject = enums.string(subject_refs)
        entry = enums.string(entry_refs) if entry_refs else None
        source = enums.string(source_refs) if source_refs else None
        evidence = enums.string(evidence_refs) if evidence_refs else None
    defs["SegmentCoverage"]["properties"]["segment_ref"] = segment
    defs["RuleCoverage"]["properties"]["entry_ref"] = rule
    defs["ClueCoverage"]["properties"]["entry_ref"] = clue
    draft["subject_refs"] = _array(subject)
    draft["lookup_intents"]["items"] = lookup_intent_schema(
        entry_ref=entry, source_ref=source, evidence_ref=evidence, search_operations=search_operations)
    defs.pop("LookupIntent")
    return schema


def subject_resolution_schema(*, subject_refs):
    """GH349: `subject_ref` is one of the report's catalogue subjects or null; identical for every segment.

    Up to the enum limit the refs are listed exactly; a larger catalogue falls back to the `S*` pattern and
    the validator remains the authority. Without subjects only null is offered.
    """
    schema = deepcopy(_strict_schema(SubjectResolution.model_json_schema()))
    refs = list(dict.fromkeys(subject_refs))
    subject = _Enums().string(refs) if len(refs) <= _ENUM_LIMIT else ref_kind("S", values=refs)
    schema["$defs"]["MentionResolution"]["properties"]["subject_ref"] = (
        {"anyOf": [subject, {"type": "null"}]} if refs else {"type": "null"})
    return schema


def decision_schema(*, entry_refs, source_refs, search_operations=("KEYWORD", "VECTOR")):
    """Identical for every JUDGE call of a report, so the system+schema prefix stays cacheable.

    Exact receipt/evidence/question membership is checked by ``_validate_decision``, and
    ``required_reads`` is completed from the actual receipts there.
    """
    schema = deepcopy(_strict_schema(ValidationDecision.model_json_schema()))
    defs, properties = schema["$defs"], schema["properties"]
    _target_span(schema)
    known = ref_kind("T1", "B", "E", values=entry_refs)
    defs["Assessment"]["properties"]["receipt_refs"] = _array(ref_kind("L"), min_items=1)
    defs["Assessment"]["properties"]["evidence_refs"] = _array(known, min_items=1)
    properties["evidence_refs"] = _array(known)
    properties["affected_question_refs"] = _array(ref_kind("Q"))
    defs["Disagreement"]["properties"]["fact_ref"] = ref_kind("B", values=entry_refs)
    defs["Disagreement"]["properties"]["evidence_ref"] = ref_kind("E")
    properties["follow_ups"]["items"] = lookup_intent_schema(
        entry_ref=ref_kind("B", values=entry_refs) if entry_refs else None,
        source_ref=ref_kind("R", values=source_refs) if source_refs else None,
        evidence_ref=ref_kind("E"), search_operations=search_operations)
    defs.pop("LookupIntent")
    return schema
