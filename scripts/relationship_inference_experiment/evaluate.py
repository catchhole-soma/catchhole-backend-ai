"""Independent exact-output oracle comparison; no engine or input-loader imports.

Only this boundary reads ``expected``. Rule premise order is meaningful; relation,
proof and source-reference collections are exact sets with duplicates rejected.
"""

import json
from collections import Counter

RELATION_FIELDS = (
    "workId",
    "subjectId",
    "objectId",
    "kind",
    "subtype",
    "temporalContextId",
    "generationGap",
)
RESULT_FIELDS = {
    "caseId",
    "outcome",
    "outcomeReason",
    "directRelations",
    "derivedRelations",
    "withheldAssertions",
    "excludedDerivedRelations",
}
GRANDCHILD_RULE = {
    "ruleId": "R_GRANDPARENT_INVERSE",
    "premiseCount": 1,
    "outputKind": "GRANDCHILD_OF",
    "derivationType": "SEMANTIC_INVERSE",
}
GRANDCHILD_PATH = ["R_BIOLOGICAL_GRANDPARENT", "R_GRANDPARENT_INVERSE"]


def _keys(value, fields, where):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{where}: missing or unsupported fields")


def _text(value, where):
    if type(value) is not str or not value.strip():
        raise ValueError(f"{where}: nonempty string required")
    return value


def _array(value, where):
    if not isinstance(value, list):
        raise TypeError(f"{where}: array required")
    return value


def _references(value, where, *, ordered=False):
    refs = [_text(item, where) for item in _array(value, where)]
    if not refs or len(refs) != len(set(refs)):
        raise ValueError(f"{where}: empty or duplicate references")
    return refs if ordered else sorted(refs)


def _relation(value, where):
    result = {field: value[field] for field in RELATION_FIELDS}
    for field in RELATION_FIELDS[:-1]:
        _text(result[field], f"{where}.{field}")
    gap = result["generationGap"]
    if gap is not None and (type(gap) is not int or gap < 1):
        raise ValueError(f"{where}.generationGap: null or positive integer required")
    return result


def _relation_key(value):
    return tuple(value[field] for field in RELATION_FIELDS)


def _sort(rows):
    return sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))


def _grandchild_trace(proof, relation):
    if (
        proof["ruleId"] != GRANDCHILD_RULE["ruleId"]
        or proof["rulePath"] != GRANDCHILD_PATH
        or len(proof["premiseAssertionIds"]) != 2
        or relation["derivationType"] != "SEMANTIC_INVERSE"
        or relation["subtype"] != "BIOLOGICAL"
        or relation["generationGap"] != 2
    ):
        raise ValueError("grandchild proof has an invalid rule path or original premise roles")
    intermediate = _array(proof["intermediateRelations"], "intermediateRelations")
    if len(intermediate) != 1:
        raise ValueError("grandchild proof requires exactly one intermediate grandparent")
    _keys(intermediate[0], RELATION_FIELDS, "intermediate grandparent")
    grandparent = _relation(intermediate[0], "intermediate grandparent")
    wanted = {
        **{field: relation[field] for field in RELATION_FIELDS},
        "kind": "GRANDPARENT_OF",
        "subjectId": relation["objectId"],
        "objectId": relation["subjectId"],
    }
    if grandparent != wanted:
        raise ValueError("intermediate grandparent does not explain this inverse conclusion")
    return {"rulePath": list(GRANDCHILD_PATH), "intermediateRelations": [grandparent]}


def _relations(value, category, *, extended=False):
    rows, seen = [], set()
    extras = {
        "directRelations": {"supportAssertionIds", "sourceIds"},
        "derivedRelations": {"derivationType", "proofs"},
        "excludedDerivedRelations": {"reason", "suppressionIds"},
    }[category]
    for raw in _array(value, category):
        _keys(raw, set(RELATION_FIELDS) | extras, category)
        row = _relation(raw, category)
        is_grandchild = row["kind"] == "GRANDCHILD_OF"
        if is_grandchild and not extended:
            raise ValueError("grandchild output requires an explicitly extended oracle")
        key = _relation_key(row)
        if key in seen:
            raise ValueError(f"{category}: duplicate relation tuple")
        seen.add(key)
        if category == "directRelations":
            for field in extras:
                row[field] = _references(raw[field], f"{category}.{field}")
        elif category == "excludedDerivedRelations":
            if raw["reason"] != "USER_SUPPRESSED":
                raise ValueError("excludedDerivedRelations: unsupported reason")
            row["reason"] = raw["reason"]
            row["suppressionIds"] = _references(raw["suppressionIds"], "suppressionIds")
        else:
            if raw["derivationType"] not in {"SEMANTIC_INVERSE", "NEW_RELATION"}:
                raise ValueError("derivedRelations: invalid derivationType")
            row["derivationType"] = raw["derivationType"]
            proofs, seen_proofs = [], set()
            for proof in _array(raw["proofs"], "proofs"):
                proof_fields = {"ruleId", "premiseAssertionIds", "sourceIds"}
                if is_grandchild:
                    proof_fields |= {"rulePath", "intermediateRelations"}
                _keys(proof, proof_fields, "proof")
                normalized = {
                    "ruleId": _text(proof["ruleId"], "proof.ruleId"),
                    # [A->B, B->C] must not be sorted into an unordered set.
                    "premiseAssertionIds": _references(
                        proof["premiseAssertionIds"], "premiseAssertionIds", ordered=True
                    ),
                    "sourceIds": _references(proof["sourceIds"], "proof.sourceIds"),
                }
                if is_grandchild:
                    normalized.update(_grandchild_trace(proof, row))
                proof_key = (normalized["ruleId"], tuple(normalized["premiseAssertionIds"]))
                if proof_key in seen_proofs:
                    raise ValueError("proofs: duplicate rule/premise roles")
                seen_proofs.add(proof_key)
                proofs.append(normalized)
            if not proofs:
                raise ValueError("derivedRelations: proof required")
            row["proofs"] = _sort(proofs)
        rows.append(row)
    return _sort(rows)


def normalize_result(result: dict, *, extended: bool = False) -> dict:
    """Validate output shape, then canonicalize only unordered collections."""
    _keys(result, RESULT_FIELDS, "result")
    outcome = result["outcome"]
    if outcome not in {"READY", "REJECTED", "UNSUPPORTED"}:
        raise ValueError("result.outcome: unsupported value")
    reason = result["outcomeReason"]
    if outcome == "READY":
        if reason is not None:
            raise ValueError("READY requires a null reason")
    else:
        _text(reason, "outcomeReason")
    normalized = {
        "caseId": _text(result["caseId"], "caseId"),
        "outcome": outcome,
        "outcomeReason": reason,
    }
    for category in ("directRelations", "derivedRelations", "excludedDerivedRelations"):
        normalized[category] = _relations(result[category], category, extended=extended)
    withheld, seen = [], set()
    for row in _array(result["withheldAssertions"], "withheldAssertions"):
        _keys(row, {"assertionId", "reason"}, "withheldAssertions")
        assertion_id = _text(row["assertionId"], "withheld assertionId")
        if assertion_id in seen:
            raise ValueError("withheldAssertions: duplicate assertion")
        seen.add(assertion_id)
        withheld.append(
            {"assertionId": assertion_id, "reason": _text(row["reason"], "withheld reason")}
        )
    normalized["withheldAssertions"] = _sort(withheld)
    if outcome != "READY" and any(
        normalized[key]
        for key in ("directRelations", "derivedRelations", "excludedDerivedRelations")
    ):
        raise ValueError("non-READY result contains relations")
    direct_supports = {
        ref for row in normalized["directRelations"] for ref in row["supportAssertionIds"]
    }
    if direct_supports & seen:
        raise ValueError("assertion is both direct support and withheld")
    included = {_relation_key(row) for row in normalized["derivedRelations"]}
    excluded = {_relation_key(row) for row in normalized["excludedDerivedRelations"]}
    if included & excluded:
        raise ValueError("derived relation is both included and excluded")
    return normalized


def _expected_result(case, *, extended=False):
    expected = case["expected"]
    allowed = RESULT_FIELDS - {"caseId"} | {"forbiddenRelations"}
    if not isinstance(expected, dict) or set(expected) - allowed:
        raise ValueError("expected: unsupported fields")
    # v1 handwritten cases predate explicit suppressed-derived output.
    result = {key: value for key, value in expected.items() if key != "forbiddenRelations"}
    result.setdefault("excludedDerivedRelations", [])
    return normalize_result({"caseId": case["caseId"], **result}, extended=extended)


def _oracle_extended(document):
    """An oracle declaration enables its trace schema, never runtime inference."""
    declarations = []
    for rule in _array(document.get("rules", []), "oracle rules"):
        if not isinstance(rule, dict):
            raise TypeError("oracle rule declaration must be an object")
        if (
            rule.get("ruleId") == GRANDCHILD_RULE["ruleId"]
            or rule.get("outputKind") == "GRANDCHILD_OF"
        ):
            if rule != GRANDCHILD_RULE or type(rule.get("premiseCount")) is not int:
                raise ValueError("malformed grandchild oracle rule declaration")
            declarations.append(rule)
    if len(declarations) > 1:
        raise ValueError("duplicate grandchild oracle rule declaration")
    return bool(declarations)


def evaluate(actual_rows: list[dict], oracle_document: dict) -> dict:
    """Compare saved flat actual rows with an independently opened oracle.

    Malformed, duplicate, missing and extra outputs are failures, never silently
    normalized away. ``forbiddenRelations`` is an explanatory nonexhaustive hint;
    exact direct/derived/excluded sets are authoritative.
    """
    failures = []
    oracle = {}
    cases = oracle_document.get("cases", []) if isinstance(oracle_document, dict) else []
    if not isinstance(cases, list) or not cases:
        return {
            "passed": False,
            "caseCount": 0,
            "passedCases": 0,
            "failures": [{"caseId": None, "reason": "INVALID_ORACLE"}],
        }
    try:
        extended = _oracle_extended(oracle_document)
    except (TypeError, ValueError) as error:
        return {
            "passed": False,
            "caseCount": len(cases),
            "passedCases": 0,
            "failures": [{"caseId": None, "reason": "INVALID_ORACLE", "detail": str(error)}],
        }
    for case in cases:
        case_id = case.get("caseId") if isinstance(case, dict) else None
        try:
            _text(case_id, "caseId")
            if case_id in oracle:
                raise ValueError("duplicate oracle caseId")
            oracle[case_id] = _expected_result(case, extended=extended)
        except (KeyError, TypeError, ValueError) as error:
            failures.append({"caseId": case_id, "reason": "INVALID_ORACLE", "detail": str(error)})
    if failures:
        return {"passed": False, "caseCount": len(cases), "passedCases": 0, "failures": failures}
    actual = {}
    if not isinstance(actual_rows, list):
        actual_rows = []
        failures.append(
            {"caseId": None, "reason": "INVALID_OUTPUT", "detail": "actual rows must be an array"}
        )
    counts = Counter(
        row.get("caseId")
        for row in actual_rows
        if isinstance(row, dict) and isinstance(row.get("caseId"), str)
    )
    for row in actual_rows:
        case_id = row.get("caseId") if isinstance(row, dict) else None
        try:
            normalized = normalize_result(row, extended=extended)
            if counts[case_id] != 1:
                raise ValueError("duplicate output caseId")
            actual[case_id] = normalized
        except (KeyError, TypeError, ValueError) as error:
            failures.append({"caseId": case_id, "reason": "INVALID_OUTPUT", "detail": str(error)})
    invalid_ids = {row["caseId"] for row in failures if isinstance(row["caseId"], str)}
    passed = 0
    for case_id, expected in oracle.items():
        if case_id in invalid_ids:
            continue
        if case_id not in actual:
            failures.append({"caseId": case_id, "reason": "MISSING_CASE"})
        elif actual[case_id] != expected:
            fields = sorted(key for key in RESULT_FIELDS if actual[case_id][key] != expected[key])
            failures.append({"caseId": case_id, "reason": "OUTPUT_MISMATCH", "fields": fields})
        else:
            passed += 1
    for case_id in actual.keys() - oracle.keys():
        failures.append({"caseId": case_id, "reason": "UNEXPECTED_CASE"})
    return {
        "passed": not failures,
        "caseCount": len(oracle),
        "passedCases": passed,
        "failures": failures,
    }
