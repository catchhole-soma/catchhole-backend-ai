"""Execute a bounded event driver and both real snapshot readers, without LLMs.

Replay performs history/identity projection before B/C. These assertions do not
measure model operation selection, Java storage, or RDF-native lifecycle logic.
"""

import socket
from copy import deepcopy

import pytest

from scripts.relationship_semantics_experiment.input import Query
from scripts.relationship_semantics_experiment.scenarios import Event, Replay

NODES = {"a": "인물 A", "b": "인물 B", "alias-a": "A의 확정 별칭"}


def slot(source="a", target="b", *, axis="SOCIAL", kind="COMPANIONSHIP", direction="MUTUAL"):
    return {
        "workId": "synthetic-work",
        "sourceNodeRef": source,
        "targetNodeRef": target,
        "axis": axis,
        "kind": kind,
        "direction": direction,
    }


def added(ref="add", episode=1, fact="f1", relation=None):
    return Event(ref, episode, "ADD", fact, relation or slot(), "두 인물은 동료다.")


def replay(events):
    return Replay("synthetic-lifecycle", "synthetic-work", NODES, events)


def lifecycle():
    return replay(
        [
            added(),
            Event("update", 2, "UPDATE", "f1", text="동료 관계가 새 근거로 확인된다."),
            Event("absence", 3, "NO_MENTION"),
            Event("end", 4, "END", "f1", text="두 사람은 동료 관계를 끝냈다."),
            Event("resume", 5, "UPDATE", "f1", text="두 사람은 다시 동료가 되었다."),
        ]
    )


@pytest.fixture(params=["typed", "rdf"])
def model_type(request):
    if request.param == "typed":
        from scripts.relationship_semantics_experiment.typed_model import TypedModel

        return TypedModel
    from scripts.relationship_semantics_experiment.rdf_model import RdfModel

    return RdfModel


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("the lifecycle experiment must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def execute(model_type, case):
    model = model_type(case)
    report = model.validate()
    assert report["conforms"], report["violations"]
    return model.query(case.queries[0])


def test_recorded_transitions_preserve_exact_status_time_and_evidence(model_type):
    history = lifecycle()
    for cutoff, status, changed, supports, ends in [
        (1, "ACTIVE", 1, ["add"], []),
        (2, "ACTIVE", 2, ["add", "update"], []),
        (3, "ACTIVE", 2, ["add", "update"], []),
        (4, "ENDED", 4, ["add", "update"], ["end"]),
        (5, "ACTIVE", 5, ["add", "resume", "update"], []),
    ]:
        case = history.snapshot(cutoff)
        result = execute(model_type, case)
        assert result == {
            "caseId": f"synthetic-lifecycle-e{cutoff}",
            "queryRef": f"q{cutoff}",
            "status": "READY",
            "reason": None,
            "facts": [
                {
                    "factRef": "f1",
                    "slot": slot(),
                    "status": status,
                    "factValidity": "VALID",
                    "knownFromEpisodeNo": 1,
                    "lastChangedEpisodeNo": changed,
                    "supportClaimRefs": [f"c-{ref}" for ref in supports],
                    "endClaimRefs": [f"c-{ref}" for ref in ends],
                    "correctionSourceRefs": [],
                    "supportSourceRefs": [f"s-{ref}" for ref in supports],
                    "endSourceRefs": [f"s-{ref}" for ref in ends],
                }
            ],
            "excludedFacts": [],
            "heldClaimRefs": [],
        }
        assert all(source.episode_no <= cutoff for source in case.sources)
        assert len(history.history) == 5


def test_history_is_supplied_by_driver_not_invented_by_snapshot_reader(model_type):
    history = lifecycle()
    final_model = model_type(history.snapshot(5))
    assert final_model.validate()["conforms"]
    query = Query(
        query_ref="past", mode="READER_KNOWLEDGE", cutoff_episode_no=3, story_context_id=None
    )
    unrecorded = final_model.query(query)
    assert unrecorded["status"] == "UNSUPPORTED"
    assert unrecorded["reason"] == "HISTORY_NOT_AVAILABLE"
    assert unrecorded["facts"] == []
    assert execute(model_type, history.snapshot(3))["facts"][0]["status"] == "ACTIVE"


def test_confirmed_alias_projection_keeps_one_latest_state_and_all_support(model_type):
    history = replay(
        [
            added("alias", 1, "old", slot("alias-a")),
            added("canonical", 2, "canonical", slot()),
            Event("identity", 3, "ALIAS", alias_ref="alias-a", canonical_ref="a"),
            Event("ended", 4, "END", "old", text="별칭으로 불리던 인물도 같은 동료 관계를 끝냈다."),
        ]
    )
    assert len(execute(model_type, history.snapshot(2))["facts"]) == 2
    merged = execute(model_type, history.snapshot(3))["facts"]
    assert len(merged) == 1 and merged[0]["status"] == "ACTIVE"
    ended = execute(model_type, history.snapshot(4))["facts"]
    assert len(ended) == 1
    assert ended[0]["factRef"] == "canonical"
    assert ended[0]["slot"] == slot()
    assert ended[0]["status"] == "ENDED"
    assert ended[0]["knownFromEpisodeNo"] == 1
    assert ended[0]["lastChangedEpisodeNo"] == 4
    assert ended[0]["supportSourceRefs"] == ["s-alias", "s-canonical"]
    assert ended[0]["endSourceRefs"] == ["s-ended"]
    # Identity projection is external input. It does not rewrite the old journal.
    assert history.history[0]["facts"]["old"]["slot"]["sourceNodeRef"] == "alias-a"


def test_current_deletion_survives_new_evidence_alias_mapping_and_past_queries(model_type):
    history = replay(
        [
            added("alias", 1, "old", slot("alias-a")),
            Event("delete", 2, "SUPPRESS", "old"),
            added("canonical", 3, "canonical", slot()),
            Event("identity", 4, "ALIAS", alias_ref="alias-a", canonical_ref="a"),
            Event("new-evidence", 5, "UPDATE", "canonical", text="또 다른 근거가 관계를 확인했다."),
        ]
    )
    for cutoff, ref in [(1, "old"), (5, "canonical")]:
        case = history.snapshot(cutoff)
        result = execute(model_type, case)
        assert result["facts"] == []
        assert result["excludedFacts"] == [{"factRef": ref, "reason": "USER_SUPPRESSED"}]
        assert case.suppressions[0].recorded_revision == 2
    assert len(history.snapshot(5).facts[0].support_claim_refs) == 3


def test_correction_is_exclusion_not_a_narrative_end(model_type):
    history = replay(
        [
            added(),
            Event("absence", 2, "NO_MENTION"),
            Event(
                "correction",
                3,
                "RETRACT",
                "f1",
                text="정정: 이전 동료 관계 기록은 잘못된 추출이다.",
            ),
        ]
    )
    case = history.snapshot(3)
    result = execute(model_type, case)
    assert result["facts"] == []
    assert result["excludedFacts"] == [{"factRef": "f1", "reason": "RETRACTED_FACT"}]
    assert case.facts[0].status == "ACTIVE"
    assert case.facts[0].end_claim_refs == ()
    assert case.facts[0].correction_source_refs == ("s-correction",)
    assert case.claims[0].curation_status == "RETRACTED"
    with pytest.raises(ValueError, match="past reconstruction after a later retraction"):
        history.snapshot(1)


def test_new_kind_and_reverse_direction_do_not_end_the_original_slot(model_type):
    history = replay(
        [
            added("trust", relation=slot(axis="ATTITUDE", kind="TRUST", direction="DIRECTED")),
            added(
                "distrust",
                2,
                "f2",
                slot("b", "a", axis="ATTITUDE", kind="DISTRUST", direction="DIRECTED"),
            ),
        ]
    )
    facts = execute(model_type, history.snapshot(2))["facts"]
    assert [
        (
            fact["slot"]["sourceNodeRef"],
            fact["slot"]["targetNodeRef"],
            fact["slot"]["kind"],
            fact["status"],
        )
        for fact in facts
    ] == [("a", "b", "TRUST", "ACTIVE"), ("b", "a", "DISTRUST", "ACTIVE")]


@pytest.mark.parametrize(
    "events",
    [
        [added(episode=0)],
        [added(episode=-1)],
        [added(episode=True)],
        [added(episode=2), Event("past", 1, "NO_MENTION")],
        [added(), added("duplicate-ref", 2)],
        [added(), added("duplicate-slot", 2, "f2")],
        [Event("missing", 1, "END", "unknown", text="관계가 끝났다.")],
        [
            added(),
            Event("end", 2, "END", "f1", text="관계가 끝났다."),
            Event("end-again", 3, "END", "f1", text="다시 끝났다."),
        ],
        [added(), Event("changed-slot", 2, "UPDATE", "f1", slot("b", "a"), "방향 변경")],
        [Event("future-evidence", 1, "ADD", "f1", slot(), "관계 근거", source_episode_no=2)],
        [Event("negative-evidence", 1, "ADD", "f1", slot(), "관계 근거", source_episode_no=-1)],
        [Event("empty-evidence", 1, "ADD", "f1", slot(), " ")],
        [Event("invented-end", 1, "NO_MENTION", "f1")],
        [Event("bad-alias", 1, "ALIAS", alias_ref="missing", canonical_ref="a")],
        [
            Event("identity", 1, "ALIAS", alias_ref="alias-a", canonical_ref="a"),
            Event("cycle", 2, "ALIAS", alias_ref="a", canonical_ref="alias-a"),
        ],
        [
            added(relation=slot("alias-a", "a")),
            Event("self-loop", 2, "ALIAS", alias_ref="alias-a", canonical_ref="a"),
        ],
        [
            added(),
            Event("correction", 2, "RETRACT", "f1", text="잘못된 기록이다."),
            Event("invalid-resume", 3, "UPDATE", "f1", text="근거"),
        ],
    ],
)
def test_invalid_events_are_rejected_before_model_execution(events):
    with pytest.raises(ValueError):
        replay(events)


def test_driver_is_bounded_and_does_not_accept_an_oracle():
    with pytest.raises(ValueError, match="1 and 64"):
        replay([Event(f"empty-{i}", i + 1, "NO_MENTION") for i in range(65)])
    with pytest.raises(TypeError):
        Event("injected", 1, "NO_MENTION", expected={"facts": []})


def test_checkpoints_are_defensive_copies_and_preserve_ended_history():
    history = lifecycle()
    before = deepcopy(history.snapshot(4).to_dict())
    copy = history.history
    copy[3]["facts"]["f1"]["status"] = "ACTIVE"
    assert history.snapshot(4).to_dict() == before
    assert history.snapshot(4).facts[0].status == "ENDED"
    with pytest.raises(ValueError):
        history.snapshot(0)
    with pytest.raises(ValueError):
        history.snapshot(6)
