import asyncio
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.world_setting_batch_recovery import (
    can_recover_response, exception_diagnostics, map_diagnostics, recover_world_batch,
)
from app.analysis.world_setting_schemas import WorldSettingComparisonBatchResult
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.schemas.worker import WorkerWorldSettingComparisonBatchCandidate, WorkerWorldSettingComparisonTarget


def candidate(index, name=None):
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=f"C{index}", candidate_id=uuid4(), subject_name="고블린",
        setting_name=name or f"설정{index}", extracted_value=f"근거{index}",
        evidence_spans=[{"quote": f"근거{index}"}],
    )


def target(properties=None):
    return WorkerWorldSettingComparisonTarget(
        world_setting_id=uuid4(), subject_name="고블린", version=1, properties=properties or [],
    )


def decision(sources, *, operation="ADD", name=None, value=None):
    return {
        "source_candidate_refs": [item.candidate_ref for item in sources],
        "consolidation_status": "SINGLE" if len(sources) == 1 else "MERGED",
        "operation": operation, "review_reason": None, "target_ref": "T1",
        "matched_scope_name": None,
        "matched_property_name": name if operation in {"UPDATE", "MERGE"} else None,
        "proposed_scope_name": None, "proposed_setting_name": name or sources[0].setting_name,
        "proposed_value": value or " / ".join(item.extracted_value for item in sources),
        "comparison_reason": "원문에 명시된 사실을 반영합니다.",
        "existing_root_property_names_to_move": [],
    }


class Comparator:
    max_attempts = 3

    def __init__(self, action):
        self.action = action
        self.calls = []

    async def compare_batch(self, category, sources, targets, **kwargs):
        assert kwargs["ordered_context"] and kwargs["preserve_source_paths"]
        assert kwargs["max_attempts_override"] == 1
        self.calls.append([item.candidate_ref for item in sources])
        output = self.action(sources)
        if isinstance(output, Exception):
            raise output
        return WorldSettingComparisonBatchResult(decisions=output), {}


def run(comparator, sources, existing=None):
    return asyncio.run(recover_world_batch(
        comparator, "MONSTER", sources, [existing or target()],
        ComparisonValidationError("initial response failed"),
    ))


def test_failed_item_does_not_discard_independent_valid_candidates():
    sources = [candidate(i) for i in range(1, 6)]
    comparator = Comparator(lambda rows: ComparisonValidationError("invalid")
                            if rows[0].candidate_ref == "C2" else [decision(rows)])
    result = run(comparator, sources)
    assert [item.source_candidate_refs for item in result.decisions] == [["C1"], ["C3"], ["C4"], ["C5"]]
    assert [item.source_candidate_refs for item in result.failures] == [["C2"]]
    assert result.failures[0].failure_code == "COMPARISON_VALIDATION_FAILED"
    assert result.recovery_calls == 5
    assert result.failures[0].diagnostics[-1].candidate_refs == ["C2"]


def test_same_original_path_sources_are_regenerated_together():
    sources = [candidate(1, "독"), candidate(2, "독"), candidate(3, "무기")]
    comparator = Comparator(lambda rows: [decision(rows)])
    result = run(comparator, sources)
    assert comparator.calls == [["C1", "C2"], ["C3"]]
    assert not result.failures
    assert result.decisions[0].source_candidate_refs == ["C1", "C2"]


@pytest.mark.parametrize("joint_failure", [False, True])
def test_shared_existing_path_is_recompared_as_one_dependency_group(joint_failure):
    sources = [candidate(i) for i in range(1, 4)]
    existing = target([{"setting_name": "숙련도", "value": "기초"}])

    def action(rows):
        if rows[0].candidate_ref == "C3":
            return [decision(rows)]
        if len(rows) > 1 and joint_failure:
            return ComparisonValidationError("joint comparison invalid")
        return [decision(rows, operation="MERGE", name="숙련도")]

    comparator = Comparator(action)
    result = run(comparator, sources, existing)
    assert comparator.calls == [["C1"], ["C2"], ["C3"], ["C1", "C2"]]
    if joint_failure:
        assert [item.source_candidate_refs for item in result.decisions] == [["C3"]]
        assert [item.source_candidate_refs for item in result.failures] == [["C1", "C2"]]
    else:
        assert not result.failures
        assert len(result.decisions) == 2


@pytest.mark.parametrize("error", [
    AiTokenQuotaExhaustedError(), OrderedInputContextError("input changed"), RuntimeError("bug"),
])
def test_recovery_stops_immediately_on_job_level_errors(error):
    comparator = Comparator(lambda rows: error)
    with pytest.raises(type(error)):
        run(comparator, [candidate(1), candidate(2)])
    assert comparator.calls == [["C1"]]
    assert not can_recover_response(error)


def test_recovery_disallows_new_synthetic_scope_even_from_alternate_comparator():
    def action(rows):
        value = decision(rows)
        if rows[0].candidate_ref == "C1":
            value["proposed_scope_name"] = "임의 분류"
        return [value]
    result = run(Comparator(action), [candidate(1), candidate(2)])
    assert [item.source_candidate_refs for item in result.failures] == [["C1"]]
    assert [item.source_candidate_refs for item in result.decisions] == [["C2"]]


def test_recovery_call_limit_does_not_choose_one_of_conflicting_writes():
    sources = [candidate(i) for i in range(1, 21)]
    comparator = Comparator(lambda rows: [decision(rows, operation="UPDATE", name="숙련도")])
    result = run(comparator, sources, target([{"setting_name": "숙련도", "value": "기초"}]))
    assert len(comparator.calls) == 20
    assert not result.decisions
    assert len(result.failures[0].source_candidate_refs) == 20
    assert result.failures[0].diagnostics[-1].rule == "RECOVERY_CALL_LIMIT"


def test_diagnostic_mapper_uses_only_input_owned_selected_paths():
    existing = target([{"scope_name": "전투", "setting_name": "독", "value": "private value"}])
    result = map_diagnostics([{
        "attempt_number": 2, "rule_code": "SOURCE_SCOPE_MISMATCH",
        "candidate_refs": ["C1", "C9", "PRIVATE_TEXT"],
        "selected_properties": [
            {"ref": "T1.P1", "target_ref": "T1", "scope_name": "전투", "setting_name": "독"},
            {"ref": "T1.P1", "target_ref": "T1", "scope_name": "PRIVATE_TEXT", "setting_name": "독"},
            {"ref": "T1.P9", "target_ref": "T1", "scope_name": "전투", "setting_name": "비밀"},
        ],
        "response": "PRIVATE_TEXT",
    }], [candidate(1)], [existing])
    assert result[0].candidate_refs == ["C1"]
    assert len(result[0].selected_properties) == 1
    assert result[0].selected_properties[0].world_setting_id == existing.world_setting_id
    wire = result[0].model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire["selectedProperties"][0]["targetWorldSettingId"] == str(existing.world_setting_id)
    assert "worldSettingId" not in wire["selectedProperties"][0]
    serialized = result[0].model_dump_json()
    assert "PRIVATE_TEXT" not in serialized and "private value" not in serialized


def test_exhausted_comparator_tuple_history_preserves_final_attempt_through_wrapping():
    error = ComparisonValidationError("invalid response")
    error.validation_diagnostics = ({
        "attempt_number": 3, "rule_code": "CANONICAL_TARGET_REQUIRED",
        "candidate_refs": ["C1"], "selected_properties": [],
    },)
    wrapped = ComparisonValidationError("comparison failed")
    wrapped.__cause__ = error
    diagnostics = exception_diagnostics(wrapped, [candidate(1)], [target()])
    assert len(diagnostics) == 1
    assert diagnostics[0].attempt == 3
    assert diagnostics[0].rule == "CANONICAL_TARGET_REQUIRED"
    assert diagnostics[0].candidate_refs == ["C1"]
