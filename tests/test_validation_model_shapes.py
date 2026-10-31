"""GH331: real Terra output shapes pass only through deterministic, meaning-preserving normalization.

No model, network or DB is called. The recorded shapes come from the GH314 V1 real-model evaluation
(`docs/episode-validation-evaluation.md` sections 3-4): `TSEG1` span references, quotes that are exact
while start/end drift by 1-4 code points, FOLLOW_UP with a filled completion, NO_CONFLICT with an impact,
required_reads missing the automatic receipts, and lookup intents with the wrong reference kind.
"""

import asyncio
import json
import logging
from collections import Counter
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from app.llm.responses import LlmTextResponse
from app.retrieval.search_contracts import SearchReadiness
from app.validation.contracts import (
    IncompleteInspection,
    IntentRefs,
    InvocationBudget,
    LookupIntent,
    QuestionBatch,
    Span,
    anchor_span,
    normalize_batch,
    split_segments,
    validate_intents,
    validate_span,
)
from app.validation.schemas import decision_schema, question_batch_schema, subject_resolution_schema
from tests.test_setting_validation_service import SyntheticData

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


def test_batch_normalization_collapses_duplicate_spans_and_names_target_context_t1():
    draft = {
        "local_ref": "q", "target_spans": [span(POST, 112, 138), span(POST)],
        "subject_refs": [], "subject_mentions": ["준호"], "property_or_rule": "우체부 지나감",
        "time_context": "현재", "conditions": [], "assertion_mode": "NARRATION", "check_intent": "확인",
        "reason": "근거", "lookup_intents": [{"ref": "L1", "kind": "COMPARISON_CONTEXT",
                                              "operation": "TARGET_CONTEXT", "purpose": "앞 문맥",
                                              "source_ref": "TSEG1", "start": 0, "end": 20}],
    }
    batch = QuestionBatch(questions=[draft], segments=[], rules=[])
    question = normalize_batch(batch, B2, aliases={"TSEG1"}).questions[0]
    assert [(row.source_ref, row.start, row.end) for row in question.target_spans] == [("T1", 112, 139)]
    assert question.lookup_intents[0].source_ref == "T1"


@pytest.mark.parametrize("intent,code", [
    ({"operation": "BASELINE", "evidence_refs": ["B21"]}, "UNKNOWN_BASELINE_REFERENCE"),
    ({"operation": "DIRECT", "evidence_refs": ["B1", "B7"]}, "UNKNOWN_SOURCE_REFERENCE"),
    ({"operation": "NEIGHBORS", "source_ref": "R1", "start": 0, "end": 9, "evidence_refs": ["B1"]},
     "UNKNOWN_EVIDENCE_REFERENCE"),
    ({"operation": "TARGET_CONTEXT", "source_ref": "TSEG1"}, "INVALID_TARGET_CONTEXT_REQUEST"),
    ({"operation": "KEYWORD", "query": "  "}, "INVALID_SEARCH_REQUEST"),
    ({"operation": "VECTOR", "query": "검 복원"}, "VECTOR_NOT_READY"),
])
def test_recorded_wrong_reference_kinds_are_rejected_before_they_become_obligations(intent, code):
    refs = IntentRefs(frozenset({"B1"}), frozenset({"R1"}), frozenset({"E1"}), len(B2), frozenset({"KEYWORD"}))
    value = LookupIntent(ref="L1", kind="COMPARISON_CONTEXT", purpose="확인", **intent)
    with pytest.raises(ValueError, match=code):
        validate_intents([value], refs)
    validate_intents([LookupIntent(ref="ok", kind="COMPARISON_CONTEXT", operation="NEIGHBORS",
                                   purpose="확인", evidence_refs=["E1"])], refs)


# --- strict per-call schemas -----------------------------------------------------------------------

def _assert_strict(node, defs):
    if isinstance(node, dict):
        assert not {"default", "oneOf", "discriminator", "maxItems"} & set(node)
        if "$ref" in node:
            assert node["$ref"].rsplit("/", 1)[-1] in defs
        if "enum" in node:
            assert node["enum"]
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and node["required"] == list(node["properties"])
        for value in node.values():
            _assert_strict(value, defs)
    elif isinstance(node, list):
        for value in node:
            _assert_strict(value, defs)


def _variants(intents):
    return {tuple(row["properties"]["operation"]["enum"]): row["properties"] for row in intents["items"]["anyOf"]}


def test_coverage_schema_offers_only_t1_selected_rules_and_operation_specific_references():
    schema = question_batch_schema(segment_refs=["TSEG1"], subject_refs=["S1", "S2"], rule_refs=["B2", "B3"],
                                   clue_refs=["CL1"], entry_refs=["B1", "B2", "B3"], source_refs=["R1", "R2"])
    _assert_strict(schema, schema["$defs"])
    defs = schema["$defs"]
    assert defs["Span"]["properties"]["source_ref"]["enum"] == ["T1"]
    assert defs["SegmentCoverage"]["properties"]["segment_ref"]["enum"] == ["TSEG1"]
    assert defs["RuleCoverage"]["properties"]["entry_ref"]["enum"] == ["B2", "B3"]  # S3: not the catalogue
    assert defs["ClueCoverage"]["properties"]["entry_ref"]["enum"] == ["CL1"]
    assert defs["QuestionDraft"]["properties"]["subject_refs"]["items"]["enum"] == ["S1", "S2"]
    variants = _variants(defs["QuestionDraft"]["properties"]["lookup_intents"])
    assert set(variants) == {("BASELINE",), ("TARGET_CONTEXT",), ("DIRECT",), ("KEYWORD", "VECTOR")}
    assert variants[("DIRECT",)]["source_ref"] == {"type": "string", "enum": ["R1", "R2"]}
    assert variants[("BASELINE",)]["evidence_refs"]["items"]["enum"] == ["B1", "B2", "B3"]
    assert variants[("KEYWORD", "VECTOR")]["query"] == {"type": "string", "minLength": 1}


def test_judge_and_planner_schemas_constrain_reference_kinds_and_stay_identical_per_report():
    schema = decision_schema(entry_refs=["B1", "B2"], source_refs=["R1"])
    _assert_strict(schema, schema["$defs"])
    properties, defs = schema["properties"], schema["$defs"]
    assert {"type": "null"} in properties["completion"]["anyOf"]
    assert {"type": "null"} in properties["impact"]["anyOf"]
    assessment = defs["Assessment"]["properties"]
    assert assessment["receipt_refs"] == {"type": "array", "items": {"type": "string", "pattern": "^(L[1-9][0-9]*)$"},
                                          "minItems": 1}
    assert assessment["evidence_refs"]["items"]["pattern"] == "^(T1|B[1-9][0-9]*|E[1-9][0-9]*)$"
    follow_ups = _variants(properties["follow_ups"])
    assert follow_ups[("NEIGHBORS",)]["evidence_refs"]["items"]["pattern"] == "^(E[1-9][0-9]*)$"
    assert follow_ups[("DIRECT",)]["source_ref"]["pattern"] == "^(R[1-9][0-9]*)$"
    assert follow_ups[("BASELINE",)]["evidence_refs"]["items"]["pattern"] == "^(B[1-9][0-9]*)$"
    # A real ref of an unexpected shape is never made unwritable; the validator still decides.
    odd = decision_schema(entry_refs=["entry-1"], source_refs=["R1"])
    assert _variants(odd["properties"]["follow_ups"])[("BASELINE",)]["evidence_refs"]["items"] == {"type": "string"}
    planner = [question_batch_schema(segment_refs=[ref], subject_refs=subjects, rule_refs=[], clue_refs=[],
                                     entry_refs=entries, source_refs=["R1", "R2"], cacheable=True)
               for ref, subjects, entries in (("TSEG1", ["S1"], ["B1"]), ("TSEG2", ["S1", "S2"], ["B1", "B7"]))]
    assert planner[0] == planner[1]
    assert ("NEIGHBORS",) not in _variants(planner[0]["$defs"]["QuestionDraft"]["properties"]["lookup_intents"])


def test_coverage_enums_are_capped_for_large_catalogues():
    big = question_batch_schema(segment_refs=["TSEG1"], subject_refs=[], rule_refs=[f"B{n}" for n in range(1, 151)],
                                clue_refs=[], entry_refs=[f"B{n}" for n in range(1, 151)], source_refs=[])
    assert big["$defs"]["RuleCoverage"]["properties"]["entry_ref"] == {"type": "string"}
    baseline = _variants(big["$defs"]["QuestionDraft"]["properties"]["lookup_intents"])[("BASELINE",)]
    assert baseline["evidence_refs"]["items"] == {"type": "string"}


def test_subject_resolution_schema_offers_only_catalogue_subjects_or_null():
    schema = subject_resolution_schema(subject_refs=["S1", "S2"])
    _assert_strict(schema, schema["$defs"])
    mention = schema["$defs"]["MentionResolution"]
    assert mention["required"] == ["mention", "subject_ref", "reason"]
    assert mention["properties"]["subject_ref"] == {"anyOf": [{"type": "string", "enum": ["S1", "S2"]},
                                                              {"type": "null"}]}
    assert mention["properties"]["mention"]["minLength"] == mention["properties"]["reason"]["minLength"] == 1
    # Built once per report: every segment's call gets the same schema (provider prompt cache).
    assert schema == subject_resolution_schema(subject_refs=["S1", "S2"])
    empty = subject_resolution_schema(subject_refs=[])
    assert empty["$defs"]["MentionResolution"]["properties"]["subject_ref"] == {"type": "null"}
    big = subject_resolution_schema(subject_refs=[f"S{n}" for n in range(1, 151)])
    assert big["$defs"]["MentionResolution"]["properties"]["subject_ref"] == {
        "anyOf": [{"type": "string", "pattern": "^(S[1-9][0-9]*)$"}, {"type": "null"}]}


# --- recorded real-model shapes through the full validation path ------------------------------------

class B2Data(SyntheticData):
    def __init__(self):
        super().__init__()
        self.texts = {"T1": B2, "R1": EPISODES["B1"], "R2": EPISODES["B1"]}
        digest = sha256(B2.encode()).hexdigest()
        self.context = replace(self.context, target_hash=digest)
        self.documents["T1"] = replace(self.documents["T1"], content_hash=digest)
        for ref in ("R1", "R2"):
            self.documents[ref] = replace(self.documents[ref],
                                          content_hash=sha256(self.texts[ref].encode()).hexdigest())
        self.entries = [{"entryRef": "B1", "domain": "characters", "subjectRef": "character:mina",
                         "state": {"name": "미나", "aliases": [],
                                   "slots": {"HABIT:기상": {"factValue": "새벽 네 시"}}}}]


def question(local_ref, spans, prop, intents=()):
    return {"local_ref": local_ref, "target_spans": spans, "subject_refs": ["S1"], "subject_mentions": ["미나"],
            "property_or_rule": prop, "time_context": "일요일 아침", "conditions": [], "assertion_mode": "NARRATION",
            "check_intent": "기존 습관과 비교", "reason": "반복 습관 확인", "lookup_intents": list(intents)}


class RecordedTerra:
    """Replays the recorded real Terra shapes; only the provider boundary is synthetic."""

    def __init__(self, planner_intents=(), follow_up=None):
        self.calls, self.planner_intents, self.follow_up = [], list(planner_intents), follow_up

    async def count_input_tokens(self, **kwargs):
        return 80

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        role = payload["role"]
        self.calls.append((role, payload, kwargs))
        if role == "RELATED_SELECTION":
            value = {"mentions": []}
        elif role in {"PLANNER", "COVERAGE"}:
            if role == "PLANNER":
                questions = [question("wake", [span(WAKE)], "새벽 기상", self.planner_intents),
                             question("post", [span(POST, 112, 138)], "우체부 방문")]
            else:
                questions = [question("market", [span(MARKET, 223, 245)], "장터 가판대")]
            value = {"questions": questions,
                     "segments": [{"segment_ref": payload["segment"]["ref"], "status": "PLANNED",
                                   "reason": "구간 확인"}],
                     "rules": [{"entry_ref": ref, "status": "CHECKED", "reason": "습관 확인"}
                               for ref in payload.get("rule_refs", [])],
                     "clue_checks": [{"entry_ref": row["ref"], "status": "NOT_APPLICABLE", "reason": "반복 표현"}
                                     for row in payload.get("mechanical_clues", [])]}
        else:
            value = self.judge(payload)
        return LlmTextResponse(text=json.dumps(value, ensure_ascii=False), input_token_count=80,
                               output_token_count=30)

    def judge(self, payload):
        receipts = [row["ref"] for row in payload["receipts"]]
        refs = ["T1", *(row["entry_ref"] for row in payload["settings"]), *(row["ref"] for row in payload["evidence"])]
        searched = any(row["intent"]["operation"] == "KEYWORD" for row in payload["receipts"])
        assessment = {"reason": "같은 대상과 시점을 확인했다", "evidence_refs": refs, "receipt_refs": receipts}
        # Recorded drift: the judge echoes the question spans as TSEG1 and one code point early.
        echoed = [{**row, "source_ref": "TSEG1", "start": row["start"] - 1, "end": row["end"] - 1}
                  for row in payload["question"]["target_spans"]]
        value = {
            "action": "DECIDED", "outcome": "NO_CONFLICT", "basis": "COMPATIBLE", "reason": "기존 습관과 같다",
            # S5: NO_CONFLICT with an impact grade.
            "impact": "REFERENCE", "impact_reason": "부수 묘사",
            "target_spans": echoed, "evidence_refs": refs,
            # required_reads cites only the last receipt (BASELINE/TARGET_CONTEXT omitted).
            "completion": {"comparison_context": assessment, "changes_exceptions": assessment,
                           "no_pending_clues": assessment,
                           "required_reads": {**assessment, "receipt_refs": receipts[-1:]}},
            "follow_ups": [], "affected_question_refs": [], "disagreements": []}
        if not searched:
            # S4: FOLLOW_UP that still fills outcome/impact/completion.
            value.update(action="FOLLOW_UP", follow_ups=[self.follow_up or {
                "ref": "change", "kind": "CHANGES_EXCEPTIONS", "operation": "KEYWORD",
                "purpose": "습관 변화 확인", "query": "미나 새벽", "source_ref": None, "start": None, "end": None,
                "evidence_refs": []}])
        return value


class RecordingCheckpoint:
    """The durable boundary: what would be published to the Backend."""

    def __init__(self):
        self.saved, self.decided, self.revised = [], [], []

    def stage(self, role, ref):
        return None

    async def save_stage(self, role, ref, payload):
        self.saved.append((role, ref, payload))

    async def merge(self, questions):
        return {q.ref: {"revision": 1, "outcome": None,
                        "state": {"lookup_obligations": [i.model_dump() for i in q.draft.lookup_intents]}}
                for q in questions}

    def load_state(self):
        return None

    async def save_state(self, state):
        pass

    async def decide(self, ref, decision, bundle):
        self.decided.append((ref, decision, bundle))

    async def revise(self, affected, pending, questions, reason):
        self.revised.append(sorted(affected))
        return {}


def inspect(model, checkpoint=None, search=None, embedding_client=None, seconds=30):
    from app.validation.service import SettingValidationService

    limits = InvocationBudget(model_calls=40, lookups=80, seconds=seconds, input_tokens=40000, output_tokens=20000,
                              segment_chars=2000, context_chars=300, catalogue_page_size=50, search_limit=10)
    return asyncio.run(SettingValidationService(B2Data(), model, limits, checkpoint=checkpoint,
                                                search=search, embedding_client=embedding_client).inspect())


def test_recorded_real_terra_shapes_are_accepted_after_normalization(caplog):
    caplog.set_level(logging.INFO, logger="app.validation.service")
    model, checkpoint = RecordedTerra(), RecordingCheckpoint()
    result = inspect(model, checkpoint)
    assert (result.status, result.failure_code, result.failure_cause) == ("COMPLETED", None, None)
    assert len(result.questions) == 3 and set(result.decisions) == {"Q1", "Q2", "Q3"}
    for role, _ref, payload in checkpoint.saved:
        if role in {"PLANNER", "COVERAGE"}:
            for draft in payload["questions"]:
                for row in draft["target_spans"]:
                    assert row["source_ref"] == "T1" and B2[row["start"]:row["end"]] == row["quote"]
    for ref, decision, bundle in checkpoint.decided:
        assert decision.outcome == "NO_CONFLICT" and decision.impact is None and decision.impact_reason is None
        assert set(decision.target_spans) == set(result.questions[int(ref[1:]) - 1].draft.target_spans)
        receipts = [row["ref"] for row in bundle["receipts"]]
        assert decision.completion.required_reads.receipt_refs == receipts
    assert result.normalizations["TARGET_REF_ALIAS"] >= 4
    assert result.normalizations["SPAN_REANCHORED"] >= 2
    assert result.normalizations["FOLLOW_UP_DECISION_FIELDS_IGNORED"] == 3
    assert result.normalizations["NO_CONFLICT_IMPACT_CLEARED"] == 3
    assert result.normalizations["REQUIRED_READS_COMPLETED"] == 3
    assert len(checkpoint.revised) == 3
    planner = next(payload for role, payload, _ in model.calls if role == "PLANNER")
    coverage = next(payload for role, payload, _ in model.calls if role == "COVERAGE")
    assert planner["target_source_ref"] == coverage["target_source_ref"] == "T1"
    assert "catalogue" not in coverage and coverage["rule_refs"] == ["B1"]
    # GH349: one identification row per subject; 미나 is matched by name, so no resolution call is needed.
    assert coverage["subject_catalogue"] == planner["subject_catalogue"] == [
        {"subject_ref": "S1", "domain": "characters", "name": "미나", "aliases": []}]
    judge_schemas = [kwargs["response_schema"].schema for role, _, kwargs in model.calls if role == "JUDGE"]
    assert len(judge_schemas) == 6 and all(schema == judge_schemas[0] for schema in judge_schemas)
    names = {kwargs["response_schema"].name for _, _, kwargs in model.calls}
    assert names == {"validation_planner_v2", "validation_coverage_v2", "validation_judge_v2"}
    assert "setting inspection completed" in caplog.text


@pytest.mark.parametrize("intent,cause", [
    ({"ref": "L8", "kind": "COMPARISON_CONTEXT", "operation": "DIRECT", "purpose": "이전 장면",
      "evidence_refs": ["B1", "B7", "B20"]}, "UNKNOWN_SOURCE_REFERENCE"),
    ({"ref": "L9", "kind": "COMPARISON_CONTEXT", "operation": "BASELINE", "purpose": "없는 항목",
      "evidence_refs": ["B21"]}, "UNKNOWN_BASELINE_REFERENCE"),
])
def test_unreadable_planner_intent_is_never_saved_and_reports_its_cause(intent, cause, caplog):
    caplog.set_level(logging.WARNING, logger="app.validation.service")
    checkpoint = RecordingCheckpoint()
    result = inspect(RecordedTerra(planner_intents=[intent]), checkpoint)
    assert (result.status, result.failure_code) == ("INCOMPLETE", "INVALID_INSPECTION_CONTRACT")
    assert (result.failure_cause, result.failure_stage) == (cause, "PLANNER:TSEG1")
    assert not checkpoint.saved  # a retry calls the model again instead of replaying a poisoned plan
    assert f"cause={cause} stage=PLANNER:TSEG1" in caplog.text


def test_follow_up_on_a_baseline_ref_is_rejected_before_revision():
    checkpoint = RecordingCheckpoint()
    result = inspect(RecordedTerra(follow_up={
        "ref": "L10", "kind": "COMPARISON_CONTEXT", "operation": "NEIGHBORS", "purpose": "앞뒤 문맥",
        "source_ref": "R2", "start": 0, "end": 10, "evidence_refs": ["B1"]}), checkpoint)
    assert (result.status, result.failure_code, result.failure_stage) == (
        "INCOMPLETE", "UNKNOWN_EVIDENCE_REFERENCE", "JUDGE:Q1")
    assert not checkpoint.revised and not checkpoint.decided


def test_absent_quote_keeps_the_rejection_but_no_longer_hides_its_cause():
    class Absent(RecordedTerra):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            if json.loads(kwargs["user_prompt"])["role"] == "PLANNER":
                value = json.loads(response.text)
                value["questions"][0]["target_spans"][0]["quote"] = "원고에 없는 문장"
                return LlmTextResponse(text=json.dumps(value, ensure_ascii=False))
            return response

    result = inspect(Absent())
    assert (result.failure_code, result.failure_cause, result.failure_stage) == (
        "INVALID_INSPECTION_CONTRACT", "INVALID_EXACT_SPAN", "PLANNER:TSEG1")


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
                change(value)
            return value

    result = inspect(Strict())
    assert (result.status, result.failure_code, result.failure_cause) == ("INCOMPLETE", "INVALID_DECISION", cause)


def test_judge_span_echo_is_replaced_by_the_question_spans_but_invented_quotes_are_rejected():
    """Final re-run: JUDGE echoed fewer or neighbouring exact spans (INVALID_DECISION_TARGET_SPANS)."""
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
        checkpoint = RecordingCheckpoint()
        result = inspect(Echo(spans), checkpoint)
        assert result.status == "COMPLETED" and result.normalizations["TARGET_SPANS_REPLACED"] == 1
        decided = {ref: decision for ref, decision, _ in checkpoint.decided}
        assert decided["Q1"].target_spans == result.questions[0].draft.target_spans
    invented = inspect(Echo([{"source_ref": "T1", "start": 0, "end": 5, "quote": "없는 인용"}]))
    assert (invented.failure_code, invented.failure_cause, invented.failure_stage) == (
        "INVALID_INSPECTION_CONTRACT", "INVALID_EXACT_SPAN", "JUDGE:Q1")


def test_model_contract_and_provider_failures_keep_a_class_level_cause_only():
    class Literal(RecordedTerra):
        def judge(self, payload):
            return {**super().judge(payload), "action": "DONE"}

    result = inspect(Literal())
    assert (result.failure_code, result.failure_cause) == ("MODEL_CONTRACT_INVALID", "MODEL_CONTRACT_LITERAL_ERROR")

    class Broken(RecordedTerra):
        async def create_text_response(self, **kwargs):
            raise ConnectionResetError("provider body must never be copied")

    broken = inspect(Broken())
    assert (broken.failure_code, broken.failure_cause) == ("MODEL_CALL_FAILED", "MODEL_CALL_CONNECTION_RESET_ERROR")


def test_worker_reports_the_specific_cause_and_keeps_typo_codes():
    from app.validation.contracts import InspectionResult, TypoCheckResult
    from app.worker.episode_validation_worker import reported_failure_code

    collapsed = InspectionResult("INCOMPLETE", failure_code="INVALID_INSPECTION_CONTRACT",
                                 failure_cause="INVALID_EXACT_SPAN")
    assert reported_failure_code(collapsed) == "INVALID_EXACT_SPAN"
    assert reported_failure_code(InspectionResult("INCOMPLETE", failure_code="BUDGET_EXHAUSTED")) == "BUDGET_EXHAUSTED"
    typo = TypoCheckResult(target_hash="a" * 64, status="INCOMPLETE", segments=[], suggestions=[],
                           failure_code="INVALID_EXACT_SPAN")
    assert reported_failure_code(typo) == "INVALID_EXACT_SPAN"
    with pytest.raises(IncompleteInspection) as raised:
        raise IncompleteInspection("INVALID_DECISION", "not a safe code")
    assert raised.value.cause is None  # free text never becomes a diagnostic


def test_segments_used_by_the_regression_are_a_single_target_segment():
    assert [segment.ref for segment in split_segments(B2, core_chars=2000, context_chars=300)] == ["TSEG1"]


READY = SearchReadiness(2, 2, 2, 4, 4)
HANG = object()  # A readiness read that never returns (a stuck DB connection).


class EmptyIndex:
    """Frozen RAG is configured but finds nothing; raw keyword reading still covers every source.

    ``readiness`` is what the job's one GH344 readiness read returns (an exception is raised instead).
    """

    def __init__(self, readiness=READY):
        self.ready, self.readiness_reads, self.searches = readiness, 0, 0

    async def readiness(self, frozen):
        self.readiness_reads += 1
        if self.ready is HANG:
            await asyncio.Event().wait()
        if isinstance(self.ready, Exception):
            raise self.ready
        return self.ready

    async def search(self, frozen, intent, embedding_client, limit):
        from types import SimpleNamespace

        self.searches += 1
        return SimpleNamespace(chunks=[], limited=False, readiness=None), set()


def _planner_search_operations(model):
    kwargs = next(kwargs for role, _, kwargs in model.calls if role == "PLANNER")
    intents = kwargs["response_schema"].schema["$defs"]["QuestionDraft"]["properties"]["lookup_intents"]
    return {operation for row in intents["items"]["anyOf"] for operation in row["properties"]["operation"]["enum"]}


def test_vector_is_offered_only_when_the_worker_can_execute_it():
    """GH331 re-run: without frozen RAG every planned episode VECTOR ended in VECTOR_NOT_READY."""
    plain = RecordedTerra()
    assert inspect(plain).status == "COMPLETED"
    assert {"KEYWORD"} <= _planner_search_operations(plain) and "VECTOR" not in _planner_search_operations(plain)
    indexed = RecordedTerra()
    assert inspect(indexed, search=EmptyIndex(), embedding_client=QUERY_EMBEDDING).status == "COMPLETED"
    assert {"KEYWORD", "VECTOR"} <= _planner_search_operations(indexed)
    checkpoint = RecordingCheckpoint()
    vector = RecordedTerra(planner_intents=[{"ref": "L3", "kind": "CHANGES_EXCEPTIONS", "operation": "VECTOR",
                                             "purpose": "습관 변화", "query": "미나 기상 변화"}])
    result = inspect(vector, checkpoint)
    assert (result.failure_code, result.failure_cause, result.failure_stage) == (
        "INVALID_INSPECTION_CONTRACT", "VECTOR_NOT_READY", "PLANNER:TSEG1")
    assert not checkpoint.saved  # never persisted as an obligation that every retry would repeat


# --- GH344: VECTOR is offered only when this job's frozen index can serve it ----------------------

QUERY_EMBEDDING = object()  # Never called: no VECTOR lookup runs in these inspections.


@pytest.mark.parametrize(("readiness", "embedding_client", "offered"), [
    (READY, QUERY_EMBEDDING, True),
    (SearchReadiness(2, 2, 1, 4, 2, ("VECTOR_INDEX_PARTIAL",)), QUERY_EMBEDDING, False),  # one episode unembedded
    (SearchReadiness(2, 2, 2, 4, 0), QUERY_EMBEDDING, False),  # no vector chunk at all
    (READY, None, False),  # claim without QUERY_EMBEDDING or worker without --query-embedding
    (SearchReadiness(1, 1, 1, 2, 2), QUERY_EMBEDDING, False),  # index scope differs from the frozen manifest
], ids=["ready", "partial", "no-vectors", "no-embedding-client", "manifest-mismatch"])
def test_vector_offer_follows_one_readiness_read_of_the_job(caplog, readiness, embedding_client, offered):
    caplog.set_level(logging.INFO, logger="app.validation.service")
    index, model = EmptyIndex(readiness), RecordedTerra()
    result = inspect(model, search=index, embedding_client=embedding_client)
    assert result.status == "COMPLETED"
    assert ("VECTOR" in _planner_search_operations(model)) is offered
    judge = [kwargs["response_schema"].schema for role, _, kwargs in model.calls if role == "JUDGE"]
    assert judge and all(("VECTOR" in {operation for variant in _variants(schema["properties"]["follow_ups"])
                                       for operation in variant}) is offered for schema in judge)
    assert index.readiness_reads == 1
    assert result.normalizations["VECTOR_OFFERED"] == int(offered)
    assert (f"vector search offered={offered} sources={readiness.source_count} "
            f"ready={readiness.vector_ready_sources}") in caplog.text


def test_unexecutable_vector_follow_up_is_rejected_when_the_index_is_partial():
    """A partial index no longer reaches the lookup: the VECTOR obligation is refused before revision."""
    checkpoint = RecordingCheckpoint()
    vector = RecordedTerra(planner_intents=[{"ref": "L3", "kind": "CHANGES_EXCEPTIONS", "operation": "VECTOR",
                                             "purpose": "습관 변화", "query": "미나 기상 변화"}])
    partial = EmptyIndex(SearchReadiness(2, 2, 1, 4, 2, ("VECTOR_INDEX_PARTIAL",)))
    result = inspect(vector, checkpoint, search=partial, embedding_client=QUERY_EMBEDDING)
    assert (result.failure_code, result.failure_cause, result.failure_stage) == (
        "INVALID_INSPECTION_CONTRACT", "VECTOR_NOT_READY", "PLANNER:TSEG1")
    assert not checkpoint.saved


def test_failed_readiness_read_offers_keyword_only_and_logs_the_type_only(caplog):
    caplog.set_level(logging.INFO, logger="app.validation.service")
    index, model = EmptyIndex(OSError("postgresql://reader:secret@db.invalid/catchhole")), RecordedTerra()
    result = inspect(model, search=index, embedding_client=QUERY_EMBEDDING)
    assert result.status == "COMPLETED"  # the job is not failed by the readiness read
    assert "VECTOR" not in _planner_search_operations(model)
    assert index.readiness_reads == 1 and result.normalizations["VECTOR_OFFERED"] == 0
    # An unreadable index (e.g. missing reader grants) would fail every KEYWORD lookup too, so the job reads
    # the frozen sources raw, exactly like the keyword-only configuration.
    assert index.searches == 0
    assert any(r.intent.operation == "KEYWORD" and r.status == "SUCCEEDED" for r in result.receipts)
    warnings = [row for row in caplog.records if row.levelno == logging.WARNING]
    assert [row.getMessage() for row in warnings] == [
        "vector search readiness unavailable error=OSError job=-; offering KEYWORD only without frozen retrieval"]
    assert "secret" not in caplog.text and "db.invalid" not in caplog.text
    assert "vector search offered" not in caplog.text


def test_stuck_readiness_read_is_capped_and_planning_keeps_its_budget(monkeypatch, caplog):
    """A stuck read ends at the cap; the first PLANNER call still runs instead of BUDGET_EXHAUSTED.

    The asyncio cap only frees the inspection. The real readiness runs a blocking session in
    ``asyncio.to_thread``: cancelling the awaiting coroutine does not stop that thread, which keeps its
    pooled connection (pool_size=--concurrency, max_overflow=0) until the database ends the statement.
    The runner's read-only engine therefore sets statement_timeout (5 s, the same bound), connect and pool
    timeouts; see ``test_statement_timeout_in_frozen_search_takes_the_existing_failure_paths`` and the
    env-gated PostgreSQL check in ``tests/test_validation_readonly_postgres.py``.
    """
    import time

    from app.validation import service

    monkeypatch.setattr(service, "VECTOR_READINESS_TIMEOUT_SECONDS", 0.05)
    caplog.set_level(logging.INFO, logger="app.validation.service")
    index, model = EmptyIndex(HANG), RecordedTerra()
    started = time.monotonic()
    result = inspect(model, search=index, embedding_client=QUERY_EMBEDDING, seconds=20)
    assert time.monotonic() - started < 5
    assert result.status == "COMPLETED"
    assert model.calls[0][0] == "PLANNER"
    assert "VECTOR" not in _planner_search_operations(model)
    assert result.normalizations["VECTOR_OFFERED"] == 0 and index.searches == 0
    assert "vector search readiness unavailable error=TimeoutError job=-" in caplog.text


def test_statement_timeout_in_frozen_search_takes_the_existing_failure_paths(caplog):
    """A database-cancelled read (statement_timeout, SQLSTATE 57014) closes its session and maps to fixed codes.

    readiness() raises the driver error to the offer check (KEYWORD only, frozen RAG off for the job);
    search() turns it into SEARCH_FAILED for KEYWORD and VECTOR alike, never an empty result.
    """
    from sqlalchemy.exc import OperationalError

    from app.validation.contracts import IncompleteInspection
    from app.validation.search import FrozenRetrievalSearchService

    class QueryCanceled(Exception):
        sqlstate = "57014"

    class CanceledSession:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True  # The pooled connection is returned even though the statement failed.
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
        frozen = await B2Data().read_input()
        with pytest.raises(OperationalError):
            await search.readiness(frozen)
        codes = []
        for operation in ("KEYWORD", "VECTOR"):
            intent = LookupIntent(ref="L1", kind="CHANGES_EXCEPTIONS", operation=operation, purpose="변화",
                                  query="미나 새벽")
            with pytest.raises(IncompleteInspection) as failure:
                await search.search(frozen, intent, QUERY_EMBEDDING, 5)
            codes.append(failure.value.code)
        return codes

    assert asyncio.run(run()) == ["SEARCH_FAILED", "SEARCH_FAILED"]
    assert sessions and all(session.closed for session in sessions)
    # Through the inspection: the cancelled readiness read withholds VECTOR and the job reads sources raw.
    caplog.set_level(logging.INFO, logger="app.validation.service")
    model, before = RecordedTerra(), len(sessions)
    result = inspect(model, search=search, embedding_client=QUERY_EMBEDDING)
    assert result.status == "COMPLETED" and result.normalizations["VECTOR_OFFERED"] == 0
    assert len(sessions) == before + 1  # Only the readiness read; KEYWORD lookups did not use the index.
    assert "vector search readiness unavailable error=OperationalError job=-" in caplog.text
    assert "statement timeout" not in caplog.text


@pytest.mark.parametrize(("seconds", "cap"), [(20, 5.0), (3, None)], ids=["budget-above-cap", "budget-below-cap"])
def test_readiness_timeout_is_the_smaller_of_the_budget_and_five_seconds(monkeypatch, seconds, cap):
    from app.validation import service

    assert service.VECTOR_READINESS_TIMEOUT_SECONDS == 5.0
    real, timeouts = asyncio.wait_for, []

    async def recording_wait_for(awaitable, timeout=None, **kwargs):
        if getattr(awaitable, "__qualname__", "") == "EmptyIndex.readiness":
            timeouts.append(timeout)
        return await real(awaitable, timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
    assert inspect(RecordedTerra(), search=EmptyIndex(), embedding_client=QUERY_EMBEDDING,
                   seconds=seconds).status == "COMPLETED"
    [timeout] = timeouts
    if cap is not None:
        assert timeout == cap
    else:  # The remaining inspection budget, already slightly below the configured seconds.
        assert seconds - 1 < timeout <= seconds


def test_resumed_job_with_saved_vector_work_keeps_vector_while_the_index_is_rebuilt(caplog):
    """Successful lookups resume JUDGE-only despite a live index rebuild (validation-execution-v1)."""
    from types import SimpleNamespace

    from app.validation.checkpoint import lookup_operations
    from tests.test_validation_checkpoint import SavedStages

    class Resumable(SavedStages):
        async def merge(self, questions):
            for q in questions:
                self.checks.setdefault(q.ref, {"revision": 1, "outcome": None, "state": {
                    "lookup_obligations": [i.model_dump() for i in q.draft.lookup_intents]}})
            return self.checks

        def planned_operations(self):  # Same scan as ValidationCheckpoint.planned_operations.
            return lookup_operations(list(self.stages.values()), list(self.checks.values()), self.state)

    class Index(EmptyIndex):
        async def search(self, frozen, intent, embedding_client, limit):
            return SimpleNamespace(chunks=[], limited=False, readiness=self.ready), set()

    intents = [{"ref": "L3", "kind": "CHANGES_EXCEPTIONS", "operation": "VECTOR", "purpose": "습관 변화",
                "query": "미나 기상 변화"}]
    checkpoint = Resumable()
    first = inspect(RecordedTerra(planner_intents=intents), checkpoint, search=Index(), embedding_client=QUERY_EMBEDDING)
    assert first.status == "COMPLETED"
    assert any(r.intent.operation == "VECTOR" and r.status == "SUCCEEDED" for r in first.receipts)

    caplog.set_level(logging.INFO, logger="app.validation.service")
    rebuilding = Index(SearchReadiness(2, 2, 1, 4, 2, ("VECTOR_INDEX_PARTIAL",)))
    model = RecordedTerra(planner_intents=intents)
    resumed = inspect(model, checkpoint, search=rebuilding, embedding_client=QUERY_EMBEDDING)
    assert resumed.status == "COMPLETED" and not model.calls
    assert rebuilding.readiness_reads == 0 and resumed.normalizations["VECTOR_OFFERED"] == 1
    assert "vector search offered=True resumed=True job=-" in caplog.text


# --- GH341: the SUSPECTED_CONFLICT change-search citation is a Worker fact -------------------------

def _change_case(outcome="SUSPECTED_CONFLICT", basis="CHANGE_NOT_FOUND", *, changes=None, operation="KEYWORD",
                 search_refs=("E1",), searched=True):
    from app.validation.contracts import (
        LookupReceipt,
        MergedQuestion,
        QuestionDraft,
        ValidationDecision,
    )

    target_span = span(WAKE, ref="T1")
    draft = QuestionDraft.model_validate(question("wake", [target_span], "새벽 기상"))
    receipts = [
        LookupReceipt("L1", LookupIntent(ref="Q1-BASELINE", kind="COMPARISON_CONTEXT", operation="BASELINE",
                                         purpose="비교"), "SUCCEEDED", ("B1",)),
        LookupReceipt("L2", LookupIntent(ref="Q1-TARGET_CONTEXT", kind="COMPARISON_CONTEXT",
                                         operation="TARGET_CONTEXT", purpose="비교"), "SUCCEEDED", ("T1",)),
    ]
    if searched:
        receipts.append(LookupReceipt("L3", LookupIntent(ref="change", kind="CHANGES_EXCEPTIONS", operation=operation,
                                                         purpose="변화 검색", query="미나 새벽"),
                                      "SUCCEEDED", tuple(search_refs)))
    cited = {"reason": "같은 대상과 시점을 확인했다", "evidence_refs": ["T1", "B1"], "receipt_refs": ["L1", "L2"]}
    value = ValidationDecision.model_validate({
        "action": "DECIDED", "outcome": outcome, "basis": basis, "reason": "합성 판정", "impact": "CAUTION",
        "impact_reason": "설정 이해에 영향", "target_spans": [target_span], "evidence_refs": ["T1", "B1"],
        "completion": {"comparison_context": cited, "no_pending_clues": cited, "required_reads": cited,
                       "changes_exceptions": cited if changes is None else changes},
        "follow_ups": [], "affected_question_refs": [], "disagreements": []})
    settings = [{"entry_ref": "B1"}]
    evidence = [{"ref": "E1", "source_ref": "R1"}]
    return value, MergedQuestion("Q1", draft, ("PLANNER:TSEG1:wake",)), settings, evidence, receipts


def _validate(case):
    from app.validation.service import SettingValidationService

    value, merged, settings, evidence, receipts = case
    stats = Counter()
    return SettingValidationService._validate_decision(value, merged, settings, evidence, receipts, B2, stats), stats


@pytest.mark.parametrize("operation", ["KEYWORD", "VECTOR"])
def test_suspected_conflict_cites_the_real_successful_change_search(operation):
    decided, stats = _validate(_change_case(operation=operation))
    changes = decided.completion.changes_exceptions
    assert (changes.receipt_refs, changes.evidence_refs) == (["L1", "L2", "L3"], ["T1", "B1", "E1"])
    assert stats["CHANGE_SEARCH_RECEIPT_FILLED"] == 1
    # An empty successful search still counts, and an empty citation gets the target as its evidence.
    empty = _change_case(changes={"reason": "변화 없음", "evidence_refs": [], "receipt_refs": []}, search_refs=())
    decided, stats = _validate(empty)
    changes = decided.completion.changes_exceptions
    assert (changes.receipt_refs, changes.evidence_refs) == (["L3"], ["T1"])
    assert stats["CHANGE_SEARCH_RECEIPT_FILLED"] == 1


def test_change_search_fill_never_invents_a_search_or_hides_a_forged_citation():
    with pytest.raises(IncompleteInspection) as missing:
        _validate(_change_case(searched=False))
    assert missing.value.cause == "INVALID_DECISION_CHANGE_SEARCH_MISSING"
    forged = {"reason": "변화 없음", "evidence_refs": ["T1"], "receipt_refs": ["L99"]}
    with pytest.raises(IncompleteInspection) as rejected:
        _validate(_change_case(changes=forged))
    assert rejected.value.cause == "INVALID_DECISION_CHANGES_EXCEPTIONS"
    # Only SUSPECTED_CONFLICT needs the search; other outcomes keep the model's citation as given.
    decided, stats = _validate(_change_case("CONFLICT", "DIRECT_CONTRADICTION"))
    assert decided.completion.changes_exceptions.receipt_refs == ["L1", "L2"]
    assert "CHANGE_SEARCH_RECEIPT_FILLED" not in stats
