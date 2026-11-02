"""Strict response schemas that only offer references of the right kind (GH331, v3 for grid v4).

OpenAI strict mode cannot make the root object conditional, so FOLLOW_UP/NO_CONFLICT field rules stay in the
prompt and in the validators, which remain the authority either way.

PLANNER and JUDGE run many times with the same system prompt, and the provider caches that identical prefix
only while the schema is identical too. References are therefore constrained by kind (`B*`, `E*`, `L*`,
`S*`, `TSEG*`) with fixed patterns instead of per-call lists, so one report has one PLANNER schema and two
JUDGE schemas (OPEN with or without search, and FINAL).
"""

import re
from copy import deepcopy

from app.validation.contracts import (
    MAX_SEARCH_QUERIES,
    SEARCH_QUERY_LIMIT,
    TARGET_REF,
    JudgeFinalAnswer,
    JudgeOpenAnswer,
    QuestionBatch,
)
from app.validation.models import SCHEMA_VERSION, _strict_schema

_REF = {"T1": TARGET_REF, "B": r"B[1-9][0-9]*", "E": r"E[1-9][0-9]*", "S": r"S[1-9][0-9]*",
        "L": r"L[1-9][0-9]*", "SEG": r"TSEG[1-9][0-9]*"}
_ANY = {"type": "string"}
PLANNER_SCHEMA_NAME = f"validation_planner_{SCHEMA_VERSION}"
JUDGE_OPEN_SCHEMA_NAME = f"validation_judge_open_{SCHEMA_VERSION}"
JUDGE_FINAL_SCHEMA_NAME = f"validation_judge_final_{SCHEMA_VERSION}"


def ref_kind(*kinds, values=()):
    """A fixed per-kind pattern; plain string if a supplied real ref does not have that shape."""
    body = "|".join(_REF[kind] for kind in kinds)
    if any(not re.fullmatch(body, value) for value in values):
        return dict(_ANY)
    return {"type": "string", "pattern": f"^({body})$"}


def _array(items, *, min_items=0, max_items=None):
    result = {"type": "array", "items": items}
    if min_items:
        result["minItems"] = min_items
    if max_items is not None:
        result["maxItems"] = max_items
    return result


def _target_span(schema):
    schema["$defs"]["Span"]["properties"]["source_ref"] = {"type": "string", "enum": [TARGET_REF]}


def planner_schema(*, segment_refs, subject_refs):
    """Identical for every segment of a report (kind patterns), so the planner prefix stays cacheable."""
    schema = deepcopy(_strict_schema(QuestionBatch.model_json_schema()))
    defs = schema["$defs"]
    _target_span(schema)
    defs["SegmentCoverage"]["properties"]["segment_ref"] = ref_kind("SEG", values=segment_refs)
    defs["QuestionDraft"]["properties"]["subject_refs"] = _array(ref_kind("S", values=subject_refs))
    return schema


def judge_schema(*, entry_refs, final, search_available):
    """OPEN (FOLLOW_UP or DECIDED; `search_queries` only when search can run) or FINAL (DECIDED only).

    Exact receipt/evidence membership is checked by the Worker's decision validation, and `required_reads`
    is completed from the actual receipts there.
    """
    model = JudgeFinalAnswer if final else JudgeOpenAnswer
    schema = deepcopy(_strict_schema(model.model_json_schema()))
    defs, properties = schema["$defs"], schema["properties"]
    _target_span(schema)
    known = ref_kind("T1", "B", "E", values=entry_refs)
    defs["Assessment"]["properties"]["receipt_refs"] = _array(ref_kind("L"), min_items=1)
    defs["Assessment"]["properties"]["evidence_refs"] = _array(known, min_items=1)
    properties["evidence_refs"] = _array(known)
    defs["Disagreement"]["properties"]["fact_ref"] = ref_kind("B", values=entry_refs)
    defs["Disagreement"]["properties"]["evidence_ref"] = ref_kind("E")
    if final:
        properties["action"] = {"type": "string", "enum": ["DECIDED"]}
    else:
        if search_available:
            properties["search_queries"] = _array(
                {"type": "string", "minLength": 1, "maxLength": SEARCH_QUERY_LIMIT}, max_items=MAX_SEARCH_QUERIES)
        else:
            properties.pop("search_queries")
            schema["required"] = [name for name in schema["required"] if name != "search_queries"]
    return schema
