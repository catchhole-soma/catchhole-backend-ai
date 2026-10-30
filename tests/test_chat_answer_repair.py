"""합성 회귀: PARTIAL 답변의 빠진 요구를 같은 근거로 다시 묻는 보완 호출.

제공자·DB·원고 없이 호출 순서·입력 계약·유지 규칙만 검사한다. 실제 모델 정답률 측정이 아니다.
"""

import asyncio
import socket

import pytest

from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    BEHAVIOR,
    EMOTION,
    answer,
    claim,
    parts_case,
    search_with,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("이 회귀는 외부 서비스에 접근하지 않는다")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def scene():
    return [
        passage(EMOTION, 201, episode=12, offset=0, index=0),
        passage(BEHAVIOR, 202, episode=12, offset=1000, index=1),
    ]


def test_repair_call_asks_only_for_missing_parts_with_the_same_evidence(tmp_path):
    case = parts_case()
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",)),
        answer(
            claim(EMOTION, EMOTION, parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)),
        ),
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=scene()),
            hybrid={"repair_missing_parts": True},
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert EMOTION in finished["text"] and BEHAVIOR in finished["text"]
    # 계획 → 답변(PARTIAL) → 보완 호출. 새 검색·임베딩 없이 끝난다.
    assert len(result["raw"].requests) == 3
    assert "vector" not in result["search"].calls and len(result["embedding"].inputs) == 0
    repair = result["raw"].requests[2]["input"]["repair"]
    assert repair["missingParts"] == ["P2"]
    assert repair["previousClaims"] == [
        {"text": EMOTION, "evidenceRefs": ["E1"], "answeredFacets": ["P1"]}
    ]
    assert "repair" not in result["raw"].requests[1]["input"]
    assert [p["text"] for p in result["raw"].requests[2]["input"]["passages"]] == [
        p["text"] for p in result["raw"].requests[1]["input"]["passages"]
    ]


def test_repair_runs_once_and_search_continues_when_parts_stay_missing(tmp_path):
    case = parts_case()
    case["vector"] = []
    partial = answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",))
    case["outputs"] += [
        partial,
        partial,
        answer(
            claim(EMOTION, EMOTION, parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)),
        ),
    ]
    later = passage(BEHAVIOR, 303, episode=13, method="VECTOR")
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=[scene()[0]], vector=[later]),
            hybrid={"repair_missing_parts": True},
        )
    )
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    requests = result["raw"].requests
    # 계획, 1라운드 답, 보완 호출, 2라운드 답. 보완은 한 번만이다.
    assert len(requests) == 4
    assert "repair" in requests[2]["input"] and "repair" not in requests[3]["input"]
    assert result["search"].calls.count("vector") == 1 and len(result["embedding"].inputs) == 1


def test_repair_that_drops_an_answered_part_is_rejected(tmp_path):
    case = parts_case()
    case["vector"] = []
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",)),
        # 보완 응답이 이미 답한 P1을 버리고 P2만 답하면 원래 답을 유지한다.
        answer(claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)), missing=("P1",)),
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=scene()),
            hybrid={"max_rounds": 1, "repair_missing_parts": True},
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert EMOTION in finished["text"] and BEHAVIOR not in finished["text"]
    assert len(result["raw"].requests) == 3
