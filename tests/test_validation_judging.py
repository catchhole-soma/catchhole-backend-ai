"""Grid v4 judge state machine with synthetic data, a scripted model and a fake frozen RAG (no provider/DB)."""

import asyncio

import pytest

from app.validation.judging import (
    JUDGE_TARGET_CONTEXT_CHARS,
    WORKER_CHANGE_SEARCH_PURPOSE,
    WORKER_TARGET_SEARCH_PURPOSE,
)
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    TARGET,
    FakeEmbedding,
    FakeSearch,
    ScriptedModel,
    SyntheticData,
    budget,
    character,
    decided,
    draft,
    follow_up,
)


def inspect(data=None, model=None, *, search=None, embedding=None, experiment=False, concurrency=4, **limits):
    data = data or SyntheticData()
    service = SettingValidationService(data, model or ScriptedModel(), budget(**limits), ROLES, search=search,
                                       embedding_client=embedding, experiment=experiment,
                                       judge_concurrency=concurrency)
    return asyncio.run(service.inspect())


def failed_question(result, ref="Q1"):
    """Decision ③: a question-level failure is recorded per question; the run reports SETTING_QUESTIONS_INCOMPLETE."""
    assert result.status == "INCOMPLETE" and result.failure_code == "SETTING_QUESTIONS_INCOMPLETE"
    return result.question_failures[ref]["code"], result.question_failures[ref]["cause"]


def one_question(**changes):
    return {"TSEG1": [draft(**changes)]}


def test_db_first_open_call_decides_without_any_search():
    model = ScriptedModel(one_question())
    embedding = FakeEmbedding()
    data = SyntheticData()
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "NO_CONFLICT"
    (payload,) = model.judge_calls()
    assert payload["form"] == "OPEN" and embedding.queries == []
    _, _, kwargs = next(call for call in model.calls if call[0] == "JUDGE")
    assert kwargs["response_schema"].name == "validation_judge_open_v4"
    assert "search_queries" in kwargs["response_schema"].schema["properties"]
    assert kwargs["model"] == "gpt-6.1-terra" and kwargs["max_output_tokens"] == 2000
    # Current rows only: the character's history and the worldHistory row never reach the judge.
    assert [row["entry_ref"] for row in payload["settings"]] == ["B1"]
    assert "historyFacts" not in payload["settings"][0]["state"]
    assert [(row["intent"]["operation"], row["evidence_refs"]) for row in payload["receipts"]] == [
        ("BASELINE", ["B1"]), ("TARGET_CONTEXT", ["T1"])]
    bundle = result.evidence_bundles["Q1"]
    assert bundle["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"}]
    assert "question_refs" not in bundle and "lookup_intents" not in bundle["question"]
    assert result.decisions["Q1"].follow_ups == [] and result.decisions["Q1"].affected_question_refs == []


def test_follow_up_runs_searches_and_context_then_asks_in_final_form():
    far = TARGET.index("성문")

    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["민수가 의수를 얻은 장면"], contexts=[(far, far + 2)])
        return decided(payload, "NO_CONFLICT")

    data = SyntheticData()
    model, embedding = ScriptedModel(one_question(), judge), FakeEmbedding()
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED"
    first, second = model.judge_calls("Q1")
    assert (first["form"], second["form"]) == ("OPEN", "FINAL")
    final_schema = [kwargs["response_schema"] for role, _, kwargs in model.calls if role == "JUDGE"][1]
    assert final_schema.name == "validation_judge_final_v4"
    assert final_schema.schema["properties"]["action"]["enum"] == ["DECIDED"]
    assert "search_queries" not in final_schema.schema["properties"]
    assert second["additional_target_context"] == [{"source_ref": "T1", "start": far, "end": far + 2, "text": "성문"}]
    operations = [(row["intent"]["operation"], row["intent"]["kind"]) for row in second["receipts"]]
    assert operations == [("BASELINE", "COMPARISON_CONTEXT"), ("TARGET_CONTEXT", "COMPARISON_CONTEXT"),
                          ("TARGET_CONTEXT", "COMPARISON_CONTEXT"), ("VECTOR", "COMPARISON_CONTEXT"),
                          ("VECTOR", "COMPARISON_CONTEXT")]
    assert second["receipts"][3]["intent"]["purpose"] == "판정이 요청한 의미 검색"
    # The Worker's own target-text search runs after the judge's queries (2026-11-03).
    assert second["receipts"][4]["intent"]["purpose"] == WORKER_TARGET_SEARCH_PURPOSE
    assert [row["source_ref"] for row in second["evidence"]] == ["R2"]
    assert embedding.queries == ["민수가 의수를 얻은 장면", "민수는 양손으로 검을 쥐었다. 그는 성문 앞에서 기다렸다."]
    trace = result.traces["Q1"][0]
    assert trace["searches"] == 1 and trace["worker_searches"] == 1 and trace["context_requests"] == 1


# The stored value that contradicts "양손으로 검을 쥐었다" (B1 STATUS:손 = 왼손 상실): a finding's basis quote.
CONTRADICTION = [{"ref": "B1", "quote": "왼손 상실"}]


def test_suspected_conflict_gets_one_worker_change_search_then_a_final_call():
    def judge(payload, n):
        return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"], basis_quotes=CONTRADICTION)

    data = SyntheticData()
    model, embedding = ScriptedModel(one_question(), judge), FakeEmbedding()
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED"
    assert [payload["form"] for payload in model.judge_calls("Q1")] == ["OPEN", "FINAL"]
    change = [row for row in result.receipts if row.intent.kind == "CHANGES_EXCEPTIONS"]
    assert len(change) == 1 and change[0].intent.operation == "VECTOR"
    # 2026-11-03: check_intent, then the target sentence (no longer subject mentions + property).
    assert change[0].intent.purpose == WORKER_CHANGE_SEARCH_PURPOSE
    assert change[0].intent.query == "양손 사용 가능 여부 민수는 양손으로 검을 쥐었다."
    decision = result.decisions["Q1"]
    assert decision.outcome == "SUSPECTED_CONFLICT" and [row.quote for row in decision.basis_quotes] == ["왼손 상실"]
    assert change[0].ref in decision.completion.changes_exceptions.receipt_refs
    assert result.normalizations["CHANGE_SEARCH_INSERTED"] == 1


def test_three_calls_at_most_follow_up_then_suspected_then_change_search():
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["민수의 손 상태 변화"])
        return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"], basis_quotes=CONTRADICTION)

    data = SyntheticData()
    model = ScriptedModel(one_question(), judge)
    result = inspect(data, model, search=FakeSearch(data), embedding=FakeEmbedding())
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    assert [payload["form"] for payload in model.judge_calls("Q1")] == ["OPEN", "FINAL", "FINAL"]
    assert [row["call"] for row in result.traces["Q1"]] == [1, 2, 3]


def test_no_prior_episodes_vector_is_not_applicable_and_never_embeds():
    def judge(payload, n):
        return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"], basis_quotes=CONTRADICTION)

    data = SyntheticData(prior={})
    embedding = FakeEmbedding()
    model = ScriptedModel(one_question(), judge)
    result = inspect(data, model, search=None, embedding=embedding)
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    (change,) = [row for row in result.receipts if row.intent.operation == "VECTOR"]
    assert change.status == "SUCCEEDED" and change.evidence_refs == ()
    assert change.readiness["vector_status"] == "NOT_APPLICABLE"
    assert embedding.queries == []
    _, _, kwargs = next(call for call in model.calls if call[0] == "JUDGE")
    assert "search_queries" in kwargs["response_schema"].schema["properties"]


@pytest.mark.parametrize("search", ["absent", "not-ready"])
def test_episodes_without_a_ready_index_offer_no_search_and_fail_a_needed_change_search(search):
    def judge(payload, n):
        return decided(payload, "SUSPECTED_CONFLICT", evidence_refs=["B1"])

    data = SyntheticData()
    model = ScriptedModel(one_question(), judge)
    fake = FakeSearch(data, ready=False) if search == "not-ready" else None
    result = inspect(data, model, search=fake, embedding=FakeEmbedding())
    assert result.status == "INCOMPLETE" and failed_question(result) == ("VECTOR_NOT_READY", None)
    _, _, kwargs = next(call for call in model.calls if call[0] == "JUDGE")
    assert "search_queries" not in kwargs["response_schema"].schema["properties"]
    failed = [row for row in result.receipts if row.status == "FAILED"]
    assert [row.failure_code for row in failed] == ["VECTOR_NOT_READY"]
    assert result.normalizations["VECTOR_OFFERED"] == 0


def test_partial_index_at_search_time_keeps_the_failed_receipt_and_readiness():
    def judge(payload, n):
        return follow_up(payload, searches=["민수의 의수"])

    data = SyntheticData()
    model = ScriptedModel(one_question(), judge)

    class Flaky(FakeSearch):
        """Ready when the job starts (search offered), partial by the time the search runs."""

        async def readiness(self, frozen):
            return FakeSearch(data)._readiness()

    flaky = Flaky(data, partial=True)
    result = inspect(data, model, search=flaky, embedding=FakeEmbedding())
    assert failed_question(result)[0] == "VECTOR_INDEX_PARTIAL"
    (failed,) = [row for row in result.receipts if row.status == "FAILED"]
    assert failed.readiness["vector_status"] == "PARTIAL" and "VECTOR_INDEX_PARTIAL" in failed.readiness["limitations"]


def test_identical_queries_reuse_one_search_and_one_embedding_even_in_flight():
    plans = {"TSEG1": [draft("a"), draft("b", quote="성문 앞에서 기다렸다", subject_refs=("S2",), mentions=("성문",),
                                          prop="성문 위치")]}

    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["  성문과 민수의 이전 설정  "])
        return decided(payload)

    data = SyntheticData()
    embedding, search = FakeEmbedding(), FakeSearch(data)
    result = inspect(data, ScriptedModel(plans, judge), search=search, embedding=embedding)
    assert result.status == "COMPLETED"
    judged = [row for row in result.receipts if row.intent.purpose == "판정이 요청한 의미 검색"]
    assert len(judged) == 2 and embedding.queries.count("  성문과 민수의 이전 설정  ") == 1
    original, reused = sorted(judged, key=lambda row: row.reused_from is not None)
    assert reused.reused_from == original.ref and reused.evidence_refs == original.evidence_refs
    # Both questions span the whole short target, so their Worker target-text searches are one search too.
    targets = [row for row in result.receipts if row.intent.purpose == WORKER_TARGET_SEARCH_PURPOSE]
    assert len(targets) == 2 and sum(row.reused_from is not None for row in targets) == 1
    assert len(search.searches) == 2 and len(embedding.queries) == 2
    assert result.normalizations["RAG_CACHE_HITS"] == 2 and result.normalizations["VECTOR_SEARCHES"] == 2


def test_settings_are_read_in_batches_of_100_subjects_once_per_run():
    entries = [character(f"B{n}", f"character:{n}", f"인물{n}", {"위치": {"factType": "STATUS", "factKey": "위치", "factValue": "성문"}}) for n in range(1, 131)]
    data = SyntheticData(entries=entries)
    subjects = [f"S{n}" for n in range(1, 131)]
    plans = {"TSEG1": [draft("a", subject_refs=subjects[:70], mentions=("인물",)),
                       draft("b", quote="성문 앞에서 기다렸다", subject_refs=subjects[60:], mentions=("인물",),
                             prop="위치")]}
    result = inspect(data, ScriptedModel(plans))
    assert result.status == "COMPLETED"
    reads = [row for row in data.reads if row[0] == "SETTINGS"]
    assert [len(row[1]) for row in reads] == [100, 30]
    assert len(result.evidence_bundles["Q2"]["settings"]) == 70


def test_target_context_is_300_code_points_around_each_span():
    text = "가" * 400 + "민수는 검을 쥐었다." + "나" * 400
    data = SyntheticData(target=text)
    model = ScriptedModel({"TSEG1": [draft(quote="민수는 검을 쥐었다.", text=text)]})
    inspect(data, model, segment_chars=10000)
    (payload,) = model.judge_calls()
    (context,) = payload["target_context"]
    assert context["start"] == 400 - JUDGE_TARGET_CONTEXT_CHARS
    assert context["text"] == text[100:400 + 11 + 300] and len(context["text"]) == 611


def test_four_judges_run_at_once_and_never_more():
    quotes = ["민수는 양손으로 검을 쥐었다.", "그는 성문 앞에서 기다렸다."]
    plans = {"TSEG1": [draft(f"q{n}", quote=quotes[n % 2], prop=f"속성{n}") for n in range(9)]}
    started = []

    async def gate(payload):
        if payload["role"] == "JUDGE":
            started.append(payload["question_ref"])
            await asyncio.sleep(0.01)

    model = ScriptedModel(plans, gate=gate)
    result = inspect(SyntheticData(), model)
    assert result.status == "COMPLETED" and len(result.decisions) == 9
    assert model.peak == 4
    assert list(result.decisions) == [f"Q{n}" for n in range(1, 10)]


def test_experiment_mode_records_a_contract_error_and_judges_the_rest():
    plans = {"TSEG1": [draft("a"), draft("b", quote="성문 앞에서 기다렸다", subject_refs=("S2",), mentions=("성문",),
                                          prop="성문 위치")]}

    def judge(payload, n):
        return "{not json" if payload["question_ref"] == "Q1" else decided(payload)

    result = inspect(SyntheticData(), ScriptedModel(plans, judge), experiment=True)
    assert result.status == "INCOMPLETE" and result.failure_code == "MODEL_CONTRACT_INVALID"
    assert result.question_failures == {"Q1": {"code": "MODEL_CONTRACT_INVALID", "cause": "MODEL_CONTRACT_JSON_INVALID",
                                               "call": 1}}
    assert result.decisions["Q2"].outcome == "NO_CONFLICT"
    assert result.traces["Q1"][0]["error"] == "MODEL_CONTRACT_INVALID"


def test_production_records_the_failed_question_and_judges_the_rest():
    """Owner decision ③: a question-level error ends that question only; the run is still not a clean success."""
    plans = {"TSEG1": [draft(f"q{n}", prop=f"속성{n}") for n in range(8)]}

    def judge(payload, n):
        return "{not json" if payload["question_ref"] == "Q1" else decided(payload)

    model = ScriptedModel(plans, judge)
    result = inspect(SyntheticData(), model, concurrency=2)
    assert result.status == "INCOMPLETE" and result.failure_code == "SETTING_QUESTIONS_INCOMPLETE"
    assert result.failure_stage == "JUDGE" and result.failure_cause is None
    assert result.question_failures == {"Q1": {"code": "MODEL_CONTRACT_INVALID", "cause": "MODEL_CONTRACT_JSON_INVALID",
                                               "call": 1}}
    assert set(result.decisions) == {f"Q{n}" for n in range(2, 9)} and len(model.judge_calls()) == 8


def test_switching_the_policy_off_restores_fail_fast_but_keeps_judges_already_running(monkeypatch):
    monkeypatch.setattr("app.validation.failures.CONTINUE_AFTER_QUESTION_FAILURE", False)
    plans = {"TSEG1": [draft(f"q{n}", prop=f"속성{n}") for n in range(8)]}

    def judge(payload, n):
        return "{not json" if payload["question_ref"] == "Q1" else decided(payload)

    model = ScriptedModel(plans, judge)
    result = inspect(SyntheticData(), model, concurrency=2)
    assert result.status == "INCOMPLETE" and result.failure_code == "MODEL_CONTRACT_INVALID"
    assert result.failure_stage == "JUDGE:Q1" and result.question_failures == {}
    # Q1 and Q2 started together; Q2 finished, nothing after the failure was dispatched.
    assert set(result.decisions) == {"Q2"} and len(model.judge_calls()) < 8


@pytest.mark.parametrize(("answer", "cause"), [
    (lambda payload: follow_up(payload), "INVALID_FOLLOW_UP_EMPTY"),
    (lambda payload: follow_up(payload, searches=["가", "나", "다", "라"]), "INVALID_FOLLOW_UP_TOO_MANY_SEARCHES"),
    (lambda payload: follow_up(payload, contexts=[(0, 10_000)]), "INVALID_TARGET_CONTEXT_REQUEST"),
    (lambda payload: follow_up(payload, searches=["   "]), "INVALID_SEARCH_REQUEST"),
])
def test_unreadable_follow_ups_are_rejected_before_any_read(answer, cause):
    data = SyntheticData()
    embedding = FakeEmbedding()
    result = inspect(data, ScriptedModel(one_question(), lambda payload, n: answer(payload)),
                     search=FakeSearch(data), embedding=embedding)
    assert failed_question(result) == ("INVALID_FOLLOW_UP", cause)
    assert embedding.queries == []


def test_final_form_cannot_follow_up_and_decided_requests_are_ignored():
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, contexts=[(0, 2)])
        return follow_up(payload, searches=["다시 찾기"])

    data = SyntheticData()
    result = inspect(data, ScriptedModel(one_question(), judge), search=FakeSearch(data), embedding=FakeEmbedding())
    assert failed_question(result)[0] == "MODEL_CONTRACT_INVALID"

    def ignored(payload, n):
        return decided(payload, search_queries=["무시됨"], context_requests=[{"start": 0, "end": 2}])

    embedding = FakeEmbedding()
    result = inspect(data, ScriptedModel(one_question(), ignored), search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED" and embedding.queries == []
    assert result.normalizations["DECIDED_REQUESTS_IGNORED"] == 1


def test_sql_decimal_readiness_counts_become_json_integers_in_receipts():
    import json
    from decimal import Decimal

    from app.retrieval.search_contracts import SearchReadiness

    class DecimalSearch(FakeSearch):
        def _readiness(self):
            value = super()._readiness()
            return SearchReadiness(*(Decimal(n) for n in (value.source_count, value.text_ready_sources,
                                                         value.vector_ready_sources, value.text_chunks,
                                                         value.vector_chunks)), value.limitations)

    def judge(payload, n):
        return follow_up(payload, searches=["민수의 의수"]) if n == 1 else decided(payload)

    data = SyntheticData()
    result = inspect(data, ScriptedModel(one_question(), judge), search=DecimalSearch(data), embedding=FakeEmbedding())
    assert result.status == "COMPLETED"
    vectors = [row for row in result.receipts if row.intent.operation == "VECTOR"]
    assert len(vectors) == 2 and all(type(row.readiness["vector_chunks"]) is int for row in vectors)
    json.dumps(result.evidence_bundles["Q1"])


def test_missing_t1_in_comparison_context_is_filled_and_counted():
    def judge(payload, n):
        answer = decided(payload)
        answer["completion"]["comparison_context"]["evidence_refs"] = ["B1"]  # the judge forgot the target T1
        return answer

    result = inspect(SyntheticData(), ScriptedModel(one_question(), judge))
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "NO_CONFLICT"
    assert result.decisions["Q1"].completion.comparison_context.evidence_refs == ["T1", "B1"]
    assert result.normalizations["COMPARISON_CONTEXT_TARGET_FILLED"] == 1


def test_missing_db_basis_requires_vector_before_accepting_no_conflict():
    data = SyntheticData(entries=[])
    model = ScriptedModel(one_question(subject_refs=()))
    embedding = FakeEmbedding()
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding, experiment=True)
    assert result.status == "COMPLETED"
    assert [p["form"] for p in model.judge_calls()] == ["OPEN", "FINAL"]
    assert len(embedding.queries) == 1
    assert any(r.intent.operation == "VECTOR" and r.status == "SUCCEEDED" for r in result.receipts)


def test_missing_db_basis_without_ready_rag_is_incomplete_not_no_conflict():
    model = ScriptedModel(one_question(subject_refs=()))
    result = inspect(SyntheticData(entries=[]), model)
    assert failed_question(result) == ("VECTOR_NOT_READY", None)
    assert "Q1" not in result.decisions


@pytest.mark.parametrize("sufficient,quote", [(False, "왼손 상실"), (True, "존재하지 않는 근거")])
def test_a_broad_or_invalid_db_quote_cannot_skip_the_required_search(sufficient, quote):
    def judge(payload, n):
        return decided(payload, db_evidence_sufficient=sufficient,
                       basis_quotes=[{"ref": "B1", "quote": quote}])
    data, embedding = SyntheticData(), FakeEmbedding()
    model = ScriptedModel(one_question(), judge)
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED"
    assert len(model.judge_calls()) == 2 and len(embedding.queries) == 1
    assert result.normalizations["DB_GAP_SEARCH_INSERTED"] == 1
    assert "db_evidence_sufficient" not in result.decisions["Q1"].model_dump()


def test_context_only_follow_up_does_not_satisfy_required_source_search():
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, contexts=[(0, len(TARGET))])
        return decided(payload, db_evidence_sufficient=False)
    data, embedding = SyntheticData(), FakeEmbedding()
    model = ScriptedModel(one_question(), judge)
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED"
    assert [p["form"] for p in model.judge_calls()] == ["OPEN", "FINAL", "FINAL"]
    assert len(embedding.queries) == 1


def test_db_gap_with_empty_scope_records_not_applicable_without_embedding():
    data, embedding = SyntheticData(entries=[], prior={}), FakeEmbedding()
    model = ScriptedModel(one_question(subject_refs=()))
    result = inspect(data, model, search=FakeSearch(data), embedding=embedding)
    assert result.status == "COMPLETED" and not embedding.queries
    vectors = [r for r in result.receipts if r.intent.operation == "VECTOR"]
    assert len(vectors) == 1 and vectors[0].readiness["vector_status"] == "NOT_APPLICABLE"
    assert len(model.judge_calls()) == 2


def test_required_db_gap_search_failure_remains_incomplete():
    data = SyntheticData(entries=[])
    result = inspect(data, ScriptedModel(one_question(subject_refs=())),
                     search=FakeSearch(data, fail="SEARCH_FAILED"), embedding=FakeEmbedding())
    assert failed_question(result) == ("SEARCH_FAILED", None)
    assert not result.decisions


def test_sufficiency_is_required_in_provider_schema_and_strictly_boolean():
    from pydantic import ValidationError

    from app.validation.contracts import JudgeFinalAnswer, JudgeOpenAnswer
    from app.validation.schemas import judge_schema
    for final, model in [(False, JudgeOpenAnswer), (True, JudgeFinalAnswer)]:
        schema = judge_schema(entry_refs=(), final=final, search_available=True)
        assert "db_evidence_sufficient" in schema["required"]
        assert schema["properties"]["db_evidence_sufficient"]["type"] == "boolean"
        with pytest.raises(ValidationError):
            model.model_validate({"db_evidence_sufficient": "true"})
