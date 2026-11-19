"""Application eligibility, deliberately outside OWL's monotonic semantics."""

from __future__ import annotations


def prepare(case):
    scope = case["scope"]
    revision = scope["snapshotRevision"]
    eligible_sources = {
        source["id"]: source
        for source in case["sources"]
        if source["workId"] == scope["workId"]
        and source["contextId"] == scope["contextId"]
        and source["episodeNo"] <= scope["cutoffEpisode"]
        and source["recordedRevision"] <= revision
        and (source["withdrawnRevision"] is None or revision < source["withdrawnRevision"])
    }
    included, excluded, sources_by_statement = [], [], {}
    for row in sorted(case["statements"], key=lambda item: item["id"]):
        reason = None
        if row["workId"] != scope["workId"] or row["contextId"] != scope["contextId"]:
            reason = "OUT_OF_SCOPE"
        elif row["recordedRevision"] > revision:
            reason = "NOT_YET_RECORDED"
        elif row["withdrawnRevision"] is not None and revision >= row["withdrawnRevision"]:
            reason = "WITHDRAWN"
        elif not (
            row["validFrom"] <= scope["storyTime"]
            and (row["validTo"] is None or scope["storyTime"] < row["validTo"])
        ):
            reason = "OUTSIDE_STORY_TIME"
        elif row["modality"] != "STRICT":
            reason = "NON_STRICT"
        elif not row["confirmed"]:
            reason = "UNCONFIRMED"
        valid_sources = sorted(set(row["sourceIds"]) & eligible_sources.keys())
        if reason is None and not valid_sources:
            reason = "NO_ELIGIBLE_SOURCE"
        if reason:
            excluded.append({"id": row["id"], "reason": reason})
        else:
            included.append(row)
            sources_by_statement[row["id"]] = valid_sources
    return included, excluded, eligible_sources, sources_by_statement


def explain_support(ids, included, sources, source_map):
    by_id = {row["id"]: row for row in included}
    statement_ids = sorted(ids)
    fact_ids = [
        key
        for key in statement_ids
        if by_id[key]["axiom"]["type"] in {"classAssertion", "propertyAssertion"}
    ]
    source_ids = sorted({source for key in statement_ids for source in source_map[key]})
    return {
        "statementIds": statement_ids,
        "axiomIds": sorted(set(statement_ids) - set(fact_ids)),
        "factIds": fact_ids,
        "sourceIds": source_ids,
        "sourceAlternatives": [
            {"statementId": key, "sourceIds": source_map[key]} for key in statement_ids
        ],
        "sources": [
            {
                "id": key,
                "episodeNo": sources[key]["episodeNo"],
                "sha256": sources[key]["sha256"],
                "text": sources[key]["text"],
                "textStatus": "AVAILABLE" if sources[key]["text"] is not None else "PURGED",
            }
            for key in source_ids
        ],
    }
