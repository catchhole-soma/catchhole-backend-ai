"""Synthetic response replay; the actual rejected episode-33 response is unavailable."""

import asyncio
from copy import deepcopy
import json
import logging

import pytest

from app.analysis import world_setting_comparator as module
from app.analysis import world_setting_batch_recovery as recovery_module
from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.ordered_world_diagnostics import validation_diagnostics
from app.analysis.ordered_world_rule_diagnostics import (
    OrderedWorldRuleDiagnosticError, diagnose_world_rule,
)
from app.analysis.world_setting_batch_recovery import map_diagnostics, recover_world_batch
from app.analysis.world_setting_schemas import WorldSettingComparisonBatchResult
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.exceptions.failure_classification import comparison_failure_code
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerWorldSettingComparisonDiagnostic
from tests.test_ordered_world_property_selection import (
    candidate, decision, offline_dependencies, run, target,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("offline_dependencies")


def empty_target():
    return target(provisional=True).model_copy(update={"properties": []})


def test_five_source_replay_keeps_failure_boundary_and_last_batch_recovery_rules(caplog):
    sources = [candidate(f"C{i}", name=name) for i, name in enumerate(
        ("분류 방식", "분류 기준", "분류 기준", "정의", "핵심 특징"), 1,
    )]
    before = [row.model_dump() for row in sources]
    stored = empty_target()
    valid_shape = [decision(source) for source in sources]
    missing_target = deepcopy(valid_shape)
    missing_target[2]["target_ref"] = None
    # This is one possible invalid form, not a reconstruction of the lost output.
    duplicate_paths = deepcopy(valid_shape)
    duplicate_paths[2]["proposed_value"] = "PRIVATE_REJECTED_VALUE"
    responses = [missing_target, missing_target, duplicate_paths,
                 [valid_shape[0]], duplicate_paths[1:3], [valid_shape[3]], [valid_shape[4]]]
    requests = []

    class Client:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            return LlmTextResponse(text=json.dumps({"decisions": responses[len(requests) - 1]}))

    async def execute():
        comparator = module.WorldSettingComparator(
            llm_client=Client(), model="offline-test", max_attempts=3,
            max_output_tokens=3000, batch_max_output_tokens=16000,
        )
        try:
            await comparator.compare_batch("MONSTER", sources, [stored], ordered_context=True)
        except Exception as error:
            assert comparison_failure_code(error).value == "COMPARISON_VALIDATION_FAILED"
            assert "FINAL_PATH_DUPLICATED" in str(error)
            return await recover_world_batch(comparator, "MONSTER", sources, [stored], error)
        raise AssertionError("The invalid final paths must not be accepted")

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(execute())
    assert len(requests) == 7 and result.recovery_calls == 4
    assert [row.source_candidate_refs for row in result.decisions] == [["C1"], ["C4"], ["C5"]]
    assert [row.source_candidate_refs for row in result.failures] == [["C2", "C3"]]
    history = [row.model_dump(by_alias=True) for row in result.diagnostics]
    assert [(row["attempt"], row["rule"], row["phase"], row["stage"], row["candidateRefs"])
            for row in history] == [
        (1, "CANONICAL_TARGET_REQUIRED", "BATCH", "DECISION_VALIDATION", ["C3"]),
        (2, "CANONICAL_TARGET_REQUIRED", "BATCH", "DECISION_VALIDATION", ["C3"]),
        (3, "FINAL_PATH_DUPLICATED", "BATCH", "SCOPE_PLAN", ["C2", "C3"]),
        (5, "FINAL_PATH_DUPLICATED", "RECOVERY", "SCOPE_PLAN", ["C2", "C3"]),
    ]
    assert [row.model_dump() for row in sources] == before
    rendered = json.dumps(history) + caplog.text
    for private in ("PRIVATE_REJECTED_VALUE", "SECRET_SOURCE_EVIDENCE", str(stored.provisional_subject_key)):
        assert private not in rendered


@pytest.mark.parametrize("form,expected_stage,expected_rule", [
    ("schema", "RESPONSE_SCHEMA", "RESPONSE_SCHEMA_INVALID"),
    ("property", "PROPERTY_SELECTION", "MATCHED_PROPERTY_REF_INVALID"),
    ("reason", "DECISION_VALIDATION", "REASON_UUID_FORBIDDEN"),
    ("review", "DECISION_VALIDATION", "SCOPE_UNRESOLVED_SOURCE_INVALID"),
])
def test_failure_stage_and_rule_remain_typed_on_last_attempt(form, expected_stage, expected_rule):
    source = candidate(scope="행동 및 사냥 방식", name="함정 사용")
    response = decision(source, operation="UPDATE", matched_ref="T1.P1")
    if form == "schema":
        response["operation"] = "PRIVATE_UNKNOWN_OPERATION"
    elif form == "property":
        response["matched_property_ref"] = "PRIVATE_UNKNOWN_PROPERTY"
    elif form == "reason":
        response["comparison_reason"] = "사용자 설명 " + "a18b1bb6-2eb5-45b4-a30b-8f0694b0c123"
    else:
        response.update(operation="REVIEW_REQUIRED", review_reason="SCOPE_UNRESOLVED",
                        proposed_setting_name=source.setting_name, proposed_scope_name=None)
    error, calls = run([{"decisions": [response]}], [source], [target()], ordered_context=True)
    assert isinstance(error, Exception) and len(calls) == 3
    assert comparison_failure_code(error).value == "COMPARISON_VALIDATION_FAILED"
    assert error.validation_diagnostics[-1]["stage"] == expected_stage
    assert error.validation_diagnostics[-1]["phase"] == "BATCH"
    assert error.validation_diagnostics[-1]["rule_code"] == expected_rule
    assert error.validation_diagnostics[-1]["candidate_refs"] == ["C1"]
    assert "PRIVATE_UNKNOWN" not in json.dumps(error.validation_diagnostics)


def test_projected_scope_plan_is_distinguished_from_original_response_plan(monkeypatch):
    sources = [candidate("C1", name="정의"), candidate("C2", name="특징")]

    def conflicting_projection(result, *args):
        second = result.decisions[1].model_copy(update={"proposed_setting_name": "정의"})
        return result.model_copy(update={"decisions": [result.decisions[0], second]})

    monkeypatch.setattr(module, "_project_batch_comparison_result", conflicting_projection)
    error, calls = run([{"decisions": [decision(source) for source in sources]}],
                       sources, [empty_target()], ordered_context=True)
    assert len(calls) == 3
    assert error.validation_diagnostics[-1]["stage"] == "PROJECTED_SCOPE_PLAN"
    assert error.validation_diagnostics[-1]["candidate_refs"] == ["C1", "C2"]
    assert error.validation_diagnostics[-1]["rule_code"] == "FINAL_PATH_DUPLICATED"


@pytest.mark.parametrize("error", [
    OrderedInputContextError("fixed input"), AiTokenQuotaExhaustedError(), ValueError("provider execution"),
])
def test_execution_errors_are_never_reclassified_as_diagnostic_response_failures(error):
    result, calls = run([error], [candidate()], [empty_target()], ordered_context=True)
    assert result is error and len(calls) == 1
    assert not hasattr(result, "validation_diagnostics")


def test_unknown_error_stays_unknown_and_diagnostic_fields_are_allowlisted():
    error = ValueError("PRIVATE_ERROR_WITH_SOURCE")
    with pytest.raises(ValueError) as caught:
        with diagnose_world_rule(True, "SCOPE_PLAN", 0, ["C1"]):
            raise error
    assert caught.value is error
    source, stored = candidate(), target()
    payload = {"candidates": [{"ref": "C1"}], "targets": []}
    diagnostics = validation_diagnostics(payload, 3, error, None)
    assert diagnostics[0]["rule_code"] == "COMPARISON_VALIDATION_FAILED"
    assert diagnostics[0]["candidate_refs"] == []
    assert "PRIVATE_ERROR" not in json.dumps(diagnostics)
    diagnostics[0].update(stage={"private": "data"}, phase="UNKNOWN_PRIVATE")
    mapped = map_diagnostics(diagnostics, [source], [stored])
    assert mapped[0].stage is None and mapped[0].phase is None
    with pytest.raises(ValueError):
        WorkerWorldSettingComparisonDiagnostic(attempt=1, rule="VALID", stage="UNKNOWN_PRIVATE")


def test_legacy_validator_error_keeps_its_original_type_and_message():
    with pytest.raises(ValueError, match="same final path") as caught:
        with diagnose_world_rule(False, "SCOPE_PLAN", 0, ["C1"]):
            raise ValueError("Batch decisions must not propose the same final path.")
    assert type(caught.value) is ValueError


def test_diagnostic_only_rule_does_not_enable_narrower_combined_recovery_isolation():
    with pytest.raises(OrderedWorldRuleDiagnosticError) as caught:
        with diagnose_world_rule(True, "SCOPE_PLAN", 1, ["C1", "C2"]):
            raise ValueError("Batch decisions must not propose the same final path.")
    # recover_world_batch reads this existing attribute to narrow failures. The
    # newly known diagnostic candidates must not silently change that policy.
    assert not hasattr(caught.value, "source_candidate_refs")
    assert caught.value.diagnostic_candidate_refs == ("C1", "C2")


def test_combined_recovery_keeps_all_previous_failure_scope_with_detailed_rule(monkeypatch):
    sources = [candidate("C1", name="정의"), candidate("C2", name="특징")]
    validator = recovery_module._validate_batch_comparison_result

    def fail_union(result, *args, **kwargs):
        validator(result, *args, **kwargs)
        if len(result.decisions) == 2:
            with diagnose_world_rule(True, "SCOPE_PLAN", 1, ["C2"]):
                raise ValueError("Batch decisions must not propose the same final path.")

    class Comparator:
        max_attempts = 3

        async def compare_batch(self, category, candidates, targets, **kwargs):
            rows = []
            for source in candidates:
                row = decision(source)
                row.pop("matched_property_ref")
                row.update(matched_scope_name=None, matched_property_name=None)
                rows.append(row)
            return WorldSettingComparisonBatchResult.model_validate({"decisions": rows}), {}

    monkeypatch.setattr(recovery_module, "_validate_batch_comparison_result", fail_union)
    result = asyncio.run(recover_world_batch(
        Comparator(), "MONSTER", sources, [empty_target()], ComparisonValidationError("initial"),
    ))
    assert result.decisions == []
    assert [row.source_candidate_refs for row in result.failures] == [["C1"], ["C2"]]
    detail = next(row for row in result.diagnostics if row.rule == "FINAL_PATH_DUPLICATED")
    assert detail.candidate_refs == ["C2"]
    assert detail.stage == "SCOPE_PLAN" and detail.phase == "RECOVERY"
