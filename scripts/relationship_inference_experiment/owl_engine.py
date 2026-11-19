"""C: actual OWL-RL closure, independently reconstructed assertion witnesses.

The shared policy layer qualifies direct assertions before this module runs.
Every call builds a fresh query-scoped graph: OWL-RL is monotonic, so removing a
premise requires recomputation rather than retaining an earlier closure. RDF
edges collapse duplicate assertions; the original assertion index separately
preserves every alternative proof. This index is not an OWL explanation API.

Only conclusions found in the closure can be returned. Proof witnesses and
closure conclusions must agree exactly; unsupported application edges fail
closed instead of disappearing through an output predicate whitelist.
"""

from __future__ import annotations

import json
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns
from typing import TYPE_CHECKING
from urllib.parse import quote

from owlrl import DeductiveClosure, OWLRL_Semantics
from rdflib import OWL, Graph, Namespace, URIRef

from .output import finish, relation, relation_key

if TYPE_CHECKING:
    from .policy import Prepared


REL = Namespace("urn:catchhole:relationship-inference:")
ONTOLOGY_PATH = Path(__file__).parent / "owl" / "base.ttl"
PARENT_PREDICATES = {
    "BIOLOGICAL": REL.biologicalParent,
    "ADOPTIVE": REL.adoptiveParent,
}
DERIVED_PREDICATES = {
    REL.biologicalChild: ("CHILD_OF", "BIOLOGICAL", None, "SEMANTIC_INVERSE"),
    REL.adoptiveChild: ("CHILD_OF", "ADOPTIVE", None, "SEMANTIC_INVERSE"),
    REL.biologicalGrandparent: ("GRANDPARENT_OF", "BIOLOGICAL", 2, "NEW_RELATION"),
}


class InferenceMismatchError(ValueError):
    """The rule witnesses and actual OWL engine output disagree."""


def _elapsed(start: int) -> float:
    return (perf_counter_ns() - start) / 1_000_000


def _expand(graph: Graph) -> None:
    """Run the same real closure settings for execution and saved examples."""
    DeductiveClosure(
        OWLRL_Semantics, rdfs_closure=False, axiomatic_triples=False, datatype_axioms=False
    ).expand(graph)


def _ontology() -> Graph:
    # Parse a local string, never a caller-provided URL or remote ontology.
    return Graph().parse(data=ONTOLOGY_PATH.read_text(encoding="utf-8"), format="turtle")


def _input_predicate(assertion: dict) -> URIRef:
    kind = assertion["kind"]
    if kind == "PARENT_OF":
        return PARENT_PREDICATES[assertion["subtype"]]
    if kind == "TRUST":
        return REL.trust
    if kind == "ANCESTOR_OF":
        # Preserve the explicit gap/subtype without making ancestor transitive
        # or treating a stated three-generation ancestor as a grandparent.
        return REL[f"ancestor/{assertion['subtype']}/{assertion['generationGap']}"]
    raise ValueError(f"unsupported direct assertion kind: {kind}")


def _build_graph(prepared: Prepared) -> tuple[Graph, dict[URIRef, str], set[tuple]]:
    case = prepared.case
    scope = [case["caseId"], case["snapshotRevision"], case["query"]]
    digest = sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    entities = {
        entity["entityId"]: URIRef(f"{REL}entity/{digest}/{quote(entity['entityId'], safe='')}")
        for entity in case["entities"]
    }
    graph = _ontology()
    inputs = set()
    for assertion in prepared.eligible:
        if (
            assertion["workId"] != case["query"]["workId"]
            or assertion["temporalContextId"] != case["query"]["temporalContextId"]
        ):
            raise ValueError("prepared assertions must belong to one query work and context")
        triple = (
            entities[assertion["subjectId"]],
            _input_predicate(assertion),
            entities[assertion["objectId"]],
        )
        inputs.add(triple)
        graph.add(triple)
    return graph, {iri: ref for ref, iri in entities.items()}, inputs


def _proof(rule: str, *premises: dict) -> dict:
    return {
        "ruleId": rule,
        "premiseAssertionIds": [row["assertionId"] for row in premises],
        "sourceIds": sorted({ref for row in premises for ref in row["sourceIds"]}),
    }


def _witnesses(prepared: Prepared) -> dict[tuple, dict]:
    """Enumerate original assertion combinations, independently of B and OWL."""
    witnesses = {}

    def add(value: dict, derivation_type: str, proof: dict) -> None:
        key = relation_key(value)
        row = witnesses.setdefault(key, {**value, "derivationType": derivation_type, "proofs": []})
        row["proofs"].append(proof)

    parents = sorted(
        (row for row in prepared.eligible if row["kind"] == "PARENT_OF"),
        key=lambda row: row["assertionId"],
    )
    biological_by_subject = defaultdict(list)
    for parent in parents:
        inverse = relation(parent)
        inverse.update(subjectId=parent["objectId"], objectId=parent["subjectId"], kind="CHILD_OF")
        add(inverse, "SEMANTIC_INVERSE", _proof("R_PARENT_INVERSE", parent))
        if parent["subtype"] == "BIOLOGICAL":
            biological_by_subject[parent["subjectId"]].append(parent)
    for first in parents:
        if first["subtype"] != "BIOLOGICAL":
            continue
        for second in biological_by_subject[first["objectId"]]:
            grandparent = relation(first)
            grandparent.update(objectId=second["objectId"], kind="GRANDPARENT_OF", generationGap=2)
            add(
                grandparent,
                "NEW_RELATION",
                _proof("R_BIOLOGICAL_GRANDPARENT", first, second),
            )
    return witnesses


def _closure_relations(
    prepared: Prepared, graph: Graph, entities: dict[URIRef, str], inputs: set[tuple]
) -> dict[tuple, dict]:
    conclusions = {}
    observed_inputs = set()
    query = prepared.case["query"]
    for subject, predicate, obj in graph:
        if predicate == OWL.sameAs and subject != obj:
            # OWL's reflexive sameAs bookkeeping is not entity resolution.
            raise InferenceMismatchError("unexpected non-reflexive OWL identity inference")
        if not str(predicate).startswith(str(REL)):
            continue  # OWL/RDF vocabulary bookkeeping is not a product relation.
        if predicate not in DERIVED_PREDICATES:
            triple = (subject, predicate, obj)
            if triple not in inputs:
                raise InferenceMismatchError(f"unexpected application edge: {predicate}")
            observed_inputs.add(triple)
            continue
        if subject not in entities or obj not in entities:
            raise InferenceMismatchError("OWL conclusion introduced an unknown entity")
        kind, subtype, gap, derivation_type = DERIVED_PREDICATES[predicate]
        value = {
            "workId": query["workId"],
            "subjectId": entities[subject],
            "objectId": entities[obj],
            "kind": kind,
            "subtype": subtype,
            "temporalContextId": query["temporalContextId"],
            "generationGap": gap,
            "derivationType": derivation_type,
        }
        conclusions[relation_key(value)] = value
    if observed_inputs != inputs:
        raise InferenceMismatchError("OWL closure lost a direct application edge")
    return conclusions


def run_measured(prepared: Prepared, *, extended: bool = False) -> tuple[dict, dict]:
    """Return result and local phase durations; shared policy time is external."""
    if extended:
        raise NotImplementedError("grandchild extension is not installed in the base experiment")
    metrics = {
        "constructionMs": 0.0,
        "inferenceMs": 0.0,
        "proofMs": 0.0,
        "projectionMs": 0.0,
        "inputTripleCount": 0,
        "inputFactTripleCount": 0,
        "closureTripleCount": 0,
    }
    if prepared.outcome != "READY":
        started = perf_counter_ns()
        result = finish(prepared, [])
        metrics["projectionMs"] = _elapsed(started)
        return result, metrics

    started = perf_counter_ns()
    graph, entities, inputs = _build_graph(prepared)
    metrics["constructionMs"] = _elapsed(started)
    metrics["inputTripleCount"] = len(graph)
    metrics["inputFactTripleCount"] = len(inputs)

    started = perf_counter_ns()
    _expand(graph)
    metrics["inferenceMs"] = _elapsed(started)
    metrics["closureTripleCount"] = len(graph)

    started = perf_counter_ns()
    witnesses = _witnesses(prepared)
    metrics["proofMs"] = _elapsed(started)

    started = perf_counter_ns()
    conclusions = _closure_relations(prepared, graph, entities, inputs)
    if conclusions.keys() != witnesses.keys():
        missing = len(witnesses.keys() - conclusions.keys())
        extra = len(conclusions.keys() - witnesses.keys())
        raise InferenceMismatchError(f"OWL/proof mismatch: missing={missing}, extra={extra}")
    derived = [{**value, "proofs": witnesses[key]["proofs"]} for key, value in conclusions.items()]
    result = finish(prepared, derived)
    metrics["projectionMs"] = _elapsed(started)
    return result, metrics


def run(prepared: Prepared, *, extended: bool = False) -> dict:
    return run_measured(prepared, extended=extended)[0]
