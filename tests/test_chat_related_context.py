"""Authored related-context and recovery contracts, not live semantic accuracy.

All classifications and quotes below are synthetic scripted outputs. The tests
check whether coverage, references and bounded recovery preserve those decisions.
"""

import asyncio
import copy
import logging
import socket

import pytest

from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    BEHAVIOR,
    EMOTION,
    PARTS,
    answer,
    assert_answer,
    claim,
    parts_case,
    search_with,
)


RELATED = "첫 봉화를 켠 뒤 미라는 탑의 문을 닫았다."
UNSUPPORTED = "검증하지 못한 설명을 정답으로 표시했다."


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Related-context replays cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def related_claim(*, related=("P1",), answered=()):
    return {
        **claim(RELATED, RELATED, parts=answered),
        "relatedFacets": list(related),
    }


def test_supported_related_context_stays_partial_and_continues_search(tmp_path):
    case = parts_case(parts=[PARTS[0]])
    source = passage(RELATED, 151, episode=11)
    fresh = passage("그날 경비대는 장부를 정리했다.", 152, episode=12, method="VECTOR")
    case["outputs"] += [
        answer(related_claim(), missing=("P1",)),
        answer(missing=("P1",), coverage="NONE"),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[source], vector=[fresh]))
    )

    response = assert_answer(result)
    assert "vector" in result["search"].calls
    assert len(result["embedding"].inputs) == 1
    assert PARTS[0]["description"] in result["embedding"].inputs[0][0]
    assert RELATED in response["text"]
    assert PARTS[0]["description"] in response["text"]
    assert "확정하기 어려워요" in response["text"]
    assert [item["chunkId"] for item in response["retrievalSources"]] == [str(source.chunk_id)]


def test_related_context_alone_cannot_claim_complete_coverage(tmp_path):
    case = parts_case(parts=[PARTS[0]])
    case["outputs"] += [
        answer(related_claim(), coverage="COMPLETE"),
        answer(related_claim(), missing=("P1",)),
    ]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_rounds": 1},
            search_setup=search_with(keyword=[passage(RELATED, 153)]),
        )
    )

    response = assert_answer(result)
    assert len(result["raw"].requests) == 3
    assert "REQUESTED_PARTS_INCOMPLETE" in result["raw"].requests[-1]["system"]
    assert RELATED in response["text"]
    assert "확정하기 어려워요" in response["text"]


@pytest.mark.parametrize(
    ("answered", "related"),
    [
        ((), ("P2",)),
        (("P2",), ()),
        ((), ("P1", "P1")),
        (("P1", "P1"), ()),
        (("P1",), ("P1",)),
    ],
)
def test_invalid_related_or_answered_refs_are_rejected_before_publish(tmp_path, answered, related):
    case = parts_case(parts=[PARTS[0]])
    invalid = {
        **claim(UNSUPPORTED, EMOTION, parts=answered),
        "relatedFacets": list(related),
    }
    case["outputs"] += [
        answer(invalid, missing=() if answered else ("P1",)),
        answer(claim(EMOTION, EMOTION, parts=("P1",))),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[passage(EMOTION, 154)]))
    )

    response = assert_answer(result)
    assert len(result["raw"].requests) == len(result["spring"].reservations) == 3
    assert "이전 출력이 계약 검증에 실패했습니다." in result["raw"].requests[-1]["system"]
    assert UNSUPPORTED not in response["text"]
    assert EMOTION in response["text"]
    assert result["embedding"].inputs == []


def test_second_search_validation_exhaustion_preserves_previous_valid_partial(tmp_path):
    case = parts_case()
    source = passage(EMOTION, 155, episode=11)
    fresh = passage(BEHAVIOR, 156, episode=12, method="VECTOR")
    invalid = answer(claim(UNSUPPORTED, "이번 입력에 없는 지원 구절입니다."))
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",)),
        invalid,
        copy.deepcopy(invalid),
    ]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[source], vector=[fresh]))
    )

    response = assert_answer(result)
    assert len(result["raw"].requests) == 4
    assert len(result["embedding"].inputs) == 1
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 5
    assert EMOTION in response["text"]
    assert PARTS[1]["description"] in response["text"]
    assert UNSUPPORTED not in response["text"]
    assert [item["chunkId"] for item in response["retrievalSources"]] == [str(source.chunk_id)]
    assert response["retrievalSources"][0]["quote"] == source.text
    assert [item["chunkId"] for item in result["spring"].receipts[-1]["sources"]] == [
        str(source.chunk_id)
    ]


def test_one_remaining_text_call_rejects_bad_quote_then_finishes_safely(tmp_path, caplog):
    case = parts_case(parts=[PARTS[0]])
    case["outputs"].append(
        answer(claim(UNSUPPORTED, "이번 입력에 없는 지원 구절입니다.", parts=("P1",)))
    )

    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = asyncio.run(
            replay(
                tmp_path,
                case,
                hybrid={"max_model_calls": 2},
                search_setup=search_with(keyword=[passage(EMOTION, 157)]),
            )
        )

    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert UNSUPPORTED not in response["text"]
    assert response.get("retrievalSources", []) == []
    assert len(result["raw"].requests) == len(result["spring"].reservations) == 2
    assert len(result["spring"].reports) == 2
    assert result["embedding"].inputs == []
    records = [record for record in caplog.records if record.name == "app.chatbot.qa"]
    assert len(records) == 1
    assert records[0].validation_errors == [{"code": "GROUNDING_QUOTE_NOT_FOUND", "path": "$"}]
    assert records[0].max_attempts == 1
