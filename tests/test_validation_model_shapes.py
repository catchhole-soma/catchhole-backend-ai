"""GH331 (grid v4): real Terra output shapes pass only through deterministic, meaning-preserving normalization.

No model, network or DB is called. The recorded shapes come from the GH314 V1 real-model evaluation
(`docs/episode-validation-evaluation.md` sections 3-4): `TSEG1` span references, quotes that are exact while
start/end drift by 1-4 code points, FOLLOW_UP with a filled completion, NO_CONFLICT with an impact and
required_reads missing the automatic receipts.
"""

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

import pytest

from app.llm.responses import LlmTextResponse
from app.retrieval.search_contracts import SearchReadiness
from app.validation.contracts import (
    IncompleteInspection,
    QuestionBatch,
    Span,
    anchor_span,
    normalize_batch,
    validate_span,
)
from app.validation.schemas import judge_schema, planner_schema
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import ROLES, FakeEmbedding, SyntheticData, budget

FIXTURE = Path(__file__).parent / "fixtures" / "episode_validation_cases.json"
EPISODES = {episode["episodeRef"]: episode["text"]
            for work in json.loads(FIXTURE.read_text(encoding="utf-8"))["works"]
            for episode in work["episodes"]}
B2 = EPISODES["B2"]
WAKE = "미나는 평소처럼 새벽 네 시에 일어나 호두빵을 구웠다."
POST = "우체부 준호가 자전거를 타고 빵집 앞을 지나갔다."
MARKET = "광장에는 벌써 일요일 장터가 열려 있었다."


def span(quote, start=None, end=None, ref="TSEG1"):
    start = B2.index(quote) if start is None else start
    return {"source_ref": ref, "start": start, "end": start + len(quote) if end is None else end, "quote": quote}


# --- normalizer ----------------------------------------------------------------------------------

def test_segment_alias_becomes_target_ref_and_drifted_offsets_follow_the_exact_quote():
    stats = Counter()
    aliased = anchor_span(Span(**span(WAKE)), B2, aliases={"TSEG1"}, stats=stats)
    assert (aliased.source_ref, aliased.start, aliased.end) == ("T1", 24, 54)
    # Recorded real shape: [112,138) for a 27-code-point quote that really sits at [112,139).
    drifted = anchor_span(Span(**span(POST, 112, 138)), B2, aliases={"TSEG1"}, stats=stats)
    assert (drifted.source_ref, drifted.start, drifted.end) == ("T1", 112, 139)
    shifted = anchor_span(Span(**span(MARKET, B2.index(MARKET) + 2)), B2, aliases={"TSEG1"}, stats=stats)
    assert B2[shifted.start:shifted.end] == MARKET and validate_span(shifted, B2)
    assert stats == {"TARGET_REF_ALIAS": 3, "SPAN_REANCHORED": 2}


def test_unknown_refs_absent_quotes_ties_and_windows_keep_the_strict_rejection():
    for value in (Span(**span(WAKE, ref="T2")), Span(**span(WAKE, ref="R1"))):
        with pytest.raises(ValueError, match="INVALID_EXACT_SPAN"):
            validate_span(anchor_span(value, B2, aliases={"TSEG1"}), B2)
    absent = Span(source_ref="T1", start=24, end=30, quote="원고에 없는 문장")
    assert anchor_span(absent, B2) is absent
    text = "보리  보리"
    tie = Span(source_ref="T1", start=2, end=4, quote="보리")
    assert anchor_span(tie, text) is tie  # equidistant repeats are ambiguous
    outside = Span(source_ref="T1", start=1, end=3, quote="보리")
    moved = anchor_span(outside, text, accept=lambda start, end: start >= 4)
    assert (moved.start, moved.end) == (4, 6)
    assert anchor_span(outside, text, accept=lambda start, end: start >= 9) is outside
    exact = Span(source_ref="T1", start=4, end=6, quote="보리")
    assert anchor_span(exact, text) is exact  # an exact span is never moved to another repeat


def test_batch_normalization_collapses_duplicate_spans():
    question = {"local_ref": "wake", "target_spans": [span(WAKE), span(WAKE, 25)], "subject_refs": ["S1"],
                "subject_mentions": ["미나"], "property_or_rule": "기상", "time_context": "아침", "conditions": [],
                "assertion_mode": "NARRATION", "check_intent": "습관", "reason": "확인"}
    batch = QuestionBatch.model_validate({"questions": [question], "segments": [
        {"segment_ref": "TSEG1", "status": "PLANNED", "reason": "확인"}]})
    stats = Counter()
    (normalized,) = normalize_batch(batch, B2, aliases={"TSEG1"}, stats=stats).questions
    assert [(row.source_ref, row.start) for row in normalized.target_spans] == [("T1", 24)]
    assert stats["SPAN_REANCHORED"] == 1


# --- schemas -------------------------------------------------------------------------------------

def test_planner_and_judge_schemas_constrain_reference_kinds_and_stay_identical_per_report():
    planner = planner_schema(segment_refs=["TSEG1", "TSEG2"], subject_refs=["S1", "S2"])
    assert planner == planner_schema(segment_refs=["TSEG9"], subject_refs=["S1"])  # kind patterns only
    assert set(planner["properties"]) == {"questions", "segments"}
    draft = planner["$defs"]["QuestionDraft"]
    assert "lookup_intents" not in draft["properties"] and "lookup_intents" not in draft["required"]
    assert planner["$defs"]["Span"]["properties"]["source_ref"] == {"type": "string", "enum": ["T1"]}
    assert planner["$defs"]["SegmentCoverage"]["properties"]["segment_ref"]["pattern"] == "^(TSEG[1-9][0-9]*)$"

    open_schema = judge_schema(entry_refs=(), final=False, search_available=True)
    assert open_schema["properties"]["search_queries"] == {
        "type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "maxItems": 3}
    assert open_schema["properties"]["action"]["enum"] == ["FOLLOW_UP", "DECIDED"]
    assert set(open_schema["required"]) == set(open_schema["properties"])
    assert "follow_ups" not in open_schema["properties"] and "affected_question_refs" not in open_schema["properties"]
    request = open_schema["$defs"]["ContextRequest"]
    assert set(request["properties"]) == {"start", "end"} and request["additionalProperties"] is False
    no_search = judge_schema(entry_refs=(), final=False, search_available=False)
    assert "search_queries" not in no_search["properties"] and "search_queries" not in no_search["required"]
    assert "context_requests" in no_search["properties"]

    final = judge_schema(entry_refs=(), final=True, search_available=True)
    assert final["properties"]["action"] == {"type": "string", "enum": ["DECIDED"]}
    assert {"search_queries", "context_requests"}.isdisjoint(final["properties"])
    assert final["properties"]["completion"] == {"$ref": "#/$defs/CompletionAssessment"}
    assert "null" not in json.dumps(final["properties"]["outcome"])
    for schema in (open_schema, final):
        assert schema["$defs"]["Assessment"]["properties"]["receipt_refs"]["items"]["pattern"] == "^(L[1-9][0-9]*)$"
        assert schema["$defs"]["Assessment"]["properties"]["evidence_refs"]["items"]["pattern"] == \
            "^(T1|B[1-9][0-9]*|E[1-9][0-9]*)$"


# --- recorded real-model shapes through the full validation path ------------------------------------

def b2_data(**kwargs):
    entries = [{"entryRef": "B1", "domain": "characters", "subjectRef": "character:mina",
                "state": {"name": "미나", "aliases": [], "slots": {"HABIT:기상": {"factValue": "새벽 네 시"}}}}]
    return SyntheticData(target=B2, entries=entries, prior={"R1": EPISODES["B1"]}, **kwargs)


def question(local_ref, spans, prop):
    return {"local_ref": local_ref, "target_spans": spans, "subject_refs": ["S1"], "subject_mentions": ["미나"],
            "property_or_rule": prop, "time_context": "일요일 아침", "conditions": [], "assertion_mode": "NARRATION",
            "check_intent": "기존 습관과 비교", "reason": "반복 습관 확인"}


class RecordedTerra:
    """Replays the recorded real Terra shapes; only the provider boundary is synthetic."""

    def __init__(self):
        self.calls = []

    async def count_input_tokens(self, **kwargs):
        return 80

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        self.calls.append((payload["role"], payload, kwargs))
        if payload["role"] == "PLANNER":
            ref = payload["segment"]["ref"]
            questions = [question("wake", [span(WAKE)], "새벽 기상"), question("post", [span(POST, 112, 138)], "우체부 방문"),
                         question("market", [span(MARKET, 223, 245)], "장터 가판대")]
            value = {"questions": questions, "segments": [{"segment_ref": ref, "status": "PLANNED", "reason": "확인"}]}
        else:
            value = self.judge(payload)
        return LlmTextResponse(text=json.dumps(value, ensure_ascii=False), input_token_count=80, output_token_count=30)

    def judge(self, payload):
        receipts = [row["ref"] for row in payload["receipts"]]
        refs = ["T1", *(row["entry_ref"] for row in payload["settings"]), *(row["ref"] for row in payload["evidence"])]
        assessment = {"reason": "같은 대상과 시점을 확인했다", "evidence_refs": refs, "receipt_refs": receipts}
        # Recorded drift: the judge echoes the question spans as TSEG1 and one code point early.
        echoed = [{**row, "source_ref": "TSEG1", "start": row["start"] - 1, "end": row["end"] - 1}
                  for row in payload["question"]["target_spans"]]
        value = {
            "action": "DECIDED", "outcome": "NO_CONFLICT", "basis": "COMPATIBLE", "reason": "기존 습관과 같다",
            # S5: NO_CONFLICT with an impact grade.
            "impact": "REFERENCE", "impact_reason": "부수 묘사", "target_spans": echoed, "evidence_refs": refs,
            # required_reads cites only the last receipt (BASELINE/TARGET_CONTEXT omitted).
            "completion": {"comparison_context": assessment, "changes_exceptions": assessment,
                           "no_pending_clues": assessment, "required_reads": {**assessment, "receipt_refs": receipts[-1:]}},
            "disagreements": []}
        if payload["form"] == "OPEN":
            # S4: FOLLOW_UP that still fills outcome/impact/completion; it asks for wider target context.
            value.update(action="FOLLOW_UP", search_queries=[], context_requests=[{"start": 0, "end": 40}])
        return value


def inspect(model, *, search=None, embedding_client=None, seconds=30, data=None):
    limits = budget(seconds=seconds, segment_chars=2000, context_chars=300)
    service = SettingValidationService(data or b2_data(), model, limits, ROLES, search=search,
                                       embedding_client=embedding_client)
    return asyncio.run(service.inspect())


def test_recorded_real_terra_shapes_are_accepted_after_normalization(caplog):
    caplog.set_level(logging.INFO, logger="app.validation.service")
    model = RecordedTerra()
    result = inspect(model)
    assert result.status == "COMPLETED", (result.failure_code, result.failure_cause)
    assert len(result.decisions) == 3
    for decision in result.decisions.values():
        assert decision.outcome == "NO_CONFLICT" and decision.impact is None
        assert all(row.source_ref == "T1" for row in decision.target_spans)
    stats = result.normalizations
    assert stats["TARGET_REF_ALIAS"] >= 3 and stats["SPAN_REANCHORED"] >= 2
    assert stats["FOLLOW_UP_DECISION_FIELDS_IGNORED"] == 3 and stats["NO_CONFLICT_IMPACT_CLEARED"] == 3
    assert stats["REQUIRED_READS_COMPLETED"] == 3
    assert "setting inspection completed" in caplog.text


@pytest.mark.parametrize("change,cause", [
    (lambda value: value.update(outcome="NEEDS_CONTEXT", basis="AMBIGUOUS_MEANING", impact=None,
                                impact_reason=None), "INVALID_DECISION_IMPACT_MISSING"),
    # Recorded B2 diagnostic re-check shape: no_pending_clues cited nothing.
    (lambda value: value["completion"].update(no_pending_clues={"reason": "새 단서 없음", "evidence_refs": [],
                                                                "receipt_refs": []}),
     "INVALID_DECISION_NO_PENDING_CLUES"),
    (lambda value: value["completion"]["required_reads"].update(receipt_refs=["L99"]),
     "INVALID_DECISION_REQUIRED_READS"),
])
def test_remaining_decision_rules_stay_strict_with_specific_causes(change, cause):
    class Strict(RecordedTerra):
        def judge(self, payload):
            value = super().judge(payload)
            if value["action"] == "DECIDED":
                value["completion"] = json.loads(json.dumps(value["completion"]))
                change(value)
            return value

    result = inspect(Strict())
    # Decision ③: the run reports SETTING_QUESTIONS_INCOMPLETE; each failed question keeps its specific cause.
    assert (result.status, result.failure_code) == ("INCOMPLETE", "SETTING_QUESTIONS_INCOMPLETE")
    assert {(row["code"], row["cause"]) for row in result.question_failures.values()} == {("INVALID_DECISION", cause)}


def test_judge_span_echo_is_replaced_by_the_question_spans_but_invented_quotes_are_rejected():
    class Echo(RecordedTerra):
        def __init__(self, spans):
            super().__init__()
            self.spans = spans

        def judge(self, payload):
            value = super().judge(payload)
            if value["action"] == "DECIDED" and payload["question_ref"] == "Q1":
                value["target_spans"] = self.spans
            return value

    for spans in ([], [span(POST)]):  # omitted, or another exact sentence of the target
        result = inspect(Echo(spans))
        assert result.status == "COMPLETED" and result.normalizations["TARGET_SPANS_REPLACED"] == 1
        assert result.decisions["Q1"].target_spans == result.questions[0].draft.target_spans
    invented = inspect(Echo([{"source_ref": "T1", "start": 0, "end": 5, "quote": "없는 인용"}]))
    assert invented.failure_code == "SETTING_QUESTIONS_INCOMPLETE"
    assert {key: row["code"] for key, row in invented.question_failures.items()} == {"Q1": "INVALID_INSPECTION_CONTRACT"}
    assert invented.question_failures["Q1"]["cause"] == "INVALID_EXACT_SPAN"


def test_model_contract_and_provider_failures_keep_a_class_level_cause_only():
    class Literal(RecordedTerra):
        def judge(self, payload):
            return {**super().judge(payload), "action": "DONE"}

    result = inspect(Literal())
    assert result.failure_code == "SETTING_QUESTIONS_INCOMPLETE"
    assert {(row["code"], row["cause"]) for row in result.question_failures.values()} == {
        ("MODEL_CONTRACT_INVALID", "MODEL_CONTRACT_LITERAL_ERROR")}

    class Broken(RecordedTerra):
        async def create_text_response(self, **kwargs):
            raise ConnectionResetError("provider body must never be copied")

    broken = inspect(Broken())
    assert (broken.failure_code, broken.failure_cause) == ("MODEL_CALL_FAILED", "MODEL_CALL_CONNECTION_RESET_ERROR")
    assert broken.failure_stage == "PLANNER:TSEG1"


def test_worker_reports_the_specific_cause_and_keeps_typo_codes():
    from app.validation.contracts import InspectionResult, TypoCheckResult
    from app.worker.episode_validation_worker import reported_failure_code

    collapsed = InspectionResult("INCOMPLETE", failure_code="INVALID_INSPECTION_CONTRACT",
                                 failure_cause="INVALID_EXACT_SPAN")
    assert reported_failure_code(collapsed) == "INVALID_EXACT_SPAN"
    assert reported_failure_code(InspectionResult("INCOMPLETE", failure_code="BUDGET_EXHAUSTED")) == "BUDGET_EXHAUSTED"
    questions = InspectionResult("INCOMPLETE", failure_code="SETTING_QUESTIONS_INCOMPLETE", failure_stage="JUDGE")
    assert reported_failure_code(questions) == "SETTING_QUESTIONS_INCOMPLETE"
    typo = TypoCheckResult(target_hash="a" * 64, status="INCOMPLETE", segments=[], suggestions=[],
                           failure_code="INVALID_EXACT_SPAN")
    assert reported_failure_code(typo) == "INVALID_EXACT_SPAN"
    with pytest.raises(IncompleteInspection) as raised:
        raise IncompleteInspection("INVALID_DECISION", "not a safe code")
    assert raised.value.cause is None  # free text never becomes a diagnostic


# --- search offer (GH344 readiness, grid v4) ---------------------------------------------------------

HANG = object()


class Index:
    def __init__(self, readiness=None):
        self.value, self.readiness_reads, self.searches = readiness, 0, 0

    async def readiness(self, frozen):
        self.readiness_reads += 1
        if self.value is HANG:
            await asyncio.sleep(30)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value or SearchReadiness(1, 1, 1, 3, 3)

    async def search(self, frozen, query, embedding_client, limit):
        self.searches += 1
        raise AssertionError("not expected")


def offered(model):
    return "search_queries" in next(kwargs["response_schema"].schema["properties"]
                                    for role, _, kwargs in model.calls if role == "JUDGE")


@pytest.mark.parametrize(("readiness", "embedding", "expected"), [
    (SearchReadiness(1, 1, 1, 3, 3), True, True),
    (SearchReadiness(1, 1, 0, 3, 0, ("VECTOR_INDEX_PARTIAL",)), True, False),
    (SearchReadiness(1, 1, 1, 3, 3), False, False),
])
def test_search_is_offered_only_after_one_complete_readiness_read(caplog, readiness, embedding, expected):
    caplog.set_level(logging.INFO, logger="app.validation.service")
    index, model = Index(readiness), RecordedTerra()
    result = inspect(model, search=index, embedding_client=FakeEmbedding() if embedding else None)
    assert result.status == "COMPLETED"
    assert offered(model) is expected and result.normalizations["VECTOR_OFFERED"] == int(expected)
    assert index.readiness_reads == (1 if embedding else 0) and index.searches == 0


def test_failed_or_stuck_readiness_read_offers_no_search_and_logs_the_type_only(monkeypatch, caplog):
    from app.validation import service

    caplog.set_level(logging.INFO, logger="app.validation.service")
    index, model = Index(OSError("postgresql://reader:secret@db.invalid/catchhole")), RecordedTerra()
    result = inspect(model, search=index, embedding_client=FakeEmbedding())
    assert result.status == "COMPLETED" and not offered(model)
    assert "vector search readiness unavailable error=OSError job=-; search not offered" in caplog.text
    assert "secret" not in caplog.text and "db.invalid" not in caplog.text

    import time

    monkeypatch.setattr(service, "VECTOR_READINESS_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    stuck = inspect(RecordedTerra(), search=Index(HANG), embedding_client=FakeEmbedding(), seconds=20)
    assert time.monotonic() - started < 5 and stuck.status == "COMPLETED"
    assert "vector search readiness unavailable error=TimeoutError job=-" in caplog.text


def test_statement_timeout_in_frozen_search_maps_to_search_failed():
    """A database-cancelled read (statement_timeout, SQLSTATE 57014) closes its session and maps to a fixed code."""
    from sqlalchemy.exc import OperationalError

    from app.validation.search import FrozenRetrievalSearchService

    class QueryCanceled(Exception):
        sqlstate = "57014"

    class CanceledSession:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True
            return False

        def _cancel(self, *args, **kwargs):
            raise OperationalError("SELECT ...", {}, QueryCanceled("canceling statement due to statement timeout"))

        scalar = execute = scalars = _cancel

    sessions = []

    def factory():
        sessions.append(CanceledSession())
        return sessions[-1]

    search = FrozenRetrievalSearchService(factory)

    async def run():
        frozen = await b2_data().read_input()
        with pytest.raises(OperationalError):
            await search.readiness(frozen)
        with pytest.raises(IncompleteInspection) as failure:
            await search.search(frozen, "미나 새벽", FakeEmbedding(), 5)
        return failure.value.code

    assert asyncio.run(run()) == "SEARCH_FAILED"
    assert sessions and all(session.closed for session in sessions)


@pytest.mark.parametrize(("seconds", "cap"), [(20, 5.0), (3, None)], ids=["budget-above-cap", "budget-below-cap"])
def test_readiness_timeout_is_the_smaller_of_the_budget_and_five_seconds(monkeypatch, seconds, cap):
    from app.validation import service

    assert service.VECTOR_READINESS_TIMEOUT_SECONDS == 5.0
    real, timeouts = asyncio.wait_for, []

    async def recording_wait_for(awaitable, timeout=None, **kwargs):
        if getattr(awaitable, "__qualname__", "") == "Index.readiness":
            timeouts.append(timeout)
        return await real(awaitable, timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
    assert inspect(RecordedTerra(), search=Index(), embedding_client=FakeEmbedding(),
                   seconds=seconds).status == "COMPLETED"
    [timeout] = timeouts
    if cap is not None:
        assert timeout == cap
    else:  # The remaining inspection budget, already slightly below the configured seconds.
        assert seconds - 1 < timeout <= seconds
