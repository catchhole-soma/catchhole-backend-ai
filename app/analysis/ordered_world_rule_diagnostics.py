"""Diagnostic-only detail for existing world validator rules.

This exception deliberately has no ``source_candidate_refs`` attribute: recovery
uses that existing attribute to choose a failure boundary. Adding observability
must not narrow an error that previously failed the whole combined result.
"""

from contextlib import contextmanager

VALIDATION_STAGES = frozenset({
    "RESPONSE_SCHEMA", "PROPERTY_SELECTION", "DECISION_VALIDATION",
    "SCOPE_PLAN", "PROJECTED_SCOPE_PLAN",
})
VALIDATION_PHASES = frozenset({"BATCH", "RECOVERY"})

# Exact developer-owned literals only. Never inspect arbitrary exception prose,
# stringify rejected values, or invent a rule for an unknown validator error.
RULE_CODES = {
    "The provider must not classify output-budget overflow.": "PROVIDER_BUDGET_REVIEW_FORBIDDEN",
    "SCOPE_UNRESOLVED must identify compatible ambiguous source candidates.": "SCOPE_UNRESOLVED_SOURCE_INVALID",
    "ADD must not include a matched property path.": "ADD_MATCH_FORBIDDEN",
    "A matched EXCLUDE requires target_ref.": "EXCLUDE_TARGET_REQUIRED",
    "UPDATE and MERGE require a matched target property.": "UPDATE_MERGE_MATCH_REQUIRED",
    "UPDATE and MERGE must preserve the stored scope name.": "STORED_SCOPE_CHANGED",
    "UPDATE and MERGE must preserve the stored property name.": "STORED_PROPERTY_CHANGED",
    "Batch decisions must not propose the same final path.": "FINAL_PATH_DUPLICATED",
    "Batch decisions must not propose scalar and scoped paths under the same top-level name.": "FINAL_ROOT_SCOPE_CONFLICT",
    "A scope name must differ from its setting name.": "SCOPE_EQUALS_PROPERTY",
    "Existing root properties may move only with a scoped ADD target.": "ROOT_MOVE_ADD_REQUIRED",
    "An existing root property may move only once per batch.": "ROOT_MOVE_REPEATED",
    "A requested root property move does not exist.": "ROOT_MOVE_SOURCE_NOT_FOUND",
    "A moved root property must be a distinct child of the proposed scope.": "ROOT_MOVE_CHILD_CONFLICT",
    "A requested root property move conflicts at its destination.": "ROOT_MOVE_DESTINATION_CONFLICT",
    "A moved root property must not also be updated or merged.": "ROOT_MOVE_UPDATE_CONFLICT",
    "comparison_reason must not expose a local reference.": "REASON_LOCAL_REFERENCE_FORBIDDEN",
    "comparison_reason must not expose a UUID.": "REASON_UUID_FORBIDDEN",
    "comparison_reason must not expose an internal enum or key.": "REASON_INTERNAL_TOKEN_FORBIDDEN",
}


class OrderedWorldRuleDiagnosticError(ValueError):
    """A fixed existing rule and local candidates, without changing recovery policy."""

    def __init__(self, code, stage, decision_index, candidate_refs):
        self.diagnostic_rule_code = code if code in RULE_CODES.values() else "COMPARISON_VALIDATION_FAILED"
        self.validation_stage = stage if stage in VALIDATION_STAGES else "DECISION_VALIDATION"
        self.decision_index = decision_index
        self.diagnostic_candidate_refs = tuple(candidate_refs)
        super().__init__(self.diagnostic_rule_code)


@contextmanager
def diagnose_world_rule(enabled, stage, decision_index=None, candidate_refs=()):
    """Annotate known response rules; unrelated/fixed-input exceptions escape unchanged."""
    try:
        yield
    except ValueError as error:
        if not enabled:
            raise
        if type(error) is ValueError and len(error.args) == 1 and type(error.args[0]) is str:
            code = RULE_CODES.get(error.args[0])
            if code is not None:
                raise OrderedWorldRuleDiagnosticError(code, stage, decision_index, candidate_refs) from error
        # Existing typed path errors and Pydantic validation errors retain their
        # original type, causal chain and candidate-isolation semantics.
        if stage in VALIDATION_STAGES and not hasattr(error, "validation_stage"):
            try:
                error.validation_stage = stage
            except Exception:  # A diagnostic annotation cannot replace the original error.
                pass
        raise
