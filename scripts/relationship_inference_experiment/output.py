"""Common output shaping only; inference and proof discovery remain independent."""

from copy import deepcopy

from .input import RELATION_FIELDS
from .policy import Prepared, active_suppressions, canonical_suppression_key


def relation_key(row: dict) -> tuple:
    return tuple(row[field] for field in RELATION_FIELDS)


def relation(row: dict) -> dict:
    return {field: row[field] for field in RELATION_FIELDS}


def _sort_key(row: dict) -> tuple:
    key = relation_key(row)
    return (*key[:-1], -1 if key[-1] is None else key[-1])


def base_result(prepared: Prepared) -> dict:
    grouped = {}
    for row in prepared.eligible:
        key = relation_key(row)
        if key not in grouped:
            grouped[key] = {**relation(row), "supportAssertionIds": [], "sourceIds": []}
        grouped[key]["supportAssertionIds"].append(row["assertionId"])
        grouped[key]["sourceIds"].extend(row["sourceIds"])
    direct = []
    for row in grouped.values():
        row["supportAssertionIds"] = sorted(set(row["supportAssertionIds"]))
        row["sourceIds"] = sorted(set(row["sourceIds"]))
        direct.append(row)
    return {
        "caseId": prepared.case["caseId"],
        "outcome": prepared.outcome,
        "outcomeReason": prepared.outcome_reason,
        "directRelations": sorted(direct, key=_sort_key),
        "derivedRelations": [],
        "withheldAssertions": sorted(
            deepcopy(prepared.withheld), key=lambda row: row["assertionId"]
        ),
        "excludedDerivedRelations": [],
    }


def finish(prepared: Prepared, derived: list[dict]) -> dict:
    """Apply derived-output suppression and deterministic order, never infer.

    A suppressed row contains only the seven relation fields, USER_SUPPRESSED,
    and matching current suppression IDs. It is an excluded conclusion, not a
    replacement proof. Duplicate conclusions/proofs are not silently repaired.
    """
    result = base_result(prepared)
    if prepared.outcome != "READY":
        if derived:
            raise ValueError("non-ready input must not have derived conclusions")
        return result
    ids_by_key = {}
    for row in active_suppressions(prepared.case):
        ids_by_key.setdefault(canonical_suppression_key(row), []).append(row["suppressionId"])
    for row in derived:
        key = canonical_suppression_key(row)
        if key in prepared.suppressed_keys:
            result["excludedDerivedRelations"].append(
                {
                    **relation(row),
                    "reason": "USER_SUPPRESSED",
                    "suppressionIds": sorted(ids_by_key[key]),
                }
            )
        else:
            copied = deepcopy(row)
            for proof in copied["proofs"]:
                proof["sourceIds"] = sorted(proof["sourceIds"])
            copied["proofs"] = sorted(
                copied["proofs"],
                key=lambda proof: (
                    proof["ruleId"],
                    tuple(proof["premiseAssertionIds"]),
                    tuple(proof["sourceIds"]),
                ),
            )
            result["derivedRelations"].append(copied)
    result["derivedRelations"].sort(key=_sort_key)
    result["excludedDerivedRelations"].sort(key=_sort_key)
    return result
