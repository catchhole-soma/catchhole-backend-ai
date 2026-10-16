"""Mixed RDB/source answers preserve missing subject coverage without stopping search."""

import asyncio
import socket

import pytest

from app.chatbot.qa_render import notices
from tests.test_chat_hybrid import answer, replay, scenario


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Hybrid coverage tests must not use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def mixed_subjects():
    case = scenario("mixed_current_and_scene")
    case["question"] = "아리아와 루나의 현재 능력과 아리아가 반지를 버린 이유는?"
    case["outputs"][0]["subjects"].append({"domain": "CHARACTER", "name": "루나"})
    return case


@pytest.mark.parametrize("missing", ["empty_selection", "subset", "no_rows"])
def test_missing_rdb_coverage_remains_visible_after_source_answer(tmp_path, missing):
    case = mixed_subjects()
    setup = None
    if missing == "empty_selection":
        case["outputs"] = [
            case["outputs"][0],
            {"outcome": "INSUFFICIENT_EVIDENCE", "rowRefs": []},
            answer(),
        ]
    elif missing == "no_rows":
        case["outputs"] = [case["outputs"][0], answer()]

        def setup(spring):
            for entry in spring.catalog:
                entry["current"] = []

    result = asyncio.run(replay(tmp_path, case, spring_setup=setup))
    finish = result["finish"]
    assert finish["failure"] is None
    assert finish["result"]["outcome"] == "ANSWER"
    assert "강물" in finish["result"]["text"]
    assert "keyword" in result["search"].calls
    assert notices(["NO_RELATED_SETTING"])[0] in finish["result"]["limitations"]
    final_payload = result["raw"].requests[-1]["input"]
    assert notices(["NO_RELATED_SETTING"])[0] in final_payload["limitations"]


def test_all_selected_rdb_subjects_do_not_get_missing_coverage_notice(tmp_path):
    case = mixed_subjects()
    # Keep both direct quotes in this coverage fixture, including their source
    # rechecks and the two final current-subject checks. Budget limits are tested separately.
    case["maxToolCalls"] = 24
    case["outputs"][1]["rowRefs"] = ["R1", "R3"]
    case["outputs"][-1]["claims"][0]["evidenceRefs"] = ["E3"]
    result = asyncio.run(replay(tmp_path, case, config={"max_tool_calls": 24}))
    finish = result["finish"]
    assert finish["failure"] is None
    assert finish["result"]["outcome"] == "ANSWER"
    assert "아리아" in finish["result"]["text"] and "루나" in finish["result"]["text"]
    assert notices(["NO_RELATED_SETTING"])[0] not in finish["result"]["limitations"]
    current = [q for q in result["spring"].queries if q["tool"] == "CHARACTER_CURRENT"]
    assert len(current) == 4
    assert [q["targetId"] for q in current[:2]] == [q["targetId"] for q in current[2:]]
    assert all(q["size"] == 1 for q in current[2:])


def test_source_only_question_does_not_require_rdb_coverage(tmp_path):
    result = asyncio.run(replay(tmp_path, scenario("lexical_scene")))
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert notices(["NO_RELATED_SETTING"])[0] not in result["finish"]["result"]["limitations"]


def test_rdb_selection_clarification_still_stops_source_search(tmp_path):
    case = mixed_subjects()
    case["outputs"] = [
        case["outputs"][0],
        {"outcome": "CLARIFICATION", "rowRefs": []},
    ]
    result = asyncio.run(replay(tmp_path, case))
    assert result["finish"]["result"]["outcome"] == "CLARIFICATION"
    assert result["search"].calls == []
