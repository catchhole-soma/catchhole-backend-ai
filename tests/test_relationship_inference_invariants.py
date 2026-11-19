"""Independent composite-path golden checks across six revision snapshots.

Five people and seven original assertions give four A-B-C proofs plus one
A-D-C proof. C-E adds further two-hop conclusions, but never A-E ancestry.
Expected premise combinations below are handwritten, not engine-generated.
Each snapshot is constructed independently: this tests full recomputation,
not a persistent event log, incremental deletion, or database transactions.
"""

from copy import deepcopy

import pytest

from scripts.relationship_inference_experiment import owl_engine, python_engine
from scripts.relationship_inference_experiment.policy import prepare

EDGES = (
    ("a1", "A", "B"),
    ("a1b", "A", "B"),
    ("a2", "B", "C"),
    ("a2b", "B", "C"),
    ("a3", "A", "D"),
    ("a4", "D", "C"),
    ("a5", "C", "E"),
)
ALL_AC_PROOFS = (
    ("a1", "a2"),
    ("a1", "a2b"),
    ("a1b", "a2"),
    ("a1b", "a2b"),
    ("a3", "a4"),
)
SURVIVING_AC_PROOFS = (("a1b", "a2"), ("a1b", "a2b"), ("a3", "a4"))
ALTERNATE_AC_PROOF = (("a3", "a4"),)
BE_PROOFS = (("a2", "a5"), ("a2b", "a5"))
DE_PROOFS = (("a4", "a5"),)
VARIANTS = (
    "initial",
    "one_support_withdrawn",
    "parent_suppressed",
    "conclusion_suppressed",
    "parent_suppressed_new_support",
    "conclusion_suppressed_new_support",
)


def _assertion(assertion_id, subject, obj):
    return {
        "assertionId": assertion_id,
        "workId": "w",
        "subjectId": subject,
        "objectId": obj,
        "kind": "PARENT_OF",
        "subtype": "BIOLOGICAL",
        "generationGap": None,
        "assertionState": "ASSERTED",
        "factValidity": "VALID",
        "nativeTime": {"domain": "FAMILY", "scope": "CURRENT"},
        "temporalContextId": "now",
        "sourceIds": [f"s-{assertion_id}"],
        "validationIssues": [],
    }


def _source(assertion_id):
    return {
        "sourceId": f"s-{assertion_id}",
        "workId": "w",
        "episodeNo": 1,
        "supportStatus": "ACTIVE",
        "quoteAvailability": "AVAILABLE",
        "text": "synthetic parent assertion",
        "quote": "parent assertion",
    }


def _relation(kind, subject, obj):
    return {
        "workId": "w",
        "subjectId": subject,
        "objectId": obj,
        "kind": kind,
        "subtype": "BIOLOGICAL",
        "temporalContextId": "now",
        "generationGap": 2 if kind in {"GRANDPARENT_OF", "GRANDCHILD_OF"} else None,
    }


def _snapshot(variant):
    case = {
        "caseId": "composite-paths",
        "snapshotRevision": VARIANTS.index(variant) + 1,
        "snapshotThroughEpisode": 1,
        "query": {
            "workId": "w",
            "cutoffEpisode": 1,
            "temporalContextId": "now",
            "inputMode": "EPISODE_SNAPSHOT",
        },
        "entities": [
            {"entityId": name, "name": name, "workId": "w", "identityStatus": "RESOLVED"}
            for name in "ABCDE"
        ],
        "sources": [_source(assertion_id) for assertion_id, _, _ in EDGES],
        "assertions": [_assertion(*edge) for edge in EDGES],
        "suppressions": [],
    }
    if variant == "one_support_withdrawn":
        case["sources"][0]["supportStatus"] = "WITHDRAWN"
    if variant.startswith("parent_suppressed"):
        case["suppressions"] = [
            {
                **_relation("PARENT_OF", "A", "B"),
                "suppressionId": "hide-ab",
                "recordedRevision": 2,
                "active": True,
            }
        ]
    if variant.startswith("conclusion_suppressed"):
        case["suppressions"] = [
            {
                **_relation("GRANDPARENT_OF", "A", "C"),
                "suppressionId": "hide-ac",
                "recordedRevision": 2,
                "active": True,
            }
        ]
    if variant.endswith("new_support"):
        case["sources"].append(_source("new"))
        case["assertions"].append(_assertion("new", "A", "B"))
    return case


def _proofs(rule, premise_groups, *, intermediate=None):
    """Serialize explicit golden premise combinations without finding paths."""
    rows = []
    for premises in premise_groups:
        proof = {
            "ruleId": rule,
            "premiseAssertionIds": list(premises),
            "sourceIds": sorted(f"s-{ref}" for ref in premises),
        }
        if intermediate is not None:
            proof.update(
                rulePath=["R_BIOLOGICAL_GRANDPARENT", "R_GRANDPARENT_INVERSE"],
                intermediateRelations=[deepcopy(intermediate)],
            )
        rows.append(proof)
    return sorted(rows, key=lambda row: tuple(row["premiseAssertionIds"]))


def _expected(variant):
    direct = {
        ("A", "B"): ("a1", "a1b"),
        ("B", "C"): ("a2", "a2b"),
        ("A", "D"): ("a3",),
        ("D", "C"): ("a4",),
        ("C", "E"): ("a5",),
    }
    ac_proofs, withheld = ALL_AC_PROOFS, []
    if variant == "one_support_withdrawn":
        direct[("A", "B")] = ("a1b",)
        ac_proofs = SURVIVING_AC_PROOFS
        withheld = [{"assertionId": "a1", "reason": "WITHDRAWN_FACT"}]
    elif variant.startswith("parent_suppressed"):
        del direct[("A", "B")]
        ac_proofs = ALTERNATE_AC_PROOF
        withheld = [{"assertionId": ref, "reason": "USER_SUPPRESSED"} for ref in ("a1", "a1b")]
        if variant.endswith("new_support"):
            withheld.append({"assertionId": "new", "reason": "USER_SUPPRESSED"})
    elif variant.startswith("conclusion_suppressed"):
        ac_proofs = ()
        if variant.endswith("new_support"):
            direct[("A", "B")] = ("a1", "a1b", "new")

    direct_rows, derived = {}, {}
    for (subject, obj), support in direct.items():
        direct_rows[(subject, obj)] = {
            **_relation("PARENT_OF", subject, obj),
            "supportAssertionIds": list(support),
            "sourceIds": sorted(f"s-{ref}" for ref in support),
        }
        derived[("CHILD_OF", obj, subject)] = {
            **_relation("CHILD_OF", obj, subject),
            "derivationType": "SEMANTIC_INVERSE",
            "proofs": _proofs("R_PARENT_INVERSE", [(ref,) for ref in support]),
        }
    # Handwritten endpoint pairs and premise combinations, not a graph join.
    chains = (
        (("A", "C"), ("C", "A"), ac_proofs),
        (("B", "E"), ("E", "B"), BE_PROOFS),
        (("D", "E"), ("E", "D"), DE_PROOFS),
    )
    for gp_ends, gc_ends, premises in chains:
        if not premises:
            continue
        intermediate = _relation("GRANDPARENT_OF", *gp_ends)
        derived[("GRANDPARENT_OF", *gp_ends)] = {
            **intermediate,
            "derivationType": "NEW_RELATION",
            "proofs": _proofs("R_BIOLOGICAL_GRANDPARENT", premises),
        }
        derived[("GRANDCHILD_OF", *gc_ends)] = {
            **_relation("GRANDCHILD_OF", *gc_ends),
            "derivationType": "SEMANTIC_INVERSE",
            "proofs": _proofs("R_GRANDPARENT_INVERSE", premises, intermediate=intermediate),
        }
    excluded = []
    if variant.startswith("conclusion_suppressed"):
        excluded = [
            {
                **_relation(kind, subject, obj),
                "reason": "USER_SUPPRESSED",
                "suppressionIds": ["hide-ac"],
            }
            for kind, subject, obj in (("GRANDCHILD_OF", "C", "A"), ("GRANDPARENT_OF", "A", "C"))
        ]
    return direct_rows, derived, withheld, excluded


@pytest.mark.parametrize("engine", [python_engine, owl_engine], ids=["B", "C"])
@pytest.mark.parametrize("variant", VARIANTS)
def test_composite_paths_preserve_exact_proofs_and_suppression_boundaries(engine, variant):
    case = _snapshot(variant)
    original = deepcopy(case)
    actual = engine.run(prepare(case), extended=True)
    direct, derived, withheld, excluded = _expected(variant)

    assert actual["caseId"] == "composite-paths"
    assert actual["outcome"] == "READY" and actual["outcomeReason"] is None
    assert len(actual["directRelations"]) == len(direct)
    assert {(row["subjectId"], row["objectId"]): row for row in actual["directRelations"]} == direct
    assert len(actual["derivedRelations"]) == len(derived)
    assert {
        (row["kind"], row["subjectId"], row["objectId"]): row for row in actual["derivedRelations"]
    } == derived
    assert actual["withheldAssertions"] == withheld
    assert len(actual["excludedDerivedRelations"]) == len(excluded)
    assert {
        (row["kind"], row["subjectId"], row["objectId"]): row
        for row in actual["excludedDerivedRelations"]
    } == {(row["kind"], row["subjectId"], row["objectId"]): row for row in excluded}
    assert case == original

    # A-B-C-E and A-D-C-E are three generations. Neither of the bounded
    # parent-chain/inverse rules may turn those into grandparent/grandchild.
    for rows in (
        actual["directRelations"],
        actual["derivedRelations"],
        actual["excludedDerivedRelations"],
    ):
        assert not any(
            (row["subjectId"], row["objectId"]) in {("A", "E"), ("E", "A")} for row in rows
        )
        assert all(row["kind"] != "ANCESTOR_OF" for row in rows)


@pytest.mark.parametrize("engine", [python_engine, owl_engine], ids=["B", "C"])
def test_running_revision_snapshots_does_not_leave_a_stale_closure(engine):
    original = engine.run(prepare(_snapshot("initial")), extended=True)
    for variant in VARIANTS[1:]:
        engine.run(prepare(_snapshot(variant)), extended=True)
    assert engine.run(prepare(_snapshot("initial")), extended=True) == original
