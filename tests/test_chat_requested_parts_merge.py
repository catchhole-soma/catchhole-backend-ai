"""Offline regressions for additive request parts and snapshot citation identities."""

from types import SimpleNamespace

import pytest

from app.chatbot.hybrid import _Evidence, _HybridSession
from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.hybrid_grounding import resolve_support_references
from app.schemas.chat_hybrid import GroundedAnswer, HybridPlan
from tests.test_chat_hybrid_items import passage


def session():
    value = object.__new__(_HybridSession)
    value.hcfg = HybridSettings()
    value.codes = []
    value.collection_conditions = []
    value.execution = SimpleNamespace(context=SimpleNamespace(question="각 기술을 설명해 줘"))
    return value


def plan():
    return HybridPlan.model_validate(
        {
            "disposition": "PROCEED",
            "registeredCurrentRequired": False,
            "rdbMode": "NONE",
            "sourceNeed": "PROCESS",
            "subjects": [],
            "searchTerms": [],
            "includeEvidence": True,
            "episodeFrom": None,
            "episodeTo": None,
            "perItem": False,
            "requestedFacets": [],
            "requestedParts": [
                {"ref": "P1", "description": "습득 이후 심리"},
                {"ref": "P2", "description": "실제 이후 행동"},
                {"ref": "P3", "description": "동료의 반응"},
            ],
            "eventContext": "처음 기술을 습득한 사건",
        }
    )


def evidence(ref, text, number, *, episode, offset=0, index=0):
    chunk = passage(text, number, episode=episode, offset=offset, index=index)
    return _Evidence(ref, text, episode, chunk=chunk)


def answer(text, refs, facets, support_ref, quote, *, row_refs=()):
    return GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [
                {
                    "text": text,
                    "evidenceRefs": refs,
                    "rowRefs": list(row_refs),
                    "answeredFacets": facets,
                    "eventMatch": "MATCH",
                    "supports": [{"evidenceRef": support_ref, "quote": quote}],
                }
            ],
            "conflicts": [],
            "missingFacets": [p for p in ["P1", "P2", "P3"] if p not in facets],
        }
    )


@pytest.mark.parametrize("row_refs", [(), ("R1",)])
def test_partial_overlap_preserves_every_previously_answered_part_and_remaps_supports(row_refs):
    state = session()
    old_evidence = evidence("E7", "세린은 안심하고 문을 열었다.", 91, episode=7)
    new_evidence = evidence("E7", "문을 연 세린에게 동료는 고개를 끄덕였다.", 92, episode=8)
    old = answer(
        "안심한 뒤 문을 열었다.", ["E7"], ["P1", "P2"], "E7", old_evidence.text, row_refs=row_refs
    )
    new = answer(
        "문을 열자 동료가 고개를 끄덕였다.",
        ["E7"],
        ["P2", "P3"],
        "E7",
        new_evidence.text,
        row_refs=row_refs,
    )
    state.evidences = [new_evidence]

    merged, sources = state.merge_partial_snapshot((old, (old_evidence,)), new, plan())

    assert merged.coverage == "COMPLETE" and merged.missing_facets == []
    assert [claim.text for claim in merged.claims] == [old.claims[0].text, new.claims[0].text]
    assert [claim.evidence_refs for claim in merged.claims] == [["E1"], ["E2"]]
    assert [claim.supports[0].evidence_ref for claim in merged.claims] == ["E1", "E2"]
    assert [source.chunk.chunk_id for source in sources] == [
        old_evidence.chunk.chunk_id,
        new_evidence.chunk.chunk_id,
    ]
    for claim in merged.claims:
        assert (
            resolve_support_references(claim.supports, state.evidence_passages(sources))
            == claim.evidence_refs
        )
    assert old.claims[0].supports[0].evidence_ref == new.claims[0].supports[0].evidence_ref == "E7"


@pytest.mark.parametrize("spanning", [False, True])
def test_snapshot_remapping_resolves_adjacent_or_spanning_support_before_renumbering(spanning):
    state = session()
    text = "세린은 문을 열었다. 붉은 불꽃이 피어올랐다. 동료가 다가왔다."
    left = evidence("E4", text[:25], 93, episode=9)
    right = evidence("E9", text[15:], 94, episode=9, offset=15, index=1)
    quote = text[10:30] if spanning else "동료가 다가왔다."
    actual_refs = ["E4", "E9"] if spanning else ["E9"]
    # The model named the left chunk of a merged passage; grounding already
    # corrected evidence_refs to the original chunks actually covering the quote.
    old = answer("기존에 확인한 두 요구", actual_refs, ["P1", "P2"], "E4", quote)
    current = evidence("E4", "다른 장면에서 동료는 고개를 끄덕였다.", 95, episode=10)
    new = answer("새로 확인한 동료 반응", ["E4"], ["P3"], "E4", current.text)
    state.evidences = [current]

    merged, sources = state.merge_partial_snapshot((old, (left, right)), new, plan())

    assert merged.coverage == "COMPLETE"
    assert merged.claims[0].supports[0].quote == quote
    assert merged.claims[0].supports[0].evidence_ref == "E1"
    assert merged.claims[0].evidence_refs == (["E1", "E2"] if spanning else ["E1"])
    for claim in merged.claims:
        assert (
            resolve_support_references(claim.supports, state.evidence_passages(sources))
            == claim.evidence_refs
        )


def test_fully_replaced_claim_is_not_duplicated():
    state = session()
    previous = evidence("E1", "기존 심리 자료", 96, episode=11)
    current = evidence("E1", "새 심리와 행동 자료", 97, episode=12)
    old = answer("기존 심리", ["E1"], ["P1"], "E1", previous.text)
    new = answer("새 심리와 행동", ["E1"], ["P1", "P2"], "E1", current.text)
    state.evidences = [current]
    merged, sources = state.merge_partial_snapshot((old, (previous,)), new, plan())
    assert [claim.text for claim in merged.claims] == ["새 심리와 행동"]
    assert merged.missing_facets == ["P3"]
    assert len(sources) == 1 and sources[0].chunk == current.chunk


def test_item_expansion_queries_only_that_items_missing_parts_and_preserves_event():
    state = session()
    value = plan().model_copy(update={"per_item": True})
    draft = GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [],
            "conflicts": [],
            "itemAnswers": [
                {
                    "itemRef": "I1",
                    "answers": [{"facet": "P1", "text": "심리 확인", "evidenceRefs": ["E1"]}],
                    "missingFacets": ["P2"],
                },
                {
                    "itemRef": "I2",
                    "answers": [{"facet": "P2", "text": "행동 확인", "evidenceRefs": ["E2"]}],
                    "missingFacets": ["P1"],
                },
            ],
        }
    )
    first = state.expansion_query(value, draft, "첫 기술", item_ref="I1")
    second = state.expansion_query(value, draft, "둘째 기술", item_ref="I2")
    assert "실제 이후 행동" in first and "습득 이후 심리" not in first
    assert "습득 이후 심리" in second and "실제 이후 행동" not in second
    assert value.event_context in first and value.event_context in second
    assert "첫 기술" in first and "둘째 기술" in second


def test_missing_part_hint_survives_a_long_expansion_query():
    state = session()
    previous = answer("확인된 심리", ["E1"], ["P1"], "E1", "원문")
    query = state.expansion_query(plan(), previous, "긴 검색 표현 " * 200)
    assert len(query) <= 1000
    assert "실제 이후 행동" in query and "동료의 반응" in query


@pytest.mark.parametrize("retain_old_proof", [False, True])
def test_same_unfinished_part_keeps_new_detail_only_with_previous_proofs(retain_old_proof):
    state = session()
    first = evidence("E1", "협상은 시작됐다.", 110, episode=4)
    second = evidence("E2", "운송 권리는 공동으로 갖는다.", 111, episode=5)
    old = answer("협상 시작", ["E1"], [], "E1", first.text)
    old.claims[0] = old.claims[0].model_copy(update={"related_facets": ["P1"]})
    new = answer(second.text, ["E2"], [], "E2", second.text)
    new.claims[0] = new.claims[0].model_copy(update={"related_facets": ["P1"]})
    if retain_old_proof:
        new.claims.insert(0, old.claims[0].model_copy(deep=True))
    state.evidences = [first, second]
    merged, _ = state.merge_partial_snapshot((old, (first,)), new, plan())
    assert merged.coverage == "PARTIAL"
    assert merged.missing_facets == old.missing_facets
    assert (second.text in " ".join(c.text for c in merged.claims)) is retain_old_proof


def test_previous_source_draft_rebinds_identity_and_never_reuses_missing_sources():
    state = session()
    original = evidence("E1", "원래 확인한 사실", 112, episode=4)
    other = evidence("E1", "다른 자료", 113, episode=5)
    old = answer("원래 확인한 사실", ["E1"], ["P1"], "E1", original.text)
    state.preserve_supported_evidence(old, [original])
    state.evidences = [
        other,
        original.__class__("E2", original.text, original.episode, chunk=original.chunk),
    ]
    claims = state.previous_source_claims()
    assert claims[0]["evidenceRefs"] == ["E2"]
    assert claims[0]["supports"][0]["evidenceRef"] == "E2"
    state.evidences = [other]
    assert state.previous_source_claims() == []
