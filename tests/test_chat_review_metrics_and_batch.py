"""Offline review regressions through the real hybrid runtime and call ledger.

Scripted judgments verify grouping, budget boundaries and accounting, not model
accuracy. Only synthetic subjects and values are used; external sockets fail.
"""

import asyncio
import json
import socket
from collections import Counter

import pytest

from app.chatbot.hybrid_prompts import PLAN_VERSION
from app.chatbot.qa_prompts import FIELD_VERSION
from app.chatbot.qa_render import NOTICES
from app.chatbot.replay import ScriptedTextClient
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Review regressions must not call an external service")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def metric_counts(result, name):
    return Counter(
        {
            sample.labels["purpose"]: sample.value
            for metric in result["metrics"].registry.collect()
            for sample in metric.samples
            if sample.name == name
        }
    )


@pytest.mark.parametrize("retry_stage", [None, "plan", "field", "answer"])
def test_field_selection_and_retries_match_the_reserved_call_purposes(tmp_path, retry_stage):
    spec = scenario()
    spec["question"] = "술법의 소모 마력을 설명해 줘."
    decision = spec["outputs"][0]
    decision.update(
        registeredCurrentRequired=False,
        rdbMode="CURRENT",
        sourceNeed="NONE",
        subjects=[
            {
                "domain": "WORLD",
                "name": "술법",
                "focus": {"factType": None, "attribute": "소모 마력"},
            }
        ],
        searchTerms=[],
        includeEvidence=False,
        answerShape="SUMMARY",
    )
    stages = [
        ("plan", decision, PLAN_VERSION, "CHAT_INTERPRETATION"),
        ("field", {"outcome": "ANSWER", "fieldRefs": ["F1"]}, FIELD_VERSION, "CHAT_INTERPRETATION"),
        (
            "answer",
            {"claims": [{"text": "소모 마력은 30이에요.", "rowRefs": ["R1"]}]},
            "chat-registered-answer-v4",
            "CHAT_ANSWER",
        ),
    ]
    outputs, versions, purposes = [], [], []
    for stage, output, version, purpose in stages:
        if stage == retry_stage:
            outputs.append({"unexpected": "invalid structured output"})
            versions.append(version)
            purposes.append(purpose)
        outputs.append(output)
        versions.append(version)
        purposes.append(purpose)

    def setup(spring):
        spring.catalog = [
            catalog_subject(
                701,
                "술법",
                domain="WORLD",
                current=[
                    item(
                        701,
                        "술법",
                        category="POWER_SYSTEM",
                        key="mana",
                        label="소모 마력",
                        value="30",
                    ),
                    item(
                        701,
                        "술법",
                        category="POWER_SYSTEM",
                        key="recovery",
                        label="회복 마력",
                        value="5",
                    ),
                ],
            )
        ]

    result = asyncio.run(replay(tmp_path, spec, outputs=outputs, spring_setup=setup))
    assert result["finish"]["failure"] is None
    assert "소모 마력은 30" in result["finish"]["result"]["text"]
    assert [request["prompt_cache_key"] for request in result["raw"].requests] == versions
    assert [call["purpose"] for call in result["spring"].reservations] == purposes
    for name in ("chat_retrieval_model_calls_total", "chat_retrieval_model_seconds_count"):
        assert metric_counts(result, name) == Counter(purposes)
    assert len(result["spring"].reports) == len(purposes)
    assert not result["search"].calls and not result["embedding"].inputs


def count_case(subjects):
    spec = scenario()
    spec["question"] = "현재 사망한 인물은 몇 명이야?"
    spec["outputs"][0].update(
        registeredCurrentRequired=False,
        rdbMode="CURRENT",
        sourceNeed="NONE",
        subjects=[],
        searchTerms=[],
        includeEvidence=False,
        answerShape="COUNT",
        collection={
            "domain": "CHARACTER",
            "factType": "STATUS",
            "worldCategory": None,
            "attribute": None,
            "predicate": "사망한",
        },
    )
    spec["outputs"] = spec["outputs"][:1]

    def setup(spring):
        spring.catalog = [
            catalog_subject(
                700 + n,
                f"합성{n:03}",
                current=[
                    item(
                        700 + n,
                        f"합성{n:03}",
                        category="STATUS",
                        key="state",
                        label="상태",
                        value=value,
                    )
                ],
            )
            for n, value in enumerate(subjects, 1)
        ]

    return spec, setup


def script_condition_judgments(monkeypatch, *, retry_first=False):
    """Return one predetermined MATCH for each observed synthetic subject.

    The provider stub does not choose the batch size or change input values.
    That decision remains with the product orchestrator under test.
    """
    original = ScriptedTextClient.create_text_response
    first = True

    async def respond(self, system_prompt, user_prompt, **kwargs):
        nonlocal first
        if kwargs.get("prompt_cache_key") == "chat-current-condition-v2":
            payload = json.loads(user_prompt)
            response = {
                "decisions": [
                    {
                        "itemRef": entry["itemRef"],
                        "verdict": "MATCH",
                        "rowRefs": [row["rowRef"] for row in entry["rows"]],
                        "explanation": "사망한 상태로 등록되어 있어요.",
                    }
                    for entry in payload["items"]
                ]
            }
            self.outputs.insert(0, {"decisions": []} if first and retry_first else response)
            first = False
        return await original(self, system_prompt, user_prompt, **kwargs)

    monkeypatch.setattr(ScriptedTextClient, "create_text_response", respond)


def condition_requests(result):
    return [
        request
        for request in result["raw"].requests
        if request["prompt_cache_key"] == "chat-current-condition-v2"
    ]


def assert_complete_count(result, count):
    assert result["finish"]["failure"] is None
    answer = result["finish"]["result"]
    assert f"확인한 인물은 {count}명" in answer["text"]
    assert "아직 검토하지 못한" not in answer["text"]
    assert NOTICES["CONTEXT_LIMIT"] not in answer["limitations"]
    observed = [
        entry["itemRef"] for r in condition_requests(result) for entry in r["input"]["items"]
    ]
    assert len(observed) == count and len(set(observed)) == count
    assert any(q.get("readToken") for q in result["spring"].queries)


@pytest.mark.parametrize("interpretation_cap", [1500, 3000])
def test_all_100_subjects_fit_answer_budget_without_unnecessary_partial_result(
    tmp_path, monkeypatch, interpretation_cap
):
    script_condition_judgments(monkeypatch)
    spec, setup = count_case(["사망"] * 100)
    result = asyncio.run(
        replay(
            tmp_path,
            spec,
            spring_setup=setup,
            config={"max_output_tokens": interpretation_cap, "answer_max_output_tokens": 3000},
        )
    )
    assert_complete_count(result, 100)
    requests = condition_requests(result)
    assert [len(r["input"]["items"]) for r in requests] == [30, 30, 30, 10]
    assert {r["max_output_tokens"] for r in requests} == {3000}
    assert len(result["spring"].reservations) == 5


def test_smaller_answer_budget_splits_every_subject_without_changing_interpretation_budget(
    tmp_path, monkeypatch
):
    script_condition_judgments(monkeypatch)
    spec, setup = count_case(["사망"] * 100)
    result = asyncio.run(
        replay(
            tmp_path,
            spec,
            spring_setup=setup,
            config={"max_output_tokens": 3000, "answer_max_output_tokens": 2000},
        )
    )
    assert_complete_count(result, 100)
    assert [len(r["input"]["items"]) for r in condition_requests(result)] == [20] * 5
    assert {r["max_output_tokens"] for r in condition_requests(result)} == {2000}
    assert len(result["spring"].reservations) == 6


def test_input_budget_splits_whole_subjects_without_truncating_their_registered_values(
    tmp_path, monkeypatch
):
    script_condition_judgments(monkeypatch)
    value = "사망. " + "synthetic context " * 100
    spec, setup = count_case([value] * 24)
    result = asyncio.run(replay(tmp_path, spec, spring_setup=setup))
    assert_complete_count(result, 24)
    requests = condition_requests(result)
    assert 1 < len(requests) <= 5
    assert all(
        row["value"] == value
        for r in requests
        for entry in r["input"]["items"]
        for row in entry["rows"]
    )


@pytest.mark.parametrize("limit_kind", ["hybrid", "execution", "retry"])
def test_tight_call_budget_keeps_unreviewed_people_out_of_the_confirmed_count(
    tmp_path, monkeypatch, limit_kind
):
    script_condition_judgments(monkeypatch, retry_first=limit_kind == "retry")
    spec, setup = count_case(["사망"] * 100)
    if limit_kind == "execution":
        spec["maxCalls"] = 3
    result = asyncio.run(
        replay(
            tmp_path,
            spec,
            spring_setup=setup,
            config={"answer_max_output_tokens": 1000},
            hybrid={"max_model_calls": 6 if limit_kind == "execution" else 3},
        )
    )
    assert result["finish"]["failure"] is None
    text = result["finish"]["result"]["text"]
    reviewed = 10 if limit_kind == "retry" else 20
    assert f"확인한 인물은 {reviewed}명" in text
    assert f"아직 검토하지 못한 {100 - reviewed}명" in text
    assert NOTICES["CONTEXT_LIMIT"] in result["finish"]["result"]["limitations"]
    assert len(result["spring"].reservations) == len(result["raw"].requests) == 3
    assert metric_counts(result, "chat_retrieval_model_calls_total") == Counter(
        CHAT_INTERPRETATION=1, CHAT_ANSWER=2
    )
