"""Strict experimental input contract; no answer key or natural-language inference."""

from __future__ import annotations

import re
from copy import deepcopy
from hashlib import sha256


class InputError(ValueError):
    """Malformed data or a construct outside this experiment's input subset."""


def obj(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise InputError(f"{label}: exact fields required: {fields}")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", value):
        raise InputError("invalid identifier")


def integer(value, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < 0:
        raise InputError("expected nonnegative integer")


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
        raise InputError("expected nonempty bounded text")


def unique(rows, *, keyed=False):
    if not isinstance(rows, list):
        raise InputError("expected list")
    seen = set()
    for row in rows:
        key = row.get("id") if keyed and isinstance(row, dict) else row
        identifier(key)
        if key in seen:
            raise InputError("duplicate identifier")
        seen.add(key)
    return seen


def expression(value, vocabulary, depth=0):
    if depth > 12:
        raise InputError("class expression depth limit exceeded")
    if isinstance(value, str):
        if value not in vocabulary["classes"]:
            raise InputError("unknown class")
        return
    if not isinstance(value, dict):
        raise InputError("invalid class expression")
    operator = value.get("op")
    if not isinstance(operator, str):
        raise InputError("class-expression operator must be a string")
    if operator in {"and", "or"}:
        obj(value, "op args", "Boolean expression")
        if not isinstance(value["args"], list) or not 2 <= len(value["args"]) <= 8:
            raise InputError("Boolean expression requires 2..8 operands")
        for operand in value["args"]:
            expression(operand, vocabulary, depth + 1)
    elif operator == "not":
        obj(value, "op arg", "complement")
        expression(value["arg"], vocabulary, depth + 1)
    elif operator == "some":
        obj(value, "op property filler", "existential")
        if value["property"] not in vocabulary["properties"]:
            raise InputError("unknown property")
        expression(value["filler"], vocabulary, depth + 1)
    else:
        raise InputError("unsupported experiment class-expression construct")


def axiom(value, vocabulary, *, query=False):
    if not isinstance(value, dict):
        raise InputError("invalid axiom")
    prefix = "id " if query else ""
    kind = value.get("type")
    if not isinstance(kind, str):
        raise InputError("axiom/query type must be a string")
    if kind == "subclass":
        obj(value, prefix + "type sub super", "subclass")
        expression(value["sub"], vocabulary)
        expression(value["super"], vocabulary)
    elif kind in {"equivalent", "disjoint"} and not query:
        obj(value, "type left right", kind)
        expression(value["left"], vocabulary)
        expression(value["right"], vocabulary)
    elif kind == "classAssertion":
        obj(value, prefix + "type individual class", "class assertion")
        if value["individual"] not in vocabulary["individuals"]:
            raise InputError("unknown individual")
        expression(value["class"], vocabulary)
    elif kind == "propertyAssertion":
        fields = prefix + "type subject property object" + ("" if query else " negative")
        obj(value, fields, "property assertion")
        if value["property"] not in vocabulary["properties"]:
            raise InputError("unknown property")
        if any(value[key] not in vocabulary["individuals"] for key in ("subject", "object")):
            raise InputError("unknown property participant")
        if not query and type(value["negative"]) is not bool:
            raise InputError("negative must be a boolean")
    elif kind == "satisfiable" and query:
        obj(value, "id type class", "satisfiability query")
        expression(value["class"], vocabulary)
    else:
        raise InputError("unsupported experiment axiom/query construct")


def revision_interval(row):
    integer(row["recordedRevision"])
    integer(row["withdrawnRevision"], nullable=True)
    if row["withdrawnRevision"] is not None and row["withdrawnRevision"] <= row["recordedRevision"]:
        raise InputError("withdrawal must be after recorded revision")


def parse_case(case):
    obj(case, "caseId title description scope vocabulary sources statements queries", "case")
    identifier(case["caseId"])
    text(case["title"])
    text(case["description"])
    scope = case["scope"]
    obj(scope, "workId contextId cutoffEpisode storyTime snapshotRevision", "scope")
    for key in ("workId", "contextId"):
        identifier(scope[key])
    for key in ("cutoffEpisode", "storyTime", "snapshotRevision"):
        integer(scope[key])
    vocabulary = case["vocabulary"]
    obj(vocabulary, "classes individuals properties", "vocabulary")
    names = [unique(vocabulary[key]) for key in ("classes", "individuals", "properties")]
    if sum(map(len, names)) > 256 or sum(map(len, names)) != len(set().union(*names)):
        raise InputError("vocabulary limit exceeded or names reused between entity kinds")
    source_ids = unique(case["sources"], keyed=True)
    unique(case["statements"], keyed=True)
    unique(case["queries"], keyed=True)
    if len(source_ids) > 128 or len(case["statements"]) > 64 or len(case["queries"]) > 32:
        raise InputError("experiment input size limit exceeded")
    for source in case["sources"]:
        obj(
            source,
            "id workId contextId episodeNo recordedRevision withdrawnRevision text sha256",
            "source",
        )
        for key in ("workId", "contextId"):
            identifier(source[key])
        integer(source["episodeNo"])
        revision_interval(source)
        if not isinstance(source["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", source["sha256"]
        ):
            raise InputError("invalid source hash")
        if source["text"] is not None:
            text(source["text"])
            if sha256(source["text"].encode()).hexdigest() != source["sha256"]:
                raise InputError("source hash mismatch")
    for statement in case["statements"]:
        obj(
            statement,
            "id workId contextId modality confirmed validFrom validTo recordedRevision withdrawnRevision sourceIds axiom",
            "statement",
        )
        for key in ("workId", "contextId"):
            identifier(statement[key])
        if not isinstance(statement["modality"], str) or statement["modality"] not in {
            "STRICT",
            "DEFAULT",
            "NORM",
            "RUMOR",
        }:
            raise InputError("unsupported statement modality")
        if type(statement["confirmed"]) is not bool:
            raise InputError("confirmed must be a boolean")
        integer(statement["validFrom"])
        integer(statement["validTo"], nullable=True)
        if statement["validTo"] is not None and statement["validTo"] <= statement["validFrom"]:
            raise InputError("invalid story validity interval")
        revision_interval(statement)
        refs = unique(statement["sourceIds"])
        if not refs or not refs <= source_ids:
            raise InputError("statement must reference known sources")
        axiom(statement["axiom"], vocabulary)
    for query in case["queries"]:
        axiom(query, vocabulary, query=True)
    return deepcopy(case)


def parse_document(document):
    obj(document, "schemaVersion cases", "document")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise InputError("unsupported schema version")
    if not isinstance(document["cases"], list) or not 1 <= len(document["cases"]) <= 128:
        raise InputError("document requires 1..128 cases")
    cases = [parse_case(case) for case in document["cases"]]
    ids = [case["caseId"] for case in cases]
    if len(set(ids)) != len(ids):
        raise InputError("duplicate case ID")
    if len({key.casefold() for key in ids}) != len(ids):
        raise InputError("case IDs collide on case-insensitive filesystems")
    return cases
