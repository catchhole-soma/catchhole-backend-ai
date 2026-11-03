"""Judge-written finding title (`headline`) and the cause→result reason rule (owner feedback 2026-11-03).

Synthetic text only; the scripted model exercises the Worker's normalization, never real model quality.
"""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from app.validation.author_text import (
    HEADLINE_DROPPED,
    HEADLINE_MISSING,
    clean_headline,
    sanitize_decision,
)
from app.validation.contracts import (
    HEADLINE_LIMIT,
    JudgeFinalAnswer,
    JudgeOpenAnswer,
    ValidationDecision,
)
from app.validation.models import JUDGE_SYSTEM
from app.validation.schemas import judge_schema
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    FakeEmbedding,
    FakeSearch,
    ScriptedModel,
    SyntheticData,
    budget,
    decided,
    draft,
    follow_up,
)

CONTRACT = Path(__file__).resolve().parents[1] / "docs/contracts/validation-execution-v1.json"
STORED = [{"ref": "B1", "quote": "왼손 상실"}]  # B1 STATUS:손 contradicts "양손으로 검을 쥐었다"
TITLE = "민수의 손 상태가 기존 설정과 달라요"


def contract():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))["execution"]["judgeFlow"]["headline"]


def inspect(model, data=None):
    data = data or SyntheticData()
    service = SettingValidationService(data, model, budget(), ROLES, search=FakeSearch(data),
                                       embedding_client=FakeEmbedding())
    return asyncio.run(service.inspect())


def one(judge):
    return ScriptedModel({"TSEG1": [draft()]}, judge)


def decision(outcome, headline):
    impact = None if outcome == "NO_CONFLICT" else "CAUTION"
    basis = {"CONFLICT": "DIRECT_CONTRADICTION", "SUSPECTED_CONFLICT": "CHANGE_NOT_FOUND",
             "NEEDS_CONTEXT": "AMBIGUOUS_MEANING", "NO_CONFLICT": "COMPATIBLE"}[outcome]
    return ValidationDecision.model_validate({
        "action": "DECIDED", "outcome": outcome, "basis": basis, "reason": "합성 판정", "impact": impact,
        "impact_reason": None if impact is None else "합성 영향", "target_spans": [], "evidence_refs": ["T1"],
        "completion": None, "follow_ups": [], "affected_question_refs": [], "disagreements": [], "headline": headline})


# --- shared contract ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", contract()["workerCases"], ids=lambda row: row["name"])
def test_worker_cases_of_the_shared_contract(case):
    labels = contract()["workerLabels"]
    published = sanitize_decision(decision(case["outcome"], case["answer"]), labels, Counter())
    assert published.headline == case["published"]


def test_contract_limits_match_the_worker_and_the_sanitizer_lists_headline():
    rule = json.loads(CONTRACT.read_text(encoding="utf-8"))["execution"]["judgeFlow"]
    assert rule["headline"]["maxCodePoints"] == HEADLINE_LIMIT == 80
    assert rule["authorText"]["fields"][0] == "headline"
    assert "headline" in rule["publishedDecision"]


def test_clean_headline_counts_code_points_and_reports_sanitizing():
    assert clean_headline("😀" * HEADLINE_LIMIT, {}) == ("😀" * HEADLINE_LIMIT, False)
    assert clean_headline("😀" * (HEADLINE_LIMIT + 1), {}) == (None, False)
    assert clean_headline("S1의 상태가 B1과 달라요", {"S1": "민수", "B1": "저장된 설정"}) == (
        "민수의 상태가 저장된 설정과 달라요", True)
    assert clean_headline("L2 기준 확인", {}) == (None, True)
    assert clean_headline(None, {}) == (None, False) and clean_headline(" \n", {}) == (None, False)


def test_sanitize_counts_dropped_missing_and_sanitized_headlines():
    stats = Counter()
    sanitize_decision(decision("CONFLICT", "L3 영수증"), {}, stats)
    sanitize_decision(decision("NEEDS_CONTEXT", None), {}, stats)
    sanitize_decision(decision("SUSPECTED_CONFLICT", "S1의 손이 다를 수 있어요"), {"S1": "민수"}, stats)
    assert stats == Counter({HEADLINE_DROPPED: 1, HEADLINE_MISSING: 2, "AUTHOR_TEXT_SANITIZED": 2})
    clean = decision("CONFLICT", TITLE)
    assert sanitize_decision(clean, {}, stats) is clean  # nothing to change: same object, no new counts
    assert stats[HEADLINE_MISSING] == 2


# --- schema and prompt ----------------------------------------------------------------------------------------

def test_both_judge_schemas_require_a_nullable_headline():
    for final in (False, True):
        schema = judge_schema(entry_refs=(), final=final, search_available=True)
        assert "headline" in schema["required"]
        assert schema["properties"]["headline"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert JudgeOpenAnswer.model_validate(follow_up({"question": draft()})).headline is None
    payload = {"receipts": [{"ref": "L1"}], "settings": [], "evidence": [], "form": "FINAL",
               "question": {"target_spans": draft()["target_spans"]}}
    older = {key: value for key, value in decided(payload, "CONFLICT").items() if key != "headline"}
    assert JudgeFinalAnswer.model_validate(older).headline is None  # older scripted answers still load


def test_prompt_has_the_headline_and_cause_then_result_reason_rules():
    for phrase in ("headline은 작가 화면에 보이는 이 판정의 제목 한 줄이다", "40자 안팎",
                   "FOLLOW_UP과 NO_CONFLICT이면 headline은 null이다", "기존 설정과 달라요", "…N화와 달라요",
                   "…설정집과 달라요", "…다를 수 있어요", "누구인지 확인이 필요해요",
                   "서술자·말하는 인물이거나 장면에 이름만 나온 대상은 쓰지 않는다",
                   "데스핀드와 키메라 울프의 정수 등급이 기존 설정과 달라요",
                   "원인 다음 결과 순서의 두 문장으로 120자 안팎", "그런데 {대상 회차}에서는 {이번 값}(이)라고 해요.",
                   "target_episode_no가 있으면", ("기존 설정은 데스핀드와 키메라 울프의 정수 등급을 8등급이라고 명시하고 있어요. "
                                                   "그런데 31화에서는 9등급이라고 해요."), "headline·reason·impact_reason·disagreements의 reason"):
        assert phrase in JUDGE_SYSTEM, phrase


# --- judge flow -------------------------------------------------------------------------------------------------

def test_a_conflict_publishes_the_judge_headline_and_the_judge_gets_the_target_episode_no():
    model = one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"], basis_quotes=STORED,
                                           headline=f"  {TITLE} "))
    result = inspect(model)
    published = result.decisions["Q1"]
    assert published.outcome == "CONFLICT" and published.headline == TITLE
    assert published.model_dump()["headline"] == TITLE
    (payload,) = model.judge_calls("Q1")
    assert payload["target_episode_no"] == 101  # SyntheticData's target is 101화
    assert "target_episode_no" not in result.evidence_bundles["Q1"]  # model-only input: the bundle is unchanged
    assert HEADLINE_MISSING not in result.normalizations


def test_a_finding_without_headline_publishes_null_and_is_counted():
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"], basis_quotes=STORED,
                                                    headline=None)))
    assert result.decisions["Q1"].outcome == "CONFLICT" and result.decisions["Q1"].headline is None
    assert result.normalizations[HEADLINE_MISSING] == 1


def test_a_headline_with_internal_refs_is_sanitized_or_dropped():
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"], basis_quotes=STORED,
                                                    headline="L4 receipt 확인")))
    assert result.decisions["Q1"].headline is None
    assert result.normalizations[HEADLINE_DROPPED] == 1 and result.normalizations["AUTHOR_TEXT_SANITIZED"] >= 1


def test_no_conflict_and_follow_up_headlines_are_never_published():
    def judge(payload, n):
        if n == 1:
            answer = follow_up(payload, contexts=[(0, 4)])
            answer["headline"] = "후속 조회의 제목"
            return answer
        return decided(payload, "NO_CONFLICT", headline="민수의 손 상태가 기존 설정과 같아요")

    result = inspect(one(judge))
    assert result.decisions["Q1"].outcome == "NO_CONFLICT" and result.decisions["Q1"].headline is None
    assert result.normalizations["NO_CONFLICT_HEADLINE_CLEARED"] == 1
    assert result.normalizations["FOLLOW_UP_DECISION_FIELDS_IGNORED"] == 1


def test_the_rule_guard_clears_the_headline_with_the_finding():
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"], headline=TITLE)))
    assert result.decisions["Q1"].outcome == "NO_CONFLICT" and result.decisions["Q1"].headline is None
    assert result.normalizations["CONFLICT_QUOTE_MISSING_CLEARED"] == 1


def test_older_published_decisions_without_headline_still_load():
    loaded = decision("CONFLICT", TITLE).model_dump()
    loaded.pop("headline")
    assert ValidationDecision.model_validate(loaded).headline is None
