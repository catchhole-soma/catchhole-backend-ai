"""Authored strict planner outputs exercise routing; no live-model quality claims."""

import asyncio
import socket

import pytest

from app.chatbot.qa import response_schema
from app.schemas.chat_hybrid import HybridPlan
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import answer, claim, search_with


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Terminal planner tests cannot use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def strict_plan(disposition="CLARIFICATION", route="none"):
    value = {
        "disposition": disposition,
        "registeredCurrentRequired": False,
        "rdbMode": "NONE",
        "sourceNeed": "NONE",
        "subjects": [],
        "searchTerms": [],
        "includeEvidence": False,
        "episodeFrom": None,
        "episodeTo": None,
    }
    if route == "legacy_filter":
        value.update(
            registeredCurrentRequired=False,
            rdbMode="CHARACTER_LIST",
            characterFilter={"factType": "STATUS", "attribute": "상태", "value": "다친"},
        )
    elif route == "collection":
        value.update(
            registeredCurrentRequired=False,
            rdbMode="CURRENT",
            answerShape="LIST",
            collection={
                "domain": "CHARACTER",
                "factType": "STATUS",
                "worldCategory": None,
                "attribute": None,
                "predicate": "다친",
            },
        )
    full = HybridPlan.model_validate(value).model_dump(by_alias=True, mode="json")
    # Include modern required defaults too, rather than relying on legacy omission.
    assert set(full) == set(response_schema(HybridPlan).schema["required"])
    return full


def execution_case(*outputs):
    spec = scenario()
    spec["question"] = "다친 상태인 인물은 누구야?"
    spec["outputs"] = list(outputs)
    return spec


def assert_terminal(result, disposition, *, calls=1):
    finish = result["finish"]
    assert finish["failure"] is None, finish
    outcome = "CLARIFICATION" if disposition == "CLARIFICATION" else "INSUFFICIENT_EVIDENCE"
    assert finish["result"]["outcome"] == outcome
    expected = {
        "CLARIFICATION": "어떤 인물이나 설정",
        "PAST_STATE": "그 회차 당시의 상태를 복원할 수 없습니다",
        "UNSUPPORTED": "읽기 전용 조회 범위를 벗어납니다",
    }
    assert expected[disposition] in finish["result"]["text"]
    assert not finish["result"]["sources"] and not finish["result"].get("retrievalSources")
    assert not result["spring"].queries
    assert not result["search"].calls
    assert not result["embedding"].inputs
    assert not result["spring"].receipts
    assert len(result["raw"].requests) == calls
    assert len(result["spring"].reservations) == len(result["spring"].reports) == calls
    assert all(r["purpose"] == "CHAT_INTERPRETATION" for r in result["spring"].reservations)


@pytest.mark.parametrize("disposition", ["CLARIFICATION", "UNSUPPORTED", "PAST_STATE"])
@pytest.mark.parametrize("route", ["legacy_filter", "collection", "none"])
def test_terminal_plan_does_not_convert_unused_filters_into_provider_failure(
    tmp_path, disposition, route
):
    spec = execution_case(strict_plan(disposition, route))
    result = asyncio.run(replay(tmp_path, spec))
    assert_terminal(result, disposition)


@pytest.mark.parametrize("disposition", ["CLARIFICATION", "UNSUPPORTED", "PAST_STATE"])
def test_terminal_collection_does_not_execute_tentative_source_search(tmp_path, disposition):
    plan = strict_plan(disposition, "collection")
    plan.update(
        sourceNeed="SCENE",
        searchTerms=["다친"],
        requestedParts=[{"ref": "P1", "description": "다친 인물의 상태"}],
        includeEvidence=True,
    )
    HybridPlan.model_validate(plan)
    result = asyncio.run(replay(tmp_path, execution_case(plan)))
    assert_terminal(result, disposition)


def setup_catalog(spring):
    spring.catalog = [
        catalog_subject(
            number,
            name,
            current=[
                item(
                    number,
                    name,
                    category="STATUS",
                    key="status.injury",
                    label="상태",
                    value=value,
                    structured=value,
                )
            ],
        )
        for number, name, value in [(701, "세린", "다친"), (702, "루온", "회복한")]
    ]


@pytest.mark.parametrize("route", ["legacy_filter", "collection"])
@pytest.mark.parametrize("required_current", [False, True])
def test_proceed_preserves_valid_filters_and_current_collection_queries(
    tmp_path, route, required_current
):
    plan = strict_plan("PROCEED", route)
    plan["registeredCurrentRequired"] = required_current
    outputs = [plan]
    if route == "collection":
        outputs.append(
            {
                "decisions": [
                    {
                        "itemRef": "I1",
                        "verdict": "MATCH",
                        "rowRefs": ["R1"],
                        "explanation": "다친 상태입니다.",
                    },
                    {
                        "itemRef": "I2",
                        "verdict": "NO_MATCH",
                        "rowRefs": ["R2"],
                        "explanation": "회복했습니다.",
                    },
                ]
            }
        )
    result = asyncio.run(replay(tmp_path, execution_case(*outputs), spring_setup=setup_catalog))
    finish = result["finish"]
    assert finish["failure"] is None, finish
    assert finish["result"]["outcome"] == "ANSWER"
    assert "세린" in finish["result"]["text"] and "루온" not in finish["result"]["text"]
    queries = result["spring"].queries
    if route == "legacy_filter":
        assert [q["tool"] for q in queries] == ["CHARACTER_FIELDS", "FIND_CHARACTERS"]
        assert queries[-1]["settingKey"] == "status.injury"
        assert queries[-1]["valueEquals"] == "다친"
    else:
        assert [q["tool"] for q in queries] == ["CHARACTER_CURRENT_COLLECTION"] * 2
        assert queries[0]["factType"] == "STATUS"
        assert queries[-1]["readToken"] and queries[-1]["size"] == 1
    assert len(result["raw"].requests) == len(outputs)
    assert not result["search"].calls


@pytest.mark.parametrize("invalid", ["missing_filter", "wrong_collection_mode", "missing_subject"])
def test_invalid_proceed_plan_still_uses_planning_contract_repair(tmp_path, invalid):
    if invalid == "missing_filter":
        bad = strict_plan("PROCEED", "legacy_filter")
        bad["characterFilter"] = None
    else:
        bad = strict_plan("PROCEED", "collection")
        if invalid == "wrong_collection_mode":
            bad["rdbMode"] = "HISTORY"
        else:
            bad["collection"] = None
    repaired = strict_plan("CLARIFICATION")
    result = asyncio.run(replay(tmp_path, execution_case(bad, repaired)))
    assert_terminal(result, "CLARIFICATION", calls=2)
    assert "안전한 검증 피드백" in result["raw"].requests[1]["system"]


@pytest.mark.parametrize("disposition", ["CLARIFICATION", "GREETING", "THANKS", "HELP"])
def test_terminal_normalization_does_not_bypass_hybrid_schema_validation(tmp_path, disposition):
    bad = strict_plan("CLARIFICATION", "legacy_filter")
    bad.update(disposition=disposition, rdbMode="NONE")
    result = asyncio.run(replay(tmp_path, execution_case(bad, strict_plan("CLARIFICATION"))))
    assert_terminal(result, "CLARIFICATION", calls=2)


def test_episode_end_events_can_answer_completed_and_ongoing_parts_from_source(tmp_path):
    plan = strict_plan("PROCEED", "collection")
    plan.update(
        rdbMode="NONE",
        collection=None,
        answerShape="FACTS",
        sourceNeed="SCENE",
        searchTerms=["순찰", "수색"],
        includeEvidence=True,
        episodeFrom=12,
        episodeTo=12,
        eventContext="해당 원문 끝에서 순찰과 수색의 진행 상황",
        requestedParts=[
            {"ref": "P1", "description": "완료한 일"},
            {"ref": "P2", "description": "진행 중인 일"},
        ],
    )
    completed, ongoing = "미라는 성문 순찰을 마쳤다.", "실종자 수색은 아직 진행 중이었다."
    result = asyncio.run(
        replay(
            tmp_path,
            {
                **execution_case(
                    plan,
                    answer(
                        claim(completed, completed, parts=("P1",)),
                        claim(ongoing, ongoing, parts=("P2",)),
                    ),
                ),
                "question": "12화 원문 끝에서 순찰과 수색 중 완료한 일과 진행 중인 일을 구분해줘.",
            },
            search_setup=search_with(keyword=[passage(f"{completed} {ongoing}", 1, episode=12)]),
        )
    )
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert (
        completed in result["finish"]["result"]["text"]
        and ongoing in result["finish"]["result"]["text"]
    )
    assert not result["spring"].queries
    assert result["search"].calls
    assert result["raw"].requests[-1]["input"]["plan"]["requestedParts"] == plan["requestedParts"]


def test_historical_registered_snapshot_remains_unsupported_without_source_claims(tmp_path):
    case = execution_case(strict_plan("PAST_STATE"))
    case["question"] = "그 시점의 등록 설정 전체를 복원해줘."
    assert_terminal(asyncio.run(replay(tmp_path, case)), "PAST_STATE")
