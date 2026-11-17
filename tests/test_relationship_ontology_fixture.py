"""GH438 handwritten ontology fixture integrity, not an inference engine test.

Only stdlib and pytest are used. No app imports, model, DB, or network calls.
Run with --noconftest to bypass the repository's unrelated session setup.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "relationship_ontology_cases.json"
RELATION_KEYS = {
    "workId",
    "subjectId",
    "objectId",
    "kind",
    "subtype",
    "temporalContextId",
    "generationGap",
}
KINDS = {"PARENT_OF", "CHILD_OF", "GRANDPARENT_OF", "ANCESTOR_OF", "TRUST"}
REASONS = {
    "MISSING_PREMISE",
    "UNCERTAIN_PREMISE",
    "NON_TRANSITIVE_KIND",
    "NON_SYMMETRIC_KIND",
    "NEGATED_PREMISE",
    "HYPOTHETICAL_PREMISE",
    "FUTURE_PREMISE",
    "IDENTITY_MISMATCH",
    "UNRESOLVED_PREMISE",
    "CROSS_WORK",
    "SUBTYPE_MISMATCH",
    "CONTEXT_MISMATCH",
    "UNKNOWN_TIME",
    "WITHDRAWN_PREMISE",
    "SELF_RELATION",
    "PARENT_CYCLE",
    "GENERATION_CONFLICT",
    "CONFLICTING_PREMISE",
    "UNSUPPORTED_HISTORY",
}
WITHHELD_REASONS = {
    "UNCERTAIN_ASSERTION",
    "NEGATED_ASSERTION",
    "HYPOTHETICAL_ASSERTION",
    "FUTURE_EVIDENCE",
    "UNRESOLVED_IDENTITY",
    "CROSS_WORK",
    "DIFFERENT_CONTEXT",
    "UNKNOWN_TIME",
    "WITHDRAWN_FACT",
    "SELF_RELATION",
    "PARENT_CYCLE",
    "GENERATION_CONFLICT",
    "CONFLICTING_ASSERTION",
    "UNSUPPORTED_HISTORY",
}
VALIDATION_ISSUES = {"CROSS_WORK", "SELF_RELATION", "PARENT_CYCLE", "GENERATION_CONFLICT"}
RULES = {
    "R_PARENT_INVERSE": (1, "CHILD_OF", "SEMANTIC_INVERSE"),
    "R_BIOLOGICAL_GRANDPARENT": (2, "GRANDPARENT_OF", "NEW_RELATION"),
}


def _keys(value, keys):
    assert isinstance(value, dict), "expected object"
    assert set(value) == keys, f"unexpected/missing keys: {set(value) ^ keys}"


def _text(value):
    assert isinstance(value, str) and value.strip(), "expected nonempty string"


def _positive_integer(value):
    assert type(value) is int and value > 0, "expected positive integer"


def _refs(values, known, *, nonempty=True):
    assert isinstance(values, list), "expected reference list"
    assert not nonempty or values, "empty references"
    assert all(isinstance(value, str) for value in values), "reference must be a string"
    assert len(values) == len(set(values)), "duplicate reference"
    assert set(values) <= set(known), f"unknown reference: {set(values) - set(known)}"


def _index(items, key):
    assert isinstance(items, list), "expected object list"
    for item in items:
        assert isinstance(item, dict) and key in item, f"missing {key}"
        _text(item[key])
    result = {item[key]: item for item in items}
    assert len(result) == len(items), f"duplicate {key}"
    return result


def _relation_key(value):
    return tuple(value[key] for key in sorted(RELATION_KEYS))


def _relation(value, entities):
    assert RELATION_KEYS <= set(value), "missing relation field"
    _text(value["workId"])
    assert value["workId"] in {entity["workId"] for entity in entities.values()}
    assert value["subjectId"] in entities and value["objectId"] in entities, "unknown endpoint"
    assert value["kind"] in KINDS, "unknown relationship kind"
    assert value["subtype"] in {"BIOLOGICAL", "ADOPTIVE", "NONE"}, "unknown subtype"
    _text(value["temporalContextId"])
    if value["kind"] in {"GRANDPARENT_OF", "ANCESTOR_OF"}:
        _positive_integer(value["generationGap"])
    else:
        assert value["generationGap"] is None, "generationGap belongs to ancestry only"
    assert (value["kind"] == "TRUST") == (value["subtype"] == "NONE")


def _validate_case(case):
    _keys(
        case,
        {
            "caseId",
            "title",
            "purpose",
            "snapshotRevision",
            "snapshotThroughEpisode",
            "query",
            "entities",
            "sources",
            "assertions",
            "expected",
        },
    )
    for field in ("caseId", "title", "purpose"):
        _text(case[field])
    _positive_integer(case["snapshotRevision"])
    _positive_integer(case["snapshotThroughEpisode"])
    query = case["query"]
    _keys(query, {"workId", "cutoffEpisode", "temporalContextId", "inputMode"})
    _text(query["workId"])
    _text(query["temporalContextId"])
    _positive_integer(query["cutoffEpisode"])
    assert query["cutoffEpisode"] <= case["snapshotThroughEpisode"]
    assert query["inputMode"] in {"EPISODE_SNAPSHOT", "CURRENT_PROFILE"}

    entities = _index(case["entities"], "entityId")
    assert entities
    works = {entity["workId"] for entity in entities.values()}
    assert query["workId"] in works
    for entity in entities.values():
        _keys(entity, {"entityId", "name", "workId", "identityStatus"})
        _text(entity["name"])
        _text(entity["workId"])
        assert entity["identityStatus"] in {"RESOLVED", "UNRESOLVED"}

    sources = _index(case["sources"], "sourceId")
    for source in sources.values():
        _keys(
            source,
            {
                "sourceId",
                "workId",
                "episodeNo",
                "supportStatus",
                "quoteAvailability",
                "text",
                "quote",
            },
        )
        assert source["workId"] in works, "unknown source work"
        _positive_integer(source["episodeNo"])
        assert source["episodeNo"] <= case["snapshotThroughEpisode"]
        assert source["supportStatus"] in {"ACTIVE", "WITHDRAWN"}
        assert source["quoteAvailability"] in {"AVAILABLE", "PURGED"}
        if source["quoteAvailability"] == "AVAILABLE":
            _text(source["text"])
            _text(source["quote"])
            assert source["quote"] in source["text"], "quote absent from source text"
        else:
            assert source["text"] is None and source["quote"] is None, "purged text retained"

    assertions = _index(case["assertions"], "assertionId")
    for assertion in assertions.values():
        _keys(
            assertion,
            RELATION_KEYS
            | {
                "assertionId",
                "assertionState",
                "factValidity",
                "nativeTime",
                "sourceIds",
                "validationIssues",
            },
        )
        _relation(assertion, entities)
        assert assertion["kind"] in {"PARENT_OF", "ANCESTOR_OF", "TRUST"}
        assert assertion["assertionState"] in {
            "ASSERTED",
            "UNCERTAIN",
            "NEGATED",
            "HYPOTHETICAL",
            "CONFLICTING",
        }
        assert assertion["factValidity"] in {"VALID", "REVIEW_REQUIRED", "WITHDRAWN"}
        _keys(assertion["nativeTime"], {"domain", "scope"})
        domain, scope = assertion["nativeTime"]["domain"], assertion["nativeTime"]["scope"]
        assert domain in {"FAMILY", "EPISODE_RELATIONSHIP"}
        assert scope in (
            {"CURRENT", "HISTORICAL", "UNKNOWN"}
            if domain == "FAMILY"
            else {"PRESENT", "PAST", "HYPOTHETICAL", "UNKNOWN"}
        )
        assert (domain == "EPISODE_RELATIONSHIP") == (assertion["kind"] == "TRUST")
        _refs(assertion["sourceIds"], sources)
        _refs(assertion["validationIssues"], VALIDATION_ISSUES, nonempty=False)

    expected = case["expected"]
    _keys(
        expected,
        {
            "outcome",
            "outcomeReason",
            "directRelations",
            "derivedRelations",
            "withheldAssertions",
            "forbiddenRelations",
        },
    )
    for field in (
        "directRelations",
        "derivedRelations",
        "withheldAssertions",
        "forbiddenRelations",
    ):
        assert isinstance(expected[field], list), f"{field} must be a list"
    assert expected["outcome"] in {"READY", "REJECTED", "UNSUPPORTED"}
    if expected["outcome"] == "READY":
        assert expected["outcomeReason"] is None
    elif expected["outcome"] == "REJECTED":
        assert expected["outcomeReason"] in {"CROSS_WORK", "SELF_RELATION", "PARENT_CYCLE"}
    else:
        assert expected["outcomeReason"] == "UNSUPPORTED_HISTORY"
    if expected["outcome"] != "READY":
        assert not expected["directRelations"] and not expected["derivedRelations"]
    unsupported_history = (
        query["inputMode"] == "CURRENT_PROFILE"
        and query["cutoffEpisode"] < case["snapshotThroughEpisode"]
    )
    assert (expected["outcome"] == "UNSUPPORTED") == unsupported_history, (
        "query capability mismatch"
    )

    accepted_ids = set()
    direct_keys = set()
    for direct in expected["directRelations"]:
        _keys(direct, RELATION_KEYS | {"supportAssertionIds", "sourceIds"})
        _relation(direct, entities)
        assert direct["workId"] == query["workId"], "direct output outside query work"
        assert direct["temporalContextId"] == query["temporalContextId"], (
            "direct output outside query context"
        )
        for endpoint in ("subjectId", "objectId"):
            entity = entities[direct[endpoint]]
            assert entity["workId"] == direct["workId"], "direct endpoint belongs to another work"
            assert entity["identityStatus"] == "RESOLVED", "unresolved direct endpoint"
        key = _relation_key(direct)
        assert key not in direct_keys, "duplicate direct relation"
        direct_keys.add(key)
        _refs(direct["supportAssertionIds"], assertions)
        _refs(direct["sourceIds"], sources)
        assert not accepted_ids.intersection(direct["supportAssertionIds"]), (
            "reused direct assertion"
        )
        accepted_ids.update(direct["supportAssertionIds"])
        declared_sources = set()
        for assertion_id in direct["supportAssertionIds"]:
            assertion = assertions[assertion_id]
            assert _relation_key(assertion) == key, "direct relation differs from declared support"
            assert (
                assertion["assertionState"] == "ASSERTED" and assertion["factValidity"] == "VALID"
            )
            assert not assertion["validationIssues"]
            declared_sources.update(assertion["sourceIds"])
        assert set(direct["sourceIds"]) == declared_sources, "direct source references differ"
        for source_id in declared_sources:
            assert sources[source_id]["workId"] == direct["workId"], (
                "source belongs to another work"
            )
            assert sources[source_id]["supportStatus"] == "ACTIVE", "withdrawn source used"
            assert sources[source_id]["episodeNo"] <= query["cutoffEpisode"], "future source used"

    derived_keys = set()
    for derived in expected["derivedRelations"]:
        _keys(derived, RELATION_KEYS | {"derivationType", "proofs"})
        _relation(derived, entities)
        key = _relation_key(derived)
        assert key not in direct_keys | derived_keys, "duplicate output relation"
        derived_keys.add(key)
        assert isinstance(derived["proofs"], list) and derived["proofs"], "proof required"
        seen_proofs = set()
        for proof in derived["proofs"]:
            _keys(proof, {"ruleId", "premiseAssertionIds", "sourceIds"})
            assert proof["ruleId"] in RULES, "unknown rule"
            arity, output_kind, derivation_type = RULES[proof["ruleId"]]
            assert derived["kind"] == output_kind and derived["derivationType"] == derivation_type
            _refs(proof["premiseAssertionIds"], accepted_ids)
            assert len(proof["premiseAssertionIds"]) == arity, "wrong declared premise count"
            _refs(proof["sourceIds"], sources)
            premise_sources = {
                source_id
                for assertion_id in proof["premiseAssertionIds"]
                for source_id in assertions[assertion_id]["sourceIds"]
            }
            assert set(proof["sourceIds"]) == premise_sources, "proof sources differ from premises"
            premise_entities = {
                assertions[assertion_id][endpoint]
                for assertion_id in proof["premiseAssertionIds"]
                for endpoint in ("subjectId", "objectId")
            }
            assert {derived["subjectId"], derived["objectId"]} <= premise_entities, (
                "unrelated proof endpoint"
            )
            # Only check this declared proof's fixed positions. Do not discover
            # premises, traverse the graph, or compute an expected output set.
            premises = [assertions[ref] for ref in proof["premiseAssertionIds"]]
            assert all(premise["kind"] == "PARENT_OF" for premise in premises)
            assert all(premise["workId"] == derived["workId"] for premise in premises), (
                "proof work differs"
            )
            assert all(
                premise["temporalContextId"] == derived["temporalContextId"] for premise in premises
            ), "proof context differs"
            if proof["ruleId"] == "R_PARENT_INVERSE":
                parent = premises[0]
                assert (
                    derived["subjectId"] == parent["objectId"]
                    and derived["objectId"] == parent["subjectId"]
                ), "inverse endpoints differ"
                assert derived["subtype"] == parent["subtype"], "inverse subtype differs"
                assert derived["generationGap"] is None
            else:
                first, second = premises
                assert first["objectId"] == second["subjectId"], (
                    "declared premises are disconnected"
                )
                assert (
                    derived["subjectId"] == first["subjectId"]
                    and derived["objectId"] == second["objectId"]
                ), "grandparent endpoints differ"
                assert (
                    first["subtype"] == second["subtype"] == derived["subtype"] == "BIOLOGICAL"
                ), "grandparent subtype differs"
                assert derived["generationGap"] == 2, "grandparent generation gap differs"
            proof_key = (
                proof["ruleId"],
                frozenset(proof["premiseAssertionIds"]),
                frozenset(proof["sourceIds"]),
            )
            assert proof_key not in seen_proofs, "duplicate proof alternative"
            seen_proofs.add(proof_key)

    withheld_ids = set()
    for withheld in expected["withheldAssertions"]:
        _keys(withheld, {"assertionId", "reason"})
        assert withheld["assertionId"] in assertions, "unknown withheld assertion"
        assert withheld["assertionId"] not in withheld_ids, "duplicate withheld assertion"
        withheld_ids.add(withheld["assertionId"])
        assert withheld["reason"] in WITHHELD_REASONS
    assert not accepted_ids & withheld_ids, "assertion both accepted and withheld"
    assert accepted_ids | withheld_ids == set(assertions), "unaccounted assertion"

    forbidden_keys = set()
    for forbidden in expected["forbiddenRelations"]:
        _keys(forbidden, RELATION_KEYS | {"reason"})
        _relation(forbidden, entities)
        assert forbidden["reason"] in REASONS
        key = _relation_key(forbidden)
        assert key not in forbidden_keys, "duplicate forbidden relation"
        assert key not in direct_keys | derived_keys, "expected/forbidden output conflict"
        forbidden_keys.add(key)


def validate_fixture(data):
    """Check a declared oracle's structure and links; never derive an answer."""
    _keys(
        data,
        {"schemaVersion", "synthetic", "status", "oracle", "outputSemantics", "rules", "cases"},
    )
    assert type(data["schemaVersion"]) is int and data["schemaVersion"] == 1
    assert data["synthetic"] is True
    assert data["status"] == "PROPOSED_EXPERIMENT"
    assert data["oracle"] == "HANDWRITTEN_NOT_ENGINE_GENERATED"
    assert data["outputSemantics"] == "EXACT_SETS"
    rules = _index(data["rules"], "ruleId")
    assert set(rules) == set(RULES)
    for rule_id, rule in rules.items():
        _keys(rule, {"ruleId", "premiseCount", "outputKind", "derivationType"})
        assert (rule["premiseCount"], rule["outputKind"], rule["derivationType"]) == RULES[rule_id]
    cases = _index(data["cases"], "caseId")
    assert cases
    for case in cases.values():
        try:
            _validate_case(case)
        except AssertionError as error:
            raise AssertionError(f"{case['caseId']}: {error}") from error


@pytest.fixture(scope="module")
def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_handwritten_fixture_integrity(fixture):
    validate_fixture(fixture)


def test_documented_case_inventory_is_complete(fixture):
    ids = [case["caseId"] for case in fixture["cases"]]
    assert len(ids) == 24
    assert [case_id[:2] for case_id in ids] == [f"{number:02d}" for number in range(1, 25)]


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate_entity",
        "unknown_source",
        "missing_quote",
        "unknown_rule",
        "wrong_proof_source",
        "unaccepted_premise",
        "duplicate_proof",
        "output_conflict",
        "unaccounted_assertion",
        "purged_text_retained",
        "unknown_kind",
        "extra_field",
        "unresolved_output_endpoint",
        "foreign_output_endpoint",
        "foreign_output_source",
        "foreign_query_work",
        "query_context_changed",
        "inverse_direction",
        "inverse_subtype",
        "inverse_context",
        "grandparent_direction",
        "grandparent_subtype",
        "grandparent_context",
        "grandparent_gap",
        "reversed_premise_roles",
        "historical_query_ready",
        "historical_query_rejected",
    ],
)
def test_integrity_checks_reject_damaged_fixture(fixture, defect):
    damaged = deepcopy(fixture)
    case = damaged["cases"][2]
    if defect == "duplicate_entity":
        case["entities"].append(deepcopy(case["entities"][0]))
    elif defect == "unknown_source":
        case["assertions"][0]["sourceIds"] = ["missing"]
    elif defect == "missing_quote":
        case["sources"][0]["quote"] = "원문에 없는 문장"
    elif defect == "unknown_rule":
        case["expected"]["derivedRelations"][-1]["proofs"][0]["ruleId"] = "UNKNOWN_RULE"
    elif defect == "wrong_proof_source":
        case["expected"]["derivedRelations"][-1]["proofs"][0]["sourceIds"] = ["s1", "s2"]
    elif defect == "unaccepted_premise":
        case["expected"]["derivedRelations"][-1]["proofs"][0]["premiseAssertionIds"] = ["a1", "a2"]
    elif defect == "duplicate_proof":
        proofs = case["expected"]["derivedRelations"][-1]["proofs"]
        proofs.append(deepcopy(proofs[0]))
    elif defect == "output_conflict":
        forbidden = {key: case["expected"]["derivedRelations"][-1][key] for key in RELATION_KEYS}
        case["expected"]["forbiddenRelations"].append(dict(forbidden, reason="MISSING_PREMISE"))
    elif defect == "unaccounted_assertion":
        case["expected"]["withheldAssertions"] = []
    elif defect == "purged_text_retained":
        damaged["cases"][17]["sources"][0]["text"] = "파기됐는데 남은 원문"
    elif defect == "unknown_kind":
        case["assertions"][0]["kind"] = "MAGIC_RELATION"
    elif defect == "extra_field":
        case["query"]["unexpected"] = True
    elif defect == "unresolved_output_endpoint":
        case["entities"][0]["identityStatus"] = "UNRESOLVED"
    elif defect == "foreign_output_endpoint":
        case["entities"][0]["workId"] = "work-b"
    elif defect in {"foreign_output_source", "foreign_query_work"}:
        case["entities"].append(
            dict(entityId="other", name="다른 인물", workId="work-b", identityStatus="RESOLVED")
        )
        if defect == "foreign_output_source":
            case["sources"][0]["workId"] = "work-b"
        else:
            case["query"]["workId"] = "work-b"
    elif defect == "query_context_changed":
        case["query"]["temporalContextId"] = "another-context"
    elif defect in {"inverse_direction", "grandparent_direction"}:
        relation = case["expected"]["derivedRelations"][0 if defect.startswith("inverse") else -1]
        relation["subjectId"], relation["objectId"] = relation["objectId"], relation["subjectId"]
    elif defect in {"inverse_subtype", "grandparent_subtype"}:
        case["expected"]["derivedRelations"][0 if defect.startswith("inverse") else -1][
            "subtype"
        ] = "ADOPTIVE"
    elif defect in {"inverse_context", "grandparent_context"}:
        case["expected"]["derivedRelations"][0 if defect.startswith("inverse") else -1][
            "temporalContextId"
        ] = "another-context"
    elif defect == "grandparent_gap":
        case["expected"]["derivedRelations"][-1]["generationGap"] = 99
    elif defect == "reversed_premise_roles":
        case["expected"]["derivedRelations"][-1]["proofs"][0]["premiseAssertionIds"].reverse()
    elif defect == "historical_query_ready":
        damaged["cases"][22]["expected"].update(outcome="READY", outcomeReason=None)
    else:
        damaged["cases"][22]["expected"]["outcome"] = "REJECTED"
    with pytest.raises(AssertionError):
        validate_fixture(damaged)
