"""Grid v4 SETTING orchestration with synthetic data and scripted model outputs (never real model quality)."""

import asyncio
from copy import deepcopy

import pytest

from app.validation.contracts import segment_wire, split_setting_segments
from app.validation.models import PLANNER_SYSTEM
from app.validation.projection import model_settings
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

SECOND = "그는 성문 앞에서 기다렸다."


def inspect(data=None, model=None, *, checkpoint=None, search=None, embedding=None, experiment=False, **limits):
    service = SettingValidationService(data or SyntheticData(), model or ScriptedModel(), budget(**limits), ROLES,
                                       search=search, embedding_client=embedding, checkpoint=checkpoint,
                                       experiment=experiment)
    return asyncio.run(service.inspect())


def two_segment_plans():
    return {"TSEG1": [draft("hand")],
            "TSEG2": [draft("gate", quote=SECOND, subject_refs=("S1", "S2"), mentions=("그", "성문"), prop="위치")]}


class FakeCheckpoint:
    """Records publications the way the Java checkpoint client would, without HTTP."""

    def __init__(self, segments, *, stages=None, decided=(), grid_version=4):
        self.stages = dict(stages or {})
        self.published = []
        self.grid = {"gridVersion": grid_version, "segments": [segment_wire(segment) for segment in segments]}
        self.decided = set(decided)
        self.checks = {}

    def bind_sources(self, frozen):
        self.frozen = frozen

    def stage(self, stage, ref):
        return deepcopy(self.stages.get((stage, ref)))

    async def save_stage(self, stage, ref, payload):
        self.published.append(("plans", stage, ref))
        self.stages[stage, ref] = deepcopy(payload)

    async def setting_grid(self):
        self.published.append(("grid",))
        return deepcopy(self.grid)

    async def merge(self, questions):
        if ("MERGED_PLAN", "PLAN") not in self.stages:
            self.published.append(("plans", "MERGED_PLAN", "PLAN"))
            self.stages["MERGED_PLAN", "PLAN"] = [question.ref for question in questions]
        for question in questions:
            decided = question.ref in self.decided
            self.checks[question.ref] = {"checkId": f"check-{question.ref}", "revision": 3 if decided else 2,
                                         "checkInputDigest": "d" * 64, "outcome": "NO_CONFLICT" if decided else None,
                                         "decision": SAVED_DECISION if decided else None,
                                         "bundle": {"question_ref": question.ref} if decided else None}
        return self.checks

    async def decide(self, ref, decision, bundle):
        self.published.append(("units", ref, decision.outcome, bundle["check_revision"]))


SAVED_DECISION = {
    "action": "DECIDED", "outcome": "NO_CONFLICT", "basis": "COMPATIBLE", "reason": "저장된 판정", "impact": None,
    "impact_reason": None, "target_spans": [], "evidence_refs": ["T1"], "completion": {
        key: {"reason": "확인", "evidence_refs": ["T1"], "receipt_refs": ["L1"]}
        for key in ("comparison_context", "changes_exceptions", "no_pending_clues", "required_reads")},
    "follow_ups": [], "affected_question_refs": [], "disagreements": [],
}


def segments_of(text=TARGET, unit=20, context=100):
    return split_setting_segments(text, unit_chars=unit, context_chars=context)


def test_planner_segments_run_in_parallel_and_merge_in_segment_order():
    entered = []
    both = asyncio.Event()

    async def gate(payload):
        if payload["role"] == "PLANNER":
            entered.append(payload["segment"]["ref"])
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)  # sequential planning would time out here

    model = ScriptedModel(two_segment_plans(), gate=gate)
    result = inspect(model=model, segment_chars=20)
    assert result.status == "COMPLETED"
    assert sorted(entered) == ["TSEG1", "TSEG2"]
    assert [question.original_refs for question in result.questions] == [("PLANNER:TSEG1:hand",),
                                                                        ("PLANNER:TSEG2:gate",)]
    assert [row["segment_ref"] for row in result.coverage] == ["TSEG1", "TSEG2"]


def test_planner_reads_core_with_read_only_context_and_subject_names_only():
    model = ScriptedModel(two_segment_plans())
    inspect(model=model, segment_chars=20, context_chars=5)
    planners = [(payload, kwargs) for role, payload, kwargs in model.calls if role == "PLANNER"]
    (first, first_kwargs), _ = sorted(planners, key=lambda row: row[0]["segment"]["ref"])
    segment = first["segment"]
    assert first["text"] == TARGET[segment["context_start"]:segment["context_end"]]
    assert first["text_start"] == segment["context_start"] and segment["context_end"] == segment["end"] + 5
    assert set(first) == {"role", "segment", "text", "text_start", "target_source_ref", "source_catalogue",
                          "subject_catalogue"}
    assert first["subject_catalogue"] == [
        {"subject_ref": "S1", "domain": "characters", "name": "민수", "aliases": []},
        {"subject_ref": "S2", "domain": "worldSettings", "name": "성문", "aliases": []}]
    assert first_kwargs["system_prompt"] == PLANNER_SYSTEM and '"나"·"너"·"그"' in PLANNER_SYSTEM
    assert first_kwargs["response_schema"].name == "validation_planner_v3"
    draft_schema = first_kwargs["response_schema"].schema["$defs"]["QuestionDraft"]["properties"]
    assert "lookup_intents" not in draft_schema and draft_schema["subject_refs"]["items"]["pattern"] == "^(S[1-9][0-9]*)$"
    assert first_kwargs["call_options"].reasoning_effort == "medium" and first_kwargs["max_output_tokens"] == 2000


def test_a_span_in_the_read_only_context_is_rejected_or_recorded_in_experiment_mode():
    # TSEG1's context reaches into TSEG2; a question there belongs to TSEG2 only.
    plans = {"TSEG1": [draft("leak", quote=SECOND)], "TSEG2": [draft("gate", quote=SECOND, prop="위치")]}
    result = inspect(model=ScriptedModel(plans), segment_chars=20)
    assert (result.status, result.failure_code, result.failure_cause, result.failure_stage) == (
        "INCOMPLETE", "INVALID_INSPECTION_CONTRACT", "SPAN_OUTSIDE_SEGMENT", "PLANNER:TSEG1")
    experiment = inspect(model=ScriptedModel(plans), segment_chars=20, experiment=True)
    assert experiment.question_failures["PLANNER:TSEG1"]["cause"] == "SPAN_OUTSIDE_SEGMENT"
    assert [question.original_refs for question in experiment.questions] == [("PLANNER:TSEG2:gate",)]
    assert experiment.decisions["Q1"].outcome == "NO_CONFLICT"


def test_legitimate_zero_questions_has_complete_coverage():
    data = SyntheticData()
    result = inspect(data)
    assert result.status == "COMPLETED" and not result.questions and not result.decisions
    assert [row["status"] for row in result.coverage] == ["NO_CHECK"]
    assert ("SETTINGS", ()) not in data.reads and not [row for row in data.reads if row[0] == "SETTINGS"]


@pytest.mark.parametrize("outcome", ["CONFLICT", "SUSPECTED_CONFLICT", "NEEDS_CONTEXT"])
def test_semantic_outcomes_remain_distinct(outcome):
    data = SyntheticData()
    model = ScriptedModel({"TSEG1": [draft()]}, lambda payload, n: decided(payload, outcome, evidence_refs=["B1"]))
    result = inspect(data, model, search=FakeSearch(data), embedding=FakeEmbedding())
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == outcome


def test_fake_receipts_and_target_only_contradictions_are_rejected():
    def forged(payload, n):
        value = decided(payload)
        value["completion"]["comparison_context"]["receipt_refs"] = ["L99"]
        return value

    result = inspect(model=ScriptedModel({"TSEG1": [draft()]}, forged))
    assert (result.failure_code, result.failure_cause) == ("INVALID_DECISION", "INVALID_DECISION_COMPARISON_CONTEXT")
    target_only = inspect(model=ScriptedModel({"TSEG1": [draft()]}, lambda payload, n: decided(
        payload, "CONFLICT", evidence_refs=["T1"])))
    assert target_only.failure_cause == "INVALID_DECISION_EXTERNAL_EVIDENCE_MISSING"


def evidence_then(answer):
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["민수의 손에 관한 이전 서술"])
        return answer(payload)
    return judge


def test_db_raw_disagreement_cannot_infer_correction_from_human_or_protection():
    def conflict(payload):
        value = decided(payload, "CONFLICT", evidence_refs=["B1", "E1"])
        value["disagreements"] = [{"fact_ref": "B1", "evidence_ref": "E1", "reason": "DB와 원문이 다름",
                                   "correction_ref": None}]
        return value

    data = SyntheticData()
    data.entries[0]["state"].update(reviewSource="HUMAN", currentValueProtected=True)
    result = inspect(data, ScriptedModel({"TSEG1": [draft()]}, evidence_then(conflict)),
                     search=FakeSearch(data, hits=["R1"]), embedding=FakeEmbedding())
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "NEEDS_CONTEXT"
    assert {"B1", "E1"} <= set(result.decisions["Q1"].evidence_refs)


@pytest.mark.parametrize("applicable", [True, False])
def test_explicit_author_correction_is_used_only_in_its_exact_scope(applicable):
    data = SyntheticData()
    data.entries[0]["state"]["explicitAuthorCorrection"] = {
        "kind": "AUTHOR_CORRECTION", "ref": "AC1", "subject_ref": "S1", "property_or_rule": "손 사용",
        "time_context": "현재" if applicable else "회상", "conditions": []}
    data.rows = model_settings(data.entries)

    def corrected(payload):
        value = decided(payload)
        value["disagreements"] = [{"fact_ref": "B1", "evidence_ref": "E1", "reason": "정정 이전 값",
                                   "correction_ref": "AC1"}]
        return value

    result = inspect(data, ScriptedModel({"TSEG1": [draft()]}, evidence_then(corrected)),
                     search=FakeSearch(data, hits=["R1"]), embedding=FakeEmbedding())
    assert result.status == "COMPLETED"
    assert result.decisions["Q1"].outcome == ("NO_CONFLICT" if applicable else "NEEDS_CONTEXT")


def test_budget_exhaustion_and_input_change_never_become_ambiguity():
    exhausted = inspect(model=ScriptedModel({"TSEG1": [draft()]}), model_calls=1)
    assert (exhausted.status, exhausted.failure_code) == ("INCOMPLETE", "BUDGET_EXHAUSTED") and not exhausted.decisions
    data = SyntheticData()
    data.invalidated = "INPUT_CHANGED"
    changed = inspect(data, ScriptedModel({"TSEG1": [draft()]}))
    assert changed.failure_code == "INPUT_CHANGED" and not changed.questions and not changed.decisions


def test_execution_publishes_planners_checks_the_grid_then_merges_and_publishes_units():
    checkpoint = FakeCheckpoint(segments_of())
    result = inspect(model=ScriptedModel(two_segment_plans()), checkpoint=checkpoint, segment_chars=20)
    assert result.status == "COMPLETED"
    planners = sorted(row for row in checkpoint.published if row[:2] == ("plans", "PLANNER"))
    assert planners == [("plans", "PLANNER", "TSEG1"), ("plans", "PLANNER", "TSEG2")]
    order = [row[0] if row[0] != "plans" else row[1] for row in checkpoint.published]
    assert order.index("grid") > max(index for index, kind in enumerate(order) if kind == "PLANNER")
    assert order.index("MERGED_PLAN") > order.index("grid")
    assert sorted(row for row in checkpoint.published if row[0] == "units") == [
        ("units", "Q1", "NO_CONFLICT", 2), ("units", "Q2", "NO_CONFLICT", 2)]
    saved = checkpoint.stages["PLANNER", "TSEG1"]
    assert set(saved) == {"questions", "segments"} and "lookup_intents" not in saved["questions"][0]


@pytest.mark.parametrize(("version", "unit", "code"), [(3, 20, "GRID_VERSION_UNSUPPORTED"),
                                                       (4, 100, "GRID_SEGMENTS_MISMATCH")])
def test_execution_stops_before_merging_when_the_server_grid_differs(version, unit, code):
    checkpoint = FakeCheckpoint(segments_of(unit=unit), grid_version=version)
    result = inspect(model=ScriptedModel(two_segment_plans()), checkpoint=checkpoint, segment_chars=20)
    assert (result.failure_code, result.failure_stage) == (code, "MERGED_PLAN")
    assert ("plans", "MERGED_PLAN", "PLAN") not in checkpoint.published


def test_resume_reuses_saved_planner_stages_and_decided_checks_and_restarts_the_rest():
    first = FakeCheckpoint(segments_of())
    inspect(model=ScriptedModel(two_segment_plans()), checkpoint=first, segment_chars=20)
    resumed = FakeCheckpoint(segments_of(), stages=first.stages, decided={"Q1"})
    model = ScriptedModel(two_segment_plans())
    result = inspect(model=model, checkpoint=resumed, segment_chars=20)
    assert result.status == "COMPLETED"
    assert [role for role, _, _ in model.calls] == ["JUDGE"]
    assert model.judge_calls()[0]["question_ref"] == "Q2" and model.judge_calls()[0]["form"] == "OPEN"
    assert result.decisions["Q1"].reason == "저장된 판정"
    assert [row for row in resumed.published if row[0] == "plans"] == []


def test_experiment_mode_never_publishes():
    with pytest.raises(ValueError, match="Experiment"):
        SettingValidationService(SyntheticData(), ScriptedModel(), budget(), ROLES,
                                 checkpoint=FakeCheckpoint(segments_of()), experiment=True)


def test_settings_read_failure_is_a_run_level_stop():
    data = SyntheticData()
    data.invalidated = "DATA_ACCESS_FAILED"
    result = inspect(data, ScriptedModel({"TSEG1": [draft()]}), experiment=True)
    assert result.failure_code == "DATA_ACCESS_FAILED" and result.failure_stage == "JUDGE"
    assert result.question_failures == {}
