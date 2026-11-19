"""B: an indexed, ordered positive-rule executor; not a full Datalog engine.

Only the shared eligibility/output policy is reused by C. This module never
reads oracle data, RDF graphs, or OWL conclusions. Rules consume direct facts
unless explicitly declared otherwise; a fresh invocation owns every index.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter

from .output import base_result, finish, relation, relation_key
from .policy import Prepared
from .python_rules import Atom, Rule, rules_for


@dataclass(frozen=True)
class Fact:
    row: dict
    assertion_id: str


@dataclass(frozen=True)
class Candidate:
    row: dict
    rule: Rule
    premises: tuple[Fact | Candidate, ...]


class Index:
    """Each entry represents a support, not just a deduplicated graph edge."""

    def __init__(self):
        self.by_kind = defaultdict(list)
        self.by_subject = defaultdict(list)
        self.by_object = defaultdict(list)

    def add(self, origin: str, fact: Fact | Candidate) -> None:
        row = fact.row
        self.by_kind[(origin, row["kind"])].append(fact)
        self.by_subject[(origin, row["kind"], row["subjectId"])].append(fact)
        self.by_object[(origin, row["kind"], row["objectId"])].append(fact)

    def lookup(self, atom: Atom, bindings: dict):
        if atom.subject in bindings:
            return self.by_subject[(atom.origin, atom.kind, bindings[atom.subject])]
        if atom.object in bindings:
            return self.by_object[(atom.origin, atom.kind, bindings[atom.object])]
        return self.by_kind[(atom.origin, atom.kind)]


def _matches(rule: Rule, index: Index):
    partials = [({}, ())]
    for atom in rule.body:
        following = []
        for bindings, premises in partials:
            for fact in index.lookup(atom, bindings):
                row = fact.row
                if atom.subtype is not None and row["subtype"] != atom.subtype:
                    continue
                candidate = bindings.copy()
                for variable, value in (
                    (atom.subject, row["subjectId"]),
                    (atom.object, row["objectId"]),
                ):
                    if variable in candidate and candidate[variable] != value:
                        break
                    candidate[variable] = value
                else:
                    # Eligibility already restricts the query scope. Retain the
                    # join guard so the executor cannot conflate contexts itself.
                    if premises and any(
                        row[field] != premises[0].row[field]
                        for field in ("workId", "temporalContextId")
                    ):
                        continue
                    following.append((candidate, (*premises, fact)))
        partials = following
    return partials


def _conclusions(index: Index, rules: tuple[Rule, ...]) -> list[Candidate]:
    conclusions = []
    for rule in rules:
        stage = []
        for bindings, premises in _matches(rule, index):
            row = relation(premises[0].row)
            row.update(
                subjectId=bindings[rule.head_subject],
                objectId=bindings[rule.head_object],
                kind=rule.head_kind,
                generationGap=rule.generation_gap,
            )
            stage.append(Candidate(row, rule, premises))
        conclusions.extend(stage)
        for candidate in stage:
            index.add("derived", candidate)
    return conclusions


def _proof(candidate: Candidate) -> dict:
    premise_ids = []
    source_ids = set()
    inherited_rules, intermediate_relations = [], []
    for premise in candidate.premises:
        if isinstance(premise, Fact):
            premise_ids.append(premise.assertion_id)
            source_ids.update(premise.row["sourceIds"])
        else:
            previous = _proof(premise)
            premise_ids.extend(previous["premiseAssertionIds"])
            source_ids.update(previous["sourceIds"])
            inherited_rules.extend(previous.get("rulePath", [previous["ruleId"]]))
            intermediate_relations.extend(previous.get("intermediateRelations", []))
            intermediate_relations.append(relation(premise.row))
    proof = {
        "ruleId": candidate.rule.rule_id,
        "premiseAssertionIds": premise_ids,
        "sourceIds": sorted(source_ids),
    }
    if inherited_rules:
        proof["rulePath"] = [*inherited_rules, candidate.rule.rule_id]
        proof["intermediateRelations"] = intermediate_relations
    return proof


def _with_proofs(candidates: list[Candidate]) -> list[dict]:
    grouped = {}
    for candidate in candidates:
        key = relation_key(candidate.row)
        if key not in grouped:
            grouped[key] = {
                **candidate.row,
                "derivationType": candidate.rule.derivation_type,
                "proofs": [],
            }
        proof = _proof(candidate)
        if proof in grouped[key]["proofs"]:
            raise ValueError("duplicate proof produced by rule executor")
        grouped[key]["proofs"].append(proof)
    return list(grouped.values())


def run_measured(prepared: Prepared, *, extended: bool = False) -> tuple[dict, dict]:
    start = perf_counter()
    index = Index()
    for assertion in prepared.eligible:
        index.add("direct", Fact(assertion, assertion["assertionId"]))
    constructed = perf_counter()
    if prepared.outcome != "READY":
        result = base_result(prepared)
        end = perf_counter()
        return result, {
            "constructionMs": (constructed - start) * 1000,
            "inferenceMs": 0.0,
            "proofMs": 0.0,
            "projectionMs": (end - constructed) * 1000,
            "inputFacts": len(prepared.eligible),
            "candidateProofs": 0,
        }
    candidates = _conclusions(index, rules_for(extended=extended))
    inferred = perf_counter()
    derived = _with_proofs(candidates)
    explained = perf_counter()
    result = finish(prepared, derived)
    end = perf_counter()
    return result, {
        "constructionMs": (constructed - start) * 1000,
        "inferenceMs": (inferred - constructed) * 1000,
        "proofMs": (explained - inferred) * 1000,
        "projectionMs": (end - explained) * 1000,
        "inputFacts": len(prepared.eligible),
        "candidateProofs": len(candidates),
    }


def run(prepared: Prepared, *, extended: bool = False) -> dict:
    return run_measured(prepared, extended=extended)[0]
