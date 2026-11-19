"""Adversarial tests of the independent comparator, not an inference oracle generator."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.relationship_inference_experiment.evaluate import evaluate

FIXTURES = Path(__file__).parent / "fixtures"


def document(name="relationship_ontology_cases.json"):
    return json.loads((FIXTURES / name).read_text())


def actual_from_oracle(doc):
    # Comparator unit tests intentionally fabricate outputs. Engine tests live
    # in test_relationship_inference_cases.py and never use this helper.
    return [
        {
            "caseId": case["caseId"],
            "excludedDerivedRelations": [],
            **{
                key: deepcopy(value)
                for key, value in case["expected"].items()
                if key != "forbiddenRelations"
            },
        }
        for case in doc["cases"]
    ]


def selected(doc, index):
    doc["cases"] = [doc["cases"][index]]
    return doc


def test_original_and_policy_oracles_have_exact_output_shape():
    for filename, count in [
        ("relationship_ontology_cases.json", 24),
        ("relationship_inference_policy_cases.json", 27),
    ]:
        doc = document(filename)
        assert evaluate(actual_from_oracle(doc), doc) == {
            "passed": True,
            "caseCount": count,
            "passedCases": count,
            "failures": [],
        }


def test_set_order_is_ignored_but_duplicates_are_not():
    doc = document("relationship_inference_policy_cases.json")
    rows = actual_from_oracle(doc)
    rows.reverse()
    for row in rows:
        for category in (
            "directRelations",
            "derivedRelations",
            "withheldAssertions",
            "excludedDerivedRelations",
        ):
            row[category].reverse()
        for relation in row["directRelations"]:
            relation["supportAssertionIds"].reverse()
            relation["sourceIds"].reverse()
        for relation in row["derivedRelations"]:
            relation["proofs"].reverse()
            for proof in relation["proofs"]:
                proof["sourceIds"].reverse()
        for relation in row["excludedDerivedRelations"]:
            relation["suppressionIds"].reverse()
    assert evaluate(rows, doc)["passed"]
    rows.append(deepcopy(rows[0]))
    result = evaluate(rows, doc)
    assert not result["passed"]
    assert result["passedCases"] == 26


def mutate(row, corruption):
    direct, derived = row["directRelations"][0], row["derivedRelations"][-1]
    if corruption == "missing-direct":
        row["directRelations"].pop()
    elif corruption == "duplicate-direct":
        row["directRelations"].append(deepcopy(direct))
    elif corruption == "reversed-direct":
        direct["subjectId"], direct["objectId"] = direct["objectId"], direct["subjectId"]
    elif corruption in {"workId", "subtype", "temporalContextId", "kind"}:
        direct[corruption] = "different-value"
    elif corruption == "gap-bool":
        derived["generationGap"] = True
    elif corruption == "gap-value":
        derived["generationGap"] = 3
    elif corruption == "direct-support":
        direct["supportAssertionIds"] = ["another-assertion"]
    elif corruption == "duplicate-support":
        direct["supportAssertionIds"] *= 2
    elif corruption == "source-missing":
        derived["proofs"][0]["sourceIds"].pop()
    elif corruption == "source-extra":
        derived["proofs"][0]["sourceIds"].append("another-source")
    elif corruption == "source-duplicate":
        derived["proofs"][0]["sourceIds"] *= 2
    elif corruption == "wrong-rule":
        derived["proofs"][0]["ruleId"] = "R_NOT_THE_DECLARED_RULE"
    elif corruption == "reversed-premise-roles":
        derived["proofs"][0]["premiseAssertionIds"].reverse()
    elif corruption == "missing-proof":
        derived["proofs"].pop()
    elif corruption == "duplicate-proof":
        derived["proofs"].append(deepcopy(derived["proofs"][0]))
    elif corruption == "split-source-as-extra-proof":
        extra = deepcopy(derived["proofs"][0])
        extra["sourceIds"] = ["other-source"]
        derived["proofs"].append(extra)
    elif corruption == "derivation-type":
        derived["derivationType"] = "SEMANTIC_INVERSE"
    elif corruption == "withheld-reason":
        row["withheldAssertions"] = [{"assertionId": "a2", "reason": "WITHDRAWN_FACT"}]
    elif corruption == "unexpected-field":
        row["expected"] = "must not be ignored in actual output"
    elif corruption == "unexpected-conclusion":
        new = deepcopy(derived)
        new["objectId"] = "another-person"
        row["derivedRelations"].append(new)
    else:
        raise AssertionError(corruption)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-direct",
        "duplicate-direct",
        "reversed-direct",
        "workId",
        "subtype",
        "temporalContextId",
        "kind",
        "gap-bool",
        "gap-value",
        "direct-support",
        "duplicate-support",
        "source-missing",
        "source-extra",
        "source-duplicate",
        "wrong-rule",
        "reversed-premise-roles",
        "missing-proof",
        "duplicate-proof",
        "split-source-as-extra-proof",
        "derivation-type",
        "withheld-reason",
        "unexpected-field",
        "unexpected-conclusion",
    ],
)
def test_complete_tuple_and_proof_corruption_is_a_failure(corruption):
    doc = selected(document(), 2)
    rows = actual_from_oracle(doc)
    mutate(rows[0], corruption)
    report = evaluate(rows, doc)
    assert not report["passed"]
    assert report["passedCases"] == 0
    assert report["failures"]


def test_losing_only_one_of_four_proofs_is_a_failure():
    doc = selected(document("relationship_inference_policy_cases.json"), 4)
    rows = actual_from_oracle(doc)
    rows[0]["derivedRelations"][-1]["proofs"].pop()
    assert not evaluate(rows, doc)["passed"]


@pytest.mark.parametrize(
    "corruption", ["missing-id", "duplicate-id", "extra-proof", "wrong-reason", "duplicate-row"]
)
def test_excluded_derived_schema_and_suppression_ids_are_exact(corruption):
    doc = selected(document("relationship_inference_policy_cases.json"), 26)
    rows = actual_from_oracle(doc)
    excluded = rows[0]["excludedDerivedRelations"][0]
    if corruption == "missing-id":
        excluded["suppressionIds"].pop()
    elif corruption == "duplicate-id":
        excluded["suppressionIds"] *= 2
    elif corruption == "extra-proof":
        excluded["proofs"] = []
    elif corruption == "wrong-reason":
        excluded["reason"] = "MISSING_PREMISE"
    else:
        rows[0]["excludedDerivedRelations"].append(deepcopy(excluded))
    assert not evaluate(rows, doc)["passed"]


def test_missing_extra_and_duplicate_cases_fail():
    doc = selected(document(), 0)
    assert not evaluate([], doc)["passed"]
    rows = actual_from_oracle(doc)
    extra = dict(deepcopy(rows[0]), caseId="unexpected")
    assert not evaluate([*rows, extra], doc)["passed"]
    assert not evaluate([*rows, deepcopy(rows[0])], doc)["passed"]


def test_only_forbidden_display_hints_are_ignored():
    doc = selected(document(), 0)
    rows = actual_from_oracle(doc)
    doc["cases"][0]["expected"]["forbiddenRelations"] = object()
    assert evaluate(rows, doc)["passed"]
    rows[0]["forbiddenRelations"] = []
    assert not evaluate(rows, doc)["passed"]


@pytest.mark.parametrize(
    "corruption", ["missing-expected", "unknown-field", "duplicate-case", "duplicate-proof"]
)
def test_invalid_oracle_is_reported_not_repaired(corruption):
    doc = selected(document(), 2)
    rows = actual_from_oracle(doc)
    if corruption == "missing-expected":
        del doc["cases"][0]["expected"]
    elif corruption == "unknown-field":
        doc["cases"][0]["expected"]["quietlyIgnored"] = True
    elif corruption == "duplicate-case":
        doc["cases"].append(deepcopy(doc["cases"][0]))
    else:
        proofs = doc["cases"][0]["expected"]["derivedRelations"][-1]["proofs"]
        proofs.append(deepcopy(proofs[0]))
    report = evaluate(rows, doc)
    assert not report["passed"]
    assert report["failures"][0]["reason"] == "INVALID_ORACLE"
