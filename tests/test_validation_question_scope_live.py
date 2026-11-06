"""Opt-in real JUDGE regression, with invented text and no database writes.

VALIDATION_SCOPE_LIVE=1 .venv/bin/pytest -q tests/test_validation_question_scope_live.py
Requires LLM_API_KEY and makes paid calls (gpt-6-luna/high, eight questions in each form).
This checks model behavior, not prompt wording or scripted model answers.
"""

import asyncio
import json
import os

import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.validation.contracts import (
    BudgetState,
    InvocationBudget,
    JudgeFinalAnswer,
    JudgeOpenAnswer,
)
from app.validation.models import RoleRuntime, call_role
from app.validation.schemas import JUDGE_FINAL_SCHEMA_NAME, JUDGE_OPEN_SCHEMA_NAME, judge_schema

pytestmark = pytest.mark.skipif(os.environ.get("VALIDATION_SCOPE_LIVE") != "1",
                                reason="opt-in paid real-model regression")

CASES = [
    # One wrong modifier must not contaminate the correct role in the same sentence.
    ("인간 궁수인 세라는 활을 들었다.", "세라의 종족", "세라가 인간인지 확인", "elf", "CONFLICT"),
    ("인간 궁수인 세라는 활을 들었다.", "세라의 역할", "세라가 궁수이며 활을 쓸 수 있는지 확인", "elf", "NO_CONFLICT"),
    # The adjacent sentence is context, not an extra question about entry duration.
    ("입장은 열 시간 동안 가능했다. 다음 개방까지 30일을 기다려야 했다.",
     "입장 가능 시간", "입장 가능 시간이 열 시간인지 확인", "portal", "CONFLICT"),
    ("입장은 열 시간 동안 가능했다. 다음 개방까지 30일을 기다려야 했다.",
     "개방 주기", "다음 개방까지 30일을 기다려야 하는지 확인", "portal", "NO_CONFLICT"),
    # Two genuinely wrong properties in the same sentence must both remain findings.
    ("인간 마법사인 세라는 지팡이를 들었다.", "세라의 종족", "세라가 인간인지 확인", "elf", "CONFLICT"),
    ("인간 마법사인 세라는 지팡이를 들었다.", "세라의 역할", "세라가 마법사인지 확인", "elf", "CONFLICT"),
    ("입장은 열 시간 동안 가능했고 다음 개방까지 20일을 기다려야 했다.",
     "입장 가능 시간", "입장 가능 시간이 열 시간인지 확인", "portal", "CONFLICT"),
    ("입장은 열 시간 동안 가능했고 다음 개방까지 20일을 기다려야 했다.",
     "개방 주기", "다음 개방까지 20일을 기다려야 하는지 확인", "portal", "CONFLICT"),
]


def scope_payload(text, prop, intent, subject, form):
    state = ({"name": "세라", "slots": {
        "종족": {"factType": "IDENTITY", "factKey": "종족", "factValue": "엘프"},
        "역할": {"factType": "ROLE", "factKey": "역할", "factValue": "궁수이며 활을 사용한다. 마법사는 아니다"}}}
        if subject == "elf" else {"name": "입장 포탈", "propertiesJson": {
            "입장 가능 시간": "한 번 개방하면 다섯 시간 동안만 입장할 수 있다",
            "개방 주기": "매번 정확히 30일 뒤에만 다시 열리며 그 전에는 열리지 않는다"}})
    # For the cycle check, the wrong duration stays in the surrounding context only.
    quote = ("다음 개방까지 30일을 기다려야 했다." if prop == "개방 주기" and "30일" in text else text)
    start = text.index(quote)
    return {
        "question_ref": "Q1", "form": form, "target_episode_no": 2,
        "question": {"local_ref": "scope", "subject_refs": ["S1"], "subject_mentions": [state["name"]],
                     "property_or_rule": prop, "check_intent": intent, "time_context": "현재",
                     "conditions": [], "assertion_mode": "NARRATION", "reason": "기존 설정과 비교",
                     "target_spans": [{"source_ref": "T1", "start": start, "end": start + len(quote),
                                       "quote": quote}]},
        "target_context": [{"start": 0, "text": text}], "additional_target_context": [],
        "settings": [{"entry_ref": "B1", "subject_ref": "S1", "name": state["name"], "aliases": [],
                      "domain": "characters" if subject == "elf" else "worldSettings", "state": state}],
        "evidence": [], "source_catalogue": [],
        "receipts": [
            {"ref": "L1", "status": "SUCCEEDED", "evidence_refs": ["B1"],
             "intent": {"kind": "COMPARISON_CONTEXT", "operation": "BASELINE"}},
            {"ref": "L2", "status": "SUCCEEDED", "evidence_refs": ["T1"],
             "intent": {"kind": "COMPARISON_CONTEXT", "operation": "TARGET_CONTEXT"}},
        ],
    }


@pytest.mark.parametrize("form", ["OPEN", "FINAL"])
def test_judge_reports_only_the_asked_property_and_keeps_independent_conflicts(form):
    async def run():
        client = OpenAIResponsesClient.from_settings()
        runtime = RoleRuntime.of("gpt-6-luna", "high", 20000)
        try:
            # Four at once, matching the production judge's concurrency.
            gate = asyncio.Semaphore(4)

            async def check(case):
                text, prop, intent, subject, expected = case
                async with gate:
                    answer = await call_role(
                        client, "JUDGE", scope_payload(text, prop, intent, subject, form),
                        JudgeOpenAnswer if form == "OPEN" else JudgeFinalAnswer,
                        BudgetState(InvocationBudget(model_calls=1, lookups=1, seconds=180, input_tokens=80000,
                                                     segment_chars=10000, context_chars=1000, search_limit=10)),
                        runtime=runtime, schema=judge_schema(entry_refs=(), final=form == "FINAL",
                                                            search_available=False),
                        schema_name=JUDGE_OPEN_SCHEMA_NAME if form == "OPEN" else JUDGE_FINAL_SCHEMA_NAME,
                    )
                return {"property": prop, "expected": expected, "action": answer.action,
                        "outcome": answer.outcome, "reason": answer.reason}

            results = await asyncio.gather(*(check(case) for case in CASES))
            errors = [row for row in results
                      if row["action"] != "DECIDED" or row["outcome"] != row["expected"]]
            assert not errors, json.dumps(errors, ensure_ascii=False, indent=2)
        finally:
            await client.http_client.aclose()

    asyncio.run(run())
