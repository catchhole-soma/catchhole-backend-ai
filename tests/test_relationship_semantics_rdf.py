"""C executes SHACL and SPARQL against RDF, independent of the B evaluator."""

import json
import socket
from pathlib import Path

import pytest
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.collection import Collection

from scripts.relationship_semantics_experiment.input import load_document
from scripts.relationship_semantics_experiment.rdf_model import EX, RdfModel, validate_graph

FIXTURE = Path(__file__).parent / "fixtures" / "relationship_semantics_cases.json"


@pytest.fixture(scope="module")
def cases():
    return load_document(json.loads(FIXTURE.read_text())).cases


def resource(model, kind, index=0):
    return model._list(model.case_uri, kind)[index]


def first_fact(model):
    return resource(model, "facts")


def damage(model, mutation):
    graph = model.graph
    fact = first_fact(model)
    slot = graph.value(fact, EX.slot)
    claim = resource(model, "claims")
    source = resource(model, "sources")
    if mutation == "missing-type":
        graph.remove((fact, RDF.type, EX.Fact))
    elif mutation == "missing-field":
        graph.remove((fact, EX.status, None))
    elif mutation == "multiple-statuses":
        graph.add((fact, EX.status, Literal("ENDED")))
    elif mutation == "wrong-datatype":
        graph.set((fact, EX.knownFromEpisodeNo, Literal("one")))
    elif mutation == "unknown-property":
        graph.add((fact, EX.unmodeled, Literal("must not disappear")))
    elif mutation == "unknown-type":
        graph.add((fact, RDF.type, EX.UnmodeledClass))
    elif mutation == "unknown-reference":
        graph.set((slot, EX.sourceNodeRef, URIRef("urn:missing:node")))
    elif mutation == "wrong-quote":
        graph.set((source, EX.quote, Literal("not in synthetic text")))
    elif mutation == "wrong-work":
        graph.set((source, EX.workId, Literal("another-work")))
    elif mutation == "reversed-directed-proof":
        left = graph.value(claim, EX.subjectCandidateRefs)
        right = graph.value(claim, EX.objectCandidateRefs)
        graph.set((claim, EX.subjectCandidateRefs, right))
        graph.set((claim, EX.objectCandidateRefs, left))
    elif mutation == "ambiguous-proof":
        head = graph.value(claim, EX.subjectCandidateRefs)
        Collection(graph, head).append(resource(model, "nodes", 1))
    elif mutation == "duplicate-proof":
        head = graph.value(fact, EX.supportClaimRefs)
        Collection(graph, head).append(claim)
    elif mutation == "unconfirmed-proof":
        graph.set((claim, EX.curationStatus, Literal("HOLD")))
    elif mutation == "future-proof":
        graph.set((source, EX.episodeNo, Literal(2)))
    elif mutation == "known-before-proof":
        graph.set((source, EX.episodeNo, Literal(2)))
        graph.set((fact, EX.lastChangedEpisodeNo, Literal(2)))
    elif mutation == "base-affection":
        graph.set((slot, EX.kind, Literal("AFFECTION")))
    elif mutation == "invalid-list":
        head = graph.value(fact, EX.supportClaimRefs)
        graph.set((head, RDF.rest, head))
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-type",
        "missing-field",
        "multiple-statuses",
        "wrong-datatype",
        "unknown-property",
        "unknown-type",
        "unknown-reference",
        "wrong-quote",
        "wrong-work",
        "reversed-directed-proof",
        "ambiguous-proof",
        "duplicate-proof",
        "unconfirmed-proof",
        "future-proof",
        "known-before-proof",
        "base-affection",
        "invalid-list",
    ],
)
def test_standalone_shacl_rejects_damaged_rdf_without_input_or_b_validator(cases, mutation):
    model = RdfModel(cases[0])
    damage(model, mutation)
    result = validate_graph(model.graph)
    assert result["conforms"] is False
    assert result["violations"]


def test_standalone_shacl_requires_a_case():
    assert not validate_graph(Graph())["conforms"]


@pytest.mark.parametrize("location", ["list-head", "nested-cell", "nil", "orphan"])
def test_unmodeled_list_or_untyped_annotations_cannot_silently_disappear(cases, location):
    model = RdfModel(cases[0])
    head = model.graph.value(model.case_uri, EX.nodes)
    subject = {
        "list-head": head,
        "nested-cell": model.graph.value(head, RDF.rest),
        "nil": RDF.nil,
        "orphan": URIRef("urn:untyped:orphan"),
    }[location]
    model.graph.add((subject, EX.unmodeledProperty, Literal("payload")))
    assert not validate_graph(model.graph)["conforms"]
    with pytest.raises(ValueError, match="RDF graph is invalid"):
        model.to_case()


def test_unreachable_collection_is_rejected_even_with_valid_list_structure(cases):
    model = RdfModel(cases[0])
    Collection(model.graph, URIRef("urn:orphan:list"), [resource(model, "nodes")])
    assert not validate_graph(model.graph)["conforms"]


def test_prepared_queries_reuse_algebra_but_apply_current_bindings(cases, monkeypatch):
    from scripts.relationship_semantics_experiment import rdf_model

    model = RdfModel(cases[0])
    real_prepare = rdf_model.prepareQuery
    prepared = []

    def track_prepare(query_text):
        prepared.append(query_text)
        return real_prepare(query_text)

    rdf_model._prepared_query.cache_clear()
    monkeypatch.setattr(rdf_model, "prepareQuery", track_prepare)
    try:
        query = cases[0].queries[0]
        first = model.query(query)
        count = len(prepared)
        assert count > 0
        assert model.query(query) == first
        assert len(prepared) == count
        later = query.model_copy(update={"cutoff_episode_no": 4})
        assert model.query(later)["reason"] == "QUERY_BEYOND_SNAPSHOT"
        assert len(prepared) == count
    finally:
        rdf_model._prepared_query.cache_clear()


def test_turtle_roundtrip_is_offline_and_preserves_fields_and_queries(cases, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is outside this experiment")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for case in cases:
        model = RdfModel(case)
        expected = [model.query(query) for query in case.queries]
        serialized = model.serialize_turtle()
        model.graph = Graph().parse(data=serialized, format="turtle")
        assert model.validate()["conforms"]
        assert model.to_case().to_dict() == case.to_dict()
        assert [model.query(query) for query in case.queries] == expected


def test_query_reads_changed_rdf_instead_of_retained_input(cases):
    model = RdfModel(cases[0])
    original = model.query(cases[0].queries[0])
    slot = model.graph.value(first_fact(model), EX.slot)
    model.graph.set((slot, EX.kind, Literal("HOSTILITY")))
    actual = model.query(cases[0].queries[0])
    assert original["facts"][0]["slot"]["kind"] == "TRUST"
    assert actual["facts"][0]["slot"]["kind"] == "HOSTILITY"
    assert cases[0].facts[0].slot.kind == "TRUST"
    assert model.to_case().facts[0].slot.kind == "HOSTILITY"


def test_mutation_after_successful_validation_is_not_served(cases):
    model = RdfModel(cases[0])
    assert model.validate()["conforms"]
    damage(model, "missing-type")
    with pytest.raises(ValueError, match="RDF graph is invalid"):
        model.query(cases[0].queries[0])


def test_explicit_xsd_strings_have_same_validation_and_query_meaning(cases):
    from rdflib.namespace import XSD

    # RDF 1.1 simple strings and explicit xsd:string are equivalent. RDFLib's
    # Python term equality distinguishes them, so exercise the real validator.
    for case in (cases[0], cases[5], cases[7]):
        model = RdfModel(case)
        expected = model.query(case.queries[0])
        for subject, predicate, value in list(model.graph):
            if isinstance(value, Literal) and value.datatype is None and value.language is None:
                model.graph.remove((subject, predicate, value))
                model.graph.add((subject, predicate, Literal(str(value), datatype=XSD.string)))
        assert model.validate()["conforms"]
        assert model.query(case.queries[0]) == expected
    damage(model, "unconfirmed-proof")
    # This last case is RETRACTED: a CONFIRMED/HOLD premise is also invalid.
    assert not validate_graph(model.graph)["conforms"]
