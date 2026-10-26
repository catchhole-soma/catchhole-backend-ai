"""A failed claim/expansion cannot erase independent verified evidence.

Synthetic examples exercise production grounding, deadlines, metering and final
source validation through the HTTP replay; no real manuscript or paid model.
"""

import asyncio
import socket

import pytest

from app.chatbot.hybrid import _HybridSession
from app.chatbot.qa import _Session
from app.chatbot.replay import ScriptedTextClient
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    BEHAVIOR,
    EMOTION,
    answer,
    assert_answer,
    claim,
    parts_case,
    search_with,
)
from tests.test_chat_response_recovery import truncated_provider


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Recovery regression must stay offline")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


@pytest.mark.parametrize("bad_quote", ["불이 켜졌다.", "원문에 없는 문장입니다."])
def test_invalid_claim_is_removed_whole_while_independent_claim_survives(tmp_path, bad_quote):
    case = parts_case()
    rejected = "검증에 실패한 감정 설명은 공개하지 않습니다."
    first = claim(rejected, bad_quote, parts=("P1",))
    # A valid support on the same claim cannot rescue its unsupported wording.
    first["supports"].append({"evidenceRef": "E1", "quote": BEHAVIOR})
    case["outputs"] += [answer(first, claim(BEHAVIOR, BEHAVIOR, parts=("P2",)))]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 2},
            search_setup=search_with(keyword=[passage(f"불이 켜졌다. 불이 켜졌다. {BEHAVIOR}", 1)]),
        )
    )
    response = assert_answer(result)
    assert BEHAVIOR in response["text"] and rejected not in response["text"]
    assert len(result["raw"].requests) == 2
    assert result["search"].calls[-1] == "revalidate"
    assert result["spring"].receipts[-1]["sources"] == response["retrievalSources"]


def test_overlapping_answered_and_missing_is_related_not_complete(tmp_path):
    case = parts_case()
    partial = claim(EMOTION, EMOTION, parts=("P1", "P2"))
    case["outputs"] += [answer(partial, missing=("P2",))]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 2},
            search_setup=search_with(keyword=[passage(EMOTION, 1)]),
        )
    )
    response = assert_answer(result)
    assert EMOTION in response["text"]
    # User-facing uncertainty remains for the original missing request.
    assert "이후 행동" in response["text"]
    assert len(result["raw"].requests) == 2


def test_dropped_claim_invalidates_shared_facet_until_new_evidence_fills_it(tmp_path):
    case = parts_case(parts=[{"ref": "P1", "description": "감정과 이후 행동의 연결"}])
    case["outputs"] += [
        answer(
            claim(EMOTION, EMOTION, parts=("P1",)),
            claim(BEHAVIOR, "찾을 수 없는 인용", parts=("P1",)),
        ),
        answer(claim(f"{EMOTION} {BEHAVIOR}", f"{EMOTION} {BEHAVIOR}", ref="E2", parts=("P1",))),
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(
                keyword=[passage(EMOTION, 1)],
                vector=[passage(f"{EMOTION} {BEHAVIOR}", 2, episode=24, method="VECTOR")],
            ),
        )
    )
    response = assert_answer(result)
    assert EMOTION in response["text"] and BEHAVIOR in response["text"]
    assert len(result["embedding"].inputs) == 1
    assert len(result["raw"].requests) == 3


@pytest.mark.parametrize("bad", ["all_quotes", "unknown_ref", "unknown_part"])
def test_recovery_never_accepts_unknown_references_or_all_invalid_claims(tmp_path, bad):
    case = parts_case()
    draft = answer(claim(EMOTION, "존재하지 않는 원문입니다."), claim(BEHAVIOR, BEHAVIOR))
    if bad == "all_quotes":
        draft["claims"][1]["supports"][0]["quote"] = "다른 없는 문장입니다."
    elif bad == "unknown_ref":
        draft["claims"][1]["evidenceRefs"] = ["E99"]
    else:
        draft["claims"][1]["answeredFacets"] = ["P99"]
    case["outputs"] += [draft, draft]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 3},
            search_setup=search_with(keyword=[passage(f"{EMOTION} {BEHAVIOR}", 1)]),
        )
    )
    assert len(result["raw"].requests) == 3
    assert result["finish"]["result"]["outcome"] != "ANSWER"
    assert BEHAVIOR not in result["finish"]["result"]["text"]


def partial_case():
    case = parts_case()
    case["outputs"] += [answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",))]
    return case


def sources():
    return search_with(
        keyword=[passage(EMOTION, 1)],
        vector=[passage(BEHAVIOR, 2, episode=24, method="VECTOR")],
    )


def test_observed_latency_stops_new_calls_without_erasing_partial_answer(tmp_path, monkeypatch):
    original = _HybridSession.record_model_response

    def record(self, purpose, response):
        original(self, purpose, response)
        if purpose == "CHAT_ANSWER":
            self.last_model_seconds = 100

    monkeypatch.setattr(_HybridSession, "record_model_response", record)
    result = asyncio.run(replay(tmp_path, partial_case(), search_setup=sources()))
    assert EMOTION in assert_answer(result)["text"]
    assert not result["embedding"].inputs
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2


@pytest.mark.parametrize("source_changed", [False, True])
def test_truncated_expansion_keeps_only_finally_validated_snapshot(
    tmp_path, monkeypatch, source_changed
):
    truncated_provider(monkeypatch, first=2, repeat=True)

    def change(index, spring, search):
        if index == 2:
            search.changed = source_changed

    result = asyncio.run(
        replay(
            tmp_path,
            partial_case(),
            search_setup=sources(),
            model_hook=change,
        )
    )
    assert len(result["raw"].requests) == 4
    assert [r["kind"] for r in result["spring"].reports] == ["SETTLE"] * 5
    assert result["search"].calls[-1] == "revalidate"
    if source_changed:
        assert EMOTION not in result["finish"]["result"]["text"]
        assert not result["finish"]["result"].get("retrievalSources")
    else:
        assert EMOTION in assert_answer(result)["text"]
        assert BEHAVIOR not in result["finish"]["result"]["text"]


def test_soft_timeout_keeps_snapshot_and_unknown_usage_for_cancelled_call(tmp_path, monkeypatch):
    original = ScriptedTextClient.create_text_response
    original_time = _Session.model_time_left

    async def slow(self, *args, **kwargs):
        if len(self.requests) >= 2:
            await asyncio.sleep(1)
            raise AssertionError("The soft deadline should cancel this call")
        return await original(self, *args, **kwargs)

    def time_left(self):
        return 0.02 if self.model_calls >= 3 else original_time(self)

    # Before the third invocation there is still budget; its provider wait then
    # reaches the soft deadline while the original server deadline remains open.
    monkeypatch.setattr(ScriptedTextClient, "create_text_response", slow)
    monkeypatch.setattr(_Session, "model_time_left", time_left)
    result = asyncio.run(replay(tmp_path, partial_case(), search_setup=sources()))
    assert EMOTION in assert_answer(result)["text"]
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 4
    unknown = [r for r in result["spring"].reports if r["kind"] == "UNKNOWN"]
    assert len(unknown) == 1
    assert unknown[0]["inputTokens"] is None and unknown[0]["outputTokens"] is None
    assert result["spring"].receipts
