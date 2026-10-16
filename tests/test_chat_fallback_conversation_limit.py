"""Offline routing regressions for incomplete reference-resolution history.

Scripted model outputs exercise the actual handler, scoped mock Spring API and
usage outbox. They are not a measurement of live model interpretation quality.
"""

import asyncio
import socket

import pytest

from app.chatbot.hybrid_prompts import ANSWER_PROMPT, PLAN_PROMPT
from app.schemas.chat_execution import ConversationTurn
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_registered_fallback import (
    SOURCE,
    assert_no_search,
    catalog,
    registered_case,
    source_answer,
    tools,
)
from tests.test_chat_requested_parts import assert_answer, search_with


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Conversation fallback tests cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def followup_case():
    case = registered_case()
    case["question"] = "그 사람의 친구는 누구야?"
    # A source answer is available so an accidentally admitted fallback cannot
    # pass merely because retrieval or the synthetic provider has no response.
    case["outputs"].append(source_answer())
    return case


def with_history(mode):
    def setup(spring):
        catalog()(spring)
        history = [ConversationTurn(question="미라의 소개는?", answer="등록된 소개는 없습니다.")]
        if mode == "local":
            history = [
                ConversationTurn(question="이전 질문 " + "가" * 2990, answer="나" * 900),
                ConversationTurn(question="이전 질문 " + "다" * 2990, answer="라" * 900),
                *history,
            ]
        spring.context = spring.context.model_copy(
            update={"conversation": history, "conversation_truncated": mode == "spring"}
        )

    return setup


@pytest.mark.parametrize("mode", ["spring", "local"])
def test_incomplete_conversation_keeps_registered_gap_without_source_fallback(tmp_path, mode):
    config = (
        {"max_context_characters": max(len(PLAN_PROMPT), len(ANSWER_PROMPT)) + 2000}
        if mode == "local"
        else None
    )
    result = asyncio.run(
        replay(
            tmp_path,
            followup_case(),
            config=config,
            spring_setup=with_history(mode),
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        )
    )

    plan_input = result["raw"].requests[0]["input"]
    assert result["spring"].context.conversation_truncated is (mode == "spring")
    assert plan_input["conversationTruncated"] is True
    assert plan_input["conversation"][-1]["question"] == "미라의 소개는?"
    if mode == "local":
        assert len(plan_input["conversation"]) < len(result["spring"].context.conversation)
    assert tools(result) == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]
    assert_no_search(result)
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert result["finish"]["result"]["sources"] == []
    assert not result["finish"]["result"].get("retrievalSources")
    assert len(result["raw"].requests) == 1
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 1


def test_complete_conversation_still_allows_scoped_registered_gap_fallback(tmp_path):
    result = asyncio.run(
        replay(
            tmp_path,
            followup_case(),
            spring_setup=with_history("complete"),
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        )
    )

    response = assert_answer(result)
    assert SOURCE in response["text"]
    assert response["retrievalSources"]
    assert result["raw"].requests[0]["input"]["conversationTruncated"] is False
    assert result["search"].calls.count("keyword") == 1
    assert result["embedding"].inputs == []
    assert tools(result) == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2


def test_explicit_source_plan_keeps_its_existing_path_with_truncated_conversation(tmp_path):
    case = followup_case()
    case["question"] = "미라의 친구가 등장하는 장면은?"
    case["outputs"][0].update(rdbMode="NONE", sourceNeed="SCENE", sourceFallback=False, subjects=[])
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=with_history("spring"),
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        )
    )

    response = assert_answer(result)
    assert SOURCE in response["text"]
    assert result["raw"].requests[0]["input"]["conversationTruncated"] is True
    assert result["search"].calls.count("keyword") == 1
    assert tools(result) == []
    assert len(result["raw"].requests) == 2
