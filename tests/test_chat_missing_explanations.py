"""Offline contracts for request-specific uncertainty, not semantic model scores.

All narrative text and model outputs are authored fixtures. Socket access is
blocked even when the tests exercise the full replay/settlement path.
"""

import asyncio
import copy
import socket

import pytest
from pydantic import ValidationError

from app.chatbot.hybrid_items import (
    apply_event_scope,
    group_items,
    merge_item_answers,
    render_item_answers,
)
from app.schemas.chat_hybrid import GroundedAnswer, ItemAnswer
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import answer_row, passage
from tests.test_chat_requested_parts import answer, assert_answer, claim, parts_case, search_with
from tests.test_chat_requested_parts_merge import answer as snapshot_answer
from tests.test_chat_requested_parts_merge import evidence, plan, session


FIRST_USE_NOTE = "다만 이 장면이 최초 사용인지는 확인하지 못했어요."
REACTION_NOTE = "다만 동료가 어떻게 반응했는지는 확인하지 못했어요."
UPDATED_REACTION_NOTE = "동료가 곁에 있었지만 어떤 반응을 보였는지는 나오지 않아요."


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Missing-explanation tests cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def explanation(facet, text):
    return {"facet": facet, "text": text}


def item_payload(ref, *, answers=(), missing=("P2",), notes=()):
    return {
        "itemRef": ref,
        "answers": list(answers),
        "missingFacets": list(missing),
        "missingExplanations": list(notes),
    }


def facet(ref, text, evidence_ref):
    return {"facet": ref, "text": text, "evidenceRefs": [evidence_ref]}


def with_notes(parsed, notes):
    raw = parsed.model_dump(by_alias=True)
    raw["missingExplanations"] = notes
    return GroundedAnswer.model_validate(raw)


def test_specific_missing_note_is_published_once_with_citations_and_no_extra_call(tmp_path):
    part = {"ref": "P1", "description": "봉화를 처음 사용한 시점"}
    case = parts_case(parts=[part], event_context=None)
    case["question"] = "미라가 봉화를 처음 사용한 때는 언제야?"
    scene = "미라는 봉화를 켜 일행이 돌아올 길을 밝혔다."
    source = passage(scene, 181, episode=11)
    observation = claim(scene, scene, parts=())
    observation["relatedFacets"] = ["P1"]
    partial = answer(observation, missing=("P1",))
    partial["missingExplanations"] = [explanation("P1", FIRST_USE_NOTE)]
    case["outputs"].append(partial)

    def source_setup(search):
        search_with(keyword=[source])(search)
        search.limited = True

    result = asyncio.run(
        replay(tmp_path, case, search_setup=source_setup, hybrid={"max_rounds": 1})
    )

    response = assert_answer(result)
    text = response["text"]
    assert scene in text and "(근거 1)" in text
    assert text.count(FIRST_USE_NOTE) == 1
    assert "다만 다음 내용은 확인한 자료만으로 확정하기 어려워요:" not in text
    assert "관련 자료를 모두 검토한 결과는 아니에요" not in text
    assert "이번에 확인한 자료로는 질문의 일부만 답할 수 있었어요" not in text
    assert [entry["chunkId"] for entry in response["retrievalSources"]] == [str(source.chunk_id)]
    assert response["retrievalSources"][0]["quote"] == source.text
    assert result["spring"].receipts[-1]["sources"][0]["quote"] == source.text
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2
    assert result["embedding"].inputs == []


@pytest.mark.parametrize("target", ["general", "item"])
@pytest.mark.parametrize(
    "notes",
    [
        [explanation("P9", FIRST_USE_NOTE)],
        [explanation("P1", FIRST_USE_NOTE)],  # Answered, not a missing facet.
        [explanation("P2", FIRST_USE_NOTE), explanation("P2", REACTION_NOTE)],
        [explanation("P2", "")],
        [explanation("P2", " \n\t ")],
    ],
    ids=["unknown-facet", "not-missing", "duplicate-facet", "empty", "whitespace"],
)
def test_invalid_missing_explanations_are_rejected_at_the_schema_boundary(target, notes):
    if target == "general":
        payload = answer(
            claim("원문에서 확인한 설명입니다.", "합성 원문", parts=("P1",)), missing=("P2",)
        )
        payload["missingExplanations"] = copy.deepcopy(notes)
        schema = GroundedAnswer
    else:
        payload = item_payload(
            "I1",
            answers=[facet("P1", "원문에서 확인한 설명입니다.", "E1")],
            notes=copy.deepcopy(notes),
        )
        schema = ItemAnswer

    with pytest.raises(ValidationError):
        schema.model_validate(payload)


@pytest.mark.parametrize("new_note", [None, UPDATED_REACTION_NOTE])
def test_general_partial_merge_drops_resolved_notes_and_keeps_remaining_note(new_note):
    state = session()
    previous_source = evidence("E7", "세린은 안심했다.", 182, episode=7)
    current_source = evidence("E7", "세린은 문을 열었다.", 183, episode=8)
    previous = with_notes(
        snapshot_answer("세린은 안심했어요.", ["E7"], ["P1"], "E7", previous_source.text),
        [
            explanation("P2", "그 뒤 어떤 행동을 했는지는 확인하지 못했어요."),
            explanation("P3", REACTION_NOTE),
        ],
    )
    latest_notes = [explanation("P1", "당시 감정은 이 원문에 나오지 않아요.")]
    if new_note:
        latest_notes.append(explanation("P3", new_note))
    latest = with_notes(
        snapshot_answer("세린은 문을 열었어요.", ["E7"], ["P2"], "E7", current_source.text),
        latest_notes,
    )
    state.evidences = [current_source]

    merged, sources = state.merge_partial_snapshot((previous, (previous_source,)), latest, plan())

    assert merged.coverage == "PARTIAL"
    assert merged.missing_facets == ["P3"]
    assert [(note.facet, note.text) for note in merged.missing_explanations] == [
        ("P3", new_note or REACTION_NOTE)
    ]
    assert [entry.evidence_refs for entry in merged.claims] == [["E1"], ["E2"]]
    assert [entry.supports[0].evidence_ref for entry in merged.claims] == ["E1", "E2"]
    assert [entry.chunk.chunk_id for entry in sources] == [
        previous_source.chunk.chunk_id,
        current_source.chunk.chunk_id,
    ]


@pytest.mark.parametrize("with_answer", [False, True])
def test_each_item_keeps_its_own_missing_explanation_even_for_the_same_facet(with_answer):
    items = group_items(
        [answer_row("R1"), answer_row("R2", settingKey="skill.wind", displayName="바람막이")]
    )
    notes = [
        "빛을 익힌 장면은 찾지 못했어요.",
        "바람막이를 쓴 장면은 있지만 배운 과정은 확인하지 못했어요.",
    ]
    parsed = GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL" if with_answer else "NONE",
            "claims": [],
            "conflicts": [],
            "itemAnswers": [
                item_payload(
                    item.ref,
                    answers=[facet("P1", f"{name}을 사용한 장면이 나옵니다.", f"E{index}")]
                    if with_answer
                    else [],
                    missing=("P2",) if with_answer else ("P1", "P2"),
                    notes=[explanation("P2", note)],
                )
                for index, (item, name, note) in enumerate(
                    zip(items, ["빛", "바람막이"], notes, strict=True), 1
                )
            ],
        }
    )

    parts = render_item_answers(
        parsed,
        items,
        {"E1": 1, "E2": 2},
        labels={"P1": "사용 장면", "P2": "배운 과정"},
    )
    for index, (item, note) in enumerate(zip(items, notes, strict=True)):
        assert sum(note in part for part in parts) == 1
        own_parts = [part for part in parts if item.label in part and note in part]
        assert len(own_parts) == 1
        assert notes[1 - index] not in own_parts[0]
        if with_answer:
            assert f"(근거 {index + 1})" in own_parts[0]


def test_item_merge_and_event_scope_keep_only_the_still_missing_item_note():
    items = group_items(
        [answer_row("R1"), answer_row("R2", settingKey="skill.wind", displayName="바람막이")]
    )
    old = GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [],
            "conflicts": [],
            "itemAnswers": [
                item_payload(
                    item.ref,
                    answers=[
                        {
                            **facet("P1", "사용 장면이 나옵니다.", f"E{index}"),
                            "eventStatus": "OCCURRED",
                            "eventMatch": "MATCH",
                        }
                    ],
                    notes=[explanation("P2", note)],
                )
                for index, (item, note) in enumerate(
                    zip(items, [FIRST_USE_NOTE, REACTION_NOTE], strict=True), 1
                )
            ],
        }
    )
    latest = GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [],
            "conflicts": [],
            "itemAnswers": [
                item_payload(
                    "I1",
                    answers=[
                        {
                            **facet("P2", "스승에게 배웠어요.", "E3"),
                            "eventStatus": "OCCURRED",
                            "eventMatch": "MATCH",
                        }
                    ],
                    missing=("P1",),
                    notes=[explanation("P1", "사용 장면은 이번 원문에 나오지 않아요.")],
                ),
                item_payload("I2", missing=("P1", "P2")),
            ],
        }
    )

    guarded = apply_event_scope(old, "ACTUAL", ["P1", "P2"], event_context="기술 습득 사건")
    merged = merge_item_answers(guarded, latest, items, ["P1", "P2"], event_scope="ACTUAL")

    assert merged.coverage == "PARTIAL"
    first, second = merged.item_answers
    assert first.missing_facets == [] and first.missing_explanations == []
    assert [(entry.facet, entry.evidence_refs) for entry in first.answers] == [
        ("P1", ["E1"]),
        ("P2", ["E3"]),
    ]
    assert second.missing_facets == ["P2"]
    assert [(note.facet, note.text) for note in second.missing_explanations] == [
        ("P2", REACTION_NOTE)
    ]
    assert second.answers[0].evidence_refs == ["E2"]


@pytest.mark.parametrize(
    ("event_status", "event_match"),
    [("PLANNED", "OTHER"), ("PLANNED", "UNCERTAIN"), (None, "MATCH")],
)
def test_event_guard_discards_notes_that_depend_on_removed_observations(event_status, event_match):
    observation = claim("미라는 봉화를 켜기로 했어요.", "봉화를 켜기로 했다.", parts=())
    observation.update(relatedFacets=["P1"], eventStatus=event_status, eventMatch=event_match)
    raw = answer(observation, missing=("P1",))
    raw["missingExplanations"] = [explanation("P1", FIRST_USE_NOTE)]

    guarded = apply_event_scope(
        GroundedAnswer.model_validate(raw),
        "ACTUAL",
        ["P1"],
        event_context="첫 봉화를 켠 사건",
    )

    assert guarded.coverage == "NONE"
    assert guarded.claims == []
    assert guarded.missing_facets == ["P1"]
    assert guarded.missing_explanations == []


def test_item_conflict_discards_the_old_note_with_the_retracted_answer():
    items = group_items([answer_row("R1")])
    previous = GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [],
            "conflicts": [],
            "itemAnswers": [
                item_payload(
                    "I1",
                    answers=[facet("P1", "빛을 사용한 장면입니다.", "E1")],
                    notes=[explanation("P2", FIRST_USE_NOTE)],
                )
            ],
        }
    )
    latest = GroundedAnswer.model_validate(
        {
            "coverage": "PARTIAL",
            "claims": [],
            "conflicts": [{"rowRefs": ["R1"], "evidenceRefs": ["E2"]}],
            "itemAnswers": [item_payload("I1", missing=("P1", "P2"))],
        }
    )

    merged = merge_item_answers(previous, latest, items, ["P1", "P2"])

    assert merged.item_answers[0].answers == []
    assert merged.item_answers[0].missing_facets == ["P1", "P2"]
    assert merged.item_answers[0].missing_explanations == []
    assert merged.conflicts[0].evidence_refs == ["E2"]
