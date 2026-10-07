"""Evaluation-only projection of explicit model decisions before failed validation discards them."""

import json

from pydantic import ValidationError

from app.analysis.ordered_world_batch_contract import _VALIDATION_CODES
from app.analysis.ordered_world_rule_diagnostics import RULE_CODES, VALIDATION_STAGES
from app.domain.enums import WorldSettingComparisonReviewReason, WorldSettingOperation
from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name
from evals.multi_stage_setting.contracts import (
    ComparisonAttemptDiagnostic,
    ComparisonDecisionDiagnostic,
)


def capture_world_comparison_attempt(attempt_number, error, response_payload, context,
                                     source_ids_by_ref, targets):
    """Keep only requested explanation fields; neither evidence nor provider/request payloads."""
    code = getattr(error, "diagnostic_rule_code", None) or getattr(error, "reason_code", None)
    if code not in set(RULE_CODES.values()) | _VALIDATION_CODES:
        code = RULE_CODES.get(error.args[0]) if (
            type(error) is ValueError and len(error.args) == 1 and type(error.args[0]) is str
        ) else None
    if code is None:
        code = ("RESPONSE_SCHEMA_INVALID" if isinstance(error, ValidationError) else
                "RESPONSE_JSON_INVALID" if isinstance(error, json.JSONDecodeError) else
                "COMPARISON_VALIDATION_FAILED")
    context = context if isinstance(context, dict) else {}
    request_refs = context.get("request_candidate_refs")
    if isinstance(request_refs, (list, tuple)):
        source_ids_by_ref = {ref: ids for ref, ids in source_ids_by_ref.items() if ref in request_refs}
    refs = context.get("candidate_refs", ())
    rejected = _source_ids(refs, source_ids_by_ref) if code not in {
        "RESPONSE_SCHEMA_INVALID", "RESPONSE_JSON_INVALID", "COMPARISON_VALIDATION_FAILED",
    } else []
    stage = context.get("stage")
    if stage not in VALIDATION_STAGES:
        stage = "RESPONSE_SCHEMA"
    target_by_ref = {f"T{index}": target for index, target in enumerate(targets, 1)}
    raw_decisions = response_payload.get("decisions") if isinstance(response_payload, dict) else None
    decisions = []
    for index, raw in enumerate(raw_decisions[:20] if isinstance(raw_decisions, list) else []):
        if not isinstance(raw, dict):
            continue
        target = target_by_ref.get(raw.get("target_ref")) if type(raw.get("target_ref")) is str else None
        matched = None
        if target is not None and type(raw.get("matched_property_name")) is str:
            matched = next((prop for prop in target.properties if
                _same_name(prop.scope_name, raw.get("matched_scope_name")) and
                _same_name(prop.setting_name, raw.get("matched_property_name"))), None)
        operation = raw.get("operation")
        review_reason = raw.get("review_reason")
        decisions.append(ComparisonDecisionDiagnostic(
            decision_index=index,
            source_candidate_ids=_source_ids(raw.get("source_candidate_refs"), source_ids_by_ref),
            operation=operation if type(operation) is str and operation in {item.value for item in WorldSettingOperation} else None,
            review_reason=review_reason if type(review_reason) is str and review_reason in {
                item.value for item in WorldSettingComparisonReviewReason
            } else None,
            comparison_reason=_text(raw.get("comparison_reason"), 4000),
            target=_text(target.subject_name, 500) if target is not None else None,
            proposed_scope_name=_text(raw.get("proposed_scope_name"), 500),
            proposed_setting_name=_text(raw.get("proposed_setting_name"), 500),
            proposed_value=_text(raw.get("proposed_value"), 4000),
            matched_scope_name=_text(matched.scope_name, 500) if matched is not None else None,
            matched_property_name=_text(matched.setting_name, 500) if matched is not None else None,
            matched_path_verified=matched is not None,
        ))
    return ComparisonAttemptDiagnostic(
        attempt_number=attempt_number, rule_code=code, stage=stage,
        attribution="IDENTIFIED" if rejected else "UNKNOWN",
        batch_source_ids=_source_ids(context.get("request_candidate_refs", list(source_ids_by_ref)), source_ids_by_ref),
        rejected_source_ids=rejected, decisions=decisions,
    )


def _source_ids(refs, source_ids_by_ref):
    if not isinstance(refs, (list, tuple)) or not refs or len(refs) > 20 or not all(
        type(ref) is str and ref in source_ids_by_ref for ref in refs
    ):
        return []
    return list(dict.fromkeys(source_id for ref in refs for source_id in source_ids_by_ref[ref]))


def _text(value, limit):
    if type(value) is not str or not value.strip():
        return None
    suffix = "… (길어 일부 생략)"
    return value if len(value) <= limit else value[:limit - len(suffix)] + suffix


def _same_name(left, right):
    if left is None or right is None:
        return left is right
    return type(right) is str and normalize_world_setting_name(left) == normalize_world_setting_name(right)


def candidate_comparison_attempts(attempts, candidate_id):
    """Keep exact request history; absent legacy scope remains conservatively relevant."""
    return [attempt for attempt in attempts
            if not attempt.batch_source_ids or candidate_id in attempt.batch_source_ids]


def comparison_failure_case(failure, candidate_id, source_labels_by_id=None):
    """Project one candidate's proposed judgment; the batch error is not proof about other decisions."""
    relevant_attempts = candidate_comparison_attempts(failure.comparison_attempts, candidate_id)
    if not relevant_attempts:
        return None
    latest = relevant_attempts[-1]
    source_labels_by_id = source_labels_by_id or {}
    status = ("REJECTED" if candidate_id in latest.rejected_source_ids else
              "BATCH_ABORTED" if latest.attribution == "IDENTIFIED" else "UNKNOWN")
    attempts = []
    for attempt in relevant_attempts:
        decision = next((item for item in attempt.decisions if candidate_id in item.source_candidate_ids), None)
        attempts.append({
            "attemptNumber": attempt.attempt_number, "ruleCode": attempt.rule_code,
            "stage": attempt.stage, "rejectedSourceIds": attempt.rejected_source_ids,
            "rejectedSourceNames": [source_labels_by_id[source_id] for source_id in attempt.rejected_source_ids
                                    if source_id in source_labels_by_id],
            "decision": ({
                "operation": decision.operation.value if decision.operation else None,
                "reviewReason": decision.review_reason, "comparisonReason": decision.comparison_reason,
                "target": decision.target,
                "path": _path(decision.proposed_scope_name, decision.proposed_setting_name),
                "value": decision.proposed_value,
                "matchedPath": _path(decision.matched_scope_name, decision.matched_property_name),
                "matchedPathVerified": decision.matched_path_verified,
            } if decision is not None else None),
        })
    return {"status": status, "attempts": attempts}


def _path(scope, name):
    return f"{scope} › {name}" if scope and name else name
