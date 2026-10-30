"""Unread search candidates remain eligible; narrow scopes reuse the same budget."""

import asyncio
from dataclasses import replace
import socket

import pytest

from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import answer, assert_answer, claim, parts_case, search_with


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Candidate recovery tests cannot use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def test_vector_can_recover_a_keyword_hit_omitted_before_the_model_reads_it(tmp_path):
    case = parts_case()
    unrelated = passage("축제의 준비 기록입니다.", 101, index=0)
    useful_text = "봉화를 켠 뒤 미라는 기뻐했고, 그 경험 덕에 경비 일을 맡았습니다."
    useful = replace(passage(useful_text, 102, offset=1000, index=1), score=0.5)
    case["outputs"] += [
        answer(missing=("P1", "P2"), coverage="NONE"),
        answer(claim(useful_text, useful_text)),
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"per_episode": 1},
            search_setup=search_with(
                keyword=[unrelated, useful],
                vector=[replace(useful, methods=("VECTOR",))],
            ),
        )
    )
    response = assert_answer(result)
    assert result["search"].vector_exclusions == [(unrelated.chunk_id,)]
    assert useful_text not in str(result["raw"].requests[1]["input"]["passages"])
    assert useful_text in response["text"]
    assert response["retrievalSources"][0]["chunkId"] == str(useful.chunk_id)
    assert len(result["embedding"].inputs) == 1


@pytest.mark.parametrize(
    "source_range,expected_count", [((12, 12), 4), ((11, 12), 3), ((None, None), 3)]
)
def test_single_episode_can_supply_more_than_diversity_cap_without_more_total_evidence(
    tmp_path, source_range, expected_count
):
    case = parts_case()
    case["question"] = (
        "11~12화에서 미라가 첫 봉화를 켠 뒤 어떤 감정과 행동 변화가 있었어?"
        if source_range == (11, 12)
        else "12화에서 미라가 첫 봉화를 켠 뒤 어떤 감정과 행동 변화가 있었어?"
    )
    case["outputs"][0].update(episodeFrom=source_range[0], episodeTo=source_range[1])
    candidates = [
        passage(f"봉화의 합성 기록 {i}입니다.", 200 + i, episode=12, offset=i * 1000, index=i)
        for i in range(4)
    ]
    chosen = candidates[expected_count - 1]
    case["outputs"].append(answer(claim(chosen.text, chosen.text, ref=f"E{expected_count}")))
    # 회차 다양성 상한 예외의 산술은 예전 8개·회차당 3개 예산 기준이다.
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=candidates),
            hybrid={"max_rounds": 1, "max_evidence": 8, "per_episode": 3},
        )
    )
    assert_answer(result)
    passages = result["raw"].requests[1]["input"]["passages"]
    assert len(passages) == expected_count
    assert len(result["embedding"].inputs) == 0
    assert len(result["raw"].requests) == 2


def test_single_episode_still_obeys_total_reference_cap(tmp_path):
    case = parts_case()
    case["question"] = "12화에서 미라가 첫 봉화를 켠 뒤 어떤 감정과 행동 변화가 있었어?"
    case["outputs"][0].update(episodeFrom=12, episodeTo=12)
    candidates = [
        passage(f"봉화의 합성 기록 {i}입니다.", 300 + i, episode=12, offset=i * 1000, index=i)
        for i in range(4)
    ]
    case["outputs"].append(answer(claim(candidates[1].text, candidates[1].text, ref="E2")))
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_evidence": 2, "per_episode": 1},
            search_setup=search_with(keyword=candidates),
        )
    )
    assert_answer(result)
    assert len(result["raw"].requests[1]["input"]["passages"]) == 2
