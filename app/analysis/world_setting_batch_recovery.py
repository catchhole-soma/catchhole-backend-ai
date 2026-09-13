"""Bounded, ordered-only world comparison recovery over one frozen input.

Never salvage an unvalidated provider decision. Each recovery group is regenerated,
and overlapping read/write paths are compared together before a mixed completion.
"""

from dataclasses import dataclass, field
import re

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_world_diagnostics import validation_diagnostics
from app.analysis.ordered_world_rule_diagnostics import VALIDATION_PHASES, VALIDATION_STAGES
from app.analysis.world_setting_comparator import (
    ComparisonTargetReference, _backend_duplicate_key, _validate_batch_comparison_result,
)
from app.analysis.world_setting_schemas import WorldSettingComparisonBatchResult
from app.domain.enums import AnalysisFailureCode, WorldSettingOperation
from app.exceptions.failure_classification import comparison_failure_code, is_candidate_comparison_failure
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchFailure,
    WorkerWorldSettingComparisonDiagnostic,
    WorkerWorldSettingDiagnosticProperty,
)

MAX_RECOVERY_CALLS = 20
RECOVERABLE_RESPONSE_CODES = frozenset({
    AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
    AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
    AnalysisFailureCode.LLM_OUTPUT_TRUNCATED,
})


def can_recover_response(exc):
    return is_candidate_comparison_failure(exc) and comparison_failure_code(exc) in RECOVERABLE_RESPONSE_CODES


def _name(value):
    return None if value is None else _backend_duplicate_key(value)


def _diagnostic_history(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        history = getattr(exc, "validation_diagnostics", None)
        if isinstance(history, (list, tuple)):
            return list(history)
        exc = exc.__cause__ or exc.__context__
    return []


def map_diagnostics(history, candidates, targets, *, attempt_offset=0):
    """Only request-owned source refs and actual selected input properties leave AI."""
    candidate_refs = {item.candidate_ref for item in candidates}
    properties = {
        f"T{ti}.P{pi}": (f"T{ti}", target, prop)
        for ti, target in enumerate(targets, 1)
        for pi, prop in enumerate(target.properties, 1)
    }
    output = []
    for row in history[:30]:
        if not isinstance(row, dict):
            continue
        rule = row.get("rule_code")
        attempt = row.get("attempt_number")
        if not isinstance(rule, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", rule):
            continue
        if type(attempt) is not int or not 1 <= attempt + attempt_offset <= 30:
            continue
        selected = []
        for selection in row.get("selected_properties", []):
            if not isinstance(selection, dict):
                continue
            entry = properties.get(selection.get("ref"))
            if entry is None or selection.get("target_ref") != entry[0]:
                continue
            _, target, prop = entry
            if selection.get("scope_name") != prop.scope_name or selection.get("setting_name") != prop.setting_name:
                continue
            item = WorkerWorldSettingDiagnosticProperty(
                world_setting_id=target.world_setting_id,
                provisional_subject_key=target.provisional_subject_key,
                scope_name=prop.scope_name, property_name=prop.setting_name,
            )
            if item not in selected:
                selected.append(item)
        output.append(WorkerWorldSettingComparisonDiagnostic(
            attempt=attempt + attempt_offset, rule=rule,
            stage=(row["stage"] if isinstance(row.get("stage"), str)
                   and row["stage"] in VALIDATION_STAGES else None),
            phase=(row["phase"] if isinstance(row.get("phase"), str)
                   and row["phase"] in VALIDATION_PHASES else None),
            candidate_refs=list(dict.fromkeys(
                ref for ref in row.get("candidate_refs", [])
                if isinstance(ref, str) and ref in candidate_refs
            )), selected_properties=selected[:20],
        ))
    return output


def exception_diagnostics(exc, candidates, targets, *, attempt_offset=0):
    return map_diagnostics(_diagnostic_history(exc), candidates, targets, attempt_offset=attempt_offset)


@dataclass
class _Group:
    candidates: list
    decisions: list = field(default_factory=list)
    failure_code: AnalysisFailureCode | None = None
    diagnostics: list = field(default_factory=list)

    @property
    def refs(self):
        return [item.candidate_ref for item in self.candidates]


@dataclass
class WorldBatchRecoveryResult:
    decisions: list
    failures: list[WorkerWorldSettingComparisonBatchFailure]
    diagnostics: list[WorkerWorldSettingComparisonDiagnostic]
    recovery_calls: int


def _path(target, scope, name):
    return target, _name(scope), _name(name)


def _overlap(left, right):
    if left[0] != right[0]:
        return False
    return (left == right or
            (left[1] is None and right[1] == left[2]) or
            (right[1] is None and left[1] == right[2]))


def _footprints(group):
    reads, writes = set(), set()
    for decision in group.decisions:
        if decision.matched_property_name is not None:
            reads.add(_path(decision.target_ref, decision.matched_scope_name, decision.matched_property_name))
        if decision.operation in {WorldSettingOperation.ADD, WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}:
            writes.add(_path(decision.target_ref, decision.proposed_scope_name, decision.proposed_setting_name))
    return reads, writes


def _dependent(left, right):
    lr, lw = _footprints(left)
    rr, rw = _footprints(right)
    return any(_overlap(a, b) for a in lw for b in rr | rw) or any(
        _overlap(a, b) for a in rw for b in lr | lw
    )


def _components(groups):
    remaining = list(groups)
    output = []
    while remaining:
        component = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for group in list(remaining):
                if any(_dependent(group, member) for member in component):
                    remaining.remove(group)
                    component.append(group)
                    changed = True
        output.append(component)
    return output


def _failure(group, history):
    refs = set(group.refs)
    diagnostics = [
        item.model_copy(update={"candidate_refs": [ref for ref in item.candidate_refs if ref in refs]})
        for item in history if not item.candidate_refs or refs.intersection(item.candidate_refs)
    ][-30:]
    return WorkerWorldSettingComparisonBatchFailure(
        source_candidate_refs=group.refs,
        failure_code=group.failure_code or AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
        error_message="분리 재비교에서도 안전한 반영 결과를 확정하지 못해 해당 설정만 검토 대상으로 남겼습니다.",
        diagnostics=diagnostics,
    )


async def recover_world_batch(comparator, category, candidates, targets, initial_error, *,
                             unresolved_references=(), max_recovery_calls=MAX_RECOVERY_CALLS):
    """Regenerate original-path groups, reconcile dependencies, then revalidate union.

    Limited to response failures: quota/lease/input/API and unexpected errors escape.
    All calls see the same original target snapshot; no failed/interim result becomes
    a selectable existing property. ADD paths and root moves are restricted during
    recovery, so successful groups do not rely on another group's synthetic scope.
    """
    history = exception_diagnostics(initial_error, candidates, targets)
    initial_attempts = max((item.attempt for item in history), default=min(getattr(comparator, "max_attempts", 3), 30))
    if not history:
        history.append(WorkerWorldSettingComparisonDiagnostic(
            attempt=max(1, initial_attempts), rule=comparison_failure_code(initial_error).value,
            phase="BATCH",
        ))
    initial_groups = {}
    for candidate in candidates:
        initial_groups.setdefault((_name(candidate.scope_name), _name(candidate.setting_name)), []).append(candidate)
    calls = 0
    references = [ComparisonTargetReference(reference=f"T{i}", target=target) for i, target in enumerate(targets, 1)]

    def fail(group, rule):
        group.decisions = []
        group.failure_code = AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
        diagnostic = WorkerWorldSettingComparisonDiagnostic(
            attempt=max(1, min(30, initial_attempts + calls)), rule=rule, candidate_refs=group.refs,
            phase="RECOVERY",
        )
        group.diagnostics.append(diagnostic)
        history.append(diagnostic)

    async def run(source):
        nonlocal calls
        group = _Group(source)
        if calls >= min(MAX_RECOVERY_CALLS, max_recovery_calls) or initial_attempts + calls >= 30:
            fail(group, "RECOVERY_CALL_LIMIT")
            return group
        calls += 1
        offset = initial_attempts + calls - 1
        try:
            result, raw = await comparator.compare_batch(
                category, source, targets, ordered_context=True,
                max_attempts_override=1, preserve_source_paths=True,
                **({"unresolved_references": tuple(unresolved_references)} if unresolved_references else {}),
            )
            group.decisions = result.decisions
            # Protect the fallback boundary even for alternate comparator implementations.
            for decision in group.decisions:
                if decision.existing_root_property_names_to_move:
                    raise ComparisonValidationError("Recovery cannot move existing root properties.")
                if decision.operation == WorldSettingOperation.ADD and any(
                    (_name(candidate.scope_name), _name(candidate.setting_name)) !=
                    (_name(decision.proposed_scope_name), _name(decision.proposed_setting_name))
                    for candidate in source if candidate.candidate_ref in decision.source_candidate_refs
                ):
                    raise ComparisonValidationError("Recovery ADD must preserve each source path.")
            _validate_batch_comparison_result(result, source, references, ordered_context=True)
            history.extend(map_diagnostics(raw.get("validation_diagnostics", []), source, targets, attempt_offset=offset))
        except Exception as exc:
            if not is_candidate_comparison_failure(exc):
                raise
            group.decisions = []
            group.failure_code = comparison_failure_code(exc)
            group.diagnostics = exception_diagnostics(exc, source, targets, attempt_offset=offset)
            if not group.diagnostics:
                group.diagnostics = [WorkerWorldSettingComparisonDiagnostic(
                    attempt=offset + 1, rule=group.failure_code.value, candidate_refs=group.refs,
                    phase="RECOVERY",
                )]
            history.extend(group.diagnostics)
        return group

    groups = [await run(source) for source in initial_groups.values()]
    # Recompare groups sharing a read/write path together; never choose a winner.
    while True:
        components = _components([group for group in groups if group.failure_code is None])
        conflicts = [component for component in components if len(component) > 1]
        if not conflicts:
            break
        for component in conflicts:
            refs = {ref for group in component for ref in group.refs}
            merged = await run([candidate for candidate in candidates if candidate.candidate_ref in refs])
            groups = [group for group in groups if group not in component] + [merged]

    # Individual contracts are not sufficient: check final combined coverage/paths.
    while True:
        successful = [group for group in groups if group.failure_code is None]
        if not successful:
            break
        refs = {ref for group in successful for ref in group.refs}
        decisions = [decision for group in successful for decision in group.decisions]
        try:
            _validate_batch_comparison_result(
                WorldSettingComparisonBatchResult(decisions=decisions),
                [candidate for candidate in candidates if candidate.candidate_ref in refs],
                references, ordered_context=True,
            )
            break
        except ValueError as exc:
            # Record the union-validation rule separately from the established
            # failure-boundary marker. Diagnostic-only refs cannot narrow it.
            details = validation_diagnostics(
                {"candidates": [{"ref": item.candidate_ref} for item in candidates], "targets": []},
                max(1, min(30, initial_attempts + calls)), exc, None, phase="RECOVERY",
            )
            history.extend(map_diagnostics(details, candidates, targets))
            affected_refs = set(getattr(exc, "source_candidate_refs", ()) or ())
            affected = [group for group in successful if affected_refs.intersection(group.refs)] or successful
            for group in affected:
                fail(group, "RECOVERY_COMBINED_CONTRACT_INVALID")

    return WorldBatchRecoveryResult(
        decisions=[decision for group in groups if group.failure_code is None for decision in group.decisions],
        failures=[_failure(group, history) for group in groups if group.failure_code is not None],
        diagnostics=history[-30:], recovery_calls=calls,
    )
