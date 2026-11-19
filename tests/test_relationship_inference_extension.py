"""Independent grandchild extension oracles, trace integrity and base nonregression."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.relationship_inference_experiment import owl_engine, python_engine
from scripts.relationship_inference_experiment.evaluate import evaluate, normalize_result
from scripts.relationship_inference_experiment.input import parse_cases
from scripts.relationship_inference_experiment.policy import prepare

FIXTURES = Path(__file__).parent / "fixtures"
BASE = json.loads((FIXTURES / "relationship_ontology_cases.json").read_text())
POLICY = json.loads((FIXTURES / "relationship_inference_policy_cases.json").read_text())
EXTENSION = json.loads((FIXTURES / "relationship_inference_extension_cases.json").read_text())
ENGINES = [pytest.param(python_engine.run, id="Python"), pytest.param(owl_engine.run, id="OWL")]
FIELDS = (
    "workId",
    "subjectId",
    "objectId",
    "kind",
    "subtype",
    "temporalContextId",
    "generationGap",
)
PATH = ["R_BIOLOGICAL_GRANDPARENT", "R_GRANDPARENT_INVERSE"]


def one_document(case):
    return {**EXTENSION, "cases": [case]}


def fabricated_actual(case):
    # Used only to attack the evaluator, never as a replacement engine result.
    return {
        "caseId": case["caseId"],
        **{
            key: deepcopy(value)
            for key, value in case["expected"].items()
            if key != "forbiddenRelations"
        },
    }


def grandchild(result):
    return next(row for row in result["derivedRelations"] if row["kind"] == "GRANDCHILD_OF")


def test_declared_extension_traces_preserve_original_roles_and_sources():
    assert len(EXTENSION["cases"]) == 10
    assert EXTENSION["oracle"] == "HANDWRITTEN_NOT_ENGINE_GENERATED"
    parse_cases(EXTENSION)
    assert evaluate([fabricated_actual(case) for case in EXTENSION["cases"]], EXTENSION)["passed"]
    for case in EXTENSION["cases"]:
        assertions = {row["assertionId"]: row for row in case["assertions"]}
        sources = {row["sourceId"]: row for row in case["sources"]}
        for conclusion in case["expected"]["derivedRelations"]:
            if conclusion["kind"] != "GRANDCHILD_OF":
                assert all(
                    set(proof) == {"ruleId", "premiseAssertionIds", "sourceIds"}
                    for proof in conclusion["proofs"]
                )
                continue
            for proof in conclusion["proofs"]:
                first, second = [assertions[ref] for ref in proof["premiseAssertionIds"]]
                assert first["kind"] == second["kind"] == "PARENT_OF"
                assert first["subtype"] == second["subtype"] == "BIOLOGICAL"
                assert first["objectId"] == second["subjectId"]
                assert first["workId"] == second["workId"] == conclusion["workId"]
                assert (
                    first["temporalContextId"]
                    == second["temporalContextId"]
                    == conclusion["temporalContextId"]
                )
                assert proof["ruleId"] == "R_GRANDPARENT_INVERSE"
                assert proof["rulePath"] == PATH
                intermediate = proof["intermediateRelations"]
                assert intermediate == [
                    {
                        **{field: first[field] for field in FIELDS},
                        "objectId": second["objectId"],
                        "kind": "GRANDPARENT_OF",
                        "generationGap": 2,
                    }
                ]
                assert conclusion["subjectId"] == second["objectId"]
                assert conclusion["objectId"] == first["subjectId"]
                current_sources = {
                    ref
                    for premise in (first, second)
                    for ref in premise["sourceIds"]
                    if sources[ref]["supportStatus"] == "ACTIVE"
                    and sources[ref]["episodeNo"] <= case["query"]["cutoffEpisode"]
                }
                assert set(proof["sourceIds"]) == current_sources


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("case", EXTENSION["cases"], ids=lambda case: case["caseId"])
def test_extension_matches_independently_handwritten_exact_outputs(engine, case):
    document = one_document(case)
    actual = engine(prepare(parse_cases(document)[0]), extended=True)
    report = evaluate([actual], document)
    assert report["passed"], report


BASE_CASES = [*BASE["cases"], *POLICY["cases"]]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("case", BASE_CASES, ids=lambda case: case["caseId"])
def test_all_51_base_snapshots_are_unchanged_after_removing_only_grandchild_rows(engine, case):
    # Metamorphic nonregression, not an independent oracle for new conclusions.
    parsed = parse_cases({"schemaVersion": 2, "synthetic": True, "cases": [case]})[0]
    base = engine(prepare(parsed), extended=False)
    extended = engine(prepare(parsed), extended=True)
    projected = deepcopy(extended)
    for category in ("derivedRelations", "excludedDerivedRelations"):
        projected[category] = [row for row in projected[category] if row["kind"] != "GRANDCHILD_OF"]
    assert normalize_result(projected) == normalize_result(base)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-path",
        "missing-intermediate",
        "reversed-path",
        "missing-step",
        "wrong-terminal-rule",
        "wrong-intermediate-subject",
        "wrong-intermediate-context",
        "wrong-intermediate-work",
        "wrong-intermediate-kind",
        "wrong-intermediate-gap",
        "extra-intermediate",
        "reversed-original-roles",
        "missing-original-source",
        "extra-original-source",
        "trace-on-old-proof",
    ],
)
def test_corrupted_derivation_path_or_source_origin_is_rejected(corruption):
    case = EXTENSION["cases"][0]
    actual = fabricated_actual(case)
    proof = grandchild(actual)["proofs"][0]
    if corruption == "missing-path":
        del proof["rulePath"]
    elif corruption == "missing-intermediate":
        del proof["intermediateRelations"]
    elif corruption == "reversed-path":
        proof["rulePath"].reverse()
    elif corruption == "missing-step":
        proof["rulePath"].pop(0)
    elif corruption == "wrong-terminal-rule":
        proof["ruleId"] = "R_BIOLOGICAL_GRANDPARENT"
    elif corruption.startswith("wrong-intermediate-"):
        field, replacement = {
            "subject": ("subjectId", "another-person"),
            "context": ("temporalContextId", "other-time"),
            "work": ("workId", "other-work"),
            "kind": ("kind", "PARENT_OF"),
            "gap": ("generationGap", 3),
        }[corruption.removeprefix("wrong-intermediate-")]
        proof["intermediateRelations"][0][field] = replacement
    elif corruption == "extra-intermediate":
        proof["intermediateRelations"].append(deepcopy(proof["intermediateRelations"][0]))
    elif corruption == "reversed-original-roles":
        proof["premiseAssertionIds"].reverse()
    elif corruption == "missing-original-source":
        proof["sourceIds"].pop()
    elif corruption == "extra-original-source":
        proof["sourceIds"].append("unrelated-source")
    else:
        actual["derivedRelations"][0]["proofs"][0]["rulePath"] = list(PATH)
    assert not evaluate([actual], one_document(case))["passed"]


@pytest.mark.parametrize(
    "corruption",
    ["absent", "wrong-output", "wrong-premise-count", "boolean-count", "extra-field", "duplicate"],
)
def test_extension_oracle_requires_one_exact_rule_declaration(corruption):
    case = EXTENSION["cases"][0]
    document = deepcopy(one_document(case))
    declaration = document["rules"][-1]
    if corruption == "absent":
        document["rules"].pop()
    elif corruption == "wrong-output":
        declaration["outputKind"] = "CHILD_OF"
    elif corruption == "wrong-premise-count":
        declaration["premiseCount"] = 2
    elif corruption == "boolean-count":
        declaration["premiseCount"] = True
    elif corruption == "extra-field":
        declaration["silentlyIgnored"] = True
    else:
        document["rules"].append(deepcopy(declaration))
    report = evaluate([fabricated_actual(case)], document)
    assert not report["passed"]
    assert report["failures"][0]["reason"] == "INVALID_ORACLE"


def test_base_normalizer_does_not_silently_accept_extended_proof_fields():
    actual = fabricated_actual(EXTENSION["cases"][0])
    with pytest.raises(ValueError, match="explicitly extended oracle"):
        normalize_result(actual)
    assert normalize_result(actual, extended=True)["caseId"] == actual["caseId"]


@pytest.mark.parametrize("engine", ENGINES)
def test_extension_is_explicit_and_preserves_exact_proofs_under_input_permutation(engine):
    document = deepcopy(EXTENSION)
    first = [engine(prepare(case), extended=True) for case in parse_cases(document)]
    document["cases"].reverse()
    for case in document["cases"]:
        for field in ("entities", "sources", "assertions", "suppressions"):
            case[field].reverse()
        for assertion in case["assertions"]:
            assertion["sourceIds"].reverse()
        # Declared oracle/rules never choose the runtime extension flag.
        case.pop("expected")
    document.pop("rules")
    second = [engine(prepare(case), extended=True) for case in parse_cases(document)]
    assert sorted(first, key=lambda row: row["caseId"]) == sorted(
        second, key=lambda row: row["caseId"]
    )
    base = [engine(prepare(case), extended=False) for case in parse_cases(document)]
    assert all(
        row["kind"] != "GRANDCHILD_OF" for result in base for row in result["derivedRelations"]
    )
