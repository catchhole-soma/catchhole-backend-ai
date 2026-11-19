"""Strict, oracle-blind input contract for the offline inference experiment.

The parser visits only declared execution fields. Expected answers, forbidden
examples and consumer prose are never read. Validation issues are supplied data,
not inconsistencies discovered by this parser or an OWL reasoner.
"""

from collections.abc import Mapping
from copy import deepcopy

RELATION_FIELDS = (
    "workId",
    "subjectId",
    "objectId",
    "kind",
    "subtype",
    "temporalContextId",
    "generationGap",
)
INPUT_KINDS = {"PARENT_OF", "ANCESTOR_OF", "TRUST"}
RELATION_KINDS = INPUT_KINDS | {"CHILD_OF", "GRANDPARENT_OF", "GRANDCHILD_OF"}
VALIDATION_ISSUES = {"CROSS_WORK", "SELF_RELATION", "PARENT_CYCLE", "GENERATION_CONFLICT"}
_DOCUMENT_METADATA = {"status", "oracle", "outputSemantics", "rules", "consumerExamples"}
_CASE_METADATA = {"expected", "forbiddenRelations", "consumerExamples"}
_CASE_FIELDS = {
    "caseId",
    "title",
    "purpose",
    "snapshotRevision",
    "snapshotThroughEpisode",
    "query",
    "entities",
    "sources",
    "assertions",
    "suppressions",
}


def _object(value, required, *, optional=(), ignored=(), where="object"):
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")  # noqa: TRY004 - public input error contract
    keys = set(value)
    if keys - set(required) - set(optional) - set(ignored):
        raise ValueError(f"unsupported field in {where}")
    if set(required) - keys:
        raise ValueError(f"missing field in {where}")
    # Never copy or inspect ignored values, including poisonous oracle mappings.
    return {key: value[key] for key in (*required, *optional) if key in value}


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a nonblank string")


def _positive(value, where):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{where} must be a positive integer")


def _enum(value, choices, where):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"unsupported {where}")


def _array(value, where):
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array")  # noqa: TRY004 - public input error contract
    return value


def _refs(value, known, where, *, nonempty=False):
    _array(value, where)
    for ref in value:
        _text(ref, where)
    if nonempty and not value:
        raise ValueError(f"{where} must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"duplicate reference in {where}")
    if not set(value) <= set(known):
        raise ValueError(f"unknown reference in {where}")


def _index(rows, id_field):
    index = {}
    for row in rows:
        _text(row[id_field], id_field)
        if row[id_field] in index:
            raise ValueError(f"duplicate {id_field}")
        index[row[id_field]] = row
    return index


def _relation(row, entities, works, *, kinds):
    for field in ("workId", "subjectId", "objectId", "temporalContextId"):
        _text(row[field], field)
    if row["workId"] not in works:
        raise ValueError("unknown relation work")
    if row["subjectId"] not in entities or row["objectId"] not in entities:
        raise ValueError("unknown relation endpoint")
    _enum(row["kind"], kinds, "relation kind")
    _enum(row["subtype"], {"BIOLOGICAL", "ADOPTIVE", "NONE"}, "relation subtype")
    if (row["kind"] == "TRUST") != (row["subtype"] == "NONE"):
        raise ValueError("kind/subtype mismatch")
    if row["kind"] in {"ANCESTOR_OF", "GRANDPARENT_OF", "GRANDCHILD_OF"}:
        _positive(row["generationGap"], "generationGap")
        if row["kind"] != "ANCESTOR_OF" and row["generationGap"] != 2:
            raise ValueError("grandparent/grandchild generationGap must be 2")
    elif row["generationGap"] is not None:
        raise ValueError("generationGap belongs to ancestry only")


def parse_case(case_input: Mapping, *, schema_version: int = 2) -> dict:
    """Validate one case and detach it from caller-owned mutable data."""
    required = _CASE_FIELDS - {"title", "purpose", "suppressions"}
    optional = {"title", "purpose"} | ({"suppressions"} if schema_version == 2 else set())
    raw = _object(case_input, required, optional=optional, ignored=_CASE_METADATA, where="case")
    case = {}
    for field in ("caseId", "title", "purpose"):
        if field in raw:
            _text(raw[field], field)
            case[field] = raw[field]
    for field in ("snapshotRevision", "snapshotThroughEpisode"):
        _positive(raw[field], field)
        case[field] = raw[field]

    query = _object(
        raw["query"], ("workId", "cutoffEpisode", "temporalContextId", "inputMode"), where="query"
    )
    _text(query["workId"], "query workId")
    _text(query["temporalContextId"], "query temporalContextId")
    _positive(query["cutoffEpisode"], "cutoffEpisode")
    if query["cutoffEpisode"] > case["snapshotThroughEpisode"]:
        raise ValueError("cutoffEpisode exceeds snapshot")
    _enum(query["inputMode"], {"EPISODE_SNAPSHOT", "CURRENT_PROFILE"}, "inputMode")
    case["query"] = query

    entities = []
    for value in _array(raw["entities"], "entities"):
        row = _object(value, ("entityId", "name", "workId", "identityStatus"), where="entity")
        for field in ("entityId", "name", "workId"):
            _text(row[field], field)
        _enum(row["identityStatus"], {"RESOLVED", "UNRESOLVED"}, "identityStatus")
        entities.append(row)
    if not entities:
        raise ValueError("entities must not be empty")
    entity_index = _index(entities, "entityId")
    works = {entity["workId"] for entity in entities}
    if query["workId"] not in works:
        raise ValueError("unknown query work")
    case["entities"] = entities

    sources = []
    for value in _array(raw["sources"], "sources"):
        row = _object(
            value,
            (
                "sourceId",
                "workId",
                "episodeNo",
                "supportStatus",
                "quoteAvailability",
                "text",
                "quote",
            ),
            where="source",
        )
        _text(row["sourceId"], "sourceId")
        _text(row["workId"], "source workId")
        if row["workId"] not in works:
            raise ValueError("unknown source work")
        _positive(row["episodeNo"], "episodeNo")
        if row["episodeNo"] > case["snapshotThroughEpisode"]:
            raise ValueError("source episode exceeds snapshot")
        _enum(row["supportStatus"], {"ACTIVE", "WITHDRAWN"}, "supportStatus")
        _enum(row["quoteAvailability"], {"AVAILABLE", "PURGED"}, "quoteAvailability")
        if row["quoteAvailability"] == "PURGED":
            if row["text"] is not None or row["quote"] is not None:
                raise ValueError("purged source must not retain text or quote")
        else:
            _text(row["text"], "source text")
            _text(row["quote"], "source quote")
            if row["quote"] not in row["text"]:
                raise ValueError("quote absent from source text")
        sources.append(row)
    source_index = _index(sources, "sourceId")
    case["sources"] = sources

    assertions = []
    for value in _array(raw["assertions"], "assertions"):
        row = _object(
            value,
            RELATION_FIELDS
            + (
                "assertionId",
                "assertionState",
                "factValidity",
                "nativeTime",
                "sourceIds",
                "validationIssues",
            ),
            where="assertion",
        )
        _text(row["assertionId"], "assertionId")
        _relation(row, entity_index, works, kinds=INPUT_KINDS)
        _enum(
            row["assertionState"],
            {"ASSERTED", "UNCERTAIN", "NEGATED", "HYPOTHETICAL", "CONFLICTING"},
            "assertionState",
        )
        _enum(row["factValidity"], {"VALID", "REVIEW_REQUIRED", "WITHDRAWN"}, "factValidity")
        native = _object(row["nativeTime"], ("domain", "scope"), where="nativeTime")
        _enum(native["domain"], {"FAMILY", "EPISODE_RELATIONSHIP"}, "nativeTime domain")
        scopes = (
            {"CURRENT", "HISTORICAL", "UNKNOWN"}
            if native["domain"] == "FAMILY"
            else {"PRESENT", "PAST", "HYPOTHETICAL", "UNKNOWN"}
        )
        _enum(native["scope"], scopes, "nativeTime scope")
        if (native["domain"] == "EPISODE_RELATIONSHIP") != (row["kind"] == "TRUST"):
            raise ValueError("nativeTime domain/kind mismatch")
        row["nativeTime"] = native
        _refs(row["sourceIds"], source_index, "sourceIds", nonempty=True)
        _refs(row["validationIssues"], VALIDATION_ISSUES, "validationIssues")
        assertions.append(row)
    _index(assertions, "assertionId")
    case["assertions"] = assertions

    suppressions = []
    for value in _array(raw.get("suppressions", []), "suppressions"):
        row = _object(
            value,
            RELATION_FIELDS + ("suppressionId", "recordedRevision", "active"),
            where="suppression",
        )
        _text(row["suppressionId"], "suppressionId")
        _relation(row, entity_index, works, kinds=RELATION_KINDS)
        _positive(row["recordedRevision"], "recordedRevision")
        if type(row["active"]) is not bool:
            raise ValueError("suppression active must be boolean")
        if any(
            entity_index[row[field]]["workId"] != row["workId"]
            for field in ("subjectId", "objectId")
        ):
            raise ValueError("suppression endpoint work mismatch")
        suppressions.append(row)
    _index(suppressions, "suppressionId")
    case["suppressions"] = suppressions
    return deepcopy(case)


def parse_cases(document: dict) -> tuple[dict, ...]:
    """Project and validate v1/v2 cases without reading answers or rule prose."""
    raw = _object(
        document,
        ("schemaVersion", "cases"),
        optional=("synthetic",),
        ignored=_DOCUMENT_METADATA,
        where="document",
    )
    version = raw["schemaVersion"]
    if type(version) is not int or version not in (1, 2):
        raise ValueError("schemaVersion must be integer 1 or 2")
    if "synthetic" in raw and raw["synthetic"] is not True:
        raise ValueError("synthetic must be boolean true")
    cases = tuple(
        parse_case(value, schema_version=version) for value in _array(raw["cases"], "cases")
    )
    if not cases:
        raise ValueError("cases must not be empty")
    _index(cases, "caseId")
    return cases
