"""Bounded HTTP error metadata; never copy response prose or rejected values."""

import json
import re


# Only reviewed server-owned codes enter messages/logs. The original typed code
# stays on SpringWorkerHttpError for existing control flow, even if not listed.
SAFE_ERROR_CODES = frozenset({
    "REQUEST_VALIDATION_FAILED", "REQUEST_INVALID_ARGUMENT", "COMMON_INTERNAL_SERVER_ERROR",
    "AUTH_UNAUTHORIZED", "AUTH_FORBIDDEN", "RESOURCE_NOT_FOUND", "CONFLICT",
    "SETTING_CANDIDATE_COMPARISON_TARGET_INVALID", "SETTING_CANDIDATE_COMPARISON_BATCH_RESPONSE_INVALID",
    "SETTING_CANDIDATE_COMPARISON_OPERATION_INVALID", "SETTING_CANDIDATE_VALUE_JSON_INVALID",
    "SETTING_CANDIDATE_VALUE_INVALID", "SETTING_CANDIDATE_VALUE_TYPE_MISMATCH",
    "SETTING_CANDIDATE_COMPARISON_STALE", "SETTING_CANDIDATE_COMPARISON_STATUS_CONFLICT",
    "SETTING_CANDIDATE_COMPARISON_BATCH_NOT_FOUND", "SETTING_CANDIDATE_WORKER_JOB_INVALID",
    "ANALYSIS_JOB_LEASE_CONFLICT", "ANALYSIS_JOB_STATUS_CONFLICT", "ANALYSIS_JOB_CHECKPOINT_INCOMPLETE",
    "ANALYSIS_RUN_STATE_CONFLICT", "ANALYSIS_RUN_PREDECESSOR_INCOMPLETE", "AI_TOKEN_QUOTA_EXHAUSTED",
    "WORLD_SETTING_COMPARISON_TARGET_INVALID", "WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE",
    "WORLD_SETTING_SUBJECT_RESOLUTION_STALE", "WORLD_SETTING_SUBJECT_RESOLUTION_INVALID",
    "WORLD_SETTING_COMPARISON_BATCH_STATUS_CONFLICT", "WORLD_SETTING_COMPARISON_BATCH_COMPLETION_CONFLICT",
    "WORLD_SETTING_WORKER_JOB_INVALID",
})
_DECISION_FIELDS = frozenset({
    "candidateRef", "sourceCandidateRefs", "operation", "resolvedCanonicalFactKey", "targetSnapshotRef",
    "removedSnapshotRefs", "dependencyCandidateRefs", "proposedFactValue", "proposedValueJson",
    "temporalScope", "comparisonReason", "rawComparisonJson", "failureCode", "errorMessage",
    "targetWorldSettingId", "provisionalSubjectKey", "proposedScopeName", "proposedSettingName",
    "proposedValue", "matchedScopeName", "matchedPropertyName", "reviewReason", "consolidationStatus",
})


def validation_fields(error: dict) -> tuple[str, ...]:
    """Keep only known DTO paths. The server supplies no constraint codes today."""
    details = error.get("details")
    if not isinstance(details, list):
        return ()
    fields = []
    for detail in details[:30]:
        field = detail.get("field") if isinstance(detail, dict) else None
        if not isinstance(field, str) or len(field) > 200:
            continue
        safe = None
        if field in {"contextToken", "decisions", "failures", "rawComparisonJson"}:
            safe = field
        else:
            match = re.fullmatch(r"(decisions|failures)\[(\d{1,2})\]\.([A-Za-z]+)(?:\[\d{1,2}\])?", field)
            if match and int(match[2]) < 20 and match[3] in _DECISION_FIELDS:
                safe = f"{match[1]}[].{match[3]}"
        if safe is not None and safe not in fields:
            fields.append(safe)
        if len(fields) == 8:
            break
    return tuple(fields)


def error_message(status: int, code: str | None, reason: str | None, fields: tuple[str, ...]) -> str:
    # reason has already passed the client's fixed reason-code allowlist.
    metadata = {"status": status}
    if code in SAFE_ERROR_CODES:
        metadata["code"] = code
    if reason is not None:
        metadata["reason_code"] = reason
    if fields:
        metadata["validation_fields"] = list(fields)
    return "Spring Worker API request rejected: " + json.dumps(metadata, separators=(",", ":"))
