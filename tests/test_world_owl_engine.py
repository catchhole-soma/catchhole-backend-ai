"""Real HermiT semantic smoke tests and bounded bridge failure contracts."""

import json
import socket
from copy import deepcopy

import pytest

from scripts.world_owl_experiment.engine import Engine, EngineError

VOCABULARY = {
    "classes": [
        "Paladin",
        "Knight",
        "Mage",
        "Combatant",
        "Person",
        "Dragon",
        "Rider",
        "Impossible",
    ],
    "individuals": ["Harin", "Doyun", "DragonA"],
    "properties": ["rides"],
}
SCOPE = {"workId": "work-a", "contextId": "present"}


def not_(arg):
    return {"op": "not", "arg": arg}


def and_(*args):
    return {"op": "and", "args": list(args)}


def or_(*args):
    return {"op": "or", "args": list(args)}


def some(property_, filler):
    return {"op": "some", "property": property_, "filler": filler}


def subclass(sub, super_):
    return {"type": "subclass", "sub": sub, "super": super_}


def member(class_, individual="Harin"):
    return {"type": "classAssertion", "individual": individual, "class": class_}


def edge(*, negative=False):
    return {
        "type": "propertyAssertion",
        "subject": "Harin",
        "property": "rides",
        "object": "DragonA",
        "negative": negative,
    }


def engine(*axioms, **options):
    return Engine(
        VOCABULARY,
        SCOPE,
        [{"id": f"s{index}", "axiom": value} for index, value in enumerate(axioms)],
        **options,
    )


def ids(*indices):
    return [f"s{index}" for index in indices]


def property_query():
    value = edge()
    value.pop("negative")
    return value


def test_hierarchy_and_intersection_equivalence_are_real_semantic_entailments():
    e = engine(
        subclass("Paladin", "Knight"),
        subclass("Knight", "Combatant"),
        {"type": "equivalent", "left": "Paladin", "right": and_("Knight", "Mage")},
        member("Knight"),
        member("Mage"),
    )
    assert e.entailed(ids(2, 3, 4), member("Paladin"))
    assert e.entailed(ids(0, 1, 2, 3, 4), member("Combatant"))
    assert e.entailed(ids(0, 1), subclass("Paladin", "Combatant"))


def test_complement_contraposition_is_distinct_from_invalid_inverse_implication():
    e = engine(subclass("Knight", "Combatant"), member(not_("Combatant")), member(not_("Knight")))
    assert e.entailed(ids(0, 1), member("Knight"), negative=True)
    assert e.entailed(ids(0), subclass(not_("Combatant"), not_("Knight")))
    assert not e.entailed(ids(0, 2), member("Combatant"), negative=True)


def test_union_elimination_and_unknown_are_not_closed_world_negation():
    e = engine(member(or_("Knight", "Mage")), member(not_("Knight")))
    assert e.entailed(ids(0, 1), member("Mage"))
    assert not e.entailed(ids(0), member("Mage"))
    assert not e.entailed(ids(0), member("Mage"), negative=True)
    assert not e.entailed([], member("Mage"), negative=True)


def test_existential_definition_classifies_but_does_not_invent_a_named_witness():
    e = engine(
        {"type": "equivalent", "left": "Rider", "right": and_("Person", some("rides", "Dragon"))},
        member("Person"),
        edge(),
        member("Dragon", "DragonA"),
        member("Rider"),
    )
    assert e.entailed(ids(0, 1, 2, 3), member("Rider"))
    assert e.consistent(ids(0, 4))
    assert e.entailed(ids(0, 4), member(some("rides", "Dragon")))
    assert not e.entailed(ids(0, 4), property_query())


def test_explicit_negative_property_and_its_contradiction():
    e = engine(edge(negative=True), edge())
    assert e.entailed(ids(0), property_query(), negative=True)
    assert not e.entailed(ids(0), property_query())
    assert e.consistent(ids(0))
    assert not e.consistent(ids(0, 1))
    with pytest.raises(EngineError) as error:
        e.entailed(ids(0, 1), member("Mage"))
    assert error.value.code == "INCONSISTENT_KB"


def test_impossible_class_does_not_make_the_entire_KB_inconsistent_until_instantiated():
    e = engine(
        subclass("Impossible", "Knight"),
        subclass("Impossible", "Mage"),
        {"type": "disjoint", "left": "Knight", "right": "Mage"},
        member("Impossible"),
    )
    assert e.consistent(ids(0, 1, 2))
    assert not e.satisfiable(ids(0, 1, 2), "Impossible")
    assert not e.consistent(ids(0, 1, 2, 3))
    with pytest.raises(EngineError) as error:
        e.satisfiable(ids(0, 1, 2, 3), "Knight")
    assert error.value.code == "INCONSISTENT_KB"


def test_arbitrary_expression_entailment_agrees_with_consistency_reductio():
    expression = and_("Knight", not_("Mage"))
    e = engine(member("Knight"), member(not_("Mage")), member(not_(expression)))
    assert e.entailed(ids(0, 1), member(expression))
    assert not e.consistent(ids(0, 1, 2))


def test_empty_background_and_tautological_empty_supports():
    e = engine()
    assert e.consistent([])
    assert e.entailed([], member(or_("Knight", not_("Knight"))))
    assert not e.satisfiable([], and_("Knight", not_("Knight")))
    assert e.satisfiable([], "Knight")


@pytest.mark.parametrize("class_expression", ["Knight", and_("Knight", "Mage")])
def test_self_disjointness_is_an_empty_class_and_self_equivalence_is_a_tautology(class_expression):
    e = engine(
        {"type": "disjoint", "left": class_expression, "right": class_expression},
        {"type": "equivalent", "left": class_expression, "right": class_expression},
        member(class_expression),
    )
    assert e.consistent(ids(0))
    assert not e.satisfiable(ids(0), class_expression)
    assert not e.consistent(ids(0, 2))
    assert e.satisfiable(ids(1), class_expression)
    assert e.consistent(ids(1, 2))
    assert e.metadata()["profileChecked"] is True


@pytest.mark.parametrize("operator", [and_, or_])
def test_repeated_boolean_operands_retain_the_original_expression(operator):
    e = engine(member("Knight"))
    assert e.entailed(ids(0), member(operator("Knight", "Knight")))
    assert e.entailed([], subclass(operator("Knight", "Knight"), "Knight"))
    assert e.entailed([], subclass("Knight", operator("Knight", "Knight")))


def test_subsets_are_fresh_and_preserve_alternative_supports_without_stale_results():
    e = engine(
        subclass("Paladin", "Knight"),
        subclass("Knight", "Combatant"),
        subclass("Paladin", "Combatant"),
        member("Paladin"),
    )
    query = member("Combatant")
    assert e.entailed(ids(0, 1, 2, 3), query)
    assert e.entailed(ids(0, 1, 3), query)
    assert e.entailed(ids(2, 3), query)
    assert not e.entailed(ids(0, 3), query)
    assert e.entailed(ids(0, 1, 2, 3), query)
    assert e.metadata()["checks"] == 4
    assert e.metadata()["cacheHits"] == 1


def test_query_id_is_not_semantic_and_cache_keys_include_negation():
    e = engine(member("Knight"))
    assert e.entailed(ids(0), {"id": "first", **member("Knight")})
    assert e.entailed(ids(0), {"id": "second", **member("Knight")})
    assert e.metadata()["checks"] == 1
    assert e.metadata()["cacheHits"] == 1
    assert not e.entailed(ids(0), member("Knight"), negative=True)
    assert e.metadata()["checks"] == 2


def test_check_limit_is_a_typed_failure_while_cached_answers_remain_available():
    e = engine(member("Knight"), max_checks=1)
    assert e.consistent([])
    with pytest.raises(EngineError) as error:
        e.entailed(ids(0), member("Knight"))
    assert error.value.code == "CHECK_LIMIT"
    assert e.consistent([])
    assert e.checks == 1 and e.cache_hits == 1


def test_unknown_statement_and_negative_subclass_are_explicit_errors():
    e = engine()
    with pytest.raises(EngineError) as error:
        e.consistent(["missing"])
    assert error.value.code == "UNKNOWN_STATEMENT"
    with pytest.raises(EngineError) as error:
        e.entailed([], subclass("Knight", "Combatant"), negative=True)
    assert error.value.code == "UNSUPPORTED_QUERY"


def test_actual_java_timeout_exception_is_not_converted_to_unknown(monkeypatch):
    import jpype

    e = engine()
    real = e._rt.factory
    disposed = []

    class TimeoutReasoner:
        def getConfiguration(self):
            return type("Config", (), {"individualTaskTimeout": 17})()

        def isConsistent(self):
            raise jpype.JClass("org.semanticweb.owlapi.reasoner.TimeOutException")()

        def dispose(self):
            disposed.append(True)

    class TimeoutFactory:
        def createReasoner(self, ontology, configuration):
            return TimeoutReasoner()

    e.timeout_ms = 17
    monkeypatch.setattr(e._rt, "factory", TimeoutFactory())
    with pytest.raises(EngineError) as error:
        e.consistent([])
    assert error.value.code == "TIMEOUT"
    assert disposed == [True]
    monkeypatch.setattr(e._rt, "factory", real)


def test_rdfxml_contains_asserted_scope_axioms_and_reloads_in_owlapi():
    import jpype

    e = engine(subclass("Knight", "Combatant"), member("Knight"))
    xml = e.export_rdfxml(ids(0, 1))
    manager = e._rt.manager.createOWLOntologyManager()
    document = jpype.JClass("org.semanticweb.owlapi.io.StringDocumentSource")(xml)
    ontology = manager.loadOntologyFromOntologyDocument(document)
    reasoner = e._rt.factory.createReasoner(ontology, e._rt.configuration(5000))
    try:
        assert bool(reasoner.isConsistent())
        assert bool(reasoner.isEntailed(e._axiom(member("Combatant"))))
        assert ontology.getLogicalAxiomCount() == 2
    finally:
        reasoner.dispose()
        manager.removeOntology(ontology)
    assert e.checks == 0  # Serialization is not a hidden reasoner check.


def test_scope_isolation_input_detachment_metadata_and_no_python_network(monkeypatch):
    vocabulary = deepcopy(VOCABULARY)
    statements = [{"id": "s", "axiom": member("Knight")}]
    first = Engine(vocabulary, SCOPE, statements, timeout_ms=1234)
    second = Engine(VOCABULARY, {**SCOPE, "workId": "work-b"}, [])
    statements[0]["axiom"]["class"] = "Mage"
    vocabulary["classes"].clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("offline reasoning attempted Python networking")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    assert first.entailed(["s"], member("Knight"))
    assert not second.entailed([], member("Knight"))
    assert first._prefix != second._prefix
    metadata = first.metadata()
    assert metadata["engineVersion"] == "1.3.8.1099"
    assert metadata["owlapiVersion"] == "3.4.3"
    assert metadata["timeoutMs"] == 1234
    assert len(metadata["jarSha256"]) == 64
    assert json.dumps(metadata)
