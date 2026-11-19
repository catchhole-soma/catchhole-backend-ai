"""Actual OWL-RL execution and its independent provenance/output boundary."""

import builtins
import importlib
import json
import socket
from copy import deepcopy
from pathlib import Path

import pytest
from rdflib import OWL

from scripts.relationship_inference_experiment import owl_engine
from scripts.relationship_inference_experiment.policy import prepare

FIXTURE = Path(__file__).parent / "fixtures" / "relationship_ontology_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def prepared(case):
    execution = deepcopy(case)
    execution.pop("expected", None)
    return prepare(execution)


def canonical(value, key=None):
    if isinstance(value, dict):
        return {name: canonical(child, name) for name, child in sorted(value.items())}
    if isinstance(value, list):
        items = [canonical(child) for child in value]
        # Premise order represents roles in a chain, not a semantic set.
        return items if key == "premiseAssertionIds" else sorted(items, key=json.dumps)
    return value


@pytest.mark.parametrize("case", CASES, ids=lambda row: row["caseId"])
def test_actual_owl_closure_matches_all_handwritten_base_results(case):
    expected = {
        "caseId": case["caseId"],
        **{key: value for key, value in case["expected"].items() if key != "forbiddenRelations"},
        "excludedDerivedRelations": [],
    }
    assert canonical(owl_engine.run(prepared(case))) == canonical(expected)


def test_declared_axioms_and_measured_real_engine_execution(monkeypatch):
    ontology = owl_engine._ontology()
    assert (
        owl_engine.REL.biologicalParent,
        OWL.inverseOf,
        owl_engine.REL.biologicalChild,
    ) in ontology
    assert (owl_engine.REL.adoptiveParent, OWL.inverseOf, owl_engine.REL.adoptiveChild) in ontology
    assert list(ontology.objects(owl_engine.REL.biologicalGrandparent, OWL.propertyChainAxiom))
    calls = []
    real = owl_engine.DeductiveClosure

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(owl_engine, "DeductiveClosure", tracked)
    result, metrics = owl_engine.run_measured(prepared(CASES[2]))
    assert result["outcome"] == "READY"
    assert len(calls) == 1
    assert calls[0][0] == (owl_engine.OWLRL_Semantics,)
    assert metrics["closureTripleCount"] > metrics["inputTripleCount"]
    assert metrics["inputFactTripleCount"] == 2
    assert all(
        metrics[key] > 0 for key in ("constructionMs", "inferenceMs", "proofMs", "projectionMs")
    )


def test_removing_property_chain_cannot_be_hidden_by_python_proof_reconstruction(monkeypatch):
    real = owl_engine._ontology

    def without_chain():
        graph = real()
        graph.remove((None, OWL.propertyChainAxiom, None))
        return graph

    monkeypatch.setattr(owl_engine, "_ontology", without_chain)
    with pytest.raises(owl_engine.InferenceMismatchError, match="missing=1, extra=0"):
        owl_engine.run(prepared(CASES[2]))


@pytest.mark.parametrize(
    "kind", ["parent", "trust", "ancestor", "unsupported", "derived", "sameAs"]
)
def test_unexpected_engine_edges_are_rejected_not_filtered(monkeypatch, kind):
    real = owl_engine.DeductiveClosure

    class CorruptClosure:
        def __init__(self, *args, **kwargs):
            self.engine = real(*args, **kwargs)

        def expand(self, graph):
            parents = list(graph.triples((None, owl_engine.REL.biologicalParent, None)))
            self.engine.expand(graph)
            left, _, right = parents[0]
            predicate = {
                "parent": owl_engine.REL.biologicalParent,
                "trust": owl_engine.REL.trust,
                "ancestor": owl_engine.REL["ancestor/BIOLOGICAL/3"],
                "unsupported": owl_engine.REL.uncatalogued,
                "derived": owl_engine.REL.biologicalGrandparent,
                "sameAs": OWL.sameAs,
            }[kind]
            graph.add((left, predicate, right if kind == "sameAs" else left))

    monkeypatch.setattr(owl_engine, "DeductiveClosure", CorruptClosure)
    with pytest.raises(owl_engine.InferenceMismatchError):
        owl_engine.run(prepared(CASES[2]))


def test_engine_cannot_drop_a_direct_input_edge(monkeypatch):
    real = owl_engine.DeductiveClosure

    class MissingInput:
        def __init__(self, *args, **kwargs):
            self.engine = real(*args, **kwargs)

        def expand(self, graph):
            self.engine.expand(graph)
            graph.remove((None, owl_engine.REL.biologicalParent, None))

    monkeypatch.setattr(owl_engine, "DeductiveClosure", MissingInput)
    with pytest.raises(owl_engine.InferenceMismatchError, match="lost a direct"):
        owl_engine.run(prepared(CASES[2]))


def test_collapsed_edges_keep_every_two_by_two_assertion_proof():
    case = deepcopy(CASES[14])
    assertion = deepcopy(next(row for row in case["assertions"] if row["assertionId"] == "a3"))
    assertion.update(assertionId="a3b", sourceIds=["s3b"])
    source = deepcopy(next(row for row in case["sources"] if row["sourceId"] == "s3"))
    source["sourceId"] = "s3b"
    case["assertions"].append(assertion)
    case["sources"].append(source)
    result, metrics = owl_engine.run_measured(prepared(case))
    derived = next(row for row in result["derivedRelations"] if row["kind"] == "GRANDPARENT_OF")
    assert {tuple(proof["premiseAssertionIds"]) for proof in derived["proofs"]} == {
        ("a1", "a3"),
        ("a1", "a3b"),
        ("a1b", "a3"),
        ("a1b", "a3b"),
    }
    assert len(derived["proofs"]) == 4
    assert metrics["inputFactTripleCount"] == 2
    assert len(result["directRelations"]) == 2


def test_multiple_sources_support_one_assertion_proof_not_extra_assertions():
    case = deepcopy(CASES[2])
    case["sources"].append({**deepcopy(case["sources"][0]), "sourceId": "s1b"})
    next(row for row in case["assertions"] if row["assertionId"] == "a1")["sourceIds"].append("s1b")
    result = owl_engine.run(prepared(case))
    derived = next(row for row in result["derivedRelations"] if row["kind"] == "GRANDPARENT_OF")
    assert derived["proofs"] == [
        {
            "ruleId": "R_BIOLOGICAL_GRANDPARENT",
            "premiseAssertionIds": ["a1", "a3"],
            "sourceIds": ["s1", "s1b", "s3"],
        }
    ]


def test_each_call_recomputes_after_retraction_without_retaining_a_closure():
    first = owl_engine.run(prepared(CASES[2]))
    withdrawn = owl_engine.run(prepared(CASES[15]))
    restored = owl_engine.run(prepared(CASES[2]))
    assert any(row["kind"] == "GRANDPARENT_OF" for row in first["derivedRelations"])
    assert not any(row["kind"] == "GRANDPARENT_OF" for row in withdrawn["derivedRelations"])
    assert restored == first


def test_declined_query_does_not_invoke_owl(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported/rejected input must not invoke the inference engine")

    monkeypatch.setattr(owl_engine, "DeductiveClosure", forbidden)
    for index in (10, 18, 19, 22):
        _, metrics = owl_engine.run_measured(prepared(CASES[index]))
        assert metrics["inputTripleCount"] == metrics["closureTripleCount"] == 0
        assert metrics["constructionMs"] == metrics["inferenceMs"] == metrics["proofMs"] == 0


def test_runtime_neither_imports_b_nor_accesses_the_network(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.endswith("python_engine"):
            raise AssertionError("C must not import B")
        return original_import(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("offline experiment attempted a network connection")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    importlib.reload(owl_engine)
    assert owl_engine.run(prepared(CASES[2]))["outcome"] == "READY"


def test_extension_is_not_silently_enabled_before_the_base_is_frozen():
    with pytest.raises(NotImplementedError, match="not installed"):
        owl_engine.run(prepared(CASES[2]), extended=True)
