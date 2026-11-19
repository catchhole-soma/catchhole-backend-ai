"""Scope one immutable snapshot, query HermiT, then reconstruct justifications."""

from __future__ import annotations

from time import perf_counter

from .policy import explain_support, prepare
from .proofs import minimal_supports


def run_case(case, *, max_checks=8192):
    from .engine import Engine, EngineError

    begin = perf_counter()
    included, excluded, sources, source_map = prepare(case)
    eligible_at = perf_counter()
    if len(included) > 16:
        raise EngineError("INPUT_LIMIT", "at most 16 eligible statements per experimental snapshot")
    engine = Engine(case["vocabulary"], case["scope"], included, max_checks=max_checks)
    ready_at = perf_counter()
    ids = frozenset(row["id"] for row in included)
    consistent = engine.consistent(ids)
    decisions = []
    for query in case["queries"]:
        if not consistent:
            verdict = "BLOCKED_INCONSISTENT"
        elif query["type"] == "satisfiable":
            verdict = "SATISFIABLE" if engine.satisfiable(ids, query["class"]) else "UNSATISFIABLE"
        else:
            positive = engine.entailed(ids, query)
            negative = query["type"] != "subclass" and engine.entailed(ids, query, negative=True)
            if positive and negative:
                raise EngineError("INVARIANT_ERROR", "consistent ontology entailed both polarities")
            verdict = "ENTAILED" if positive else "REFUTED" if negative else "UNKNOWN"
        decisions.append({"id": query["id"], "query": query, "verdict": verdict, "supports": []})
    decided_at = perf_counter()

    def describe(groups):
        return [explain_support(group, included, sources, source_map) for group in groups]

    conflicts = []
    if not consistent:
        conflicts = describe(
            minimal_supports(ids, lambda selected: not engine.consistent(selected))
        )
    else:
        for row in decisions:
            query = row["query"]
            if row["verdict"] == "UNSATISFIABLE":
                predicate = lambda selected, q=query: not engine.satisfiable(selected, q["class"])
            elif row["verdict"] in {"ENTAILED", "REFUTED"}:
                predicate = lambda selected, q=query, negative=row["verdict"] == "REFUTED": (
                    engine.entailed(selected, q, negative=negative)
                )
            else:
                continue
            row["supports"] = describe(minimal_supports(ids, predicate))
    explained_at = perf_counter()
    rdfxml = engine.export_rdfxml(ids)
    finished_at = perf_counter()
    result = {
        "caseId": case["caseId"],
        "outcome": "CONSISTENT" if consistent else "INCONSISTENT",
        "eligibleStatementIds": sorted(ids),
        "excludedStatements": excluded,
        "queries": decisions,
        "conflictSets": conflicts,
        "explanationComplete": True,
        "explanationKind": "ALL_SUBSET_MINIMAL_JUSTIFICATIONS",
        "scope": case["scope"],
    }
    measurements = {
        "caseId": case["caseId"],
        "eligibilityMs": (eligible_at - begin) * 1000,
        "engineSetupMs": (ready_at - eligible_at) * 1000,
        "decisionMs": (decided_at - ready_at) * 1000,
        "explanationMs": (explained_at - decided_at) * 1000,
        "rdfExportMs": (finished_at - explained_at) * 1000,
        "totalMs": (finished_at - begin) * 1000,
        "engine": engine.metadata(),
    }
    return result, measurements, rdfxml
