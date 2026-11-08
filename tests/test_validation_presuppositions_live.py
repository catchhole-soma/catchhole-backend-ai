"""Opt-in semantic regression using invented text; raw answers stay outside the repository.

VALIDATION_PREMISE_LIVE=1 pytest -q tests/test_validation_presuppositions_live.py
Requires LLM_API_KEY. JUDGE defaults to gpt-5.6-sol/medium, PLANNER to gpt-6.1-terra/medium.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.validation.contracts import BudgetState, InvocationBudget, JudgeFinalAnswer, QuestionBatch
from app.validation.models import RoleRuntime, call_role
from app.validation.schemas import (
    JUDGE_FINAL_SCHEMA_NAME,
    PLANNER_SCHEMA_NAME,
    judge_schema,
    planner_schema,
)
from tests.test_validation_question_scope_live import scope_payload

pytestmark = pytest.mark.skipif(os.environ.get("VALIDATION_PREMISE_LIVE") != "1",
                                reason="opt-in paid factual-premise semantic regression")

# Each assertion concerns the specified property, not every adjacent sentence.
CASES = [
    ("role_in_wish", "레온은 말했다. '의사로서 병원을 세우고 싶다.'", "레온의 직업", "그 시점에 의사인지 확인", "직업", "레온의 현재 직업은 목수이며 의사는 아니다.", "CONFLICT"),
    ("count_premise", "지난번 여덟 명이서 다녀와서 허전한 걸까? 동료들이 그립다.", "지난 원정 인원", "지난 원정 인원이 여덟 명인지 확인", "지난 원정", "지난 원정에는 정확히 네 명만 참가했다.", "CONFLICT"),
    ("role_in_recall", "나는 그때 의사였던 레온과 술을 마셨던 일을 떠올렸다.", "회상 시점 직업", "그 술자리 때 의사였는지 확인", "술자리 시점", "그 술자리 당시 레온은 목수였으며 의사가 아니었다.", "CONFLICT"),
    ("future_wish", "목수 레온은 말했다. '언젠가 의사가 되고 싶다.'", "레온의 직업", "현재 직업과 희망의 구분", "직업", "레온은 목수이며 아직 의사가 아니다.", "NO_CONFLICT"),
    ("uncertain_belief", "나는 처음 보는 레온을 훑었다. '흰 가운을 보니 의사인 것 같은데.'", "레온의 직업", "직업 추측이 확정 서술인지 확인", "직업", "레온은 목수이며 의사가 아니다.", "NO_CONFLICT"),
    ("explicit_mistake", "레온을 의사라고 착각했다. 곧 그가 목수라는 것을 알았다.", "레온의 직업", "직업이 의사로 바뀐 것인지 확인", "직업", "레온은 목수다.", "NO_CONFLICT"),
    ("valid_count", "지난번 네 명이서 다녀와서 허전한 걸까? 동료들이 그립다.", "지난 원정 인원", "지난 원정 인원이 네 명인지 확인", "지난 원정", "지난 원정에는 정확히 네 명만 참가했다.", "NO_CONFLICT"),
    ("valid_role_wish", "레온은 말했다. '목수로서 학교를 짓고 싶다.'", "레온의 직업", "목수 신분 확인", "직업", "레온은 목수다.", "NO_CONFLICT"),
    ("supported_change", "지금은 의사가 된 레온이 환자를 진료했다.", "레온의 현재 직업", "현재 의사인지 확인", "직업 변화", "레온은 과거 목수였지만 이후 의사가 되어 현재 진료한다.", "NO_CONFLICT"),
    ("valid_recall", "지금은 의사인 레온과 예전에 목수 일을 함께하던 날을 떠올렸다.", "회상 시점 직업", "회상하는 시점에 목수였는지 확인", "직업 변화", "레온은 과거 목수였지만 이후 의사가 되었다.", "NO_CONFLICT"),
]


def limits():
    return BudgetState(InvocationBudget(model_calls=1, lookups=1, seconds=240, input_tokens=80000,
                                       segment_chars=10000, context_chars=1000, search_limit=10))


def save(name, value):
    directory = os.environ.get("VALIDATION_PREMISE_OUTPUT")
    if directory:
        path = Path(directory).resolve()
        repo = Path(__file__).resolve().parents[1]
        assert path != repo and repo not in path.parents
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


def test_factual_premises_and_normal_modal_sentences():
    async def run():
        client = OpenAIResponsesClient.from_settings()
        model = os.environ.get("VALIDATION_PREMISE_JUDGE", "gpt-5.6-sol")
        role = RoleRuntime.of(model, "medium", 20000)
        gate = asyncio.Semaphore(4)

        async def one(case):
            key, text, prop, intent, fact_key, fact, expected = case
            payload = scope_payload(text, prop, intent, "elf", "FINAL")
            payload["question"]["subject_mentions"] = ["레온"]
            payload["question"]["time_context"] = "대상 문장에 명시된 시점"
            payload["settings"][0].update(name="레온", state={"name": "레온", "slots": {
                fact_key: {"factType": "PROFILE", "factKey": fact_key, "factValue": fact}}})
            async with gate:
                answer = await call_role(client, "JUDGE", payload, JudgeFinalAnswer, limits(), runtime=role,
                                         schema=judge_schema(entry_refs=(), final=True, search_available=False),
                                         schema_name=JUDGE_FINAL_SCHEMA_NAME)
            return {"case": key, "expected": expected, "actual": answer.outcome, "answer": answer.model_dump()}
        try:
            rows = await asyncio.gather(*(one(case) for case in CASES))
            save("judge-controls.json", {"model": model, "effort": "medium", "rows": rows})
            errors = [{k: row[k] for k in ("case", "expected", "actual")} for row in rows if row["expected"] != row["actual"]]
            assert not errors, json.dumps(errors, ensure_ascii=False)
        finally:
            await client.http_client.aclose()
    asyncio.run(run())


def test_planner_separates_profession_from_wish():
    async def run():
        text = "레온이 말했다. '목수로서 학교를 짓고 싶다.'"
        payload = {"text": text, "text_start": 0, "target_source_ref": "T1", "segment": {"ref": "TSEG1", "start": 0, "end": len(text)},
                   "subject_catalogue": [{"subject_ref": "S1", "domain": "characters", "name": "레온", "aliases": []}], "source_catalogue": []}
        client = OpenAIResponsesClient.from_settings()
        try:
            answer = await call_role(client, "PLANNER", payload, QuestionBatch, limits(),
                                     runtime=RoleRuntime.of("gpt-6.1-terra", "medium", 20000),
                                     schema=planner_schema(segment_refs=["TSEG1"], subject_refs=["S1"]),
                                     schema_name=PLANNER_SCHEMA_NAME)
            save("planner-controls.json", answer.model_dump())
            intents = [q.property_or_rule + " " + q.check_intent for q in answer.questions]
            role = [q for q in intents if any(w in q for w in ("직업", "신분", "목수")) and not any(w in q for w in ("희망", "꿈", "소망", "포부", "싶"))]
            wish = [q for q in intents if any(w in q for w in ("희망", "꿈", "소망", "포부", "싶"))]
            assert role and wish and set(role).isdisjoint(wish), "profession and wish must have separate questions"
        finally:
            await client.http_client.aclose()
    asyncio.run(run())
