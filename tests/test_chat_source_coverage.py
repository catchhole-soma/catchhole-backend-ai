"""Synthetic regressions for independent source needs; never contains evaluation Gold."""

import asyncio
from dataclasses import replace

from app.chatbot.hybrid_prompts import ANSWER_PROMPT, SOURCE_ANSWER_PROMPT
from app.chatbot.qa import response_schema
from app.schemas.chat_hybrid import GroundedAnswer
from tests.test_chat_answer_recovery import offline_only  # noqa: F401
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
from tests.test_chat_requested_parts_merge import answer as previous_answer
from tests.test_chat_requested_parts_merge import plan, session


def test_source_only_per_item_recovers_independent_valid_claim(tmp_path):
    case = parts_case()
    case["outputs"][0]["perItem"] = True  # No registered item rows exist.
    case["outputs"] += [
        answer(
            claim("잘못된 주장", "존재하지 않는 인용", parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, parts=("P2",)),
        )
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 2},
            search_setup=search_with(keyword=[passage(BEHAVIOR, 1)]),
        )
    )
    assert result["finish"]["failure"] is None
    assert BEHAVIOR in result["finish"]["result"]["text"]
    assert "잘못된 주장" not in result["finish"]["result"]["text"]
    assert result["search"].calls[-1] == "revalidate"


def test_covered_term_rank_survives_evidence_composition(tmp_path):
    case = parts_case()
    case["outputs"] += [answer(claim(EMOTION, EMOTION, ref="E2"))]
    repeated = replace(passage("이미 답한 부분이 반복됩니다.", 1), score=0.9)
    separate = replace(passage(EMOTION, 2, episode=2), score=0.2)
    noise = [replace(passage(f"반복 {i}", i + 10, episode=i + 10), score=0.8) for i in range(12)]
    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[repeated, separate, *noise]))
    )
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert EMOTION in result["finish"]["result"]["text"]
    request = result["raw"].requests[-1]
    assert request["system"] == SOURCE_ANSWER_PROMPT
    schema = request["response_schema"].schema
    definitions = [
        d["properties"] for d in schema["$defs"].values() if "eventMatch" in d.get("properties", {})
    ]
    assert definitions and all(p["eventMatch"]["type"] == "string" for p in definitions)
    # The generic contract stays nullable for requests without event identity.
    general = response_schema(GroundedAnswer).schema
    assert all(
        "anyOf" in d["properties"]["eventMatch"]
        for d in general["$defs"].values()
        if "eventMatch" in d.get("properties", {})
    )


def test_expansion_does_not_repeat_answered_keyword_bag():
    state = session()
    state.execution.context.question = "이미 답한 화염 장면과 이후 행동을 알려줘"
    draft = previous_answer("심리 확인", ["E1"], ["P1"], "E1", "확인한 인용")
    query = state.expansion_query(plan(), draft, "화염 장면 반복 검색어")
    assert "실제 이후 행동" in query and "동료의 반응" in query
    assert plan().event_context in query
    assert "반복 검색어" not in query and state.execution.context.question not in query


def test_source_only_contract_leaves_more_budget_for_evidence():
    # This checks the cause of input pruning without changing the model/token caps.
    assert len(SOURCE_ANSWER_PROMPT.encode()) < len(ANSWER_PROMPT.encode()) * 0.55
