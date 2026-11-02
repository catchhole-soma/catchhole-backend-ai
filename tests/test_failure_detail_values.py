"""Observed validation values survive failure reporting without raw responses."""
import asyncio
import pytest
from app.schemas.worker import WorkerFailureDiagnostics
from app.exceptions.failure_classification import failure_diagnostics
from app.analysis.relationship_comparator import RelationshipComparator, relationship_comparison_prompt_data
from tests.test_relationship_comparator import context, decision


def test_optional_checks_keep_numeric_values_and_drop_unbounded_text():
    d = WorkerFailureDiagnostics(origin="WORKER", stage="RELATIONSHIP_COMPARE", reasonCode="RELATIONSHIP_INPUT_TOO_LARGE", checks=[
        {"field": "inputTokens", "rule": "MAX", "expected": "64000", "actual": "72001"},
        {"field": "selectedSubjectRefs", "rule": "IN_SET", "expected": "S1,S2", "actual": "Bearer_PRIVATE"},
    ])
    assert d.model_dump(by_alias=True, exclude_none=True).get("checks") == [
        {"field": "inputTokens", "rule": "MAX", "expected": "64000", "actual": "72001"}
    ]


def test_relationship_response_preserves_actual_unknown_reference():
    with pytest.raises(Exception) as failure:
        RelationshipComparator._validate_response({"decisions": [decision(observationRefs=["O999"])]},
                                                   relationship_comparison_prompt_data(context()))
    d = failure_diagnostics(failure.value, stage="RELATIONSHIP_COMPARE")
    assert d.model_dump(by_alias=True, exclude_none=True).get("checks") == [
        {"field": "observationRefs", "rule": "IN_SET", "expected": "O1", "actual": "O999", "itemIndex": 1}
    ]


def test_relationship_budget_records_measured_value_and_limit(monkeypatch):
    from tests.test_relationship_comparison_recovery import PerPairClient, disjoint_context, observation_budget
    observation_budget(monkeypatch)
    with pytest.raises(Exception) as failure:
        asyncio.run(RelationshipComparator(PerPairClient(), max_input_tokens=90).compare(disjoint_context(1)))
    d = failure_diagnostics(failure.value, stage="RELATIONSHIP_COMPARE")
    assert d.model_dump(by_alias=True, exclude_none=True).get("checks") == [
        {"field": "inputTokens", "rule": "MAX", "expected": "90", "actual": "100"}
    ]


def test_duplicate_ref_reports_uniqueness_instead_of_invalid_membership():
    with pytest.raises(Exception) as failure:
        RelationshipComparator._validate_response({"decisions": [decision(observationRefs=["O1", "O1"])]},
                                                   relationship_comparison_prompt_data(context()))
    checks = failure_diagnostics(failure.value, stage="RELATIONSHIP_COMPARE").checks
    assert [(c.rule, c.expected, c.actual) for c in checks] == [("UNIQUE", "O1", "O1,O1")]


def test_invalid_ref_after_eight_valid_refs_is_still_visible():
    from copy import deepcopy
    current = context()
    observation = current["observations"][0]
    current["observations"] = [dict(deepcopy(observation), ref=f"O{i}") for i in range(1, 10)]
    with pytest.raises(Exception) as failure:
        RelationshipComparator._validate_response({"decisions": [decision(observationRefs=[f"O{i}" for i in range(1, 10)] + ["O999"])]},
                                                   relationship_comparison_prompt_data(current))
    checks = failure_diagnostics(failure.value, stage="RELATIONSHIP_COMPARE").checks
    assert checks[0].actual == "O999"
