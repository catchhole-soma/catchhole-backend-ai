"""Offline name-resolution regressions, independent of live model accuracy."""

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from app.chatbot.qa_lookup import FocusedLookup
from app.chatbot.qa_render import NOTICES
from app.chatbot.replay import replay_case
from app.schemas.chat_qa import QuestionSubject
from tests.test_chat_focused_lookup import answer, case, plan, tool_queries, uid
from tests.test_chat_subject_resolution import world, world_plan


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Subject category tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "category,suffix",
    [
        ("RACE", "종족"),
        ("FACTION", "세력"),
        ("LOCATION", "지역"),
        ("MONSTER", "몬스터"),
        ("POWER_SYSTEM", "능력 체계"),
        ("WORLD_RULE_HISTORY", "세계 규칙·역사"),
        ("IMPORTANT_ITEM", "주요 아이템"),
        ("IMPORTANT_ITEM", "주요 물건"),
    ],
)
async def test_complete_missing_name_retries_once_with_same_explicit_category(category, suffix):
    full_name = f"서리꽃 {suffix}"
    result = await replay_case(
        case(f"{full_name}의 특징은?", world_plan(category, name=full_name)),
        [world(1, category), world(2, "RACE" if category != "RACE" else "FACTION")],
    )

    assert answer(result)["outcome"] == "ANSWER"
    queries = tool_queries(result, "FIND_WORLD_SETTINGS")
    assert [q["query"] for q in queries] == [full_name, "서리꽃"]
    assert {q["worldCategory"] for q in queries} == {category}
    assert {q["targetId"] for q in tool_queries(result, "WORLD_CURRENT")} == {uid(1)}
    assert NOTICES["NO_RELATED_SETTING"] not in answer(result)["limitations"]


@pytest.mark.anyio
async def test_registered_name_that_includes_category_word_keeps_original_match():
    full_name = "서리꽃 종족"
    result = await replay_case(
        case(f"{full_name}의 특징은?", world_plan("RACE", name=full_name)),
        [world(1, "RACE", name=full_name, value="원래 이름의 값"), world(2, "RACE")],
    )

    assert "원래 이름의 값" in answer(result)["text"]
    assert [q["query"] for q in tool_queries(result, "FIND_WORLD_SETTINGS")] == [full_name]
    assert {q["targetId"] for q in tool_queries(result, "WORLD_CURRENT")} == {uid(1)}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "category,name",
    [
        (None, "서리꽃 종족"),
        ("FACTION", "서리꽃 종족"),
        ("RACE", "서리꽃종족"),
        ("RACE", "종족"),
        ("RACE", "종족 서리꽃"),
    ],
)
async def test_category_is_never_guessed_or_removed_inside_proper_name(category, name):
    result = await replay_case(
        case(f"{name}의 특징은?", world_plan(category, name=name)), [world(1, "RACE")]
    )
    assert answer(result)["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert len(result["queries"]) == 1


@pytest.mark.anyio
async def test_character_name_is_not_reinterpreted_as_a_world_category():
    lookup = SimpleNamespace(pages=AsyncMock(return_value=([], True)), codes=[], lookup_gaps=set())
    subject = QuestionSubject.model_validate(plan("서리꽃 종족")["subjects"][0])
    _, stop = await FocusedLookup.resolve_subject(lookup, subject)
    assert stop.result.outcome == "INSUFFICIENT_EVIDENCE"
    lookup.pages.assert_awaited_once_with(
        "FIND_CHARACTERS", query="서리꽃 종족", world_category=None
    )


@pytest.mark.anyio
async def test_empty_incomplete_first_search_cannot_start_suffix_fallback():
    lookup = SimpleNamespace(pages=AsyncMock(return_value=([], False)), codes=[], lookup_gaps=set())
    subject = QuestionSubject.model_validate(world_plan("RACE", name="서리꽃 종족")["subjects"][0])
    _, stop = await FocusedLookup.resolve_subject(lookup, subject)
    assert stop.result.outcome == "CLARIFICATION"
    assert lookup.pages.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [False, True])
async def test_ambiguous_original_name_is_not_retried(partial):
    full_name = "서리꽃 종족"
    result = await replay_case(
        case(f"{full_name}의 특징은?", world_plan("RACE", name=full_name)),
        [world(1, "RACE", name=full_name), world(2, "RACE", name=full_name)],
        config={"page_size": 1, "max_pages": 1} if partial else None,
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert {q["query"] for q in tool_queries(result, "FIND_WORLD_SETTINGS")} == {full_name}
    assert not tool_queries(result, "WORLD_CURRENT")


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [False, True])
async def test_suffix_fallback_preserves_ambiguity_and_page_completeness(partial):
    result = await replay_case(
        case("서리꽃 종족의 특징은?", world_plan("RACE", name="서리꽃 종족")),
        [world(1, "RACE"), world(2, "RACE")],
        config={"page_size": 1, "max_pages": 1} if partial else None,
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert [q["query"] for q in tool_queries(result, "FIND_WORLD_SETTINGS")] == [
        "서리꽃 종족",
        "서리꽃",
    ]
    assert not tool_queries(result, "WORLD_CURRENT")


@pytest.mark.anyio
async def test_missing_fallback_keeps_category_and_manuscript_missing_behavior():
    lookup = SimpleNamespace(pages=AsyncMock(return_value=([], True)), codes=[], lookup_gaps=set())
    subject = QuestionSubject.model_validate(world_plan("RACE", name="서리꽃 종족")["subjects"][0])
    resolved, stop = await FocusedLookup.resolve_subject(lookup, subject, allow_missing=True)
    assert resolved is None and stop is None
    assert lookup.codes == ["NO_RELATED_SETTING"]
    assert lookup.pages.await_args_list == [
        call("FIND_WORLD_SETTINGS", query="서리꽃 종족", world_category="RACE"),
        call("FIND_WORLD_SETTINGS", query="서리꽃", world_category="RACE"),
    ]


@pytest.mark.anyio
async def test_fallback_response_must_still_match_explicit_category():
    def corrupt_fallback(spring):
        original = spring.lookup

        def lookup(query):
            response = original(query)
            if query.tool.value == "FIND_WORLD_SETTINGS" and query.query == "서리꽃":
                response["items"]["content"][0]["category"] = "FACTION"
            return response

        spring.lookup = lookup

    result = await replay_case(
        case("서리꽃 종족의 특징은?", world_plan("RACE", name="서리꽃 종족")),
        [world(1, "RACE")],
        spring_setup=corrupt_fallback,
    )
    assert result["finish"]["failure"] == "NETWORK_FAILURE"
    assert not tool_queries(result, "WORLD_CURRENT")
