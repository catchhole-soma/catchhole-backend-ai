"""Request-owned property selection and bounded validation diagnostics.

Only a validated local reference can select an input path. Model prose, values,
evidence, unknown references and exception messages never enter diagnostics.
"""

import json
import re
from copy import deepcopy

from pydantic import ValidationError

from app.analysis.ordered_world_rule_diagnostics import (
    OrderedWorldRuleDiagnosticError, RULE_CODES, VALIDATION_STAGES, diagnose_world_rule,
)
from app.analysis.ordered_world_batch_contract import (
    OrderedWorldBatchValidationError,
    OrderedWorldPropertyBatchResult,
    ORDERED_WORLD_BATCH_RESPONSE_SCHEMA,
    _VALIDATION_CODES,
    _schema_issues,
)
from app.analysis.world_setting_schemas import WorldSettingComparisonBatchResult
from app.llm.protocols import LlmResponseSchema

MAX_DIAGNOSTIC_PROPERTIES = 20


def property_index(payload: dict) -> dict[str, dict]:
    """Build an index from trusted request structure, never from response field values."""
    result = {}
    for target in payload.get("targets", []):
        if not isinstance(target, dict):
            continue
        target_ref = target.get("ref")
        if not isinstance(target_ref, str) or not re.fullmatch(r"T[1-9][0-9]*", target_ref):
            continue
        for index, prop in enumerate(target.get("properties", []), 1):
            if not isinstance(prop, dict):
                continue
            scope, name = prop.get("scope_name"), prop.get("setting_name")
            if not isinstance(name, str) or scope is not None and not isinstance(scope, str):
                continue
            ref = f"{target_ref}.P{index}"
            result[ref] = {"ref": ref, "target_ref": target_ref,
                           "scope_name": scope, "setting_name": name}
    return result


def restore_selected_properties(
    result: OrderedWorldPropertyBatchResult, payload: dict,
) -> WorldSettingComparisonBatchResult:
    index = property_index(payload)
    decisions = []
    for decision_index, selection in enumerate(result.decisions):
        item = selection.model_dump(mode="json")
        selected_ref = item.pop("matched_property_ref")
        selected = index.get(selected_ref) if selected_ref is not None else None

        def reject(code):
            raise OrderedWorldBatchValidationError(
                reason_code=code, decision_index=decision_index,
                source_candidate_refs=selection.source_candidate_refs,
                target_ref=selection.target_ref, property_indices=[],
            )

        if selected_ref is not None and selected is None:
            reject("MATCHED_PROPERTY_REF_INVALID")
        if selected is not None and selected["target_ref"] != selection.target_ref:
            reject("MATCHED_PROPERTY_TARGET_MISMATCH")
        item["matched_scope_name"] = selected["scope_name"] if selected else None
        item["matched_property_name"] = selected["setting_name"] if selected else None
        if selection.operation in {"UPDATE", "MERGE"}:
            if selection.proposed_scope_name is not None or selection.proposed_setting_name is not None:
                reject("EXISTING_PROPOSED_PATH_FORBIDDEN")
            item["proposed_scope_name"] = item["matched_scope_name"]
            item["proposed_setting_name"] = item["matched_property_name"]
        decisions.append(item)
    with diagnose_world_rule(True, "PROPERTY_SELECTION"):
        return WorldSettingComparisonBatchResult.model_validate({"decisions": decisions})


def property_response_schema(payload: dict) -> LlmResponseSchema:
    """Constrain the provider to the same complete property allowlist used by the decoder."""
    schema = deepcopy(ORDERED_WORLD_BATCH_RESPONSE_SCHEMA.schema)
    refs = list(property_index(payload))
    field = schema["$defs"]["OrderedWorldPropertyDecision"]["properties"]
    field["matched_property_ref"] = (
        {"anyOf": [{"type": "string", "enum": refs}, {"type": "null"}]}
        if refs else {"type": "null"}
    )
    return LlmResponseSchema(name=ORDERED_WORLD_BATCH_RESPONSE_SCHEMA.name, schema=schema)


def validation_diagnostics(
    payload: dict, attempt_number: int, error: Exception, response_payload: dict | None,
    *, phase: str = "BATCH",
) -> list[dict]:
    """Separate actual allowed selections from input suggestions and omit model strings."""
    index = property_index(payload)
    allowed_sources = {
        item["ref"] for item in payload.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
        and re.fullmatch(r"C[1-9][0-9]*", item["ref"])
    }
    if isinstance(error, OrderedWorldRuleDiagnosticError):
        issues = [{"reason_code": (error.diagnostic_rule_code
                                   if error.diagnostic_rule_code in RULE_CODES.values()
                                   else "COMPARISON_VALIDATION_FAILED"),
                   "decision_index": error.decision_index}]
    elif isinstance(error, OrderedWorldBatchValidationError):
        issues = [{"reason_code": (error.reason_code if error.reason_code in _VALIDATION_CODES
                                   else "COMPARISON_VALIDATION_FAILED"),
                   "decision_index": error.decision_index}]
    elif isinstance(error, ValidationError):
        issues = _schema_issues(error)
    else:
        issues = [{"reason_code": ("RESPONSE_JSON_INVALID" if isinstance(error, json.JSONDecodeError)
                                   else "COMPARISON_VALIDATION_FAILED")}]
    response_decisions = response_payload.get("decisions") if isinstance(response_payload, dict) else None
    diagnostics = []
    for issue in issues[:8]:
        decision_index = issue.get("decision_index")
        selection = None
        if (type(decision_index) is int and 0 <= decision_index < 20
                and isinstance(response_decisions, list) and decision_index < len(response_decisions)
                and isinstance(response_decisions[decision_index], dict)):
            selection = response_decisions[decision_index]
        candidate_refs = []
        selected_properties = []
        if selection is not None:
            refs = selection.get("source_candidate_refs")
            if (isinstance(refs, list) and refs and len(refs) <= 20
                    and all(isinstance(ref, str) and ref in allowed_sources for ref in refs)):
                candidate_refs = list(dict.fromkeys(refs))
                selected_ref = selection.get("matched_property_ref")
                selected = index.get(selected_ref) if isinstance(selected_ref, str) else None
                if selected is not None and selected["target_ref"] == selection.get("target_ref"):
                    selected_properties = [dict(selected)]
        # Typed metadata is diagnostic-only. Recheck every source against the
        # actual request, including a missing source with no response index.
        typed_refs = (error.diagnostic_candidate_refs
                      if isinstance(error, OrderedWorldRuleDiagnosticError)
                      else error.source_candidate_refs
                      if isinstance(error, OrderedWorldBatchValidationError) else ())
        if typed_refs and all(type(ref) is str and ref in allowed_sources for ref in typed_refs):
            candidate_refs = list(dict.fromkeys(typed_refs))
        stage = getattr(error, "validation_stage", None)
        if not isinstance(stage, str) or stage not in VALIDATION_STAGES:
            stage = ("DECISION_VALIDATION" if isinstance(error, OrderedWorldBatchValidationError)
                     else "RESPONSE_SCHEMA")
        diagnostic = {
            "attempt_number": attempt_number,
            "rule_code": issue.get("reason_code", "RESPONSE_SCHEMA_INVALID"),
            "candidate_refs": candidate_refs,
            "selected_properties": selected_properties,
            "allowed_matched_properties": list(index.values())[:MAX_DIAGNOSTIC_PROPERTIES],
            "stage": stage,
            "phase": "RECOVERY" if phase == "RECOVERY" else "BATCH",
        }
        if type(decision_index) is int and 0 <= decision_index < 20:
            diagnostic["decision_index"] = decision_index
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
    return diagnostics
