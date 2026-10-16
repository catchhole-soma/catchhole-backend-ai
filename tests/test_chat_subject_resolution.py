"""Offline query routing regressions; scripted plans do not measure LLM accuracy."""

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.chatbot.qa import response_schema
from app.chatbot.qa_lookup import FocusedLookup
from app.chatbot.replay import replay_case
from app.schemas.chat_execution import ConversationTurn
from app.schemas.chat_qa import QuestionPlan, QuestionSubject
from tests.test_chat_focused_lookup import (
    answer,
    case,
    catalog_subject,
    item,
    plan,
    tool_queries,
    uid,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Subject resolution tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def world(number, category, *, name="서리꽃", value="저온에 강함"):
    entry = catalog_subject(
        number,
        name,
        domain="WORLD",
        current=[item(number, name, category=category, key="특성", label="특성", value=value)],
    )
    entry["subject"]["category"] = category
    return entry


def world_plan(category, *, name="서리꽃"):
    decision = plan(name, domain="WORLD", fact_type=None)
    decision["subjects"][0]["worldCategory"] = category
    return decision


@pytest.mark.anyio
async def test_named_world_category_filters_before_subject_ambiguity():
    catalog = [world(1, "RACE"), world(2, "FACTION", value="상단"), world(3, "IMPORTANT_ITEM")]
    result = await replay_case(case("서리꽃 종족의 특성", world_plan("RACE")), catalog)
    assert answer(result)["outcome"] == "ANSWER"
    assert "저온에 강함" in answer(result)["text"] and "상단" not in answer(result)["text"]
    assert tool_queries(result, "FIND_WORLD_SETTINGS")[0]["worldCategory"] == "RACE"
    assert {q["targetId"] for q in tool_queries(result, "WORLD_CURRENT")} == {uid(1)}


@pytest.mark.anyio
async def test_missing_world_category_keeps_categories_in_clarification_candidates():
    result = await replay_case(
        case("서리꽃의 특성", world_plan(None)), [world(1, "RACE"), world(2, "FACTION")]
    )
    data = answer(result)
    assert data["outcome"] == "CLARIFICATION"
    assert "서리꽃 (종족)" in data["text"] and "서리꽃 (세력)" in data["text"]
    assert not tool_queries(result, "WORLD_CURRENT")


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [False, True])
async def test_category_does_not_make_multiple_or_unread_subjects_unique(partial):
    result = await replay_case(
        case("서리꽃 종족 특성", world_plan("RACE")),
        [world(1, "RACE"), world(2, "RACE")],
        config={"page_size": 1, "max_pages": 1} if partial else None,
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert not tool_queries(result, "WORLD_CURRENT")


@pytest.mark.anyio
async def test_filtered_search_contract_cannot_return_another_category():
    def corrupt_category(spring):
        lookup = spring.lookup

        def changed(query):
            response = lookup(query)
            if query.tool.value == "FIND_WORLD_SETTINGS":
                response["items"]["content"][0]["category"] = "FACTION"
            return response

        spring.lookup = changed

    result = await replay_case(
        case("서리꽃 종족 특성", world_plan("RACE")),
        [world(1, "RACE")],
        spring_setup=corrupt_category,
    )
    assert result["finish"]["failure"] == "NETWORK_FAILURE"
    assert not tool_queries(result, "WORLD_CURRENT")


@pytest.mark.anyio
async def test_category_clarification_keeps_original_subject_and_reads_current_database():
    def history(spring):
        spring.context = spring.context.model_copy(
            update={
                "conversation": [
                    ConversationTurn(
                        question="서리꽃의 특징을 알려줘",
                        answer="종족과 세력 중 어떤 대상을 말하나요? 이전에 알려진 특성은 오래된 값입니다.",
                    )
                ]
            }
        )

    result = await replay_case(
        case("종족을 말한 거야", world_plan("RACE")),
        [world(1, "RACE", value="새로 수정된 특성"), world(2, "FACTION", value="다른 대상")],
        spring_setup=history,
    )
    assert "새로 수정된 특성" in answer(result)["text"]
    assert "오래된 값" not in answer(result)["text"]
    assert tool_queries(result, "FIND_WORLD_SETTINGS")[0]["worldCategory"] == "RACE"
    assert (
        result["modelRequests"][0]["input"]["conversation"][0]["question"]
        == "서리꽃의 특징을 알려줘"
    )


@pytest.mark.anyio
async def test_current_category_correction_is_not_overridden_by_old_conversation():
    def history(spring):
        spring.context = spring.context.model_copy(
            update={
                "conversation": [
                    ConversationTurn(question="서리꽃 종족의 특징은?", answer="저온에 강해요.")
                ]
            }
        )

    result = await replay_case(
        case("이번엔 서리꽃이라는 세력의 특징", world_plan("FACTION")),
        [world(1, "RACE"), world(2, "FACTION", value="상단 연합")],
        spring_setup=history,
    )
    assert "상단 연합" in answer(result)["text"]
    assert {q["targetId"] for q in tool_queries(result, "WORLD_CURRENT")} == {uid(2)}


@pytest.mark.anyio
async def test_no_category_match_does_not_retry_as_unfiltered_subject():
    result = await replay_case(case("서리꽃 종족 특성", world_plan("RACE")), [world(1, "FACTION")])
    assert answer(result)["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert len(result["queries"]) == 1
    assert result["queries"][0]["worldCategory"] == "RACE"


@pytest.mark.anyio
async def test_manuscript_caller_can_continue_after_missing_registered_subject():
    lookup = SimpleNamespace(pages=AsyncMock(return_value=([], True)), codes=[], lookup_gaps=set())
    subject = QuestionSubject.model_validate(world_plan("RACE")["subjects"][0])
    resolved, stop = await FocusedLookup.resolve_subject(lookup, subject, allow_missing=True)
    assert resolved is None and stop is None
    assert lookup.codes == ["NO_RELATED_SETTING"]
    lookup.pages.assert_awaited_once_with(
        "FIND_WORLD_SETTINGS", query="서리꽃", world_category="RACE"
    )


def test_subject_category_is_nullable_but_not_a_character_or_authority_selector():
    old = QuestionSubject(domain="WORLD", name="서리꽃")
    assert old.world_category is None
    schema = response_schema(QuestionSubject).schema
    assert "worldCategory" in schema["required"]
    for values in [
        {"domain": "CHARACTER", "name": "서리꽃", "worldCategory": "RACE"},
        {"domain": "WORLD", "name": "서리꽃", "worldCategory": "CUSTOM"},
        {"domain": "WORLD", "name": "서리꽃", "worldCategory": "RACE", "workId": "other"},
    ]:
        with pytest.raises(ValidationError):
            QuestionSubject.model_validate(values)


def test_same_world_name_in_different_explicit_categories_is_not_a_duplicate_subject():
    decision = world_plan("RACE")
    decision["subjects"].append(world_plan("FACTION")["subjects"][0])
    assert len(QuestionPlan.model_validate(decision).subjects) == 2
    decision["subjects"][1]["worldCategory"] = "RACE"
    with pytest.raises(ValidationError):
        QuestionPlan.model_validate(decision)
