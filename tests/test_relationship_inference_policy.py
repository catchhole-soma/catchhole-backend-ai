"""Input and shared policy tests; no inference engine, model, DB or network."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.relationship_inference_experiment.input import parse_cases
from scripts.relationship_inference_experiment.output import (
    base_result,
    finish,
    relation,
    relation_key,
)
from scripts.relationship_inference_experiment.policy import (
    active_suppressions,
    canonical_suppression_key,
    prepare,
)

FIXTURE = Path(__file__).parent / "fixtures" / "relationship_ontology_cases.json"


def document(index=0):
    original = json.loads(FIXTURE.read_text(encoding="utf-8"))
    original["cases"] = [original["cases"][index]]
    return original


def one_case(index=0):
    return parse_cases(document(index))[0]


def suppression(row, suppression_id="delete-1", **changes):
    return {
        **relation(row),
        "suppressionId": suppression_id,
        "recordedRevision": 1,
        "active": True,
        **changes,
    }


def set_path(value, path, replacement):
    cursor = value
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = replacement


@pytest.mark.parametrize("index", range(24))
def test_original_direct_and_eligibility_match_handwritten_oracle(index):
    source = document(index)
    case = parse_cases(source)[0]
    expected = source["cases"][0]["expected"]
    actual = base_result(prepare(case))
    assert actual["outcome"] == expected["outcome"]
    assert actual["outcomeReason"] == expected["outcomeReason"]
    assert actual["directRelations"] == sorted(expected["directRelations"], key=relation_key)
    assert actual["withheldAssertions"] == sorted(
        expected["withheldAssertions"], key=lambda row: row["assertionId"]
    )
    assert actual["derivedRelations"] == actual["excludedDerivedRelations"] == []
    assert actual["caseId"] == case["caseId"]


class OracleTrap(dict):
    """An oracle field may exist, but no consumer may read its value."""

    def __getitem__(self, key):
        if key in {"expected", "forbiddenRelations", "consumerExamples", "rules", "oracle"}:
            raise AssertionError(f"oracle value read: {key}")
        return super().__getitem__(key)


def test_allowlist_never_reads_answer_forbidden_consumer_or_rule_values():
    original = document()
    clean = parse_cases(original)
    case = OracleTrap(original["cases"][0])
    case.update(forbiddenRelations=object(), consumerExamples=object())
    original["cases"] = [case]
    original["consumerExamples"] = object()
    assert parse_cases(OracleTrap(original)) == clean


def test_minimal_envelope_and_serialized_sanitized_inputs_are_reusable():
    case = one_case()
    minimal = {"schemaVersion": 2, "cases": [case]}
    assert parse_cases(json.loads(json.dumps(minimal))) == (case,)
    minimal.update(synthetic=True, status="OFFLINE_INFERENCE_EXPERIMENT")
    assert parse_cases(minimal) == (case,)
    assert "expected" not in case


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schemaVersion",), True),
        (("schemaVersion",), "1"),
        (("schemaVersion",), 3),
        (("synthetic",), 1),
        (("synthetic",), False),
        (("cases", 0, "snapshotRevision"), True),
        (("cases", 0, "snapshotRevision"), 0),
        (("cases", 0, "snapshotThroughEpisode"), -1),
        (("cases", 0, "query", "cutoffEpisode"), "1"),
        (("cases", 0, "query", "cutoffEpisode"), 99),
        (("cases", 0, "query", "inputMode"), "HISTORY"),
        (("cases", 0, "caseId"), " "),
        (("cases", 0, "entities", 0, "name"), ""),
        (("cases", 0, "entities", 0, "identityStatus"), "MAYBE"),
        (("cases", 0, "sources", 0, "episodeNo"), True),
        (("cases", 0, "sources", 0, "episodeNo"), 99),
        (("cases", 0, "sources", 0, "quote"), "원문에 없는 인용"),
        (("cases", 0, "sources", 0, "supportStatus"), "UNKNOWN"),
        (("cases", 0, "sources", 0, "quoteAvailability"), "PURGED"),
        (("cases", 0, "assertions", 0, "subjectId"), "missing"),
        (("cases", 0, "assertions", 0, "sourceIds"), ["missing"]),
        (("cases", 0, "assertions", 0, "sourceIds"), []),
        (("cases", 0, "assertions", 0, "sourceIds"), ["s1", "s1"]),
        (("cases", 0, "assertions", 0, "kind"), "CHILD_OF"),
        (("cases", 0, "assertions", 0, "subtype"), "NONE"),
        (("cases", 0, "assertions", 0, "generationGap"), 1),
        (("cases", 0, "assertions", 0, "nativeTime", "scope"), "PRESENT"),
        (("cases", 0, "assertions", 0, "validationIssues"), ["UNREGISTERED"]),
    ],
)
def test_structural_corruption_is_rejected(path, value):
    source = document()
    set_path(source, path, value)
    with pytest.raises(ValueError):
        parse_cases(source)


@pytest.mark.parametrize("group", ["entities", "sources", "assertions"])
def test_duplicate_member_ids_are_rejected(group):
    source = document()
    source["cases"][0][group].append(deepcopy(source["cases"][0][group][0]))
    with pytest.raises(ValueError, match="duplicate"):
        parse_cases(source)


def test_duplicate_cases_and_unknown_execution_fields_are_rejected():
    source = document()
    source["cases"].append(deepcopy(source["cases"][0]))
    with pytest.raises(ValueError, match="duplicate caseId"):
        parse_cases(source)
    source = document()
    source["cases"][0]["query"]["inventedPolicy"] = "ACCEPT_ALL"
    with pytest.raises(ValueError, match="unsupported field"):
        parse_cases(source)


def test_v1_does_not_silently_accept_v2_suppression_fields():
    source = document()
    source["cases"][0]["suppressions"] = []
    with pytest.raises(ValueError, match="unsupported field"):
        parse_cases(source)


def test_suppression_type_reference_and_revision_contract():
    case = one_case()
    case["suppressions"] = [suppression(case["assertions"][0])]
    for field, invalid in [
        ("active", 1),
        ("recordedRevision", True),
        ("recordedRevision", 0),
        ("subjectId", "missing"),
        ("kind", "FRIEND_OF"),
    ]:
        changed = deepcopy(case)
        changed["suppressions"][0][field] = invalid
        with pytest.raises(ValueError):
            prepare(changed)
    case["suppressions"].append(deepcopy(case["suppressions"][0]))
    with pytest.raises(ValueError, match="duplicate suppressionId"):
        prepare(case)


def test_source_or_filters_future_and_withdrawn_sources_without_mutating_input():
    case = one_case()
    case["snapshotThroughEpisode"] = 3
    template = case["sources"][0]
    case["sources"].extend(
        [
            {**template, "sourceId": "future", "episodeNo": 3},
            {**template, "sourceId": "withdrawn", "supportStatus": "WITHDRAWN"},
            {**template, "sourceId": "second-visible"},
        ]
    )
    case["assertions"][0]["sourceIds"] = ["s1", "future", "withdrawn", "second-visible"]
    original = deepcopy(case)
    prepared = prepare(case)
    assert prepared.eligible[0]["sourceIds"] == ["s1", "second-visible"]
    assert prepared.case["assertions"][0]["sourceIds"] == original["assertions"][0]["sourceIds"]
    assert case == original
    case["sources"][0]["text"] = "caller changed"
    assert prepared.case["sources"][0]["text"] == original["sources"][0]["text"]


@pytest.mark.parametrize(
    ("sources", "reason"),
    [
        ([("WITHDRAWN", 1), ("WITHDRAWN", 3)], "WITHDRAWN_FACT"),
        ([("WITHDRAWN", 1), ("ACTIVE", 3)], "FUTURE_EVIDENCE"),
    ],
)
def test_source_or_no_eligible_support_has_explicit_reason(sources, reason):
    case = one_case()
    case["snapshotThroughEpisode"] = 3
    template = case["sources"][0]
    case["sources"] = [
        {**template, "sourceId": f"s{index}", "supportStatus": status, "episodeNo": episode}
        for index, (status, episode) in enumerate(sources)
    ]
    case["assertions"][0]["sourceIds"] = [row["sourceId"] for row in case["sources"]]
    assert prepare(case).withheld == ({"assertionId": "a1", "reason": reason},)


def test_purged_quote_does_not_withdraw_an_active_fact():
    case = one_case()
    case["sources"][0].update(quoteAvailability="PURGED", text=None, quote=None)
    assert prepare(case).eligible[0]["sourceIds"] == ["s1"]


@pytest.mark.parametrize("inverse", [False, True])
def test_parent_suppression_canonicalizes_child_form_and_all_independent_supports(inverse):
    case = one_case(14)
    row = case["assertions"][0]
    deleted = suppression(row)
    if inverse:
        deleted.update(subjectId=row["objectId"], objectId=row["subjectId"], kind="CHILD_OF")
    case["suppressions"] = [deleted]
    prepared = prepare(case)
    assert [row["assertionId"] for row in prepared.eligible] == ["a3"]
    assert prepared.withheld == (
        {"assertionId": "a1", "reason": "USER_SUPPRESSED"},
        {"assertionId": "a1b", "reason": "USER_SUPPRESSED"},
    )


def test_suppression_uses_current_revision_for_all_reader_cutoffs_but_not_future_revision():
    case = one_case()
    case["snapshotThroughEpisode"] = 3
    case["suppressions"] = [suppression(case["assertions"][0], recordedRevision=2)]
    assert prepare(case).eligible
    case["snapshotRevision"] = 2
    assert prepare(case).withheld == ({"assertionId": "a1", "reason": "USER_SUPPRESSED"},)
    case["query"]["cutoffEpisode"] = 3
    assert prepare(case).withheld == ({"assertionId": "a1", "reason": "USER_SUPPRESSED"},)
    case["suppressions"][0]["active"] = False
    assert prepare(case).eligible


def test_suppression_does_not_cross_context_or_work():
    case = one_case()
    row = case["assertions"][0]
    case["entities"].extend(
        [
            {"entityId": "b1", "name": "도윤", "workId": "work-b", "identityStatus": "RESOLVED"},
            {"entityId": "b2", "name": "서연", "workId": "work-b", "identityStatus": "RESOLVED"},
        ]
    )
    case["suppressions"] = [
        suppression(row, "context", temporalContextId="other"),
        suppression(row, "work", workId="work-b", subjectId="b1", objectId="b2"),
    ]
    assert prepare(case).eligible
    assert not prepare(case).suppressed_keys


@pytest.mark.parametrize("inverse", [False, True])
def test_derived_suppression_has_exact_schema_and_current_matching_ids(inverse):
    case = one_case(2)
    parent = case["assertions"][0]
    derived = {
        **relation(parent),
        "objectId": "harin",
        "kind": "GRANDPARENT_OF",
        "generationGap": 2,
        "derivationType": "NEW_RELATION",
        "proofs": [
            {
                "ruleId": "R_BIOLOGICAL_GRANDPARENT",
                "premiseAssertionIds": ["a1", "a3"],
                "sourceIds": ["s3", "s1"],
            }
        ],
    }
    row = relation(derived)
    if inverse:
        row.update(subjectId="harin", objectId="doyun", kind="GRANDCHILD_OF")
    case["suppressions"] = [
        suppression(row, "z"),
        suppression(row, "a"),
        suppression(row, "inactive", active=False),
        suppression(row, "future", recordedRevision=case["snapshotRevision"] + 1),
    ]
    prepared = prepare(case)
    assert len(prepared.eligible) == 2
    actual = finish(prepared, [derived])
    assert actual["derivedRelations"] == []
    assert actual["excludedDerivedRelations"] == [
        {**relation(derived), "reason": "USER_SUPPRESSED", "suppressionIds": ["a", "z"]}
    ]
    assert len(active_suppressions(prepared.case)) == 2


def test_finish_sorts_without_changing_proof_role_order_or_hiding_duplicates():
    prepared = prepare(one_case(2))
    derived = {
        **relation(prepared.eligible[0]),
        "objectId": "harin",
        "kind": "GRANDPARENT_OF",
        "generationGap": 2,
        "derivationType": "NEW_RELATION",
        "proofs": [
            {
                "ruleId": "R_BIOLOGICAL_GRANDPARENT",
                "premiseAssertionIds": ["a1", "a3"],
                "sourceIds": ["s3", "s1"],
            }
        ],
    }
    original = deepcopy(derived)
    result = finish(prepared, [derived, deepcopy(derived)])
    assert len(result["derivedRelations"]) == 2
    assert result["derivedRelations"][0]["proofs"][0]["sourceIds"] == ["s1", "s3"]
    assert result["derivedRelations"][0]["proofs"][0]["premiseAssertionIds"] == ["a1", "a3"]
    assert derived == original


def test_fatal_annotation_rejects_whole_input_including_otherwise_valid_assertions():
    case = one_case(2)
    case["assertions"][0]["validationIssues"] = ["SELF_RELATION"]
    prepared = prepare(case)
    assert prepared.outcome == "REJECTED"
    assert not prepared.eligible
    assert {row["assertionId"] for row in prepared.withheld} == {"a1", "a2", "a3"}
    assert {row["reason"] for row in prepared.withheld} == {"SELF_RELATION"}
    with pytest.raises(ValueError, match="non-ready"):
        finish(prepared, [{"not": "a conclusion"}])


def test_cycle_annotations_are_supplied_not_newly_inferred_by_shared_policy():
    case = one_case(19)
    for row in case["assertions"]:
        row.update(factValidity="VALID", validationIssues=[])
    assert prepare(case).outcome == "READY"
    assert len(prepare(case).eligible) == 2


def test_history_capability_precedes_annotations_and_assertion_filtering():
    case = one_case(22)
    case["assertions"][0].update(validationIssues=["SELF_RELATION"], factValidity="WITHDRAWN")
    prepared = prepare(case)
    assert prepared.outcome == "UNSUPPORTED"
    assert {row["reason"] for row in prepared.withheld} == {"UNSUPPORTED_HISTORY"}


def test_specific_identity_and_unknown_time_reasons_precede_review_fallback():
    for index, reason in ((9, "UNRESOLVED_IDENTITY"), (13, "UNKNOWN_TIME")):
        prepared = prepare(one_case(index))
        assert prepared.withheld[0]["reason"] == reason


def test_visibility_precedes_suppression_and_withdrawal_precedes_future_source():
    case = one_case()
    case["snapshotThroughEpisode"] = 3
    case["sources"][0]["episodeNo"] = 3
    case["suppressions"] = [suppression(case["assertions"][0])]
    assert prepare(case).withheld[0]["reason"] == "FUTURE_EVIDENCE"
    case["assertions"][0]["factValidity"] = "WITHDRAWN"
    assert prepare(case).withheld[0]["reason"] == "WITHDRAWN_FACT"


def test_identity_key_distinguishes_full_tuple_and_only_normalizes_family_inverses():
    parent = one_case()["assertions"][0]
    child = {
        **parent,
        "subjectId": parent["objectId"],
        "objectId": parent["subjectId"],
        "kind": "CHILD_OF",
    }
    assert relation_key(parent) != relation_key(child)
    assert canonical_suppression_key(parent) == canonical_suppression_key(child)
    for field, different in (
        ("workId", "other"),
        ("subtype", "ADOPTIVE"),
        ("temporalContextId", "past"),
    ):
        assert canonical_suppression_key(parent) != canonical_suppression_key(
            {**parent, field: different}
        )


def test_five_thousand_assertions_have_no_arbitrary_small_fixture_cap():
    case = one_case()
    template = case["assertions"][0]
    case["assertions"] = [
        {**deepcopy(template), "assertionId": f"a-{index}"} for index in range(5000)
    ]
    prepared = prepare(case)
    assert len(prepared.eligible) == 5000
    assert len(base_result(prepared)["directRelations"][0]["supportAssertionIds"]) == 5000
