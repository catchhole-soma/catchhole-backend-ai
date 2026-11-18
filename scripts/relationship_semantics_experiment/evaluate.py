"""Oracle-only code: engines and the input loader never import this module."""

import json
from copy import deepcopy
from typing import Any


def _slot_key(slot: dict) -> tuple:
    ends = (slot["sourceNodeRef"], slot["targetNodeRef"])
    if slot["direction"] in {"MUTUAL", "UNDIRECTED"}:
        ends = tuple(sorted(ends))
    return (slot["workId"], *ends, slot["axis"], slot["kind"], slot["direction"])


def _fact(value: dict) -> dict:
    value = deepcopy(value)
    # Arrays are semantic sets; sorting preserves duplicates so they still fail.
    for key in (
        "supportClaimRefs",
        "endClaimRefs",
        "correctionSourceRefs",
        "supportSourceRefs",
        "endSourceRefs",
    ):
        value[key] = sorted(value[key])
    slot = value["slot"]
    if slot["direction"] in {"MUTUAL", "UNDIRECTED"}:
        slot["sourceNodeRef"], slot["targetNodeRef"] = sorted(
            (slot["sourceNodeRef"], slot["targetNodeRef"])
        )
    return value


def _normal_result(value: dict) -> dict:
    result = deepcopy(value)
    result["facts"] = sorted(
        (_fact(fact) for fact in value["facts"]), key=lambda fact: fact["factRef"]
    )
    result["excludedFacts"] = sorted(value["excludedFacts"], key=lambda fact: fact["factRef"])
    result["heldClaimRefs"] = sorted(value["heldClaimRefs"])
    return result


def expected_result(case: dict, query: dict) -> dict:
    """Expand handwritten fact refs, without reimplementing either query engine."""
    expected = query["expected"]
    known = {fact["factRef"]: fact for fact in case["facts"]}
    claims = {claim["claimRef"]: claim for claim in case["claims"]}
    facts = []
    for ref in expected["factRefs"]:
        fact = deepcopy(known[ref])
        fact["supportSourceRefs"] = sorted(
            {claims[r]["sourceRef"] for r in fact["supportClaimRefs"]}
        )
        fact["endSourceRefs"] = sorted({claims[r]["sourceRef"] for r in fact["endClaimRefs"]})
        facts.append(fact)
    return {
        "caseId": case["caseId"],
        "queryRef": query["queryRef"],
        "status": expected["status"],
        "reason": expected["reason"],
        "facts": facts,
        "excludedFacts": deepcopy(expected["excludedFacts"]),
        "heldClaimRefs": list(expected["heldClaimRefs"]),
    }


def evaluate_result(case: dict, query: dict, actual: dict) -> dict[str, Any]:
    errors = []
    expected = expected_result(case, query)
    try:
        if _normal_result(actual) != _normal_result(expected):
            errors.append("FULL_RESULT_MISMATCH")
        forbidden = {_slot_key(item["slot"]) for item in query["expected"]["forbiddenSlots"]}
        if any(_slot_key(fact["slot"]) in forbidden for fact in actual["facts"]):
            errors.append("FORBIDDEN_SLOT")
    except (KeyError, TypeError, ValueError):
        errors.append("INVALID_RESULT_STRUCTURE")
    return {
        "caseId": case["caseId"],
        "queryRef": query["queryRef"],
        "passed": not errors,
        "errors": errors,
        "expected": expected,
        "actual": actual,
    }


def evaluate_results(oracle: dict, actual: list[dict]) -> dict:
    reports = []
    expected_queries = {}
    for case in oracle["cases"]:
        for query in case["queries"]:
            key = (case["caseId"], query["queryRef"])
            if key in expected_queries:
                raise ValueError("duplicate oracle query identity")
            expected_queries[key] = (case, query)
    seen = set()
    for result in actual:
        key = (result.get("caseId"), result.get("queryRef"))
        if key in seen or key not in expected_queries:
            reports.append(
                {
                    "caseId": key[0],
                    "queryRef": key[1],
                    "passed": False,
                    "errors": ["DUPLICATE_OR_UNKNOWN_QUERY"],
                }
            )
            continue
        seen.add(key)
        reports.append(evaluate_result(*expected_queries[key], result))
    for key in sorted(expected_queries.keys() - seen):
        reports.append(
            {"caseId": key[0], "queryRef": key[1], "passed": False, "errors": ["MISSING_QUERY"]}
        )
    return {
        "passed": all(row["passed"] for row in reports),
        "queryCount": len(expected_queries),
        "reports": reports,
    }


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
