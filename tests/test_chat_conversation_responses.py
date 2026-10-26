"""Scripted routing and display regressions, not live intent classification scores."""

import asyncio
import socket

import pytest
from pydantic import ValidationError

from app.chatbot.qa_render import NOTICES, message, notices
from app.chatbot.replay import replay_case
from app.schemas.chat_execution import ConversationTurn
from app.schemas.chat_qa import QuestionPlan
from tests.test_chat_hybrid import replay, scenario as hybrid_scenario
from tests.test_chat_qa import scenario


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Conversation response tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def conversational_plan(intent):
    return {
        "intent": intent,
        "subjects": [],
        "includeEvidence": False,
        "episodeFrom": None,
        "episodeTo": None,
        "characterFilter": None,
        "collection": None,
        "answerShape": "FACTS",
    }


def truncated_history(spring):
    spring.context = spring.context.model_copy(
        update={
            "conversation": [ConversationTurn(question="이전 질문", answer="이전 답변")],
            "conversation_truncated": True,
        }
    )


@pytest.mark.parametrize("engine", ["rdb", "hybrid"])
@pytest.mark.parametrize(
    "intent,question,expected",
    [
        ("GREETING", "안녕", "안녕하세요! 작품에서 궁금한 내용을 물어보세요."),
        ("THANKS", "고마워", "천만에요. 더 궁금한 내용이 있으면 말씀해 주세요."),
        (
            "HELP",
            "이 챗봇에는 어떻게 질문하면 돼?",
            "작품의 인물이나 세계관 설정에 대해 물어보세요. 확인할 대상과 궁금한 내용을 함께 알려주시면 도움이 돼요.",
        ),
    ],
)
def test_conversational_plan_returns_fixed_answer_with_one_metered_call(
    tmp_path, engine, intent, question, expected
):
    if engine == "rdb":
        case, catalog = scenario()
        case.update(question=question, modelOutputs=[conversational_plan(intent)])
        result = asyncio.run(replay_case(case, catalog, spring_setup=truncated_history))
        queries, requests = result["queries"], result["modelRequests"]
        reservations, reports = result["reservations"], result["usageReports"]
    else:
        case = hybrid_scenario()
        plan = conversational_plan(intent)
        plan.pop("intent")
        plan.update(
            disposition=intent,
            registeredCurrentRequired=False,
            rdbMode="NONE",
            sourceNeed="NONE",
            searchTerms=[],
            sourceFallback=False,
            perItem=False,
            requestedFacets=[],
            includeRegisteredValues=False,
            eventScope=None,
            requestedParts=[],
            eventContext=None,
        )
        case.update(question=question, outputs=[plan])
        result = asyncio.run(replay(tmp_path, case, spring_setup=truncated_history))
        assert result["search"].calls == []
        assert result["embedding"].inputs == []
        queries, requests = result["spring"].queries, result["raw"].requests
        reservations, reports = result["spring"].reservations, result["spring"].reports
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"] == {
        "outcome": "ANSWER",
        "text": expected,
        "sources": [],
        "limitations": [],
    }
    assert queries == []
    assert len(requests) == len(reservations) == len(reports) == 1
    assert requests[0]["input"]["conversationTruncated"] is True
    assert reservations[0]["purpose"] == "CHAT_INTERPRETATION"
    assert reports[0]["kind"] == "SETTLE"


@pytest.mark.parametrize("intent", ["GREETING", "THANKS", "HELP"])
def test_conversational_plan_cannot_carry_factual_lookup(intent):
    plan = conversational_plan(intent)
    plan["subjects"] = [{"domain": "CHARACTER", "name": "아리아"}]
    with pytest.raises(ValidationError, match="Conversational requests do not query"):
        QuestionPlan.model_validate(plan)


@pytest.mark.parametrize(
    "field,value",
    [
        ("includeEvidence", True),
        ("episodeFrom", 1),
        ("episodeTo", 2),
        ("answerShape", "SUMMARY"),
        ("characterFilter", {"factType": "STATUS", "attribute": "상태", "value": "활성"}),
        (
            "collection",
            {
                "domain": "CHARACTER",
                "factType": None,
                "worldCategory": None,
                "attribute": None,
                "predicate": None,
            },
        ),
    ],
)
def test_conversational_plan_requires_empty_query_fields(field, value):
    with pytest.raises(ValidationError, match="Conversational requests do not query"):
        QuestionPlan.model_validate(conversational_plan("GREETING") | {field: value})


@pytest.mark.parametrize(
    "question",
    [
        "안녕, 아리아의 능력은?",
        "고마워! 아리아의 능력은?",
        "아리아의 능력 사용법을 볼게, 능력은 뭐야?",
    ],
)
def test_conversation_words_do_not_intercept_a_fact_plan(question):
    case, catalog = scenario()
    case["question"] = question
    result = asyncio.run(replay_case(case, catalog, spring_setup=truncated_history))
    answer = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert answer["outcome"] == "ANSWER"
    assert "화염구" in answer["text"]
    assert [q["tool"] for q in result["queries"]] == case["expected"]["tools"]
    assert NOTICES["CONVERSATION_LIMIT"] not in answer["limitations"]
    assert all(r["input"]["conversationTruncated"] for r in result["modelRequests"])


def test_no_registered_rows_does_not_report_unrelated_conversation_limit():
    case, catalog = scenario()
    catalog[0]["current"] = []
    result = asyncio.run(replay_case(case, catalog, spring_setup=truncated_history))
    answer = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert answer["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert NOTICES["CONVERSATION_LIMIT"] not in answer["limitations"]
    assert NOTICES["NO_RELATED_SETTING"] in answer["limitations"]
    assert result["modelRequests"][0]["input"]["conversationTruncated"] is True


def test_ambiguous_followup_keeps_conversation_limit_in_clarification():
    case, catalog = scenario()
    case.update(question="그건?", modelOutputs=[conversational_plan("CLARIFICATION")])
    result = asyncio.run(replay_case(case, catalog, spring_setup=truncated_history))
    answer = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert answer["outcome"] == "CLARIFICATION"
    assert NOTICES["CONVERSATION_LIMIT"] in answer["limitations"]
    assert result["queries"] == []


def test_display_filter_preserves_evidence_and_data_limit_notices():
    codes = ["CONVERSATION_LIMIT", "SOURCE_CHANGED", "PARTIAL_RESULTS", "CONTEXT_LIMIT"]
    expected = [NOTICES[c] for c in codes[1:]]
    assert notices(codes) == expected
    assert message("ANSWER", "확인했어요.", codes).result.limitations == expected
    assert (
        message("INSUFFICIENT_EVIDENCE", "확인이 필요해요.", codes).result.limitations == expected
    )
    assert message("CLARIFICATION", "대상을 알려주세요.", codes).result.limitations == [
        NOTICES[c] for c in codes
    ]
