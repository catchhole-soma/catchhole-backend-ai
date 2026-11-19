"""Shared eligibility and current-revision suppression, without inference."""

from copy import deepcopy
from dataclasses import dataclass

from .input import RELATION_FIELDS, parse_case


@dataclass(frozen=True)
class Prepared:
    case: dict
    eligible: tuple[dict, ...]
    outcome: str
    outcome_reason: str | None
    withheld: tuple[dict, ...]
    suppressed_keys: frozenset[tuple]


def canonical_suppression_key(row: dict) -> tuple:
    """Treat inverse family display forms as the same suppression identity."""
    values = {field: row[field] for field in RELATION_FIELDS}
    inverse = {"CHILD_OF": "PARENT_OF", "GRANDCHILD_OF": "GRANDPARENT_OF"}
    if row["kind"] in inverse:
        values["subjectId"], values["objectId"] = values["objectId"], values["subjectId"]
        values["kind"] = inverse[row["kind"]]
    return tuple(values[field] for field in RELATION_FIELDS)


def active_suppressions(case: dict) -> tuple[dict, ...]:
    """Suppression uses the current revision, not the reader's episode cutoff."""
    query = case["query"]
    return tuple(
        row
        for row in case["suppressions"]
        if row["active"]
        and row["recordedRevision"] <= case["snapshotRevision"]
        and row["workId"] == query["workId"]
        and row["temporalContextId"] == query["temporalContextId"]
    )


def prepare(value: dict) -> Prepared:
    case = parse_case(value)
    query = case["query"]
    entities = {row["entityId"]: row for row in case["entities"]}
    sources = {row["sourceId"]: row for row in case["sources"]}
    suppressed = frozenset(canonical_suppression_key(row) for row in active_suppressions(case))
    outcome, reason = "READY", None
    if (
        query["inputMode"] == "CURRENT_PROFILE"
        and query["cutoffEpisode"] < case["snapshotThroughEpisode"]
    ):
        outcome, reason = "UNSUPPORTED", "UNSUPPORTED_HISTORY"
    else:
        # Supplied issues are pre-validation annotations. No cycle or generation
        # contradiction is newly detected here or attributed to an OWL reasoner.
        issues = {issue for row in case["assertions"] for issue in row["validationIssues"]}
        # A malformed work boundary is also visible without semantic inference.
        cross_work = any(
            row["workId"] != query["workId"]
            or any(
                entities[row[field]]["workId"] != row["workId"]
                for field in ("subjectId", "objectId")
            )
            or any(sources[ref]["workId"] != row["workId"] for ref in row["sourceIds"])
            for row in case["assertions"]
        )
        if cross_work:
            issues.add("CROSS_WORK")
        for fatal in ("CROSS_WORK", "SELF_RELATION", "PARENT_CYCLE"):
            if fatal in issues:
                outcome, reason = "REJECTED", fatal
                break
    if outcome != "READY":
        withheld = tuple(
            {"assertionId": row["assertionId"], "reason": reason} for row in case["assertions"]
        )
        return Prepared(case, (), outcome, reason, withheld, suppressed)

    eligible, withheld = [], []
    states = {
        "UNCERTAIN": "UNCERTAIN_ASSERTION",
        "NEGATED": "NEGATED_ASSERTION",
        "HYPOTHETICAL": "HYPOTHETICAL_ASSERTION",
        "CONFLICTING": "CONFLICTING_ASSERTION",
    }
    for row in case["assertions"]:
        active = [ref for ref in row["sourceIds"] if sources[ref]["supportStatus"] == "ACTIVE"]
        visible = [ref for ref in active if sources[ref]["episodeNo"] <= query["cutoffEpisode"]]
        if row["factValidity"] == "WITHDRAWN":
            reason = "WITHDRAWN_FACT"
        elif "GENERATION_CONFLICT" in row["validationIssues"]:
            reason = "GENERATION_CONFLICT"
        elif row["assertionState"] in states:
            reason = states[row["assertionState"]]
        elif any(
            entities[row[field]]["identityStatus"] != "RESOLVED"
            for field in ("subjectId", "objectId")
        ):
            reason = "UNRESOLVED_IDENTITY"
        elif row["nativeTime"]["scope"] == "UNKNOWN":
            reason = "UNKNOWN_TIME"
        elif row["nativeTime"]["scope"] == "HYPOTHETICAL":
            reason = "HYPOTHETICAL_ASSERTION"
        elif row["temporalContextId"] != query["temporalContextId"]:
            reason = "DIFFERENT_CONTEXT"
        elif row["factValidity"] == "REVIEW_REQUIRED":
            reason = "UNCERTAIN_ASSERTION"
        elif not active:
            reason = "WITHDRAWN_FACT"
        elif not visible:
            reason = "FUTURE_EVIDENCE"
        elif canonical_suppression_key(row) in suppressed:
            reason = "USER_SUPPRESSED"
        else:
            reason = None
        if reason:
            withheld.append({"assertionId": row["assertionId"], "reason": reason})
        else:
            accepted = deepcopy(row)
            accepted["sourceIds"] = visible
            eligible.append(accepted)
    return Prepared(case, tuple(eligible), outcome, None, tuple(withheld), suppressed)
