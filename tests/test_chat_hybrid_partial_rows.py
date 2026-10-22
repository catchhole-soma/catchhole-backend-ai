"""Row gaps and facet gaps are independent; authored data, no paid model calls."""

import asyncio
import copy
import socket

import pytest

from app.chatbot.hybrid import _Evidence, _HybridSession
from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.qa_render import NOTICES
from app.schemas.chat_hybrid import GroundedAnswer, HybridPlan
from tests.test_chat_focused_lookup import catalog_subject, item, uid
from tests.test_chat_hybrid import replay, scenario, source_chunk


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Partial-row regression tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def history_case(*, missing_facet=False, actual_event=False):
    case = scenario()
    case.update(
        question="세린의 기력이 바뀐 시점과 경위, 연결된 원문을 전부 보여줘",
        keyword=[],
        vector=[],
    )
    plan = {
        "disposition": "PROCEED",
        "registeredCurrentRequired": False,
        "rdbMode": "HISTORY",
        "sourceNeed": "PROCESS",
        "subjects": [
            {
                "domain": "CHARACTER",
                "name": "세린",
                "focus": {"factType": "STAT", "attribute": "기력"},
            }
        ],
        "searchTerms": ["세린", "기력"],
        "includeEvidence": True,
        "episodeFrom": None,
        "episodeTo": None,
        "characterFilter": None,
        "perItem": False,
        "requestedFacets": ["TIME", "HOW"],
        "collection": None,
        "answerShape": "FACTS",
        "includeRegisteredValues": False,
        "eventScope": "ACTUAL" if actual_event else None,
    }
    answer = {
        "coverage": "PARTIAL",
        "claims": [
            {
                "text": f"{n}화에서 물약을 마신 뒤 기력이 {n * 7}로 올랐습니다.",
                "evidenceRefs": [f"E{n}"],
                "rowRefs": [f"R{n}"],
                "answeredFacets": ["TIME"] if missing_facet else ["TIME", "HOW"],
                "eventStatus": "OCCURRED" if actual_event else None,
            }
            for n in range(1, 5)
        ],
        "conflicts": [],
        "unansweredRowRefs": ["R5"],
        "itemAnswers": [],
        "missingFacets": ["HOW"] if missing_facet else [],
    }
    case["outputs"] = [plan, answer]
    return case


def setup_history(spring):
    history, evidence = [], []
    for n in range(1, 6):
        row = item(
            701,
            "세린",
            kind="HISTORY",
            category="STAT",
            key="stat.energy",
            label="기력",
            value=str(n * 7),
            episode=n,
            number=800 + n,
        )
        row["sources"] = [{"tool": "CHARACTER_EVIDENCE", "targetId": uid(800 + n)}]
        quote = f"세린은 물약을 마셨고 기력이 {n * 7}로 올랐다."
        proof = copy.deepcopy(row)
        proof.update(
            kind="EVIDENCE",
            sources=[],
            evidence={
                "status": "AVAILABLE",
                "candidateId": uid(1800 + n),
                "episodeId": uid(10000 + n),
                "sourceEpisodeNo": n,
                "sourceChunkId": None,
                "sourceHash": "b" * 64,
                "sourceVersion": "v1",
                "matchesCurrentSource": True,
                "quotes": [{"text": quote, "startOffset": 0, "endOffset": len(quote)}],
                "truncated": False,
            },
        )
        history.append(row)
        evidence.append(proof)
    subject = catalog_subject(701, "세린", history=history)
    subject["evidence"] = evidence
    spring.catalog = [subject]


@pytest.mark.parametrize("missing_facet", [False, True])
@pytest.mark.parametrize("actual_event", [False, True])
def test_partial_history_with_four_quotes_and_five_rows_is_published_without_retry(
    tmp_path, missing_facet, actual_event
):
    case = history_case(missing_facet=missing_facet, actual_event=actual_event)
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_history,
            config={"max_evidence_calls": 4},
            hybrid={"max_rounds": 1},
        )
    )
    assert result["finish"]["failure"] is None
    final = result["finish"]["result"]
    assert final["outcome"] == "ANSWER"
    assert len(result["raw"].requests) == 2  # One plan and one valid answer; no schema retry.
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2
    assert len(final["sources"]) == 4
    assert NOTICES["PARTIAL_ANSWER"] not in final["limitations"]
    assert "확인하지 못했습니다" in final["text"]
    if missing_facet:
        assert "경위·방법 정보는" in final["text"]
    assert len([q for q in result["spring"].queries if q["tool"] == "CHARACTER_EVIDENCE"]) == 8


@pytest.mark.parametrize(
    "invalid",
    [
        "complete_with_missing_row",
        "unknown_row",
        "unknown_evidence",
        "facet_overlap",
        "unaccounted_facet",
    ],
)
def test_row_gap_fix_does_not_accept_invalid_coverage_or_references(tmp_path, invalid):
    case = history_case()
    answer = case["outputs"][1]
    if invalid == "complete_with_missing_row":
        answer["coverage"] = "COMPLETE"
    elif invalid == "unknown_row":
        answer["unansweredRowRefs"] = ["R999"]
    elif invalid == "unknown_evidence":
        answer["claims"][0]["evidenceRefs"] = ["E999"]
    elif invalid == "facet_overlap":
        answer["missingFacets"] = ["HOW"]
    else:
        for claim in answer["claims"]:
            claim["answeredFacets"] = ["TIME"]
    case["outputs"].append(copy.deepcopy(answer))
    result = asyncio.run(
        replay(tmp_path, case, spring_setup=setup_history, config={"max_evidence_calls": 4})
    )
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert response["sources"] == response.get("retrievalSources", []) == []
    assert all(claim["text"] not in response["text"] for claim in answer["claims"])
    assert "R999" not in response["text"] and "E999" not in response["text"]
    assert len(result["raw"].requests) == 3


def test_partial_history_still_withholds_answer_when_its_source_changes(tmp_path):
    case = history_case()

    def after_answer(count, spring, search):
        if count == 2:
            spring.catalog[0]["evidence"][0]["evidence"]["quotes"][0]["text"] = "변경된 합성 원문"

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_history,
            config={"max_evidence_calls": 4},
            hybrid={"max_rounds": 1},
            model_hook=after_answer,
        )
    )
    assert result["finish"]["failure"] is None
    final = result["finish"]["result"]
    assert final["outcome"] == "INSUFFICIENT_EVIDENCE" and not final["sources"]
    assert NOTICES["SOURCE_CHANGED"] in final["limitations"]


def session_for_postprocessing():
    session = _HybridSession.__new__(_HybridSession)
    session.hcfg = HybridSettings()
    session.codes = []
    session.collection_conditions = []
    session.logical_items = []
    session.has_collection_condition = True
    return session


def facet_snapshot(facet, *, missing_rows):
    return GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [
                {
                    "text": "합성 근거로 일부를 확인했습니다.",
                    "evidenceRefs": ["E1"],
                    "rowRefs": ["R1"],
                    "answeredFacets": [facet],
                }
            ],
            "conflicts": [],
            "unansweredRowRefs": missing_rows,
            "missingFacets": [f for f in ["TIME", "HOW"] if f != facet],
        }
    )


def test_partial_facet_merge_keeps_unanswered_rows_from_both_snapshots():
    session = session_for_postprocessing()
    old_evidence = _Evidence("E1", "합성 시점", 1, chunk=source_chunk("ring"))
    session.evidences = [_Evidence("E1", "합성 경위", 2, chunk=source_chunk("raon"))]
    previous = facet_snapshot("TIME", missing_rows=["R5"])
    latest = facet_snapshot("HOW", missing_rows=["R6", "R5"])
    plan = HybridPlan.model_validate(history_case()["outputs"][0])
    merged, _ = session.merge_partial_snapshot((previous, (old_evidence,)), latest, plan)
    assert merged.missing_facets == []
    assert merged.unanswered_row_refs == ["R5", "R6"]
    assert merged.coverage == "PARTIAL"


def partial_claims(claims, missing_rows, *, missing_facets=()):
    return GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [
                {
                    "text": "합성 기록을 확인했습니다.",
                    "evidenceRefs": ["E1"],
                    "rowRefs": [row],
                    "answeredFacets": facets,
                }
                for row, facets in claims
            ],
            "conflicts": [],
            "unansweredRowRefs": missing_rows,
            "missingFacets": list(missing_facets),
        }
    )


@pytest.mark.parametrize(
    "latest_claims,latest_gaps,expected_gaps",
    [
        # A full latest answer for R5 resolves its old gap. Retained R1 TIME is
        # still only a partial answer for R1, even when latest omits that gap.
        ([("R5", ["TIME", "HOW"])], ["R1"], {"R1"}),
        ([("R5", ["TIME", "HOW"])], ["R6"], {"R1", "R6"}),
        # Latest still declares R5 partial: its references cannot override that.
        ([("R5", ["TIME", "HOW"])], ["R1", "R5"], {"R1", "R5"}),
        # Global TIME/HOW coverage across different rows cannot resolve R5.
        ([("R5", ["TIME"]), ("R2", ["HOW"])], ["R1"], {"R1", "R2", "R5"}),
        # R1's earlier TIME is retained alongside its new HOW. R2 still lacks HOW.
        ([("R1", ["HOW"]), ("R2", ["TIME"])], ["R5"], {"R2", "R5"}),
    ],
)
def test_partial_merge_resolves_only_fully_answered_latest_rows_and_preserves_dropped_rows(
    latest_claims, latest_gaps, expected_gaps
):
    session = session_for_postprocessing()
    old_evidence = _Evidence("E1", "합성 시점", 1, chunk=source_chunk("ring"))
    session.evidences = [_Evidence("E1", "합성 경위", 2, chunk=source_chunk("raon"))]
    previous = facet_snapshot("TIME", missing_rows=["R5"])
    latest = partial_claims(latest_claims, latest_gaps)
    plan = HybridPlan.model_validate(history_case()["outputs"][0])
    merged, _ = session.merge_partial_snapshot((previous, (old_evidence,)), latest, plan)
    assert merged.missing_facets == []
    assert set(merged.unanswered_row_refs) == expected_gaps
    assert merged.coverage == "PARTIAL"
    assert any(c.row_refs == ["R1"] and "TIME" in c.answered_facets for c in merged.claims)


def test_new_row_progress_merges_even_when_global_facet_types_are_unchanged():
    session = session_for_postprocessing()
    old_evidence = _Evidence("E1", "합성 시점", 1, chunk=source_chunk("ring"))
    session.evidences = [_Evidence("E1", "합성 경위", 2, chunk=source_chunk("raon"))]
    previous = partial_claims([("R1", ["TIME", "HOW"])], ["R5"])
    latest = partial_claims([("R5", ["TIME", "HOW"])], ["R6"])
    plan = HybridPlan.model_validate(history_case()["outputs"][0])
    merged, evidence = session.merge_partial_snapshot((previous, (old_evidence,)), latest, plan)
    assert {row for c in merged.claims for row in c.row_refs} == {"R1", "R5"}
    assert merged.unanswered_row_refs == ["R6"]
    assert len(evidence) == 2 and merged.coverage == "PARTIAL"


def test_evidence_cap_fallback_keeps_lost_row_visible_when_selecting_latest_snapshot():
    session = session_for_postprocessing()
    session.hcfg = HybridSettings(max_evidence=1, per_episode=1)
    old_evidence = _Evidence("E1", "합성 시점", 1, chunk=source_chunk("ring"))
    session.evidences = [_Evidence("E1", "합성 경위", 2, chunk=source_chunk("raon"))]
    previous = partial_claims([("R1", ["TIME"]), ("R3", ["WHY"])], ["R5"], missing_facets=["HOW"])
    latest = partial_claims([("R5", ["TIME", "HOW", "WHY"])], ["R6"])
    plan_data = history_case()["outputs"][0]
    plan_data["requestedFacets"] = ["TIME", "HOW", "WHY"]
    plan = HybridPlan.model_validate(plan_data)
    merged, evidence = session.merge_partial_snapshot((previous, (old_evidence,)), latest, plan)
    assert "EVIDENCE_CONTEXT_LIMIT" in session.codes
    assert evidence == tuple(session.evidences)
    assert set(merged.unanswered_row_refs) == {"R1", "R3", "R6"}
    assert merged.coverage == "PARTIAL"


@pytest.mark.parametrize("verdict", ["MATCH", "UNKNOWN"])
def test_condition_guard_preserves_explicit_row_gaps_even_with_all_facets_answered(verdict):
    session = session_for_postprocessing()
    session.collection_conditions = [
        {"rowRefs": ["R1", "R2", "R3", "R4"], "verdict": "MATCH"},
        {"rowRefs": ["R5"], "verdict": verdict},
    ]
    case = history_case()
    guarded = session.apply_condition_guard(
        GroundedAnswer.model_validate(case["outputs"][1]),
        HybridPlan.model_validate(case["outputs"][0]),
    )
    assert guarded.missing_facets == []
    assert guarded.unanswered_row_refs == ["R5"]
    assert guarded.coverage == "PARTIAL"
