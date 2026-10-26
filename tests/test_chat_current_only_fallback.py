"""Offline regressions for current values retained without a source answer."""

import asyncio
import copy
import socket

import pytest

from app.chatbot.hybrid import _HybridSession
from app.chatbot.qa_render import NOTICES
from tests.test_chat_current_authority import current_case, current_catalog, missing_catalog
from tests.test_chat_hybrid import CATALOG, replay
from tests.test_chat_requested_parts import answer


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Current-only fallback tests cannot use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def tool_calls(result, tool):
    return [q for q in result["spring"].queries if q["tool"] == tool]


def assert_current_only(result):
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "ANSWER"
    assert "현재 성별은 여자" in response["text"]
    assert "원문 내용은 이번 조회에서 확인하지 못" in response["text"]
    assert "근거 " not in response["text"]
    assert not response["sources"] and not response.get("retrievalSources")


def assert_current_changed(result):
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert NOTICES["CURRENT_CHANGED"] in response["limitations"]
    assert "여자" not in response["text"]
    assert not response["sources"] and not response.get("retrievalSources")


@pytest.mark.parametrize("source_changed", [False, True])
@pytest.mark.parametrize("current_changed", [False, True])
def test_current_only_drops_unrevalidated_direct_sources(
    tmp_path, source_changed, current_changed
):
    case = current_case(mixed=True)
    case["outputs"].append(answer(missing=("P1",), coverage="NONE"))

    def setup(spring):
        current_catalog(spring)
        row = spring.catalog[0]["current"][0]
        evidence = copy.deepcopy(CATALOG["catalogs"]["base"][0]["evidence"][0])
        target_id, content = evidence["id"], evidence["evidence"]
        evidence.update(row)
        evidence.update(
            kind="EVIDENCE",
            id=target_id,
            evidence=content,
            contributesToCurrent=True,
            historyOnly=False,
        )
        row["sources"] = [{"tool": "CHARACTER_EVIDENCE", "targetId": target_id}]
        spring.catalog[0]["evidence"] = [evidence]

    def mutate(count, spring, search):
        if count == 2:
            if source_changed:
                spring.catalog[0]["evidence"][0]["evidence"].update(
                    quotes=[], matchesCurrentSource=False
                )
            if current_changed:
                spring.catalog[0]["current"][0].update(value="남자", settingVersion=2)

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            model_hook=mutate,
            hybrid={"max_rounds": 1},
        )
    )
    assert len(result["raw"].requests) == 2
    assert len(tool_calls(result, "CHARACTER_CURRENT")) == 2
    assert len(tool_calls(result, "CHARACTER_EVIDENCE")) == 1
    assert not result["embedding"].inputs
    if current_changed:
        assert_current_changed(result)
    else:
        assert_current_only(result)


@pytest.mark.parametrize("mutation", [None, "version", "deleted"])
def test_time_gate_preserves_current_only_after_revalidation(tmp_path, monkeypatch, mutation):
    holder = {}

    def setup(spring):
        current_catalog(spring)
        holder["spring"] = spring

    collect = _HybridSession.collect_rows

    async def slow_collection(self, plan):
        stop = await collect(self, plan)
        # Simulate an observed model delay exceeding the remaining model time,
        # without sleeping or bypassing the real deadline predicate.
        self.last_model_seconds = self.model_time_left() + 1
        if mutation == "version":
            holder["spring"].catalog[0]["current"][0].update(value="남자", settingVersion=2)
        elif mutation == "deleted":
            holder["spring"].catalog.clear()
        return stop

    monkeypatch.setattr(_HybridSession, "collect_rows", slow_collection)
    result = asyncio.run(replay(tmp_path, current_case(mixed=True), spring_setup=setup))
    assert len(result["raw"].requests) == 1
    assert len(tool_calls(result, "CHARACTER_CURRENT")) == 2
    assert not result["search"].calls and not result["embedding"].inputs
    assert not tool_calls(result, "CHARACTER_EVIDENCE")
    if mutation:
        assert_current_changed(result)
    else:
        assert_current_only(result)


@pytest.mark.parametrize("source_only", [False, True])
def test_time_gate_without_current_rows_keeps_insufficient_result(
    tmp_path, monkeypatch, source_only
):
    case = current_case(mixed=True, required=not source_only)
    if source_only:
        case["outputs"][0]["rdbMode"] = "NONE"
    collect = _HybridSession.collect_rows

    async def slow_collection(self, plan):
        stop = await collect(self, plan)
        self.last_model_seconds = self.model_time_left() + 1
        return stop

    monkeypatch.setattr(_HybridSession, "collect_rows", slow_collection)
    result = asyncio.run(replay(tmp_path, case, spring_setup=missing_catalog))
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "여자" not in response["text"] and "다른 이름" not in response["text"]
    assert not response["sources"] and not response.get("retrievalSources")
    assert len(result["raw"].requests) == 1
    assert not tool_calls(result, "CHARACTER_CURRENT")
    assert not result["search"].calls and not result["embedding"].inputs


@pytest.mark.parametrize("verdict", ["MATCH", "UNKNOWN"])
def test_time_gate_does_not_promote_unknown_current_conditions(tmp_path, monkeypatch, verdict):
    case = current_case(mixed=True)
    case["question"] = "현재 여자 인물과 원문을 알려줘."
    case["outputs"][0].update(
        subjects=[],
        searchTerms=["여자"],
        collection={
            "domain": "CHARACTER",
            "factType": "PROFILE",
            "worldCategory": None,
            "attribute": None,
            "predicate": "여자",
        },
        requestedParts=[{"ref": "P1", "description": "원문 내용"}],
    )
    case["outputs"].append(
        {
            "decisions": [
                {
                    "itemRef": "I1",
                    "verdict": verdict,
                    "rowRefs": ["R1"],
                    "explanation": "현재 성별 설정을 확인했습니다.",
                }
            ]
        }
    )
    collect = _HybridSession.collect_rows

    async def slow_collection(self, plan):
        stop = await collect(self, plan)
        self.last_model_seconds = self.model_time_left() + 1
        return stop

    monkeypatch.setattr(_HybridSession, "collect_rows", slow_collection)
    result = asyncio.run(replay(tmp_path, case, spring_setup=current_catalog))
    assert result["finish"]["failure"] is None
    assert len(result["raw"].requests) == 2
    assert not result["search"].calls and not result["embedding"].inputs
    if verdict == "MATCH":
        assert_current_only(result)
        assert len(tool_calls(result, "CHARACTER_CURRENT_COLLECTION")) == 2
    else:
        response = result["finish"]["result"]
        assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert NOTICES["CONDITION_UNCERTAIN"] in response["limitations"]
        assert "여자" not in response["text"] and "세린" not in response["text"]
        assert not response["sources"] and not response.get("retrievalSources")
