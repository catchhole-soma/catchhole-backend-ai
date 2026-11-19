"""Handwritten policy snapshots and independent B/C execution against them.

These are repeated snapshot computations, not persistence/event-reducer tests.
Oracle integrity checks inspect declared proofs; they do not discover or create
missing expected conclusions. Runtime functions receive only parsed input.
"""

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.relationship_inference_experiment import owl_engine, python_engine
from scripts.relationship_inference_experiment.evaluate import (
    RELATION_FIELDS,
    evaluate,
    normalize_result,
)
from scripts.relationship_inference_experiment.input import parse_cases
from scripts.relationship_inference_experiment.policy import prepare

FIXTURES = Path(__file__).parent / "fixtures"
BASE = json.loads((FIXTURES / "relationship_ontology_cases.json").read_text())
POLICY = json.loads((FIXTURES / "relationship_inference_policy_cases.json").read_text())
ENGINES = [pytest.param(python_engine.run, id="Python"), pytest.param(owl_engine.run, id="OWL")]


def relation_key(row):
    return tuple(row[field] for field in RELATION_FIELDS)


def sources_for(case, assertion):
    sources = {row["sourceId"]: row for row in case["sources"]}
    return {
        ref
        for ref in assertion["sourceIds"]
        if sources[ref]["supportStatus"] == "ACTIVE"
        and sources[ref]["episodeNo"] <= case["query"]["cutoffEpisode"]
    }


def oracle_result(case):
    return {
        "caseId": case["caseId"],
        "excludedDerivedRelations": [],
        **{key: value for key, value in case["expected"].items() if key != "forbiddenRelations"},
    }


def validate_declared_oracle(case):
    result = normalize_result(oracle_result(case))
    assertions = {row["assertionId"]: row for row in case["assertions"]}
    entities = {row["entityId"]: row for row in case["entities"]}
    source_rows = {row["sourceId"]: row for row in case["sources"]}
    supports = set()
    for direct in result["directRelations"]:
        declared_sources = set()
        for ref in direct["supportAssertionIds"]:
            assertion = assertions[ref]
            assert relation_key(assertion) == relation_key(direct)
            assert assertion["assertionState"] == "ASSERTED"
            assert assertion["factValidity"] == "VALID" and not assertion["validationIssues"]
            assert assertion["workId"] == case["query"]["workId"]
            assert assertion["temporalContextId"] == case["query"]["temporalContextId"]
            for endpoint in (assertion["subjectId"], assertion["objectId"]):
                assert entities[endpoint]["identityStatus"] == "RESOLVED"
                assert entities[endpoint]["workId"] == assertion["workId"]
            live_sources = sources_for(case, assertion)
            assert live_sources
            assert all(
                source_rows[source]["workId"] == assertion["workId"] for source in live_sources
            )
            declared_sources.update(live_sources)
            supports.add(ref)
        assert declared_sources == set(direct["sourceIds"])
    withheld = {row["assertionId"] for row in result["withheldAssertions"]}
    assert supports.isdisjoint(withheld)
    assert supports | withheld == set(assertions)
    for derived in result["derivedRelations"]:
        for proof in derived["proofs"]:
            refs = proof["premiseAssertionIds"]
            assert set(refs) <= supports
            premises = [assertions[ref] for ref in refs]
            declared_sources = set().union(*(sources_for(case, premise) for premise in premises))
            assert declared_sources == set(proof["sourceIds"])
            first = premises[0]
            if proof["ruleId"] == "R_PARENT_INVERSE":
                assert len(premises) == 1 and first["kind"] == "PARENT_OF"
                assert derived["derivationType"] == "SEMANTIC_INVERSE"
                wanted = {field: first[field] for field in RELATION_FIELDS}
                wanted.update(
                    subjectId=first["objectId"], objectId=first["subjectId"], kind="CHILD_OF"
                )
            else:
                assert proof["ruleId"] == "R_BIOLOGICAL_GRANDPARENT"
                assert len(premises) == 2
                second = premises[1]
                assert first["kind"] == second["kind"] == "PARENT_OF"
                assert first["subtype"] == second["subtype"] == "BIOLOGICAL"
                assert first["workId"] == second["workId"]
                assert first["temporalContextId"] == second["temporalContextId"]
                assert first["objectId"] == second["subjectId"]
                assert derived["derivationType"] == "NEW_RELATION"
                wanted = {field: first[field] for field in RELATION_FIELDS}
                wanted.update(objectId=second["objectId"], kind="GRANDPARENT_OF", generationGap=2)
            assert relation_key(derived) == relation_key(wanted)
    suppressions = {row["suppressionId"]: row for row in case.get("suppressions", [])}
    for excluded in result["excludedDerivedRelations"]:
        for ref in excluded["suppressionIds"]:
            suppression = suppressions[ref]
            assert suppression["active"]
            assert suppression["recordedRevision"] <= case["snapshotRevision"]
            assert relation_key(suppression) == relation_key(excluded)


@pytest.mark.parametrize("document", [BASE, POLICY], ids=["original24", "policy27"])
def test_handwritten_oracles_have_valid_declared_proofs_without_generating_answers(document):
    assert len(parse_cases(document)) == len(document["cases"])
    for case in document["cases"]:
        validate_declared_oracle(case)


def test_inventory_and_lifecycle_snapshots_are_explicit():
    assert POLICY["schemaVersion"] == 2
    assert POLICY["oracle"] == "HANDWRITTEN_NOT_ENGINE_GENERATED"
    assert len(POLICY["cases"]) == 27
    assert Counter(case["expected"]["outcome"] for case in BASE["cases"]) == {
        "READY": 20,
        "REJECTED": 3,
        "UNSUPPORTED": 1,
    }
    lifecycle = POLICY["cases"][:4]
    assert [case["snapshotRevision"] for case in lifecycle] == [1, 2, 3, 4]
    assert [
        sum(
            len(row["proofs"])
            for row in case["expected"]["derivedRelations"]
            if row["kind"] == "GRANDPARENT_OF"
        )
        for case in lifecycle
    ] == [1, 2, 1, 0]
    four = POLICY["cases"][4]["expected"]["derivedRelations"][-1]
    assert [proof["premiseAssertionIds"] for proof in four["proofs"]] == [
        ["ab", "bd"],
        ["ab", "bd2"],
        ["ab2", "bd"],
        ["ab2", "bd2"],
    ]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("case", POLICY["cases"], ids=lambda case: case["caseId"])
def test_independent_engines_match_each_handwritten_policy_snapshot(engine, case):
    document = {**POLICY, "cases": [case]}
    parsed = parse_cases(document)[0]
    actual = engine(prepare(parsed))
    report = evaluate([actual], document)
    assert report["passed"], report


class OracleTrap(dict):
    def __getitem__(self, key):
        if key in {"expected", "forbiddenRelations", "consumerExamples", "rules"}:
            raise AssertionError(f"execution accessed oracle/display field {key}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default

    def items(self):
        for key in self:
            yield key, self[key]


def selected_policy():
    return {
        **deepcopy(POLICY),
        "cases": [deepcopy(POLICY["cases"][index]) for index in (4, 6, 12, 14, 22)],
    }


@pytest.mark.parametrize("engine", ENGINES)
def test_execution_is_invariant_to_missing_changed_and_unreadable_oracles(engine):
    document = selected_policy()
    baseline = [engine(prepare(case)) for case in parse_cases(document)]
    for variant in ("missing", "changed", "unreadable"):
        changed = deepcopy(document)
        for case in changed["cases"]:
            if variant == "missing":
                del case["expected"]
            elif variant == "changed":
                case["expected"] = {"wrong": "the runtime must not inspect this"}
        if variant == "unreadable":
            changed["cases"] = [OracleTrap(case) for case in changed["cases"]]
            changed = OracleTrap(changed)
        actual = [engine(prepare(case)) for case in parse_cases(changed)]
        assert actual == baseline


@pytest.mark.parametrize("engine", ENGINES)
def test_input_permutation_preserves_assertion_roles_and_all_proof_alternatives(engine):
    document = selected_policy()
    baseline = [engine(prepare(case)) for case in parse_cases(document)]
    changed = deepcopy(document)
    changed["cases"].reverse()
    for case in changed["cases"]:
        for field in ("entities", "sources", "assertions", "suppressions"):
            case[field].reverse()
        for assertion in case["assertions"]:
            assertion["sourceIds"].reverse()
            assertion["validationIssues"].reverse()
    actual = [engine(prepare(case)) for case in parse_cases(changed)]
    assert sorted(actual, key=lambda row: row["caseId"]) == sorted(
        baseline, key=lambda row: row["caseId"]
    )
    assert evaluate(actual, document)["passed"]
