"""Supported source context survives expansion; rejected prose is never published.

Authored offline cases use the real handler, scoped HTTP client and SQLite outbox.
The small injected input budget exercises trimming without a paid model call.
"""

import asyncio
import copy
import socket
from dataclasses import replace

import pytest

from app.chatbot.hybrid import _HybridSession
from app.chatbot.qa_render import notices
from tests.test_chat_hybrid import answer as legacy_answer
from tests.test_chat_hybrid import replay, scenario, source_chunk
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    BEHAVIOR,
    EMOTION,
    answer,
    assert_answer,
    claim,
    parts_case,
    search_with,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Evidence retention tests cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def candidates():
    keyword = [
        replace(
            passage(f"봉화 축제 주변 기록 {index}", 400 + index, episode=100 + index),
            score=1.0 - index / 100,
        )
        for index in range(7)
    ]
    supported = replace(passage(EMOTION, 407, episode=107), score=0.01)
    keyword.append(supported)
    vector = [
        replace(
            passage(
                BEHAVIOR if index == 0 else f"새로운 경비 기록 {index}",
                420 + index,
                episode=120 + index,
                method="VECTOR",
            ),
            score=0.8 - index / 100,
        )
        for index in range(8)
    ]
    return keyword, vector


def limit_expansion_context(monkeypatch, maximum):
    original = _HybridSession.fits

    def fits(self, prompt, payload, schema):
        # Preserve real accounting checks while forcing a deterministic boundary
        # after expansion. Each reference is a whole, separate source passage.
        refs = sum(len(item["references"]) for item in payload.get("passages", []))
        return original(self, prompt, payload, schema) and (self.rounds < 2 or refs <= maximum)

    monkeypatch.setattr(_HybridSession, "fits", fits)


def input_refs(request):
    return {
        reference["evidenceRef"]: item["text"]
        for item in request["input"]["passages"]
        for reference in item["references"]
    }


def test_low_rank_supported_source_survives_fresh_hits_and_input_trim_then_merges(
    tmp_path, monkeypatch
):
    keyword, vector = candidates()
    case = parts_case()
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, ref="E8", parts=("P1",)), missing=("P2",)),
        answer(claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)), missing=("P1",)),
    ]
    limit_expansion_context(monkeypatch, 2)

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=keyword, vector=vector))
    )

    response = assert_answer(result)
    assert input_refs(result["raw"].requests[1])["E8"] == EMOTION
    assert input_refs(result["raw"].requests[2]) == {"E1": EMOTION, "E2": BEHAVIOR}
    assert EMOTION in response["text"] and BEHAVIOR in response["text"]
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(keyword[-1].chunk_id),
        str(vector[0].chunk_id),
    ]
    assert "확정하기 어려워요" not in response["text"]
    assert len(result["raw"].requests) == 3
    assert len(result["embedding"].inputs) == 1
    assert result["search"].calls[-1] == "revalidate"
    assert result["spring"].receipts[-1]["sources"] == response["retrievalSources"]


def test_expansion_prioritizes_all_new_ranks_after_verified_evidence(tmp_path, monkeypatch):
    keyword, vector = candidates()
    vector[0] = replace(
        passage("다른 사건의 기록", 420, episode=120, method="VECTOR"), score=vector[0].score
    )
    vector[3] = replace(passage(BEHAVIOR, 423, episode=123, method="VECTOR"), score=vector[3].score)
    case = parts_case()
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, ref="E8", parts=("P1",)), missing=("P2",)),
        answer(claim(BEHAVIOR, BEHAVIOR, ref="E5", parts=("P2",)), missing=("P1",)),
    ]
    limit_expansion_context(monkeypatch, 5)
    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=keyword, vector=vector))
    )
    response = assert_answer(result)
    assert EMOTION in response["text"] and BEHAVIOR in response["text"]
    refs = input_refs(result["raw"].requests[2])
    assert refs["E1"] == EMOTION and refs["E5"] == BEHAVIOR
    assert len(refs) == 5
    assert len(result["raw"].requests) == 3


@pytest.mark.parametrize("adjacent_anchor", [False, True])
def test_invalid_draft_retains_located_quote_candidate_but_never_its_rejected_claim(
    tmp_path, adjacent_anchor
):
    keyword, vector = candidates()
    if adjacent_anchor:
        # E7 anchors the merged passage, but only E8 covers the actual quote.
        text = f"미라는 봉화 앞에 섰다. {EMOTION}"
        start = text.index("첫 봉화")
        keyword[-2:] = [
            replace(passage(text[: start + 4], 406, episode=107), score=0.02),
            replace(
                passage(text[start:], 407, episode=107, offset=start, index=1),
                score=0.01,
            ),
        ]
    case = parts_case()
    rejected = "거절한 초안의 문장은 게시하면 안 됩니다."
    invalid = answer(claim(rejected, EMOTION, ref="E7" if adjacent_anchor else "E8", parts=("P1",)))
    # The quote exists, but P2 is neither answered nor explicitly missing.
    case["outputs"] += [
        invalid,
        copy.deepcopy(invalid),
        answer(
            claim(EMOTION, EMOTION, ref="E1", parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)),
        ),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=keyword, vector=vector))
    )

    response = assert_answer(result)
    assert len(result["raw"].requests) == 4  # plan, two rejected drafts, new valid answer
    final_input = input_refs(result["raw"].requests[-1])
    assert final_input["E1"] == EMOTION and final_input["E2"] == BEHAVIOR
    assert rejected not in response["text"]
    assert EMOTION in response["text"] and BEHAVIOR in response["text"]
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(keyword[-1].chunk_id),
        str(vector[0].chunk_id),
    ]
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 5


def test_valid_none_still_replaces_unhelpful_prior_candidates_with_fresh_hits(tmp_path):
    keyword, vector = candidates()
    case = parts_case()
    case["outputs"] += [
        answer(missing=("P1", "P2"), coverage="NONE"),
        answer(claim(BEHAVIOR, BEHAVIOR, parts=("P2",)), missing=("P1",)),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=keyword, vector=vector))
    )

    response = assert_answer(result)
    final_input = input_refs(result["raw"].requests[-1])
    assert set(final_input.values()) == {item.text for item in vector}
    assert EMOTION not in final_input.values()
    assert EMOTION not in response["text"] and BEHAVIOR in response["text"]
    assert len(result["raw"].requests) == 3


def test_retained_second_direct_quote_is_renumbered_without_colliding_with_new_source(tmp_path):
    case = scenario("mixed_current_and_scene")
    case["outputs"][1]["rowRefs"] = ["R1", "R2"]
    registered_quote = "아리아의 마력은 30이다."
    partial = legacy_answer(ref="E2", coverage="PARTIAL", claim=registered_quote)
    partial["claims"][0]["supports"] = [{"evidenceRef": "E2", "quote": registered_quote}]
    final = legacy_answer(ref="E1", claim=registered_quote)
    final["claims"][0]["supports"] = [{"evidenceRef": "E1", "quote": registered_quote}]
    source_claim = legacy_answer(ref="E2")["claims"][0]
    source_claim["supports"] = [{"evidenceRef": "E2", "quote": source_chunk("ring").text}]
    final["claims"].append(source_claim)
    case["outputs"][2:] = [partial, final]

    result = asyncio.run(replay(tmp_path, case, hybrid={"max_evidence": 2, "per_episode": 2}))

    response = assert_answer(result)
    assert input_refs(result["raw"].requests[2])["E2"] == registered_quote
    assert input_refs(result["raw"].requests[3]) == {
        "E1": registered_quote,
        "E2": source_chunk("ring").text,
    }
    direct_queries = [
        query for query in result["spring"].queries if query["tool"] == "CHARACTER_EVIDENCE"
    ]
    assert len(direct_queries) == 3
    assert direct_queries[-1]["targetId"] == direct_queries[1]["targetId"]
    assert response["sources"] == [
        {"tool": "CHARACTER_EVIDENCE", "targetId": direct_queries[1]["targetId"]}
    ]
    assert [source["chunkId"] for source in response["retrievalSources"]] == [
        str(source_chunk("ring").chunk_id)
    ]
    assert registered_quote in response["text"] and "강물" in response["text"]


@pytest.mark.parametrize("source_changed", [False, True])
def test_unfittable_protected_context_restores_valid_partial_and_revalidates_sources(
    tmp_path, monkeypatch, source_changed
):
    keyword, vector = candidates()
    case = parts_case()
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, ref="E8", parts=("P1",)), missing=("P2",)),
        # There must be no model call consuming this unsupported replacement.
        answer(claim("게시하면 안 되는 새 완료 답변", BEHAVIOR, parts=("P1", "P2"))),
    ]
    limit_expansion_context(monkeypatch, 0)

    def mutate_after_first_answer(count, spring, search):
        if count == 2:
            search.changed = source_changed

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=keyword, vector=vector),
            model_hook=mutate_after_first_answer,
        )
    )

    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert len(result["raw"].requests) == 2
    assert len(result["embedding"].inputs) == 1
    assert result["search"].calls[-1] == "revalidate"
    assert "게시하면 안 되는" not in response["text"]
    if source_changed:
        assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert EMOTION not in response["text"]
        assert notices(["SOURCE_CHANGED"])[0] in response["limitations"]
    else:
        assert response["outcome"] == "ANSWER"
        assert EMOTION in response["text"]
        assert BEHAVIOR not in response["text"]
        assert [source["chunkId"] for source in response["retrievalSources"]] == [
            str(keyword[-1].chunk_id)
        ]
        assert result["spring"].receipts[-1]["sources"] == response["retrievalSources"]
