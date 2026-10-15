"""Authored temporal/causal examples exercise transport, not model comprehension.

The scripted planner and answer supply the intended interpretation themselves.
Passing these replays does not prove that a live model distinguishes chronology,
feelings or causal attribution; those require inspection of real responses.
"""

import asyncio
import socket

import pytest

from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    answer,
    assert_answer,
    claim,
    parts_case,
    search_with,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Temporal replays cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


@pytest.mark.parametrize("causal_subject", ["그 경험", "그때 느낀 감정"])
def test_authored_temporal_answer_preserves_before_after_and_requested_causal_scope(
    tmp_path, causal_subject
):
    parts = [
        {"ref": "P1", "description": "첫 봉화를 켠 뒤 느낀 감정"},
        {"ref": "P2", "description": f"{causal_subject}이 이후 행동에 준 영향"},
    ]
    case = parts_case(parts=parts)
    case["question"] = (
        f"미라가 첫 봉화를 켠 뒤 어떤 감정을 느꼈고, {causal_subject}이 이후 행동에 어떤 영향을 줬어?"
    )
    before = "첫 봉화를 켜기 직전 미라는 잠시 망설였다."
    after = "불이 붙은 뒤에는 자신이 선택한 일을 받아들인다고 말했다."
    effect = "그 경험 때문에 미라는 다음 날부터 경비에 자원했다."
    source_text = f"{before} {after} {effect}"
    temporal_answer = (
        "점화 직전에는 잠시 망설였고, 직후에는 자신의 선택을 받아들인다고 말했습니다. "
        "직후 감정의 이름은 명시되지 않았습니다."
    )
    observation = claim(temporal_answer, f"{before} {after}", parts=("P1",))
    if causal_subject == "그 경험":
        case["outputs"].append(answer(observation, claim(effect, effect, parts=("P2",))))
    else:
        # An experience's consequence does not by itself establish that a
        # particular emotion caused the action. This is authored, not judged.
        case["outputs"] += [
            answer(observation, missing=("P2",)),
            answer(
                claim(temporal_answer, f"{before} {after}", ref="E2", parts=("P1",)),
                missing=("P2",),
            ),
        ]
    source = passage(source_text, 70, episode=11)
    unrelated = passage("다음 날 경비대는 창고를 정리했다.", 71, episode=12, method="VECTOR")

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(keyword=[source], vector=[unrelated]),
        )
    )

    response = assert_answer(result)
    answer_inputs = [
        request["input"] for request in result["raw"].requests if "passages" in request["input"]
    ]
    assert answer_inputs
    assert all(payload["plan"]["requestedParts"] == parts for payload in answer_inputs)
    assert temporal_answer in response["text"]
    assert [item["chunkId"] for item in response["retrievalSources"]] == [str(source.chunk_id)]
    if causal_subject == "그 경험":
        assert effect in response["text"]
        assert result["embedding"].inputs == []
    else:
        assert effect not in response["text"]
        assert len(result["embedding"].inputs) == 1
        assert parts[1]["description"] in result["embedding"].inputs[0][0]
        assert parts[1]["description"] in response["text"]
