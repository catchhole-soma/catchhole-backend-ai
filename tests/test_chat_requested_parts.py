"""Authored request/grounding regressions; no real manuscript or provider calls.

These tests exercise the real hybrid handler, scoped HTTP contracts and outbox
using the shared offline replay. Scripted classification is not model accuracy.
"""

import asyncio
import copy
from dataclasses import replace
import socket

import pytest

from app.chatbot.hybrid_items import requested_facets
from app.schemas.chat_execution import ConversationTurn
from app.schemas.chat_hybrid import HybridPlan
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario
from tests.test_chat_hybrid_items import passage


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Requested-part replays cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


PARTS = [
    {"ref": "P1", "description": "첫 봉화 점화 직후 느낀 감정"},
    {"ref": "P2", "description": "그 경험이 이후 행동에 준 영향"},
]
EVENT = "미라의 첫 봉화 점화 사건"
EMOTION = "첫 봉화를 켠 뒤 미라는 두려움이 사라졌다고 말했다."
BEHAVIOR = "그 경험 때문에 미라는 다음 날부터 혼자 경비를 서기로 했다."


def parts_case(*, parts=None, event_context=EVENT):
    case = scenario()
    case.update(
        question="미라가 첫 봉화를 켠 뒤 어떤 감정을 느꼈고 이후 행동은 어떻게 달라졌어?",
        keyword=[],
        vector=[],
    )
    case["outputs"] = [
        {
            "disposition": "PROCEED",
            "registeredCurrentRequired": False,
            "rdbMode": "NONE",
            "sourceNeed": "PROCESS",
            "subjects": [],
            "searchTerms": ["봉화"],
            "includeEvidence": True,
            "episodeFrom": None,
            "episodeTo": None,
            "characterFilter": None,
            "perItem": False,
            "requestedFacets": [],
            "requestedParts": copy.deepcopy(parts if parts is not None else PARTS),
            "eventContext": event_context,
            "eventScope": None,
            "includeRegisteredValues": False,
        }
    ]
    return case


def claim(text, quote, *, ref="E1", parts=("P1", "P2"), event_match="MATCH"):
    return {
        "text": text,
        "evidenceRefs": [ref],
        "rowRefs": [],
        "answeredFacets": list(parts),
        "supports": [{"evidenceRef": ref, "quote": quote}],
        "eventMatch": event_match,
        "eventStatus": None,
    }


def answer(*claims, missing=(), coverage=None):
    return {
        "coverage": coverage or ("PARTIAL" if missing else "COMPLETE"),
        "claims": list(claims),
        "conflicts": [],
        "unansweredRowRefs": [],
        "itemAnswers": [],
        "missingFacets": list(missing),
    }


def search_with(*, keyword=(), vector=(), neighbors=()):
    def setup(search):
        search.keyword_chunks = list(keyword)
        search.vector_chunks = list(vector)
        search.neighbor_chunks = list(neighbors)

    return setup


def assert_answer(result):
    assert result["finish"]["failure"] is None, result["finish"]
    assert result["finish"]["result"]["outcome"] == "ANSWER", result["finish"]
    return result["finish"]["result"]


def test_two_independent_requests_keep_their_meaning_in_the_answer_input(tmp_path):
    case = parts_case()
    text = f"{EMOTION} {BEHAVIOR}"
    case["outputs"].append(
        answer(claim(EMOTION, EMOTION, parts=("P1",)), claim(BEHAVIOR, BEHAVIOR, parts=("P2",)))
    )

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[passage(text, 1)]))
    )

    response = assert_answer(result)
    payload = result["raw"].requests[-1]["input"]
    assert payload["requestedFacets"] == ["P1", "P2"]
    assert payload["plan"]["requestedParts"] == PARTS
    assert payload["plan"]["eventContext"] == EVENT
    assert EMOTION in response["text"] and BEHAVIOR in response["text"]
    assert result["embedding"].inputs == []
    assert requested_facets(HybridPlan.model_validate(case["outputs"][0])) == ["P1", "P2"]


def test_general_event_keeps_both_high_ranked_parts_from_one_episode(tmp_path):
    case = parts_case()
    episodes = [30, 34, 63, 4, 4, 13, 16]
    texts = [
        "봄 축제의 봉화는 늦게 꺼졌다.",
        "경비대가 새 봉화대를 수리했다.",
        "노인은 지난 봉화 축제를 회상했다.",
        EMOTION,
        BEHAVIOR,
        "시장은 봉화대가 있는 마을을 떠났다.",
        "새 경비병들은 봉화 깃발을 접었다.",
    ]
    candidates = [
        replace(
            passage(
                text,
                80 + rank,
                episode=episode,
                index=1 if rank == 5 else 0,
                offset=200 if rank == 5 else 0,
            ),
            score=1.0 - rank / 20,
        )
        for rank, (episode, text) in enumerate(zip(episodes, texts, strict=True), 1)
    ]
    case["outputs"].append(
        answer(
            claim(EMOTION, EMOTION, ref="E4", parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, ref="E5", parts=("P2",)),
        )
    )

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_evidence": 5},
            search_setup=search_with(keyword=candidates),
        )
    )

    response = assert_answer(result)
    passages = result["raw"].requests[-1]["input"]["passages"]
    references = {
        reference["evidenceRef"]: item["text"]
        for item in passages
        for reference in item["references"]
    }
    assert references["E4"] == EMOTION
    assert references["E5"] == BEHAVIOR
    assert len(references) == 5
    assert [item["chunkId"] for item in response["retrievalSources"]] == [
        str(candidates[3].chunk_id),
        str(candidates[4].chunk_id),
    ]
    assert result["embedding"].inputs == []


@pytest.mark.parametrize("keyword_count", [8, 12])
def test_no_answer_keyword_round_yields_context_to_lower_scored_vector_hits(
    tmp_path, keyword_count
):
    case = parts_case()
    keyword = [
        replace(passage(f"무관한 축제 기록 {rank}", 200 + rank, episode=100 + rank), score=1.0)
        for rank in range(keyword_count)
    ]
    supported = f"{EMOTION} {BEHAVIOR}"
    vector = [
        replace(
            passage(
                supported if rank == 4 else f"다른 경비대의 기록 {rank}",
                300 + rank,
                episode=200 + rank,
                method="VECTOR",
            ),
            score=0.7 - rank / 100,
        )
        for rank in range(12)
    ]
    case["outputs"] += [
        answer(missing=("P1", "P2"), coverage="NONE"),
        answer(claim(supported, supported, ref="E5")),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=keyword, vector=vector))
    )

    response = assert_answer(result)
    assert len(result["raw"].requests) == 3  # plan, unsuccessful keyword answer, vector answer
    first_input = result["raw"].requests[1]["input"]["passages"]
    final_input = result["raw"].requests[2]["input"]["passages"]
    assert {p["text"] for p in first_input} == {c.text for c in keyword[:8]}
    assert {p["text"] for p in final_input} == {c.text for c in vector[:8]}
    assert all(c.text not in {p["text"] for p in final_input} for c in keyword)
    assert (
        next(p for p in final_input if p["text"] == supported)["references"][0]["evidenceRef"]
        == "E5"
    )
    assert len(result["embedding"].inputs) == 1
    assert set(result["search"].vector_exclusions[0]) == {c.chunk_id for c in keyword[:8]}
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(vector[4].chunk_id)
    ]
    assert supported in response["text"]


def test_unreported_second_request_is_rejected_and_retried_before_publish(tmp_path):
    case = parts_case()
    text = f"{EMOTION} {BEHAVIOR}"
    first = claim(EMOTION, EMOTION, parts=("P1",))
    case["outputs"] += [
        answer(first),  # P2 is neither answered nor reported missing.
        answer(first, claim(BEHAVIOR, BEHAVIOR, parts=("P2",))),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[passage(text, 2)]))
    )

    response = assert_answer(result)
    assert len(result["raw"].requests) == 3
    assert "REQUESTED_PARTS_INCOMPLETE" in result["raw"].requests[-1]["system"]
    assert BEHAVIOR in response["text"]
    assert len(result["spring"].reservations) == 3


@pytest.mark.parametrize("event_match", ["OTHER", "UNCERTAIN"])
def test_other_or_uncertain_event_cannot_end_search_as_a_complete_answer(tmp_path, event_match):
    case = parts_case()
    wrong = "세 번째 봉화를 켠 뒤 미라는 자신감이 생겨 원정대에 지원했다."
    right = f"{EMOTION} {BEHAVIOR}"
    unrelated = passage(wrong, 3, episode=40)
    matching = passage(right, 4, episode=11, method="VECTOR")
    case["outputs"] += [
        answer(claim(wrong, wrong, event_match=event_match)),
        answer(claim(right, right)),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[unrelated], vector=[matching]))
    )

    response = assert_answer(result)
    assert "vector" in result["search"].calls
    assert len(result["embedding"].inputs) == 1
    assert EVENT in result["embedding"].inputs[0][0]
    assert right in response["text"] and wrong not in response["text"]
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(matching.chunk_id)
    ]


def test_support_in_neighbor_replaces_wrong_chunk_in_the_published_citation(tmp_path):
    case = parts_case()
    text = f"미라는 문 앞에서 멈췄다. {EMOTION} {BEHAVIOR}"
    start = text.index("첫 봉화")
    end = text.index("두려움")
    earlier = passage(text[:end], 5, episode=11, index=0)
    later = passage(text[start:], 6, episode=11, index=1, offset=start, method="NEIGHBOR")
    case["outputs"].append(answer(claim(f"{EMOTION} {BEHAVIOR}", f"{EMOTION} {BEHAVIOR}")))

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[earlier], neighbors=[later]))
    )

    response = assert_answer(result)
    initial_passages = result["raw"].requests[-1]["input"]["passages"]
    assert len(initial_passages) == 1
    assert len(initial_passages[0]["references"]) == 2
    assert [source["chunkId"] for source in response["retrievalSources"]] == [str(later.chunk_id)]
    assert response["retrievalSources"][0]["quote"] == later.text
    assert response["retrievalSources"][0]["startOffset"] == later.start_offset
    assert str(earlier.chunk_id) not in [
        source["chunkId"] for source in result["spring"].receipts[-1]["sources"]
    ]


def test_complementary_partial_answers_merge_without_reusing_wrong_evidence_refs(tmp_path):
    case = parts_case()
    emotion = passage(EMOTION, 7, episode=11)
    behavior = passage(BEHAVIOR, 8, episode=12, method="VECTOR")
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",)),
        # The supported first passage is retained as E1; the fresh one is E2.
        answer(claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)), missing=("P1",)),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[emotion], vector=[behavior]))
    )

    response = assert_answer(result)
    sources = response["retrievalSources"]
    assert [source["chunkId"] for source in sources] == [
        str(emotion.chunk_id),
        str(behavior.chunk_id),
    ]
    assert f"{EMOTION} (근거 1)" in response["text"]
    assert f"{BEHAVIOR} (근거 2)" in response["text"]
    assert "확정하기 어렵습니다" not in response["text"]
    assert PARTS[1]["description"] in result["embedding"].inputs[0][0]


def test_explicit_new_event_keeps_its_plan_despite_same_subject_in_conversation(tmp_path):
    new_event = "미라의 강 구조 사건"
    parts = [{"ref": "P1", "description": "강에서 사람을 구한 직후 감정"}]
    case = parts_case(parts=parts, event_context=new_event)
    case["question"] = "이번에는 미라가 강에서 사람을 구한 직후 감정을 알려줘."
    case["outputs"][0]["searchTerms"] = ["강"]
    text = "강에서 사람을 구한 미라는 안도감을 느꼈다."
    case["outputs"].append(answer(claim(text, text, parts=("P1",))))

    def conversation(spring):
        spring.context = spring.context.model_copy(
            update={
                "conversation": [
                    ConversationTurn(
                        question="미라가 처음 봉화를 켠 때는?", answer="가을 축제였다."
                    )
                ]
            }
        )

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=[passage(text, 9, episode=30)]),
            spring_setup=conversation,
        )
    )

    response = assert_answer(result)
    planner_payload = result["raw"].requests[0]["input"]
    answer_payload = result["raw"].requests[-1]["input"]
    assert planner_payload["conversation"][0]["question"] == "미라가 처음 봉화를 켠 때는?"
    assert answer_payload["plan"]["eventContext"] == new_event
    assert answer_payload["plan"]["requestedParts"] == parts
    assert text in response["text"] and "가을 축제" not in response["text"]


def test_per_item_uses_the_same_requested_parts_and_rejects_missing_part(tmp_path):
    parts = [
        {"ref": "P1", "description": "기술을 처음 배운 계기"},
        {"ref": "P2", "description": "배운 뒤 생활에 준 영향"},
    ]
    case = parts_case(parts=parts, event_context=None)
    case["question"] = "미라의 각 스킬을 처음 배운 계기와 배운 뒤 생활에 준 영향을 알려줘."
    plan = case["outputs"][0]
    plan.update(
        registeredCurrentRequired=False,
        rdbMode="CURRENT",
        perItem=True,
        subjects=[
            {
                "domain": "CHARACTER",
                "name": "미라",
                "focus": {"factType": "SKILL", "attribute": None},
            }
        ],
        searchTerms=["스킬"],
    )
    cause = "미라는 길을 잃은 뒤 등불 기술을 배웠다."
    impact = "등불을 배운 이후에는 밤에도 길을 찾을 수 있었다."
    text = f"{cause} {impact}"

    def facet(ref, value):
        return {
            "facet": ref,
            "text": value,
            "evidenceRefs": ["E1"],
            "supports": [{"evidenceRef": "E1", "quote": value}],
            "eventMatch": None,
            "eventStatus": None,
        }

    incomplete = {
        "coverage": "COMPLETE",
        "claims": [],
        "conflicts": [],
        "itemAnswers": [{"itemRef": "I1", "answers": [facet("P1", cause)], "missingFacets": []}],
        "missingFacets": [],
    }
    complete = copy.deepcopy(incomplete)
    complete["itemAnswers"][0]["answers"].append(facet("P2", impact))
    case["outputs"] += [incomplete, complete]

    def catalog(spring):
        row = item(
            701, "미라", category="SKILL", key="skill.light", label="등불", value="빛을 낸다"
        )
        spring.catalog = [catalog_subject(701, "미라", current=[row])]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=catalog,
            search_setup=search_with(keyword=[passage(text, 10, episode=15)]),
        )
    )

    response = assert_answer(result)
    payload = result["raw"].requests[-1]["input"]
    assert payload["requestedFacets"] == ["P1", "P2"]
    assert [entry["itemRef"] for entry in payload["items"]] == ["I1"]
    assert len(result["raw"].requests) == 3
    assert cause in response["text"] and impact in response["text"]
    assert result["embedding"].inputs == []


def conflict_case():
    case = parts_case(parts=[{"ref": "P1", "description": "첫 봉화 기술을 배운 계기"}])
    case["question"] = "미라가 첫 봉화 기술을 배운 계기를 알려줘."
    case["outputs"][0].update(
        registeredCurrentRequired=False,
        rdbMode="CURRENT",
        subjects=[
            {
                "domain": "CHARACTER",
                "name": "미라",
                "focus": {"factType": "SKILL", "attribute": None},
            }
        ],
    )
    return case


def conflict_catalog(spring):
    row = item(
        701, "미라", category="SKILL", key="skill.beacon", label="봉화", value="경비대에서 배움"
    )
    spring.catalog = [catalog_subject(701, "미라", current=[row])]


def conflict_answer(quote, *, event_match="MATCH", ref="E1"):
    result = answer(missing=("P1",))
    result["conflicts"] = [
        {
            "rowRefs": ["R1"],
            "evidenceRefs": [ref],
            "supports": [{"evidenceRef": ref, "quote": quote}],
            "eventMatch": event_match,
        }
    ]
    return result


def test_conflict_with_nonexistent_support_cannot_publish_a_disagreement(tmp_path):
    case = conflict_case()
    text = "미라는 첫 봉화 기술을 혼자 연구해서 배웠다."
    invalid = conflict_answer("제공된 원문에 없는 문장이다.")
    case["outputs"] += [invalid, copy.deepcopy(invalid)]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=conflict_catalog,
            search_setup=search_with(keyword=[passage(text, 11, episode=11)]),
        )
    )

    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert response["sources"] == response.get("retrievalSources", []) == []
    assert "제공된 원문에 없는 문장이다." not in response["text"]
    assert text not in response["text"]
    assert "다르게 나타납니다" not in response["text"]
    assert len(result["raw"].requests) == 3
    assert "GROUNDING_QUOTE_NOT_FOUND" in result["raw"].requests[-1]["system"]


@pytest.mark.parametrize("event_match", ["OTHER", "UNCERTAIN"])
def test_conflict_for_another_or_uncertain_event_is_not_preserved_as_a_disagreement(
    tmp_path, event_match
):
    case = conflict_case()
    unrelated_text = "미라는 세 번째 봉화 기술을 상인에게 배웠다."
    matching_text = "미라는 첫 봉화 기술을 경비대에서 배웠다."
    unrelated = passage(unrelated_text, 12, episode=40)
    matching = passage(matching_text, 13, episode=11, method="VECTOR")
    resolved = claim(matching_text, matching_text, parts=("P1",))
    resolved["rowRefs"] = ["R1"]
    case["outputs"] += [
        conflict_answer(unrelated_text, event_match=event_match),
        answer(resolved),
    ]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=conflict_catalog,
            search_setup=search_with(keyword=[unrelated], vector=[matching]),
        )
    )

    response = assert_answer(result)
    assert "vector" in result["search"].calls
    assert matching_text in response["text"]
    assert "다르게 나타납니다" not in response["text"]
    assert "어느 쪽이 옳은지" not in " ".join(response["limitations"])
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(matching.chunk_id)
    ]


def test_conflict_support_replaces_wrong_neighbor_reference_in_the_final_receipt(tmp_path):
    case = conflict_case()
    quote = "미라는 첫 봉화 기술을 혼자 연구해서 배웠다."
    text = f"날이 밝자 미라는 도서관에 갔다. {quote}"
    start = text.index("미라는 첫")
    end = text.index("혼자")
    earlier = passage(text[:end], 14, episode=11, index=0)
    later = passage(text[start:], 15, episode=11, index=1, offset=start, method="NEIGHBOR")
    case["outputs"].append(conflict_answer(quote))

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=conflict_catalog,
            search_setup=search_with(keyword=[earlier], neighbors=[later]),
            hybrid={"max_rounds": 1},
        )
    )

    response = assert_answer(result)
    assert "다르게 나타납니다" in response["text"]
    assert [source["chunkId"] for source in response["retrievalSources"]] == [str(later.chunk_id)]
    assert [source["chunkId"] for source in result["spring"].receipts[-1]["sources"]] == [
        str(later.chunk_id)
    ]


def test_per_item_corrected_support_anchor_survives_next_round_without_the_wrong_neighbor(
    tmp_path,
):
    case = conflict_case()
    case["outputs"][0].update(
        perItem=True,
        eventContext=None,
        requestedParts=[
            {"ref": "P1", "description": "봉화 기술을 배운 계기"},
            {"ref": "P2", "description": "배운 뒤 행동에 준 영향"},
        ],
    )
    cause = "미라는 길을 잃은 뒤 봉화 기술을 배웠다."
    impact = "봉화를 배운 이후에는 밤에도 길을 찾을 수 있었다."
    original = f"미라는 도서관에 들어갔다. {cause}"
    start, end = original.index("미라는 길"), original.index("뒤 봉화")
    earlier = passage(original[:end], 16, episode=11, index=0)
    later = passage(original[start:], 17, episode=11, index=1, offset=start, method="NEIGHBOR")
    expansion = passage(impact, 18, episode=12, method="VECTOR")

    def facet(part, text, ref):
        return {
            "facet": part,
            "text": text,
            "evidenceRefs": [ref],
            "supports": [{"evidenceRef": ref, "quote": text}],
            "eventMatch": None,
            "eventStatus": None,
        }

    first = answer(missing=(), coverage="PARTIAL")
    first["itemAnswers"] = [
        {"itemRef": "I1", "answers": [facet("P1", cause, "E1")], "missingFacets": ["P2"]}
    ]
    second = answer()
    second["itemAnswers"] = [
        {
            "itemRef": "I1",
            "answers": [facet("P1", cause, "E2"), facet("P2", impact, "E3")],
            "missingFacets": [],
        }
    ]
    case["outputs"] += [first, second]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=conflict_catalog,
            search_setup=search_with(keyword=[earlier], neighbors=[later], vector=[expansion]),
        )
    )

    response = assert_answer(result)
    payload = result["raw"].requests[-1]["input"]
    provided = {
        ref["evidenceRef"] for source in payload["passages"] for ref in source["references"]
    }
    previous = payload["previousItemAnswers"][0]["answers"][0]
    assert "E1" not in provided
    assert previous["evidenceRefs"] == ["E2"]
    assert [support["evidenceRef"] for support in previous["supports"]] == ["E2"]
    assert all(support["evidenceRef"] in provided for support in previous["supports"])
    assert cause in response["text"] and impact in response["text"]
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(later.chunk_id),
        str(expansion.chunk_id),
    ]
