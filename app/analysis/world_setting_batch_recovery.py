"""Validate independent world decisions and retry affected atomic groups once."""

from dataclasses import dataclass, field
import re

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_world_diagnostics import validation_diagnostics
from app.analysis.ordered_world_rule_diagnostics import VALIDATION_PHASES, VALIDATION_STAGES
from app.analysis.world_setting_comparator import (
    ComparisonTargetReference, _backend_duplicate_key, _validate_batch_comparison_result,
    _project_batch_comparison_result,
)
from app.analysis.ordered_world_batch_contract import OrderedWorldPropertyBatchResult
from app.analysis.ordered_world_diagnostics import restore_selected_properties
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
    generated_scopes: set = field(default_factory=set)
    dependency_reads: set = field(default_factory=set)
    dependency_writes: set = field(default_factory=set)

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
    reads, writes = set(group.dependency_reads), set(group.dependency_writes)
    for decision in group.decisions:
        if decision.matched_property_name is not None:
            reads.add(_path(decision.target_ref, decision.matched_scope_name, decision.matched_property_name))
        if decision.operation in {WorldSettingOperation.ADD, WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE}:
            writes.add(_path(decision.target_ref, decision.proposed_scope_name, decision.proposed_setting_name))
        for name in decision.existing_root_property_names_to_move:
            source = _path(decision.target_ref, None, name)
            reads.add(source)
            writes.add(source)
            writes.add(_path(decision.target_ref, decision.proposed_scope_name, name))
    return reads, writes


def _dependent(left, right):
    lr, lw = _footprints(left)
    rr, rw = _footprints(right)
    return bool(set(left.refs) & set(right.refs) or left.generated_scopes & right.generated_scopes) or any(_overlap(a, b) for a in lw for b in rr | rw) or any(
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
        error_message="재비교에서도 안전한 반영 결과를 확정하지 못해 해당 설정 비교가 실패했습니다.",
        diagnostics=diagnostics,
    )


def _response_rows(error):
    payload = getattr(error, "world_batch_response_payload", None)
    rows = payload.get("decisions") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) and 1 <= len(rows) <= 20 else None


def _validate_group(result, source, references, ordered_context, *, complete_scope_plan=False):
    _validate_batch_comparison_result(
        result, source, references, ordered_context=ordered_context,
        require_generated_scope_siblings=complete_scope_plan,
    )
    return _project_batch_comparison_result(result, source, references)


def _mark_generated_scopes(group, targets):
    for decision in group.decisions:
        if decision.operation != WorldSettingOperation.ADD or decision.proposed_scope_name is None:
            continue
        scope = _name(decision.proposed_scope_name)
        # Groups creating one shared structure are dependent even with distinct
        # child paths: removing one may invalidate the remaining scope's siblings.
        if any(_name(item.scope_name) != scope for item in group.candidates):
            group.generated_scopes.add((decision.target_ref, scope))


def _record_rejected_dependencies(group, row, request, references):
    """Read only known target identity and bounded paths; never authorize proposals."""
    if not isinstance(row, dict):
        return
    target_ref = row.get("target_ref")
    if target_ref is not None and (not isinstance(target_ref, str)
                                  or target_ref not in {reference.reference for reference in references}):
        return
    name, scope = row.get("proposed_setting_name"), row.get("proposed_scope_name")
    path_valid = (isinstance(name, str) and 1 <= len(name.strip()) <= 100
                  and (scope is None or isinstance(scope, str) and 1 <= len(scope.strip()) <= 100))
    if path_valid and row.get("operation") in ("ADD", "UPDATE", "MERGE"):
        group.dependency_writes.add(_path(target_ref, scope, name))
        if scope is not None and any(_name(source.scope_name) != _name(scope) for source in group.candidates):
            group.generated_scopes.add((target_ref, _name(scope)))
    selected = row.get("matched_property_ref")
    matched_name, matched_scope = row.get("matched_property_name"), row.get("matched_scope_name")
    matched_path_valid = (isinstance(matched_name, str)
                          and (matched_scope is None or isinstance(matched_scope, str)))
    for reference in references:
        if reference.reference != target_ref:
            continue
        for index, prop in enumerate(reference.target.properties, 1):
            if selected == f"{target_ref}.P{index}" or (
                selected is None and matched_path_valid and _name(matched_name) == _name(prop.setting_name)
                and _name(matched_scope) == _name(prop.scope_name)
            ):
                group.dependency_reads.add(_path(target_ref, prop.scope_name, prop.setting_name))
        moves = row.get("existing_root_property_names_to_move")
        if isinstance(moves, list) and path_valid:
            for name in moves:
                if not isinstance(name, str):
                    continue
                if any(prop.scope_name is None and _name(prop.setting_name) == _name(name)
                       for prop in reference.target.properties):
                    original = _path(target_ref, None, name)
                    group.dependency_reads.add(original)
                    group.dependency_writes.update({original, _path(target_ref, scope, name)})


async def compare_world_batch_with_recovery(comparator, category, candidates, targets, *,
                                          ordered_context=False, unresolved_references=(),
                                          max_recovery_calls=MAX_RECOVERY_CALLS,
                                          evaluation_failure_callback=None):
    """Shared production and FIXED comparison boundary for every review mode.

    Returns (WorldBatchRecoveryResult, raw_result). Successful sources occur once
    in decisions; failed sources occur once in failures. Existing input references
    remain request-local and unchanged across every bounded retry.
    """
    kwargs = {"ordered_context": ordered_context, "max_attempts_override": 1}
    if unresolved_references:
        kwargs["unresolved_references"] = tuple(unresolved_references)
    if evaluation_failure_callback is not None:
        kwargs["evaluation_failure_callback"] = evaluation_failure_callback
    initial_error = None
    try:
        result, raw = await comparator.compare_batch(category, candidates, targets, **kwargs)
    except Exception as error:
        if not can_recover_response(error):
            raise
        initial_error = error
    if initial_error is not None:
        # Leave the handler before awaiting so an unrelated retry fault does not
        # inherit the rejected response as its implicit exception context.
        recovered = await recover_world_batch(
            comparator, category, candidates, targets, initial_error,
            ordered_context=ordered_context, unresolved_references=unresolved_references,
            max_recovery_calls=max_recovery_calls,
            evaluation_failure_callback=evaluation_failure_callback,
        )
        return recovered, {
            "recoveryMode": "VALIDATED_INDEPENDENT_DECISIONS",
            "recoveryCalls": recovered.recovery_calls,
            "decisions": [item.model_dump(mode="json") for item in recovered.decisions],
        }
    return WorldBatchRecoveryResult(
        decisions=result.decisions, failures=[],
        diagnostics=map_diagnostics(raw.get("validation_diagnostics", []), candidates, targets),
        recovery_calls=0,
    ), raw


async def recover_world_batch(comparator, category, candidates, targets, initial_error, *,
                             ordered_context=True, unresolved_references=(),
                             max_recovery_calls=MAX_RECOVERY_CALLS,
                             evaluation_failure_callback=None):
    """Preserve independently validated decisions; retry each affected component once.

    Rejected proposals are used only to find conservative dependencies. They never
    become completion decisions until schema, actual path and structural validation
    succeeds. Invalid JSON has no salvageable decisions and retries the whole input
    once. Quota, lease, fixed-input and provider execution errors always escape.
    """
    if not can_recover_response(initial_error):
        raise initial_error
    history = exception_diagnostics(initial_error, candidates, targets)
    initial_attempts = max((item.attempt for item in history), default=1)
    if not history:
        history.append(WorkerWorldSettingComparisonDiagnostic(
            attempt=initial_attempts, rule=comparison_failure_code(initial_error).value, phase="BATCH",
        ))
    references = [ComparisonTargetReference(reference=f"T{i}", target=target)
                  for i, target in enumerate(targets, 1)]
    allowed = {item.candidate_ref: item for item in candidates}
    calls = 0

    def fail(group, rule, code=AnalysisFailureCode.COMPARISON_VALIDATION_FAILED):
        group.failure_code = code
        diagnostic = WorkerWorldSettingComparisonDiagnostic(
            attempt=min(30, max(1, initial_attempts + calls)), rule=rule,
            candidate_refs=group.refs, phase="RECOVERY",
        )
        history.append(diagnostic)

    async def retry(group):
        nonlocal calls
        if calls >= min(MAX_RECOVERY_CALLS, max_recovery_calls):
            fail(group, "RECOVERY_CALL_LIMIT")
            return group
        calls += 1
        offset = initial_attempts + calls - 1
        kwargs = {"ordered_context": ordered_context, "max_attempts_override": 1,
                  "defer_generated_scope_validation": True}
        if unresolved_references:
            kwargs["unresolved_references"] = tuple(unresolved_references)
        if evaluation_failure_callback is not None:
            def callback(attempt, error, payload, context):
                evaluation_failure_callback(attempt + offset, error, payload, context)
            kwargs["evaluation_failure_callback"] = callback
        try:
            result, raw = await comparator.compare_batch(category, group.candidates, targets, **kwargs)
            group.decisions = _validate_group(result, group.candidates, references, ordered_context).decisions
            group.failure_code = None
            group.dependency_reads.clear()
            group.dependency_writes.clear()
            group.generated_scopes.clear()
            _mark_generated_scopes(group, targets)
            history.extend(map_diagnostics(raw.get("validation_diagnostics", []), group.candidates,
                                           targets, attempt_offset=offset))
        except Exception as error:
            if not can_recover_response(error):
                raise
            reads, writes = _footprints(group)
            group.dependency_reads.update(reads)
            group.dependency_writes.update(writes)
            # A second invalid decision response cannot earn another retry or an
            # unvalidated partial success, including within a merged source group.
            for row in _response_rows(error) or []:
                _record_rejected_dependencies(
                    group, row, getattr(error, "world_batch_request_payload", {}), references,
                )
            group.decisions = []
            fail(group, comparison_failure_code(error).value, comparison_failure_code(error))
            history.extend(exception_diagnostics(error, group.candidates, targets, attempt_offset=offset))
        return group

    rows = _response_rows(initial_error)
    if rows is None:
        groups = [await retry(_Group(list(candidates)))]
    else:
        request = getattr(initial_error, "world_batch_request_payload", {})
        groups = []
        covered = set()
        for row in rows:
            raw_refs = row.get("source_candidate_refs") if isinstance(row, dict) else None
            refs = [ref for ref in raw_refs if isinstance(ref, str) and ref in allowed] if isinstance(raw_refs, list) else []
            source = [item for item in candidates if item.candidate_ref in refs]
            if not source:
                continue
            group = _Group(source)
            covered.update(refs)
            _record_rejected_dependencies(group, row, request, references)
            try:
                if ordered_context:
                    parsed = OrderedWorldPropertyBatchResult.model_validate({"decisions": [row]})
                    result = restore_selected_properties(parsed, request)
                else:
                    result = WorldSettingComparisonBatchResult.model_validate({"decisions": [row]})
                # Retain typed paths for dependency analysis even if domain validation
                # rejects the decision. These are never returned as successful data.
                group.decisions = result.decisions
                group.decisions = _validate_group(result, source, references, ordered_context).decisions
            except (TypeError, ValueError, ComparisonValidationError):
                fail(group, "RECOVERY_DECISION_INVALID")
            _mark_generated_scopes(group, targets)
            groups.append(group)
        missing = [item for item in candidates if item.candidate_ref not in covered]
        if missing:
            # Preserve the original same-path source unit for absent decisions.
            by_path = {}
            for item in missing:
                by_path.setdefault((_name(item.scope_name), _name(item.setting_name)), []).append(item)
            for source in by_path.values():
                group = _Group(source)
                fail(group, "SOURCE_REF_MISSING")
                groups.append(group)
        # Every overlapping source group is invalid, including a formerly valid
        # decision that shares a source with a rejected sibling.
        for index, left in enumerate(groups):
            for right in groups[index + 1:]:
                if set(left.refs) & set(right.refs):
                    fail(left, "SOURCE_REF_DUPLICATED")
                    fail(right, "SOURCE_REF_DUPLICATED")

        def affected_union(successful):
            refs = {ref for group in successful for ref in group.refs}
            observed = []
            try:
                _validate_batch_comparison_result(
                    WorldSettingComparisonBatchResult(decisions=[d for group in successful for d in group.decisions]),
                    [item for item in candidates if item.candidate_ref in refs], references,
                    ordered_context=ordered_context,
                    validation_observer=lambda error, stage, index, source_refs: observed.extend(source_refs),
                )
                return []
            except (TypeError, ValueError, ComparisonValidationError) as error:
                affected = set(observed) | set(getattr(error, "source_candidate_refs", ()) or ())
                affected.update(getattr(error, "diagnostic_candidate_refs", ()) or ())
                return [group for group in successful if affected.intersection(group.refs)] or successful

        while True:
            successful = [group for group in groups if group.failure_code is None]
            affected = affected_union(successful) if successful else []
            if not affected:
                break
            for group in affected:
                fail(group, "RECOVERY_COMBINED_CONTRACT_INVALID")
        # Source overlap and actual reads/writes/structure define the atomic retry.
        components = _components(groups)
        retry_groups = []
        for component in components:
            if not any(group.failure_code is not None for group in component):
                retry_groups.extend(component)
                continue
            refs = {ref for group in component for ref in group.refs}
            joint = _Group([item for item in candidates if item.candidate_ref in refs])
            for member in component:
                reads, writes = _footprints(member)
                joint.dependency_reads.update(reads)
                joint.dependency_writes.update(writes)
                joint.generated_scopes.update(member.generated_scopes)
            retry_groups.append(await retry(joint))
        groups = retry_groups

    # One union validation after retries; new conflicts fail their complete
    # dependency component. No winner selection and no repeated regroup calls.
    while True:
        # A failed retry may expose a new dependency on a preserved decision.
        # Preserve attempted paths when failing; dependent successes cannot survive.
        for component in _components(groups):
            if any(group.failure_code is not None for group in component):
                for group in component:
                    if group.failure_code is None:
                        fail(group, "RECOVERY_DEPENDENCY_FAILED")
        successful = [group for group in groups if group.failure_code is None]
        if not successful:
            break
        refs = {ref for group in successful for ref in group.refs}
        observed = []
        try:
            _validate_batch_comparison_result(
                WorldSettingComparisonBatchResult(decisions=[d for group in successful for d in group.decisions]),
                [item for item in candidates if item.candidate_ref in refs], references,
                ordered_context=ordered_context,
                validation_observer=lambda error, stage, index, source_refs: observed.extend(source_refs),
            )
            break
        except (TypeError, ValueError, ComparisonValidationError) as error:
            affected = set(observed) | set(getattr(error, "source_candidate_refs", ()) or ())
            affected.update(getattr(error, "diagnostic_candidate_refs", ()) or ())
            invalid = [group for group in successful if affected.intersection(group.refs)] or successful
            for component in _components(successful):
                if any(group in invalid for group in component):
                    for group in component:
                        fail(group, "RECOVERY_COMBINED_CONTRACT_INVALID")
    groups.sort(key=lambda group: min(candidates.index(item) for item in group.candidates))
    decisions = [decision for group in groups if group.failure_code is None for decision in group.decisions]
    failures = [_failure(group, history) for group in groups if group.failure_code is not None]
    refs = [ref for decision in decisions for ref in decision.source_candidate_refs]
    refs.extend(ref for failure in failures for ref in failure.source_candidate_refs)
    if len(refs) != len(set(refs)) or set(refs) != set(allowed):
        raise ComparisonValidationError("Recovery must cover every source exactly once.")
    return WorldBatchRecoveryResult(
        decisions=decisions, failures=failures, diagnostics=history[-30:], recovery_calls=calls,
    )
