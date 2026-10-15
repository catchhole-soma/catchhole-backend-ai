"""A later search cannot discard an earlier partial answer or change its E references."""

import asyncio
from dataclasses import replace
import socket
from uuid import UUID

import pytest

from app.chatbot.qa_render import notices
from app.chatbot.replay import ScriptedTextClient
from tests.test_chat_hybrid import answer, replay, scenario, source_chunk


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Hybrid fallback tests must not use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def replace_keyword_evidence(search, name="weather"):
    # Keep the default eight-reference cap: eight higher-ranked, distinct episodes evict E1.
    search.vector_chunks = [
        replace(
            source_chunk(name, method="VECTOR"),
            chunk_id=UUID(int=3000 + index),
            episode_id=UUID(int=4000 + index),
            episode_no=10 + index,
            source_key=f"synthetic/{name}/{index}",
            score=0.99,
        )
        for index in range(8)
    ]


def partial_case(later=None):
    case = scenario("lexical_scene")
    case["outputs"] = [
        case["outputs"][0],
        answer(coverage="PARTIAL", claim="아리아는 반지를 강물에 던졌다."),
        later or answer(coverage="NONE"),
    ]
    return case


def test_later_none_restores_partial_answer_with_its_original_evidence(tmp_path):
    result = asyncio.run(replay(tmp_path, partial_case(), search_setup=replace_keyword_evidence))
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert "질문의 일부만 답할 수 있었어요" in finished["text"]
    assert notices(["PARTIAL_ANSWER"])[0] not in finished["limitations"]
    assert [source["sourceEpisodeNo"] for source in finished["retrievalSources"]] == [3]
    assert "반지" in finished["retrievalSources"][0]["quote"]
    assert "비가 내렸다" not in finished["text"]
    # The last draft saw a different E1; the receipt and final revalidation must use the old one.
    assert (
        result["raw"].requests[-1]["input"]["passages"][0]["text"] == source_chunk("weather").text
    )
    assert result["search"].calls[-1] == "revalidate"
    assert result["spring"].receipts[-1]["sources"] == finished["retrievalSources"]
    assert len(result["raw"].requests) == 3 and len(result["embedding"].inputs) == 1


def test_later_complete_answer_takes_priority_over_saved_partial(tmp_path):
    result = asyncio.run(
        replay(
            tmp_path,
            partial_case(answer()),
            search_setup=lambda search: replace_keyword_evidence(search, "ring"),
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert [source["sourceEpisodeNo"] for source in finished["retrievalSources"]] == [10]
    assert notices(["PARTIAL_ANSWER"])[0] not in finished["limitations"]


def test_equal_partial_coverage_keeps_first_supported_answer_without_quality_guess(tmp_path):
    result = asyncio.run(
        replay(
            tmp_path,
            partial_case(answer(coverage="PARTIAL")),
            search_setup=lambda search: replace_keyword_evidence(search, "ring"),
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert [source["sourceEpisodeNo"] for source in finished["retrievalSources"]] == [3]
    assert "- 아리아는 반지를 강물에 던졌다. (근거 1)" in finished["text"]


def test_saved_partial_is_withheld_when_its_source_changed_during_expansion(tmp_path):
    def changed(count, spring, search):
        if count == 3:
            search.changed = True

    result = asyncio.run(
        replay(
            tmp_path,
            partial_case(),
            search_setup=replace_keyword_evidence,
            model_hook=changed,
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "반지를 강물" not in finished["text"]
    assert result["search"].calls[-1] == "revalidate"
    assert notices(["SOURCE_CHANGED"])[0] in finished["limitations"]


def test_no_partial_answer_keeps_insufficient_evidence_result(tmp_path):
    case = partial_case()
    case["outputs"][1] = answer(coverage="NONE")
    result = asyncio.run(replay(tmp_path, case, search_setup=replace_keyword_evidence))
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "revalidate" not in result["search"].calls


@pytest.mark.parametrize("changed", [False, True])
def test_direct_partial_snapshot_is_restored_and_requeried(tmp_path, changed):
    case = scenario("mixed_current_and_scene")
    case["outputs"][-1] = answer(coverage="NONE")

    def change_direct(count, spring, search):
        if changed and count == 4:
            spring.catalog[0]["evidence"][0]["evidence"]["quotes"][0]["text"] = "변경된 원문"

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_rounds": 1, "max_evidence": 1, "per_episode": 1},
            model_hook=change_direct,
        )
    )
    finished = result["finish"]["result"]
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_EVIDENCE") == 2
    if changed:
        assert finished["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert "화염구" not in finished["text"]
    else:
        assert finished["outcome"] == "ANSWER"
        assert "화염구" in finished["text"]
        assert finished["sources"] and not finished.get("retrievalSources")
        assert "질문의 일부만 답할 수 있었어요" in finished["text"]
        assert notices(["PARTIAL_ANSWER"])[0] not in finished["limitations"]


def test_partial_snapshot_survives_later_invalid_responses_with_its_original_source(tmp_path):
    case = partial_case()
    case["outputs"][2:] = ["invalid JSON", "invalid JSON"]
    result = asyncio.run(replay(tmp_path, case, search_setup=replace_keyword_evidence))
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "ANSWER"
    assert "아리아는 반지를 강물에 던졌다." in response["text"]
    assert "질문의 일부만 답할 수 있었어요" in response["text"]
    assert "invalid JSON" not in response["text"]
    assert [source["sourceEpisodeNo"] for source in response["retrievalSources"]] == [3]
    assert result["search"].calls[-1] == "revalidate"
    assert result["spring"].receipts[-1]["sources"] == response["retrievalSources"]
    assert len(result["raw"].requests) == 4


def test_partial_snapshot_does_not_swallow_later_provider_failure(tmp_path, monkeypatch):
    original = ScriptedTextClient.create_text_response

    async def fail_later(self, *args, **kwargs):
        if len(self.requests) == 2:
            raise RuntimeError("synthetic provider transport failure")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ScriptedTextClient, "create_text_response", fail_later)
    result = asyncio.run(replay(tmp_path, partial_case(), search_setup=replace_keyword_evidence))
    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    assert "revalidate" not in result["search"].calls
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 4
    assert result["spring"].reports[-1]["kind"] == "UNKNOWN"


def test_partial_snapshot_reserves_final_revalidation_before_spending_search_budget(tmp_path):
    result = asyncio.run(
        replay(
            tmp_path,
            partial_case(),
            config={"max_tool_calls": 6},
            search_setup=replace_keyword_evidence,
        )
    )
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert len(result["raw"].requests) == 2
    assert result["search"].calls == ["keyword", "neighbors", "revalidate"]
    assert len(result["spring"].receipts) == 2
