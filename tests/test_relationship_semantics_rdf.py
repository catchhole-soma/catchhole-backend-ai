"""C executes SHACL and SPARQL against RDF, independent of the B evaluator."""

import json
import socket
from pathlib import Path

import pytest
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.collection import Collection

from scripts.relationship_semantics_experiment.input import Case, load_document
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


@pytest.mark.parametrize(
    ("axis", "direction"),
    [("ATTITUDE", "DIRECTED"), ("SOCIAL", "MUTUAL"), ("INTERACTION", "UNDIRECTED")],
)
def test_extended_affection_is_vocabulary_only_not_axis_or_direction_mapping(
    cases, axis, direction
):
    data = cases[0].to_dict()
    data["facts"][0]["slot"].update(kind="AFFECTION", axis=axis, direction=direction)
    data["sources"][0]["text"] = data["sources"][0]["quote"] = "도윤은 서연에게 호감을 느낀다."
    case = Case.model_validate(data)
    model = RdfModel(case, extended=True)
    assert not validate_graph(model.graph)["conforms"]
    assert validate_graph(model.graph, extended=True)["conforms"]
    actual = model.query(case.queries[0])["facts"][0]
    assert actual["slot"] == case.facts[0].slot.to_dict()
    assert model.to_case().to_dict() == case.to_dict()


def test_extended_directed_filters_preserve_endpoint_orientation(cases):
    case = cases[0]
    model = RdfModel(case, extended=True)
    for node, perspective, expected in [
        ("doyun", "OUTGOING", "f1"),
        ("doyun", "INCOMING", "f2"),
        ("seoyeon", "OUTGOING", "f2"),
        ("seoyeon", "INCOMING", "f1"),
    ]:
        query = case.queries[0].model_copy(
            update={"axis": "ATTITUDE", "node_ref": node, "perspective": perspective}
        )
        result = model.query(query)
        assert result["status"] == "READY"
        assert [fact["factRef"] for fact in result["facts"]] == [expected]
        original = next(fact for fact in case.facts if fact.fact_ref == expected)
        assert result["facts"][0]["slot"] == original.slot.to_dict()
        assert result["excludedFacts"][0]["reason"] == "QUERY_FILTER"
    assert RdfModel(case).query(query)["reason"] == "QUERY_FILTER_NOT_SUPPORTED"


@pytest.mark.parametrize("direction", ["MUTUAL", "UNDIRECTED"])
def test_symmetric_filters_return_one_original_fact_from_either_endpoint(cases, direction):
    data = cases[1].to_dict()
    data["facts"][1]["slot"]["direction"] = direction
    case = Case.model_validate(data)
    model = RdfModel(case, extended=True)
    for node in ("doyun", "seoyeon"):
        for perspective in ("INCOMING", "OUTGOING"):
            query = case.queries[0].model_copy(
                update={"axis": "SOCIAL", "node_ref": node, "perspective": perspective}
            )
            result = model.query(query)
            assert [fact["factRef"] for fact in result["facts"]] == ["f2"]
            assert result["facts"][0]["slot"] == case.facts[1].slot.to_dict()


def test_filtered_fact_does_not_require_its_unavailable_history(cases):
    data = cases[0].to_dict()
    data["facts"][0]["lastChangedEpisodeNo"] = 3
    case = Case.model_validate(data)
    model = RdfModel(case, extended=True)
    query = case.queries[0].model_copy(
        update={"cutoff_episode_no": 1, "node_ref": "doyun", "perspective": "INCOMING"}
    )
    result = model.query(query)
    assert result["status"] == "READY"
    assert [fact["factRef"] for fact in result["facts"]] == ["f2"]
    assert result["excludedFacts"] == [{"factRef": "f1", "reason": "QUERY_FILTER"}]
    assert model.query(query.model_copy(update={"perspective": "OUTGOING"}))["reason"] == (
        "HISTORY_NOT_AVAILABLE"
    )
    axis_only = query.model_copy(update={"node_ref": None, "perspective": None, "axis": "SOCIAL"})
    assert model.query(axis_only)["status"] == "READY"
    assert model.query(axis_only)["facts"] == []


def test_fact_filters_do_not_reclassify_or_hide_held_claims(cases):
    case = cases[5]
    query = case.queries[0].model_copy(
        update={"axis": "ATTITUDE", "node_ref": "doyun", "perspective": "OUTGOING"}
    )
    result = RdfModel(case, extended=True).query(query)
    assert result["facts"] == []
    assert result["heldClaimRefs"] == ["c1", "c2"]


def test_same_name_does_not_turn_another_node_into_the_filter_identity(cases):
    data = cases[0].to_dict()
    same_name = dict(data["nodes"][1], nodeRef="another-seoyeon")
    data["nodes"].append(same_name)
    case = Case.model_validate(data)
    model = RdfModel(case, extended=True)
    query = case.queries[0].model_copy(
        update={"node_ref": "another-seoyeon", "perspective": "OUTGOING"}
    )
    assert model.query(query)["facts"] == []
    original = query.model_copy(update={"node_ref": "seoyeon"})
    assert [fact["factRef"] for fact in model.query(original)["facts"]] == ["f2"]


@pytest.mark.parametrize(
    ("case_index", "reason"),
    [(3, "FUTURE_KNOWLEDGE"), (7, "RETRACTED_FACT"), (8, "USER_SUPPRESSED")],
)
def test_visibility_reasons_take_precedence_over_query_filter(cases, case_index, reason):
    case = cases[case_index]
    query = case.queries[0].model_copy(update={"axis": "INTERACTION"})
    result = RdfModel(case, extended=True).query(query)
    assert result["status"] == "READY"
    assert result["excludedFacts"] == [{"factRef": case.facts[0].fact_ref, "reason": reason}]


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("field", ["workId", "axis", "kind", "direction"])
def test_mixed_string_notation_never_revives_a_suppressed_slot(cases, extended, field):
    from rdflib.namespace import XSD

    case = cases[8]
    model = RdfModel(case, extended=extended)
    suppression = resource(model, "suppressions")
    slot = model.graph.value(suppression, EX.slot)
    value = model.graph.value(slot, EX[field])
    model.graph.set((slot, EX[field], Literal(str(value), datatype=XSD.string)))
    assert model.validate()["conforms"]
    # Both the final decision and history gating must use value equality.
    for cutoff in (3, 1):
        query = case.queries[0].model_copy(update={"cutoff_episode_no": cutoff})
        result = model.query(query)
        assert result["status"] == "READY"
        assert result["facts"] == []
        assert result["excludedFacts"] == [{"factRef": "f1", "reason": "USER_SUPPRESSED"}]


@pytest.mark.parametrize(
    ("group", "field"),
    [
        ("nodes", "nodeRef"),
        ("sources", "sourceRef"),
        ("claims", "claimRef"),
        ("facts", "factRef"),
        ("queries", "queryRef"),
        ("suppressions", "suppressionRef"),
    ],
)
def test_mixed_string_notation_cannot_hide_duplicate_identifiers(cases, group, field):
    from rdflib.namespace import XSD

    data = cases[8 if group == "suppressions" else 0].to_dict()
    if len(data[group]) == 1:
        data[group].append(dict(data[group][0], **{field: "another-id"}))
    model = RdfModel(Case.model_validate(data))
    assert model.validate()["conforms"]
    first, second = model._list(model.case_uri, group)[:2]
    identifier = str(model.graph.value(first, EX[field]))
    model.graph.set((second, EX[field], Literal(identifier, datatype=XSD.string)))
    result = validate_graph(model.graph)
    assert not result["conforms"]
    assert any("Identifiers must be unique" in issue["message"] for issue in result["violations"])


def test_mixed_string_notation_cannot_hide_duplicate_canonical_slots(cases):
    from rdflib.namespace import XSD

    model = RdfModel(cases[1])
    first = model.graph.value(resource(model, "facts", 0), EX.slot)
    second = model.graph.value(resource(model, "facts", 1), EX.slot)
    for field in ("axis", "kind", "direction"):
        value = model.graph.value(second, EX[field])
        model.graph.set((first, EX[field], Literal(str(value), datatype=XSD.string)))
    result = validate_graph(model.graph)
    assert not result["conforms"]
    assert any("canonical fact slot" in issue["message"] for issue in result["violations"])


def test_mixed_string_notation_for_node_work_does_not_change_membership(cases):
    from rdflib.namespace import XSD

    case = cases[0]
    model = RdfModel(case)
    expected = model.query(case.queries[0])
    node = resource(model, "nodes")
    model.graph.set((node, EX.workId, Literal(case.work_id, datatype=XSD.string)))
    assert model.validate()["conforms"]
    assert model.query(case.queries[0]) == expected
