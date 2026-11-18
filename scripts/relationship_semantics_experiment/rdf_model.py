"""Independent, local RDF/SHACL/SPARQL implementation of the synthetic contract.

Python maps input fields and assembles output; shapes and SPARQL own domain
validation and inclusion decisions. No typed-model validator/query is imported.
"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pyshacl import validate as shacl_validate
from rdflib import RDF, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import SH, XSD
from rdflib.plugins.sparql import prepareQuery

from .input import Case, Query

EX = Namespace("urn:catchhole:relationship:")
HERE = Path(__file__).parent
# Scalars are literal values. Reference fields remain links to typed resources.
SCALARS = {
    "Case": (
        "caseId",
        "title",
        "productQuestionId",
        "purpose",
        "workId",
        "snapshotThroughEpisodeNo",
        "snapshotRevision",
    ),
    "Node": ("nodeRef", "name", "workId"),
    "Source": ("sourceRef", "workId", "episodeNo", "text", "quote"),
    "Claim": ("claimRef", "statementMode", "curationStatus", "temporalScope", "narrativeContextId"),
    "Fact": ("factRef", "status", "factValidity", "knownFromEpisodeNo", "lastChangedEpisodeNo"),
    "Slot": ("workId", "axis", "kind", "direction"),
    "Suppression": ("suppressionRef", "reason", "appliesTo", "recordedRevision"),
    "Query": ("queryRef", "mode", "cutoffEpisodeNo", "storyContextId", "axis", "perspective"),
}
GROUPS = {
    "nodes": ("Node", "nodeRef"),
    "sources": ("Source", "sourceRef"),
    "claims": ("Claim", "claimRef"),
    "facts": ("Fact", "factRef"),
    "suppressions": ("Suppression", "suppressionRef"),
    "queries": ("Query", "queryRef"),
}
REFERENCE_LISTS = {
    "Claim": {"subjectCandidateRefs": "nodes", "objectCandidateRefs": "nodes"},
    "Fact": {
        "supportClaimRefs": "claims",
        "endClaimRefs": "claims",
        "correctionSourceRefs": "sources",
    },
}
INTEGER_FIELDS = {
    "snapshotThroughEpisodeNo",
    "snapshotRevision",
    "episodeNo",
    "knownFromEpisodeNo",
    "lastChangedEpisodeNo",
    "recordedRevision",
    "cutoffEpisodeNo",
}


@lru_cache(maxsize=4)
def _prepared_query(filename: str):
    """Compile fixed local queries once; bind values without caching results.

    First use pays parsing/compilation cost. Later queries reuse the prepared
    algebra across cases, while the current graph and bindings remain inputs.
    """
    if filename not in {"fact_decisions.rq", "query_status.rq", "held_claims.rq", "provenance.rq"}:
        raise ValueError("Only fixed experiment queries are supported")
    return prepareQuery((HERE / "queries" / filename).read_text())


def validate_graph(graph: Graph, extended: bool = False) -> dict:
    """Validate an arbitrary RDF graph using local shapes, independently of Case.

    No inference, imports, JavaScript, remote graph store or remote queries run.
    Engine/shape failures are exceptions, not ordinary nonconformance results.
    """
    shapes = Graph().parse(data=(HERE / "shapes.ttl").read_text(), format="turtle")
    if extended:
        # The vocabulary extension is declared in the shapes document. The
        # adapter selects it; SHACL still validates the actual RDF kind values.
        kind_property = next(shapes.subjects(SH.path, EX.kind))
        allowed = Collection(shapes, shapes.value(kind_property, SH["in"]))
        for kind in shapes.objects(EX.ExtendedKindVocabulary, EX.additionalKind):
            allowed.append(kind)
    conforms, report, _ = shacl_validate(
        graph,
        shacl_graph=shapes,
        inference="none",
        advanced=False,
        js=False,
        do_owl_imports=False,
        meta_shacl=False,
    )
    if not isinstance(report, Graph):
        # pySHACL returns a ValidationFailure object for engine-level failure.
        raise RuntimeError(f"SHACL engine failure: {report}")  # noqa: TRY004
    violations = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        component = report.value(result, SH.sourceConstraintComponent)
        messages = sorted(str(value) for value in report.objects(result, SH.resultMessage))
        violations.append(
            {"code": str(component).rsplit("#", 1)[-1], "message": "; ".join(messages)}
        )
    return {
        "conforms": bool(conforms),
        "violations": sorted(violations, key=lambda item: (item["code"], item["message"])),
    }


class RdfModel:
    def __init__(self, case: Case, extended: bool = False):
        self.extended = extended
        self.graph = Graph()
        self.graph.bind("ex", EX)
        self.case_uri = URIRef(f"urn:catchhole:case:{quote(case.case_id, safe='')}:")
        self._validated_triples = None
        data = case.to_dict()
        resources = {}
        for group, (_, id_field) in GROUPS.items():
            resources[group] = {}
            for index, item in enumerate(data[group]):
                resources[group].setdefault(
                    item[id_field], URIRef(f"{self.case_uri}{group}/{index}")
                )

        def reference(group, identifier):
            return resources[group].get(
                identifier, URIRef(f"{self.case_uri}missing/{group}/{quote(identifier, safe='')}")
            )

        def write_object(uri, kind, value):
            self.graph.add((uri, RDF.type, EX[kind]))
            if kind != "Case":
                self.graph.add((uri, EX.case, self.case_uri))
            for field in SCALARS[kind]:
                if value.get(field) is not None:
                    datatype = XSD.integer if field in INTEGER_FIELDS else None
                    self.graph.add((uri, EX[field], Literal(value[field], datatype=datatype)))
            for field, group in REFERENCE_LISTS.get(kind, {}).items():
                self._write_list(uri, field, [reference(group, ref) for ref in value[field]])
            if kind == "Claim":
                self.graph.add((uri, EX.source, reference("sources", value["sourceRef"])))
            if kind == "Query" and value.get("nodeRef") is not None:
                self.graph.add((uri, EX.node, reference("nodes", value["nodeRef"])))
            if kind == "Slot":
                for field in ("sourceNodeRef", "targetNodeRef"):
                    self.graph.add((uri, EX[field], reference("nodes", value[field])))
            if kind in {"Fact", "Suppression"}:
                slot_uri = URIRef(f"{uri}/slot")
                self.graph.add((uri, EX.slot, slot_uri))
                write_object(slot_uri, "Slot", value["slot"])

        write_object(self.case_uri, "Case", data)
        for group, (kind, _) in GROUPS.items():
            uris = []
            for index, value in enumerate(data[group]):
                uri = URIRef(f"{self.case_uri}{group}/{index}")
                uris.append(uri)
                write_object(uri, kind, value)
            self._write_list(self.case_uri, group, uris)

    def _write_list(self, owner, field, values):
        head = URIRef(f"{owner}/list/{field}") if values else RDF.nil
        self.graph.add((owner, EX[field], head))
        if values:
            Collection(self.graph, head, values)

    def validate(self) -> dict:
        result = validate_graph(self.graph, self.extended)
        self._validated_triples = frozenset(self.graph) if result["conforms"] else None
        return result

    def _require_valid(self):
        # The graph is intentionally public for independent RDF mutation tests.
        if self._validated_triples != frozenset(self.graph):
            result = self.validate()
            if not result["conforms"]:
                raise ValueError(f"RDF graph is invalid: {result['violations']}")

    def _list(self, owner, field):
        return list(Collection(self.graph, self.graph.value(owner, EX[field])))

    def _decode(self, uri, kind):
        value = {}
        for field in SCALARS[kind]:
            term = self.graph.value(uri, EX[field])
            value[field] = term.toPython() if term is not None else None
        for field, group in REFERENCE_LISTS.get(kind, {}).items():
            id_field = GROUPS[group][1]
            value[field] = [
                str(self.graph.value(ref, EX[id_field])) for ref in self._list(uri, field)
            ]
        if kind == "Claim":
            value["sourceRef"] = str(
                self.graph.value(self.graph.value(uri, EX.source), EX.sourceRef)
            )
        if kind == "Query":
            ref = self.graph.value(uri, EX.node)
            value["nodeRef"] = str(self.graph.value(ref, EX.nodeRef)) if ref is not None else None
        if kind == "Slot":
            for field in ("sourceNodeRef", "targetNodeRef"):
                value[field] = str(self.graph.value(self.graph.value(uri, EX[field]), EX.nodeRef))
        if kind in {"Fact", "Suppression"}:
            value["slot"] = self._decode(self.graph.value(uri, EX.slot), "Slot")
        return value

    def to_case(self) -> Case:
        self._require_valid()
        data = self._decode(self.case_uri, "Case")
        for group, (kind, _) in GROUPS.items():
            data[group] = [self._decode(uri, kind) for uri in self._list(self.case_uri, group)]
        return Case.model_validate(data)

    def serialize_turtle(self) -> str:
        return self.graph.serialize(format="turtle")

    def _select(self, filename, **bindings):
        return self.graph.query(_prepared_query(filename), initBindings=bindings)

    def query(self, query: Query) -> dict:
        self._require_valid()
        query_data = query.to_dict()
        # External query arguments have the same structural contract as stored
        # Query nodes. These checks do not decide fact inclusion or eligibility.
        if (query.node_ref is None) != (query.perspective is None):
            raise ValueError("nodeRef and perspective must occur together")
        nodes_by_id = {
            str(self.graph.value(node, EX.nodeRef)): node
            for node in self._list(self.case_uri, "nodes")
        }
        if query.node_ref is not None and query.node_ref not in nodes_by_id:
            raise ValueError("query nodeRef is unknown")
        if (
            query.cutoff_episode_no < 1
            or ((query.mode == "READER_KNOWLEDGE") != (query.story_context_id is None))
            or (query.mode == "STORY_TIME" and not query.story_context_id.strip())
        ):
            raise ValueError("invalid query cutoff or story context")
        bindings = {
            "case": self.case_uri,
            "cutoff": Literal(query.cutoff_episode_no, datatype=XSD.integer),
            "mode": Literal(query.mode),
            "hasFilter": Literal(
                any(
                    query_data.get(field) is not None
                    for field in ("axis", "nodeRef", "perspective")
                )
            ),
            "filtersEnabled": Literal(self.extended),
            "hasAxisFilter": Literal(query.axis is not None),
            "filterAxis": Literal(query.axis or ""),
            "hasNodeFilter": Literal(query.node_ref is not None),
            "filterNode": nodes_by_id.get(query.node_ref, EX.NoFilterNode),
            "perspective": Literal(query.perspective or ""),
        }
        decisions = list(self._select("fact_decisions.rq", **bindings))
        reason_rows = list(self._select("query_status.rq", **bindings))
        reason = str(reason_rows[0].reason) if reason_rows else None
        output = {
            "caseId": str(self.graph.value(self.case_uri, EX.caseId)),
            "queryRef": query.query_ref,
            "status": "UNSUPPORTED" if reason else "READY",
            "reason": reason,
            "facts": [],
            "excludedFacts": [],
            "heldClaimRefs": [],
        }
        for row in decisions:
            fact_ref = str(self.graph.value(row.fact, EX.factRef))
            decision = "UNSUPPORTED_QUERY" if reason else str(row.decision)
            if decision != "INCLUDED":
                output["excludedFacts"].append({"factRef": fact_ref, "reason": decision})
                continue
            fact = self._decode(row.fact, "Fact")
            for role, field in (
                (EX.supportClaimRefs, "supportSourceRefs"),
                (EX.endClaimRefs, "endSourceRefs"),
            ):
                fact[field] = sorted(
                    {
                        str(result.sourceRef)
                        for result in self._select("provenance.rq", fact=row.fact, role=role)
                    }
                )
            output["facts"].append(fact)
        if not reason:
            # HOLD claims have no confirmed axis/slot. They remain scoped to
            # the case and reader cutoff even when confirmed facts are filtered.
            output["heldClaimRefs"] = sorted(
                {str(row.claimRef) for row in self._select("held_claims.rq", **bindings)}
            )
        output["facts"].sort(key=lambda value: value["factRef"])
        output["excludedFacts"].sort(key=lambda value: value["factRef"])
        return output
