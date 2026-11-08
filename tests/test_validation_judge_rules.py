"""Judge rules (2026-11-03): findings need a contradicting earlier quote, and two Worker-built VECTOR queries.

Synthetic text only; the scripted model exercises the Worker's guard and search flow, never real model quality.
"""

import asyncio
from collections import Counter

import pytest

from app.validation.contracts import SEARCH_QUERY_LIMIT, JudgeFinalAnswer, QuestionDraft
from app.validation.judging import (
    TARGET_QUERY_CONTEXT_CHARS,
    UNQUOTED_FINDING_REASON,
    WORKER_CHANGE_SEARCH_PURPOSE,
    WORKER_TARGET_SEARCH_PURPOSE,
    change_search_query,
    clear_unquoted_finding,
    target_search_query,
)
from app.validation.models import JUDGE_SYSTEM, PLANNER_SYSTEM
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    TARGET,
    FakeEmbedding,
    FakeSearch,
    ScriptedModel,
    SyntheticData,
    budget,
    decided,
    draft,
    follow_up,
)

STORED = [{"ref": "B1", "quote": "왼손 상실"}]  # B1 STATUS:손, the value that contradicts "양손으로 검을 쥐었다"


def inspect(model, data=None, *, search=True, checkpoint=None, **limits):
    data = data or SyntheticData()
    service = SettingValidationService(data, model, budget(**limits), ROLES,
                                       search=FakeSearch(data) if search else None,
                                       embedding_client=FakeEmbedding() if search else None, checkpoint=checkpoint)
    return asyncio.run(service.inspect())


def one(judge, **changes):
    return ScriptedModel({"TSEG1": [draft(**changes)]}, judge)


def question(spans, check_intent="확인할 내용"):
    return QuestionDraft.model_validate({**draft(), "check_intent": check_intent, "target_spans": [
        {"source_ref": "T1", "start": start, "end": end, "quote": quote} for start, end, quote in spans]})


def span_of(text, quote):
    start = text.index(quote)
    return start, start + len(quote), quote


# --- guard: a finding needs a contradicting quote ------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["CONFLICT", "SUSPECTED_CONFLICT"])
def test_guard_clears_only_unquoted_findings_without_disagreements(outcome):
    payload = {"receipts": [{"ref": "L1"}], "settings": [{"entry_ref": "B1"}], "evidence": [], "form": "FINAL",
               "question": {"target_spans": draft()["target_spans"]}}
    answer = JudgeFinalAnswer.model_validate(decided(payload, outcome, evidence_refs=["B1"]))
    stats = Counter()
    cleared, original = clear_unquoted_finding(answer, [], stats)
    assert original == outcome and stats[f"{outcome}_QUOTE_MISSING_CLEARED"] == 1
    assert (cleared.outcome, cleared.basis, cleared.impact, cleared.impact_reason, cleared.reason) == (
        "NO_CONFLICT", "COMPATIBLE", None, None, UNQUOTED_FINDING_REASON)
    assert cleared.evidence_refs == answer.evidence_refs and cleared.completion == answer.completion
    kept, none = clear_unquoted_finding(answer, ["a checked quote"], stats)
    assert kept is answer and none is None
    disagreeing = answer.model_copy(update={"disagreements": [
        {"fact_ref": "B1", "evidence_ref": "E1", "reason": "다름", "correction_ref": None}]})
    assert clear_unquoted_finding(disagreeing, [], stats) == (disagreeing, None)
    for other in ("NEEDS_CONTEXT", "NO_CONFLICT"):
        plain = JudgeFinalAnswer.model_validate(decided(payload, other))
        assert clear_unquoted_finding(plain, [], stats) == (plain, None)
    assert sum(stats.values()) == 1


def test_conflict_without_a_quote_is_published_as_no_conflict_and_counted():
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"])))
    assert result.status == "COMPLETED"
    decision = result.decisions["Q1"]
    assert (decision.outcome, decision.basis, decision.impact, decision.impact_reason) == (
        "NO_CONFLICT", "COMPATIBLE", None, None)
    assert decision.reason == UNQUOTED_FINDING_REASON and decision.basis_quotes == []
    assert result.normalizations["CONFLICT_QUOTE_MISSING_CLEARED"] == 2
    assert result.normalizations["DB_GAP_SEARCH_INSERTED"] == 1
    trace = result.traces["Q1"][-1]
    assert (trace["cleared"], trace["cleared_impact"], trace["cleared_reason"], trace["decided"]) == (
        "CONFLICT", "CAUTION", "합성 판정", "NO_CONFLICT")


def test_a_quote_that_is_not_in_the_stored_value_does_not_keep_a_finding():
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"],
                                                    basis_quotes=[{"ref": "B1", "quote": "오른손 상실"}])))
    assert result.decisions["Q1"].outcome == "NO_CONFLICT"
    assert result.normalizations["BASIS_QUOTES_DROPPED"] == 2
    assert result.normalizations["DB_GAP_SEARCH_INSERTED"] == 1
    assert result.normalizations["CONFLICT_QUOTE_MISSING_CLEARED"] == 2
    assert result.normalizations["DB_GAP_SEARCH_INSERTED"] == 1


def test_a_conflict_with_the_contradicting_stored_value_is_kept():
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"], basis_quotes=STORED)))
    decision = result.decisions["Q1"]
    assert decision.outcome == "CONFLICT" and decision.impact == "CAUTION"
    assert [row.quote for row in decision.basis_quotes] == ["왼손 상실"]
    assert "CONFLICT_QUOTE_MISSING_CLEARED" not in result.normalizations


def test_unquoted_suspected_still_gets_the_change_search_then_final_and_is_cleared_there():
    model = one(lambda payload, n: decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"]))
    result = inspect(model)
    assert [payload["form"] for payload in model.judge_calls("Q1")] == ["OPEN", "FINAL"]
    (change,) = [row for row in result.receipts if row.intent.kind == "CHANGES_EXCEPTIONS"]
    assert change.intent.purpose == WORKER_CHANGE_SEARCH_PURPOSE and result.normalizations["CHANGE_SEARCH_INSERTED"] == 1
    assert result.decisions["Q1"].outcome == "NO_CONFLICT"
    assert result.normalizations["SUSPECTED_CONFLICT_QUOTE_MISSING_CLEARED"] == 1
    assert [row.get("cleared") for row in result.traces["Q1"]] == [None, "SUSPECTED_CONFLICT"]


def test_suspected_quoting_an_earlier_episode_sentence_after_the_change_search_is_kept():
    def judge(payload, n):
        if n == 1:
            return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"])
        (row,) = payload["evidence"]  # the change search found R2's chunk
        return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1", row["ref"]],
                       basis_quotes=[{"ref": row["ref"], "quote": "민수는 의수를 얻었고"}])

    result = inspect(one(judge))
    decision = result.decisions["Q1"]
    assert decision.outcome == "SUSPECTED_CONFLICT" and decision.basis_quotes[0].source_ref == "R2"
    assert decision.completion.changes_exceptions.receipt_refs  # the change receipt is still required and present


def test_db_raw_disagreements_keep_their_needs_context_rule_and_needs_context_is_never_cleared():
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["민수의 손에 관한 이전 서술"])
        value = decided(payload, "CONFLICT", "DB_RAW_DISAGREEMENT", evidence_refs=["B1", "E1"])
        value["disagreements"] = [{"fact_ref": "B1", "evidence_ref": "E1", "reason": "저장 값과 원문이 다름",
                                   "correction_ref": None}]
        return value

    result = inspect(one(judge))
    assert (result.decisions["Q1"].outcome, result.decisions["Q1"].basis) == ("NEEDS_CONTEXT", "DB_RAW_DISAGREEMENT")
    assert not any(key.endswith("_QUOTE_MISSING_CLEARED") for key in result.normalizations)
    plain = inspect(one(lambda payload, n: decided(payload, "NEEDS_CONTEXT")))
    assert plain.decisions["Q1"].outcome == "NEEDS_CONTEXT"


def test_a_cleared_finding_is_published_as_a_valid_no_conflict_unit():
    from app.validation.contracts import split_setting_segments
    from tests.test_validation_checkpoint import RecordingJava, execution_claim, real_checkpoint

    java = RecordingJava(split_setting_segments(TARGET, unit_chars=10000, context_chars=100))
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"])),
                     checkpoint=real_checkpoint(java, execution_claim()))
    assert result.status == "COMPLETED"
    (unit,) = java.published("units")
    assert unit["outcome"] == "NO_CONFLICT" and unit["payload"]["impact"] is None
    assert unit["payload"]["reason"] == UNQUOTED_FINDING_REASON


# --- the Worker's target-text search -------------------------------------------------------------------------

LINES = ["첫째 줄의 아주 긴 배경 서술이 이어진다" * 6, "둘째 줄에서 그는 문을 열었다.", "“원래 그렇게 하기로 정했잖아.”",
         "셋째 줄은 바로 뒤의 반응이다.", "넷째 줄은 다음 장면의 시작이며 꽤 길게 계속된다" * 6]
TEXT = "\n\n".join(LINES)


def test_target_query_keeps_whole_lines_around_the_span_and_collapses_whitespace():
    query = target_search_query(question([span_of(TEXT, "원래 그렇게 하기로 정했잖아.")]), TEXT)
    assert query == "둘째 줄에서 그는 문을 열었다. “원래 그렇게 하기로 정했잖아.” 셋째 줄은 바로 뒤의 반응이다."
    assert "\n" not in query and TARGET_QUERY_CONTEXT_CHARS == 100


def test_target_query_without_line_breaks_takes_exactly_the_context_window():
    text = "가" * 300 + "약속" + "나" * 300
    query = target_search_query(question([span_of(text, "약속")]), text)
    assert query == "가" * TARGET_QUERY_CONTEXT_CHARS + "약속" + "나" * TARGET_QUERY_CONTEXT_CHARS


def test_target_query_merges_overlapping_windows_and_joins_far_ones_in_target_order():
    near = question([span_of(TEXT, "셋째 줄"), span_of(TEXT, "둘째 줄")])
    assert target_search_query(near, TEXT) == "둘째 줄에서 그는 문을 열었다. “원래 그렇게 하기로 정했잖아.” 셋째 줄은 바로 뒤의 반응이다."
    text = "앞 장면의 약속.\n" + "가" * 400 + "\n뒤 장면의 다짐."
    far = question([span_of(text, "뒤 장면의 다짐."), span_of(text, "앞 장면의 약속.")])
    assert target_search_query(far, text) == "앞 장면의 약속. 뒤 장면의 다짐."


def test_an_over_long_target_query_falls_back_to_the_span_quotes():
    # Six spans mid-line: six 205-code-point windows (1,235 with spaces) exceed the 1,000 limit.
    text = "\n".join("가" * 250 + f"{index}번 문장" + "가" * 250 for index in range(12))
    spans = [span_of(text, f"{index}번 문장") for index in range(0, 12, 2)]
    query = target_search_query(question(spans), text)
    assert query == " ".join(f"{index}번 문장" for index in range(0, 12, 2)) and len(query) <= SEARCH_QUERY_LIMIT


def test_change_query_is_check_intent_then_target_sentences():
    draft_ = question([span_of(TEXT, "셋째 줄은 바로 뒤의 반응이다."), span_of(TEXT, "“원래 그렇게 하기로 정했잖아.”")],
                      check_intent="  두 사람의\n사전 약속을 확인한다. ")
    assert change_search_query(draft_) == "두 사람의 사전 약속을 확인한다. “원래 그렇게 하기로 정했잖아.” 셋째 줄은 바로 뒤의 반응이다."
    long = change_search_query(question([span_of(TEXT, LINES[0])], check_intent="가" * 990))
    assert len(long) <= SEARCH_QUERY_LIMIT and long.startswith("가" * 990 + " 첫째 줄")


def test_follow_up_with_searches_adds_one_worker_target_search_after_the_judges():
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["첫 검색", "둘째 검색", "셋째 검색"])
        return decided(payload)

    data = SyntheticData()
    embedding = FakeEmbedding()
    service = SettingValidationService(data, one(judge), budget(), ROLES, search=FakeSearch(data),
                                       embedding_client=embedding)
    result = asyncio.run(service.inspect())
    assert result.status == "COMPLETED"
    vectors = [row for row in result.receipts if row.intent.operation == "VECTOR"]
    assert [row.intent.purpose for row in vectors] == ["판정이 요청한 의미 검색"] * 3 + [WORKER_TARGET_SEARCH_PURPOSE]
    worker = vectors[-1]
    assert (worker.intent.kind, worker.intent.ref) == ("COMPARISON_CONTEXT", "Q1-TARGET_VECTOR")
    assert worker.intent.query == target_search_query(QuestionDraft.model_validate(draft()), TARGET)
    assert embedding.queries[-1] == worker.intent.query and len(embedding.queries) == 4
    assert result.traces["Q1"][0]["searches"] == 3 and result.traces["Q1"][0]["worker_searches"] == 1
    assert result.normalizations["TARGET_SEARCH_INSERTED"] == 1
    assert worker.ref in result.decisions["Q1"].completion.required_reads.receipt_refs


def test_context_only_follow_ups_and_db_decisions_never_add_the_target_search():
    def judge(payload, n):
        return follow_up(payload, contexts=[(0, 2)]) if n == 1 else decided(payload)

    result = inspect(one(judge))
    assert result.status == "COMPLETED" and not [row for row in result.receipts if row.intent.operation == "VECTOR"]
    assert result.traces["Q1"][0]["worker_searches"] == 0 and "TARGET_SEARCH_INSERTED" not in result.normalizations
    plain = inspect(one(lambda payload, n: decided(payload)))
    assert not [row for row in plain.receipts if row.intent.operation == "VECTOR"]


def test_the_worker_target_search_spends_the_lookup_budget_like_any_search():
    def judge(payload, n):
        return follow_up(payload, searches=["첫 검색", "둘째 검색"]) if n == 1 else decided(payload)

    # INPUT, target READ_SOURCE, SUBJECTS and one SETTINGS read are 4 lookups; 2 judge searches + 1 Worker search = 7.
    assert inspect(one(judge), lookups=7).status == "COMPLETED"
    stopped = inspect(one(judge), lookups=6)
    assert (stopped.status, stopped.failure_code) == ("INCOMPLETE", "BUDGET_EXHAUSTED")


# --- prompt -------------------------------------------------------------------------------------------------

def test_the_java_http_driver_rebuilds_the_worker_target_query_from_the_judge_payload():
    """Java's HTTP fixtures cap QUERY_EMBEDDING at 1: their synthetic judge must ask exactly the Worker's sentence."""
    from tests.support.validation_execution_http_client import SyntheticProvider

    text = "\n".join(f"{index}번째 줄 " + "가나다" * 40 for index in range(12)) + "\n"
    spans = [("가운데", 1000), ("끝", len(text) - 30), ("처음", 3)]
    model = ScriptedModel({"TSEG1": [draft(f"q{n}", quote=text[start:start + 6], text=text, start=start, prop=name)
                                     for n, (name, start) in enumerate(spans)]})
    asyncio.run(SettingValidationService(SyntheticData(target=text), model, budget(), ROLES).inspect())
    for payload in model.judge_calls():
        expected = target_search_query(QuestionDraft.model_validate(payload["question"]), text)
        assert SyntheticProvider.worker_target_query(payload) == expected and expected


# --- NO_CONFLICT basis labelling slip (31화 rerun: 7 questions failed with NO_CONFLICT + CHANGE_NOT_FOUND) -----

@pytest.mark.parametrize("basis", ["CHANGE_NOT_FOUND", "AMBIGUOUS_MEANING", "DIRECT_CONTRADICTION"])
def test_no_conflict_with_another_basis_code_is_normalized_instead_of_failing(basis):
    result = inspect(one(lambda payload, n: decided(payload, "NO_CONFLICT", basis)))
    assert result.status == "COMPLETED" and result.question_failures == {}
    assert (result.decisions["Q1"].outcome, result.decisions["Q1"].basis) == ("NO_CONFLICT", "COMPATIBLE")
    assert result.normalizations["NO_CONFLICT_BASIS_NORMALIZED"] == 1


def test_a_finding_with_the_wrong_basis_still_fails():
    result = inspect(one(lambda payload, n: decided(payload, "NEEDS_CONTEXT", "COMPATIBLE")))
    assert result.question_failures["Q1"]["cause"] == "INVALID_DECISION_BASIS_MISMATCH"


def test_judge_prompt_keeps_basis_per_outcome_without_blanket_guess_exemption():
    assert "인물의 착각·오판이므로 NO_CONFLICT" not in JUDGE_SYSTEM
    assert "찾지 못해서 NO_CONFLICT인 경우에도 basis는 COMPATIBLE" in JUDGE_SYSTEM


# --- disagreements misused for the target contradiction (planted-error test: P1 was lost this way) -----------

def test_a_target_disagreement_is_dropped_and_the_quoted_conflict_is_kept():
    misuse = [{"fact_ref": "B1", "evidence_ref": "T1", "reason": "저장된 설정과 이번 원고가 다르다"}]
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", evidence_refs=["B1"], basis_quotes=STORED,
                                                    disagreements=misuse)))
    assert result.question_failures == {}
    decision = result.decisions["Q1"]
    assert decision.outcome == "CONFLICT" and decision.disagreements == []
    assert result.normalizations["INVALID_DISAGREEMENTS_DROPPED"] == 1


def test_a_db_raw_basis_with_only_invalid_disagreements_falls_back_to_the_outcome_basis():
    misuse = [{"fact_ref": "B1", "evidence_ref": "T1", "reason": "다르다"}]
    result = inspect(one(lambda payload, n: decided(payload, "CONFLICT", "DB_RAW_DISAGREEMENT", evidence_refs=["B1"],
                                                    basis_quotes=STORED, disagreements=misuse)))
    assert result.question_failures == {}
    assert (result.decisions["Q1"].outcome, result.decisions["Q1"].basis) == ("CONFLICT", "DIRECT_CONTRADICTION")


def test_prompts_scope_disagreements_name_stored_settings_and_add_places_to_subjects():
    assert "대상 원고(T1)와 이전 기록의 차이는 disagreements가 아니라" in JUDGE_SYSTEM
    assert "settings 값을 \"설정집\"이라고 부르지 않는다" in JUDGE_SYSTEM
    assert "그 장소도 subject_refs에 함께 넣는다" in PLANNER_SYSTEM
