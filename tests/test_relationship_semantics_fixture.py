"""GH438 consumer examples: handwritten data integrity, no product execution."""

from copy import deepcopy
import json
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "relationship_semantics_cases.json"
SLOT_KEYS = {"workId", "sourceNodeRef", "targetNodeRef", "axis", "kind", "direction"}
KINDS = {
    "FRIENDSHIP",
    "COMPANIONSHIP",
    "COOPERATION",
    "MENTORSHIP",
    "ROMANCE",
    "HOSTILITY",
    "TRUST",
    "DISTRUST",
    "AFFECTION",
    "ACQUAINTANCE",
    "OTHER",
}
CONSUMERS = {"RELATIONSHIP_GRAPH", "SEARCH", "REVIEW"}


def keys(value, expected):
    assert isinstance(value, dict) and set(value) == expected, "object fields differ"


def text(value):
    assert isinstance(value, str) and value.strip(), "nonempty text required"


def positive(value):
    assert type(value) is int and value > 0, "positive integer required"


def index(items, field):
    assert isinstance(items, list), "list required"
    for item in items:
        assert isinstance(item, dict) and field in item
        text(item[field])
    indexed = {item[field]: item for item in items}
    assert len(indexed) == len(items), "duplicate identifier"
    return indexed


def refs(items, known, nonempty=False):
    assert isinstance(items, list) and all(isinstance(item, str) for item in items)
    assert not nonempty or items, "empty reference list"
    assert len(items) == len(set(items)) and set(items) <= set(known), "invalid references"


def slot_key(slot):
    endpoints = (slot["sourceNodeRef"], slot["targetNodeRef"])
    if slot["direction"] != "DIRECTED":
        endpoints = tuple(sorted(endpoints))
    return (slot["workId"], *endpoints, slot["axis"], slot["kind"], slot["direction"])


def check_slot(slot, nodes, work):
    keys(slot, SLOT_KEYS)
    assert slot["workId"] == work
    assert slot["sourceNodeRef"] in nodes and slot["targetNodeRef"] in nodes
    assert slot["sourceNodeRef"] != slot["targetNodeRef"]
    assert slot["axis"] in {"SOCIAL", "ATTITUDE", "INTERACTION"}
    assert slot["kind"] in KINDS
    assert slot["direction"] in {"DIRECTED", "MUTUAL", "UNDIRECTED"}
    # axis/kind/direction are independent fields, not a global kind lookup.


def check_claim_endpoints(claim, slot):
    """Claims in this fixture have manually normalized semantic endpoints."""
    assert len(claim["subjectCandidateRefs"]) == len(claim["objectCandidateRefs"]) == 1
    endpoints = (claim["subjectCandidateRefs"][0], claim["objectCandidateRefs"][0])
    target = (slot["sourceNodeRef"], slot["targetNodeRef"])
    if slot["direction"] == "DIRECTED":
        assert endpoints == target, "directed claim endpoints differ"
    else:
        assert set(endpoints) == set(target), "claim endpoints differ"


def check_claim_for_fact(claim, slot):
    check_claim_endpoints(claim, slot)
    assert claim["curationStatus"] == "CONFIRMED" and claim["statementMode"] == "ASSERTED"
    assert claim["temporalScope"] == "PRESENT", "only PRESENT supports confirmed slots"


def validate_case(case):
    keys(
        case,
        {
            "caseId",
            "title",
            "productQuestionId",
            "purpose",
            "workId",
            "snapshotThroughEpisodeNo",
            "snapshotRevision",
            "nodes",
            "sources",
            "claims",
            "facts",
            "suppressions",
            "queries",
        },
    )
    for field in ("caseId", "title", "purpose", "workId"):
        text(case[field])
    assert case["productQuestionId"] in {"P1", "P2", "P3", "P4", "P5"}
    positive(case["snapshotThroughEpisodeNo"])
    positive(case["snapshotRevision"])
    nodes, sources = index(case["nodes"], "nodeRef"), index(case["sources"], "sourceRef")
    for node in nodes.values():
        keys(node, {"nodeRef", "name", "workId"})
        text(node["name"])
        assert node["workId"] == case["workId"]
    for source in sources.values():
        keys(source, {"sourceRef", "workId", "episodeNo", "text", "quote"})
        assert source["workId"] == case["workId"]
        positive(source["episodeNo"])
        assert source["episodeNo"] <= case["snapshotThroughEpisodeNo"]
        text(source["text"])
        text(source["quote"])
        assert source["quote"] in source["text"], "quote not in source"
    claims = index(case["claims"], "claimRef")
    for claim in claims.values():
        keys(
            claim,
            {
                "claimRef",
                "sourceRef",
                "subjectCandidateRefs",
                "objectCandidateRefs",
                "statementMode",
                "curationStatus",
                "temporalScope",
                "narrativeContextId",
            },
        )
        assert claim["sourceRef"] in sources
        refs(claim["subjectCandidateRefs"], nodes, nonempty=True)
        refs(claim["objectCandidateRefs"], nodes, nonempty=True)
        assert claim["statementMode"] in {"ASSERTED", "UNCERTAIN", "HYPOTHETICAL"}
        assert claim["curationStatus"] in {"CONFIRMED", "HOLD", "RETRACTED"}
        assert claim["temporalScope"] in {"PRESENT", "PAST", "HYPOTHETICAL", "UNKNOWN"}
        text(claim["narrativeContextId"])
    facts = index(case["facts"], "factRef")
    seen_slots = set()
    for fact in facts.values():
        keys(
            fact,
            {
                "factRef",
                "slot",
                "status",
                "factValidity",
                "knownFromEpisodeNo",
                "lastChangedEpisodeNo",
                "supportClaimRefs",
                "endClaimRefs",
                "correctionSourceRefs",
            },
        )
        check_slot(fact["slot"], nodes, case["workId"])
        key = slot_key(fact["slot"])
        assert key not in seen_slots, "duplicate canonical slot"
        seen_slots.add(key)
        assert fact["status"] in {"ACTIVE", "ENDED"}
        assert fact["factValidity"] in {"VALID", "RETRACTED"}
        positive(fact["knownFromEpisodeNo"])
        positive(fact["lastChangedEpisodeNo"])
        assert (
            fact["knownFromEpisodeNo"]
            <= fact["lastChangedEpisodeNo"]
            <= case["snapshotThroughEpisodeNo"]
        )
        refs(fact["supportClaimRefs"], claims, nonempty=True)
        refs(fact["endClaimRefs"], claims)
        refs(fact["correctionSourceRefs"], sources)
        assert not set(fact["supportClaimRefs"]) & set(fact["endClaimRefs"])
        assert (fact["status"] == "ENDED") == bool(fact["endClaimRefs"]), (
            "END needs distinct end evidence"
        )
        for ref in fact["supportClaimRefs"] + fact["endClaimRefs"]:
            check_claim_endpoints(claims[ref], fact["slot"])
            assert sources[claims[ref]["sourceRef"]]["episodeNo"] <= fact["lastChangedEpisodeNo"], (
                "claim is newer than snapshot metadata"
            )
        for ref in fact["correctionSourceRefs"]:
            assert sources[ref]["episodeNo"] <= fact["lastChangedEpisodeNo"], (
                "correction is newer than snapshot metadata"
            )
        if fact["factValidity"] == "VALID":
            assert not fact["correctionSourceRefs"]
            for ref in fact["supportClaimRefs"] + fact["endClaimRefs"]:
                check_claim_for_fact(claims[ref], fact["slot"])
        else:
            assert fact["correctionSourceRefs"] and not fact["endClaimRefs"]
            assert all(
                claims[ref]["curationStatus"] == "RETRACTED" for ref in fact["supportClaimRefs"]
            )
    suppressions = index(case["suppressions"], "suppressionRef")
    suppressed_slots = set()
    for suppression in suppressions.values():
        keys(suppression, {"suppressionRef", "slot", "reason", "appliesTo", "recordedRevision"})
        check_slot(suppression["slot"], nodes, case["workId"])
        assert suppression["reason"] == "USER_DELETE" and suppression["appliesTo"] == "ALL_EPISODES"
        positive(suppression["recordedRevision"])
        assert suppression["recordedRevision"] <= case["snapshotRevision"]
        suppressed_slots.add(slot_key(suppression["slot"]))
    queries = index(case["queries"], "queryRef")
    assert queries
    for query in queries.values():
        keys(query, {"queryRef", "mode", "cutoffEpisodeNo", "storyContextId", "expected"})
        assert query["mode"] in {"READER_KNOWLEDGE", "STORY_TIME"}
        positive(query["cutoffEpisodeNo"])
        assert query["cutoffEpisodeNo"] <= case["snapshotThroughEpisodeNo"]
        expected = query["expected"]
        keys(
            expected,
            {
                "status",
                "reason",
                "factRefs",
                "excludedFacts",
                "heldClaimRefs",
                "forbiddenSlots",
                "consumerExamples",
            },
        )
        unsupported = query["mode"] == "STORY_TIME"
        assert expected["status"] == ("UNSUPPORTED" if unsupported else "READY")
        assert expected["reason"] == (
            "STORY_TIME_RECONSTRUCTION_NOT_SUPPORTED" if unsupported else None
        )
        if unsupported:
            text(query["storyContextId"])
            assert not expected["factRefs"] and not expected["heldClaimRefs"]
        else:
            assert query["storyContextId"] is None
        refs(expected["factRefs"], facts)
        refs(expected["heldClaimRefs"], claims)
        for ref in expected["heldClaimRefs"]:
            assert claims[ref]["curationStatus"] == "HOLD"
            assert sources[claims[ref]["sourceRef"]]["episodeNo"] <= query["cutoffEpisodeNo"]
        for ref in expected["factRefs"]:
            fact = facts[ref]
            assert fact["factValidity"] == "VALID"
            assert fact["lastChangedEpisodeNo"] <= query["cutoffEpisodeNo"], (
                "snapshot state is newer than query"
            )
            assert slot_key(fact["slot"]) not in suppressed_slots, "suppressed slot revived"
            for claim_ref in fact["supportClaimRefs"] + fact["endClaimRefs"]:
                assert (
                    sources[claims[claim_ref]["sourceRef"]]["episodeNo"] <= query["cutoffEpisodeNo"]
                ), "future evidence"
        excluded = index(expected["excludedFacts"], "factRef")
        assert not set(excluded) & set(expected["factRefs"])
        assert set(excluded) | set(expected["factRefs"]) == set(facts), "unclassified fact"
        for ref, exclusion in excluded.items():
            keys(exclusion, {"factRef", "reason"})
            fact = facts[ref]
            reason = exclusion["reason"]
            assert reason in {
                "FUTURE_KNOWLEDGE",
                "RETRACTED_FACT",
                "USER_SUPPRESSED",
                "UNSUPPORTED_QUERY",
            }
            if unsupported:
                assert reason == "UNSUPPORTED_QUERY"
            elif reason == "FUTURE_KNOWLEDGE":
                assert fact["knownFromEpisodeNo"] > query["cutoffEpisodeNo"]
            elif reason == "RETRACTED_FACT":
                assert fact["factValidity"] == "RETRACTED"
            elif reason == "USER_SUPPRESSED":
                assert slot_key(fact["slot"]) in suppressed_slots
            else:
                raise AssertionError("unsupported reason on a supported query")
        assert isinstance(expected["forbiddenSlots"], list)
        for forbidden in expected["forbiddenSlots"]:
            keys(forbidden, {"slot", "reason"})
            check_slot(forbidden["slot"], nodes, case["workId"])
            text(forbidden["reason"])
            assert all(
                slot_key(forbidden["slot"]) != slot_key(facts[ref]["slot"])
                for ref in expected["factRefs"]
            )
        consumers = index(expected["consumerExamples"], "consumer")
        assert set(consumers) == CONSUMERS
        for consumer in consumers.values():
            keys(consumer, {"consumer", "question", "expectedText", "factRefs"})
            text(consumer["question"])
            text(consumer["expectedText"])
            refs(consumer["factRefs"], facts)
            assert set(consumer["factRefs"]) == set(expected["factRefs"]), "consumer facts drifted"


def validate_fixture(data):
    keys(
        data,
        {
            "schemaVersion",
            "synthetic",
            "status",
            "oracle",
            "outputSemantics",
            "productIntegration",
            "cases",
        },
    )
    assert type(data["schemaVersion"]) is int and data["schemaVersion"] == 1
    assert data["synthetic"] is True and data["productIntegration"] is False
    assert data["status"] == "PROPOSED_CONSUMER_EXAMPLES"
    assert data["oracle"] == "HANDWRITTEN_NOT_ENGINE_GENERATED"
    assert data["outputSemantics"] == "EXACT_FACT_REF_SETS"
    for case in index(data["cases"], "caseId").values():
        try:
            validate_case(case)
        except AssertionError as error:
            raise AssertionError(f"{case['caseId']}: {error}") from error


@pytest.fixture(scope="module")
def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_semantics_fixture_integrity(fixture):
    validate_fixture(fixture)


def test_product_question_inventory_and_cooperation_slots(fixture):
    cases = fixture["cases"]
    assert len(cases) == 10 and sum(len(case["queries"]) for case in cases) == 11
    assert {case["productQuestionId"] for case in cases} == {"P1", "P2", "P3", "P4", "P5"}
    assert {
        (fact["slot"]["axis"], fact["slot"]["kind"], fact["slot"]["direction"])
        for fact in cases[1]["facts"]
    } == {
        ("INTERACTION", "COOPERATION", "DIRECTED"),
        ("SOCIAL", "COOPERATION", "MUTUAL"),
    }


@pytest.mark.parametrize(
    "defect",
    [
        "quote",
        "source_ref",
        "consumer_ref",
        "consumer_drift",
        "uncertain_confirmed",
        "ambiguous_confirmed",
        "future_fact",
        "collapsed_slots",
        "end_evidence_missing",
        "correction_as_end",
        "suppression_ignored",
        "suppression_as_retraction",
        "unsupported_as_ready",
        "direction_changed",
        "future_support",
        "unknown_axis",
        "retracted_endpoints_changed",
        "end_metadata_too_early",
        "correction_metadata_too_early",
    ],
)
def test_damaged_semantics_fixture_is_rejected(fixture, defect):
    data = deepcopy(fixture)
    first = data["cases"][0]
    if defect == "quote":
        first["sources"][0]["quote"] = "없는 원문"
    elif defect == "source_ref":
        first["claims"][0]["sourceRef"] = "missing"
    elif defect == "consumer_ref":
        first["queries"][0]["expected"]["consumerExamples"][0]["factRefs"] = ["missing"]
    elif defect == "consumer_drift":
        first["queries"][0]["expected"]["consumerExamples"][1]["factRefs"] = ["f1"]
    elif defect == "uncertain_confirmed":
        first["claims"][0]["statementMode"] = "UNCERTAIN"
    elif defect == "ambiguous_confirmed":
        first["claims"][0]["subjectCandidateRefs"].append("harin")
    elif defect in {"future_fact", "suppression_ignored"}:
        expected = data["cases"][3 if defect == "future_fact" else 8]["queries"][0]["expected"]
        expected["factRefs"] = ["f3" if defect == "future_fact" else "f1"]
        expected["excludedFacts"] = []
        for consumer in expected["consumerExamples"]:
            consumer["factRefs"] = list(expected["factRefs"])
    elif defect == "collapsed_slots":
        data["cases"][1]["facts"][1]["slot"] = deepcopy(data["cases"][1]["facts"][0]["slot"])
    elif defect == "end_evidence_missing":
        data["cases"][6]["facts"][0]["endClaimRefs"] = []
    elif defect == "correction_as_end":
        data["cases"][7]["facts"][0]["status"] = "ENDED"
    elif defect == "suppression_as_retraction":
        data["cases"][8]["queries"][0]["expected"]["excludedFacts"][0]["reason"] = "RETRACTED_FACT"
    elif defect == "unsupported_as_ready":
        data["cases"][4]["queries"][0]["expected"].update(status="READY", reason=None)
    elif defect == "direction_changed":
        slot = first["facts"][0]["slot"]
        slot["sourceNodeRef"], slot["targetNodeRef"] = slot["targetNodeRef"], slot["sourceNodeRef"]
    elif defect == "future_support":
        first["queries"][0]["cutoffEpisodeNo"] = 1
        first["sources"][0]["episodeNo"] = 2
        first["facts"][0]["lastChangedEpisodeNo"] = 2
    elif defect == "unknown_axis":
        first["facts"][0]["slot"]["axis"] = "INVENTED_AXIS"
    elif defect == "retracted_endpoints_changed":
        data["cases"][7]["facts"][0]["slot"]["targetNodeRef"] = "harin"
    elif defect == "end_metadata_too_early":
        data["cases"][6]["facts"][0]["lastChangedEpisodeNo"] = 1
    else:
        data["cases"][7]["facts"][0]["lastChangedEpisodeNo"] = 1
    with pytest.raises(AssertionError):
        validate_fixture(data)
