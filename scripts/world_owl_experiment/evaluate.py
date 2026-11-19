"""Independent saved-output comparison. This module never invokes a reasoner."""

from __future__ import annotations

import json


def normalized_supports(rows):
    groups = []
    for row in rows:
        ids = row["statementIds"] if isinstance(row, dict) else row
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate statement in support")
        groups.append(sorted(ids))
    if len({tuple(group) for group in groups}) != len(groups):
        raise ValueError("duplicate support")
    return sorted(groups)


def canonical(row):
    queries = row["queries"]
    if len({query["id"] for query in queries}) != len(queries):
        raise ValueError("duplicate query result")
    return {
        "caseId": row["caseId"],
        "outcome": row["outcome"],
        "conflictSets": normalized_supports(row["conflictSets"]),
        "queries": sorted(
            [
                {
                    "id": query["id"],
                    "verdict": query["verdict"],
                    "supports": normalized_supports(query["supports"]),
                }
                for query in queries
            ],
            key=lambda item: item["id"],
        ),
        "excludedStatements": sorted(row["excludedStatements"], key=lambda item: item["id"]),
    }


def verify_provenance(actual, case):
    """Audit the saved evidence against raw input, independently of policy.py.

    The semantic oracle lists statement sets. This separate audit prevents a
    correct set of IDs from masking fabricated quotes or ineligible sources.
    """
    scope = case["scope"]
    revision = scope["snapshotRevision"]
    eligible_sources = {}
    for source in case["sources"]:
        same_scope = all(source[key] == scope[key] for key in ("workId", "contextId"))
        recorded = source["recordedRevision"] <= revision
        withdrawn = (
            source["withdrawnRevision"] is not None and source["withdrawnRevision"] <= revision
        )
        if (
            same_scope
            and recorded
            and not withdrawn
            and source["episodeNo"] <= scope["cutoffEpisode"]
        ):
            eligible_sources[source["id"]] = source
    statements, source_map = {}, {}
    for row in case["statements"]:
        same_scope = all(row[key] == scope[key] for key in ("workId", "contextId"))
        recorded = row["recordedRevision"] <= revision
        withdrawn = row["withdrawnRevision"] is not None and row["withdrawnRevision"] <= revision
        expired = row["validTo"] is not None and row["validTo"] <= scope["storyTime"]
        active = row["validFrom"] <= scope["storyTime"] and not expired
        refs = sorted(key for key in row["sourceIds"] if key in eligible_sources)
        if (
            same_scope
            and recorded
            and not withdrawn
            and active
            and row["modality"] == "STRICT"
            and row["confirmed"] is True
            and refs
        ):
            statements[row["id"]] = row
            source_map[row["id"]] = refs
    if actual.get("scope") != scope or actual.get("eligibleStatementIds") != sorted(statements):
        raise ValueError("saved scope/eligible statements differ from input")
    if actual.get("explanationKind") != "ALL_SUBSET_MINIMAL_JUSTIFICATIONS":
        raise ValueError("unexpected explanation semantics")
    queries = {row["id"]: row for row in case["queries"]}
    if len(actual["queries"]) != len(queries):
        raise ValueError("query coverage mismatch")
    for query in actual["queries"]:
        if query.get("query") != queries.get(query["id"]):
            raise ValueError("saved query differs from input")
    groups = actual["conflictSets"] + [
        group for query in actual["queries"] for group in query["supports"]
    ]
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "statementIds",
            "axiomIds",
            "factIds",
            "sourceIds",
            "sourceAlternatives",
            "sources",
        }:
            raise ValueError("invalid provenance shape")
        ids = group["statementIds"]
        if len(set(ids)) != len(ids) or any(key not in statements for key in ids):
            raise ValueError("invalid or ineligible support statement")
        facts = sorted(
            key
            for key in ids
            if statements[key]["axiom"]["type"] in {"classAssertion", "propertyAssertion"}
        )
        if group["factIds"] != facts or group["axiomIds"] != sorted(set(ids) - set(facts)):
            raise ValueError("axiom/fact attribution mismatch")
        alternatives = [{"statementId": key, "sourceIds": source_map[key]} for key in sorted(ids)]
        source_ids = sorted({key for row in alternatives for key in row["sourceIds"]})
        if group["sourceAlternatives"] != alternatives or group["sourceIds"] != source_ids:
            raise ValueError("source alternatives/union mismatch")
        expected_sources = [
            {
                "id": key,
                "episodeNo": eligible_sources[key]["episodeNo"],
                "sha256": eligible_sources[key]["sha256"],
                "text": eligible_sources[key]["text"],
                "textStatus": "PURGED" if eligible_sources[key]["text"] is None else "AVAILABLE",
            }
            for key in source_ids
        ]
        if group["sources"] != expected_sources:
            raise ValueError("source text/hash/visibility mismatch")


def evaluate(actuals, oracle, inputs):
    if set(oracle) != {"schemaVersion", "cases"} or oracle["schemaVersion"] != 1:
        raise ValueError("unsupported oracle contract")
    expected = {case["caseId"]: case for case in oracle["cases"]}
    actual = {case["caseId"]: case for case in actuals}
    input_by_id = {case["caseId"]: case for case in inputs["cases"]}
    if (
        len(expected) != len(oracle["cases"])
        or len(actual) != len(actuals)
        or set(expected) != set(actual)
    ):
        raise ValueError("case coverage mismatch")
    if set(input_by_id) != set(actual) or len(input_by_id) != len(inputs["cases"]):
        raise ValueError("input coverage mismatch")
    failures = []
    for key in sorted(expected):
        if actual[key].get("explanationComplete") is not True:
            failures.append({"caseId": key, "reason": "explanation incomplete"})
            continue
        try:
            verify_provenance(actual[key], input_by_id[key])
        except (ValueError, KeyError, TypeError) as error:
            failures.append({"caseId": key, "reason": f"provenance invalid: {error}"})
            continue
        if canonical(actual[key]) != canonical(expected[key]):
            failures.append(
                {
                    "caseId": key,
                    "expected": canonical(expected[key]),
                    "actual": canonical(actual[key]),
                }
            )
    return {
        "caseCount": len(expected),
        "passed": len(expected) - len(failures),
        "failed": len(failures),
        "queryCount": sum(len(row["queries"]) for row in actuals),
        "failures": failures,
        "comparison": "exact verdict/support sets/conflicts/exclusions and independent source/scope/quote provenance audit",
    }


def digest(value):
    from hashlib import sha256

    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
