"""B: explicit Python objects, domain checks and snapshot queries.

The input is curated data, not natural language to interpret. No oracle, network,
database, RDF library, or production service participates in this implementation.
"""

from collections import Counter
from typing import Any

from .input import Case, Query, Slot

BASE_KINDS = frozenset(
    {
        "FRIENDSHIP",
        "COMPANIONSHIP",
        "COOPERATION",
        "MENTORSHIP",
        "ROMANCE",
        "HOSTILITY",
        "TRUST",
        "DISTRUST",
        "ACQUAINTANCE",
        "OTHER",
    }
)


def slot_key(slot: Slot) -> tuple[str, ...]:
    """Symmetry changes identity only; the stored endpoint order is preserved."""
    ends = (slot.source_node_ref, slot.target_node_ref)
    if slot.direction != "DIRECTED":
        ends = tuple(sorted(ends))
    return (slot.work_id, *ends, slot.axis, slot.kind, slot.direction)


class TypedModel:
    def __init__(self, case: Case, *, extended: bool = False):
        if extended:
            raise NotImplementedError("extension experiment has not been installed")
        self.case = case
        self.nodes = {node.node_ref: node for node in case.nodes}
        self.sources = {source.source_ref: source for source in case.sources}
        self.claims = {claim.claim_ref: claim for claim in case.claims}
        self._validated: bool | None = None

    def validate(self) -> dict[str, Any]:
        case = self.case
        errors: list[dict[str, str]] = []

        def check(condition: bool, code: str, where: str) -> None:
            if not condition:
                errors.append({"code": code, "message": where})

        def references(refs: tuple[str, ...], known: dict, where: str, *, required=False):
            check(not required or bool(refs), "REQUIRED_REFERENCE", where)
            check(len(refs) == len(set(refs)), "DUPLICATE_REFERENCE", where)
            check(all(ref in known for ref in refs), "UNKNOWN_REFERENCE", where)

        def slot(value: Slot, where: str):
            check(value.work_id == case.work_id, "WORK_BOUNDARY", where)
            check(
                value.source_node_ref in self.nodes and value.target_node_ref in self.nodes,
                "UNKNOWN_NODE",
                where,
            )
            check(value.source_node_ref != value.target_node_ref, "SELF_RELATION", where)
            check(value.kind in BASE_KINDS, "UNSUPPORTED_KIND", where)

        for objects, field in (
            (case.nodes, "node_ref"),
            (case.sources, "source_ref"),
            (case.claims, "claim_ref"),
            (case.facts, "fact_ref"),
            (case.suppressions, "suppression_ref"),
            (case.queries, "query_ref"),
        ):
            ids = [getattr(obj, field) for obj in objects]
            check(len(ids) == len(set(ids)), "DUPLICATE_IDENTIFIER", field)
        check(bool(case.queries), "REQUIRED_QUERY", case.case_id)
        for node in case.nodes:
            check(node.work_id == case.work_id, "WORK_BOUNDARY", node.node_ref)
        for source in case.sources:
            check(source.work_id == case.work_id, "WORK_BOUNDARY", source.source_ref)
            check(
                source.episode_no <= case.snapshot_through_episode_no,
                "FUTURE_SOURCE",
                source.source_ref,
            )
            check(source.quote in source.text, "QUOTE_NOT_IN_SOURCE", source.source_ref)
        for claim in case.claims:
            check(claim.source_ref in self.sources, "UNKNOWN_SOURCE", claim.claim_ref)
            references(claim.subject_candidate_refs, self.nodes, claim.claim_ref, required=True)
            references(claim.object_candidate_refs, self.nodes, claim.claim_ref, required=True)
        keys = [slot_key(fact.slot) for fact in case.facts]
        for key, count in Counter(keys).items():
            check(count == 1, "DUPLICATE_SLOT", repr(key))
        for fact in case.facts:
            ref = fact.fact_ref
            slot(fact.slot, ref)
            check(
                fact.known_from_episode_no
                <= fact.last_changed_episode_no
                <= case.snapshot_through_episode_no,
                "FACT_TIME_ORDER",
                ref,
            )
            references(fact.support_claim_refs, self.claims, ref, required=True)
            references(fact.end_claim_refs, self.claims, ref)
            references(fact.correction_source_refs, self.sources, ref)
            check(
                not set(fact.support_claim_refs) & set(fact.end_claim_refs),
                "OVERLAPPING_END_SUPPORT",
                ref,
            )
            check((fact.status == "ENDED") == bool(fact.end_claim_refs), "END_EVIDENCE", ref)
            support_episodes = []
            for claim_ref in (*fact.support_claim_refs, *fact.end_claim_refs):
                claim = self.claims.get(claim_ref)
                if claim is None:
                    continue
                unique = len(claim.subject_candidate_refs) == len(claim.object_candidate_refs) == 1
                check(unique, "AMBIGUOUS_SUPPORT", ref)
                if unique:
                    ends = (claim.subject_candidate_refs[0], claim.object_candidate_refs[0])
                    wanted = (fact.slot.source_node_ref, fact.slot.target_node_ref)
                    check(
                        ends == wanted
                        if fact.slot.direction == "DIRECTED"
                        else set(ends) == set(wanted),
                        "CLAIM_ENDPOINTS",
                        ref,
                    )
                source = self.sources.get(claim.source_ref)
                if source is not None:
                    check(
                        source.episode_no <= fact.last_changed_episode_no,
                        "EVIDENCE_AFTER_CHANGE",
                        ref,
                    )
                    if claim_ref in fact.support_claim_refs:
                        support_episodes.append(source.episode_no)
                if fact.fact_validity == "VALID":
                    check(
                        claim.statement_mode == "ASSERTED"
                        and claim.curation_status == "CONFIRMED"
                        and claim.temporal_scope == "PRESENT",
                        "UNCONFIRMED_SUPPORT",
                        ref,
                    )
                elif claim_ref in fact.support_claim_refs:
                    check(claim.curation_status == "RETRACTED", "RETRACTION_SUPPORT", ref)
            check(
                bool(support_episodes) and min(support_episodes) <= fact.known_from_episode_no,
                "KNOWLEDGE_BEFORE_SUPPORT",
                ref,
            )
            if fact.fact_validity == "VALID":
                check(not fact.correction_source_refs, "VALID_FACT_CORRECTION", ref)
            else:
                check(
                    bool(fact.correction_source_refs)
                    and fact.status == "ACTIVE"
                    and not fact.end_claim_refs,
                    "RETRACTION_IS_NOT_END",
                    ref,
                )
            for source_ref in fact.correction_source_refs:
                source = self.sources.get(source_ref)
                if source is not None:
                    check(
                        source.episode_no <= fact.last_changed_episode_no,
                        "CORRECTION_AFTER_CHANGE",
                        ref,
                    )
        for suppression in case.suppressions:
            slot(suppression.slot, suppression.suppression_ref)
            check(
                suppression.recorded_revision <= case.snapshot_revision,
                "FUTURE_SUPPRESSION",
                suppression.suppression_ref,
            )
        for query in case.queries:
            self._check_query(query, check)
        self._validated = not errors
        return {"conforms": not errors, "violations": errors}

    def _check_query(self, query: Query, check) -> None:
        check(
            (query.mode == "READER_KNOWLEDGE" and query.story_context_id is None)
            or (query.mode == "STORY_TIME" and bool(query.story_context_id)),
            "QUERY_CONTEXT",
            query.query_ref,
        )
        check(
            (query.node_ref is None) == (query.perspective is None),
            "QUERY_FILTER_PAIR",
            query.query_ref,
        )
        check(query.node_ref is None or query.node_ref in self.nodes, "QUERY_NODE", query.query_ref)

    def _unsupported(self, query: Query, reason: str) -> dict[str, Any]:
        return {
            "caseId": self.case.case_id,
            "queryRef": query.query_ref,
            "status": "UNSUPPORTED",
            "reason": reason,
            "facts": [],
            "excludedFacts": [
                {"factRef": fact.fact_ref, "reason": "UNSUPPORTED_QUERY"}
                for fact in sorted(self.case.facts, key=lambda value: value.fact_ref)
            ],
            "heldClaimRefs": [],
        }

    def query(self, query: Query) -> dict[str, Any]:
        if self._validated is None:
            self.validate()
        if not self._validated:
            raise ValueError("invalid input; inspect validate() violations")
        problems = []
        self._check_query(
            query, lambda condition, code, _: problems.append(code) if not condition else None
        )
        if problems:
            raise ValueError(",".join(problems))
        if query.mode == "STORY_TIME":
            return self._unsupported(query, "STORY_TIME_RECONSTRUCTION_NOT_SUPPORTED")
        if query.cutoff_episode_no > self.case.snapshot_through_episode_no:
            return self._unsupported(query, "QUERY_BEYOND_SNAPSHOT")
        if query.axis is not None or query.node_ref is not None:
            return self._unsupported(query, "QUERY_FILTER_NOT_SUPPORTED")
        suppressed = {slot_key(value.slot) for value in self.case.suppressions}
        included, excluded = [], []
        for fact in sorted(self.case.facts, key=lambda value: value.fact_ref):
            reason = None
            if fact.known_from_episode_no > query.cutoff_episode_no:
                reason = "FUTURE_KNOWLEDGE"
            elif slot_key(fact.slot) in suppressed:
                reason = "USER_SUPPRESSED"
            elif fact.fact_validity == "RETRACTED":
                reason = "RETRACTED_FACT"
            if reason:
                excluded.append({"factRef": fact.fact_ref, "reason": reason})
            else:
                included.append(fact)
        if any(fact.last_changed_episode_no > query.cutoff_episode_no for fact in included):
            return self._unsupported(query, "HISTORY_NOT_AVAILABLE")
        facts = []
        for fact in included:
            value = fact.to_dict()
            value["supportSourceRefs"] = sorted(
                {self.claims[ref].source_ref for ref in fact.support_claim_refs}
            )
            value["endSourceRefs"] = sorted(
                {self.claims[ref].source_ref for ref in fact.end_claim_refs}
            )
            facts.append(value)
        return {
            "caseId": self.case.case_id,
            "queryRef": query.query_ref,
            "status": "READY",
            "reason": None,
            "facts": facts,
            "excludedFacts": excluded,
            "heldClaimRefs": sorted(
                claim.claim_ref
                for claim in self.case.claims
                if claim.curation_status == "HOLD"
                and self.sources[claim.source_ref].episode_no <= query.cutoff_episode_no
            ),
        }

    def to_case(self) -> Case:
        return Case.model_validate(self.case.to_dict())
