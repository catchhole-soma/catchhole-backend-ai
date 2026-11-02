from dataclasses import replace

import pytest


def question(ref="q1", subject_refs=("S1",), mention="민수", start=0):
    from app.validation.contracts import QuestionDraft, Span
    return QuestionDraft(
        local_ref=ref, target_spans=[Span(source_ref="T1", start=start, end=start + 2,
                                         quote="민수")],
        subject_refs=list(subject_refs), subject_mentions=[mention], property_or_rule="손 사용",
        time_context="현재", conditions=["검을 쥠"], assertion_mode="NARRATION",
        check_intent="양손 사용 가능 여부", reason="행동 전제 확인",
    )


def test_typo_segmentation_covers_every_code_point_and_keeps_exact_repeated_quotes():
    from app.validation.contracts import Span, split_segments, validate_span
    text = "😀민수\n민수는 검을 쥐었다.\n" * 3
    segments = split_segments(text, core_chars=9, context_chars=4)
    assert "".join(text[s.start:s.end] for s in segments) == text
    assert [(s.start, s.end) for s in segments][:2] == [(0, 9), (9, 18)]
    assert all(s.context_start <= s.start < s.end <= s.context_end for s in segments)
    assert validate_span(Span(source_ref="T1", start=1, end=3, quote="민수"), text)
    with pytest.raises(ValueError):
        validate_span(Span(source_ref="T1", start=0, end=2, quote="민수"), text)


def test_merging_preserves_every_span_and_planner_origin():
    from app.validation.contracts import merge_questions
    one, two = question(), question("q2", start=10)
    merged = merge_questions([(one, "PLANNER:TSEG1"), (two, "PLANNER:TSEG2")])
    assert len(merged) == 1
    assert merged[0].original_refs == ("PLANNER:TSEG1:q1", "PLANNER:TSEG2:q2")
    assert [s.start for s in merged[0].draft.target_spans] == [0, 10]
    assert "lookup_intents" not in merged[0].draft.model_dump()
    # Unknown identities are never collapsed merely because both ID sets are empty.
    assert len(merge_questions([
        (question("q3", (), "낯선 남자"), "PLANNER:TSEG1"),
        (question("q4", (), "낯선 여자"), "PLANNER:TSEG2"),
    ])) == 2
    assert len(merge_questions([
        (one, "PLANNER:TSEG1"),
        (two.model_copy(update={"time_context": "회상"}), "PLANNER:TSEG2"),
    ])) == 2


def test_planner_draft_rejects_lookup_intents():
    from pydantic import ValidationError

    from app.validation.contracts import QuestionDraft
    with pytest.raises(ValidationError):
        QuestionDraft.model_validate({**question().model_dump(), "lookup_intents": []})


def test_batch_validation_is_per_segment_core_only_and_planned_iff_questions():
    from app.validation.contracts import (
        QuestionBatch,
        SegmentCoverage,
        Span,
        split_setting_segments,
        validate_batch,
    )
    text = "민수가 쉬었다.\n민수가 검을 쥐었다."
    first, second = split_setting_segments(text, unit_chars=10, context_chars=4)
    empty = QuestionBatch(questions=[], segments=[SegmentCoverage(segment_ref=first.ref, status="NO_CHECK",
                                                                   reason="비교할 사실 없음")])
    validate_batch(empty, first, {"S1"}, text)
    with pytest.raises(ValueError, match="SEGMENT_COVERAGE_MISSING"):
        validate_batch(empty, second, {"S1"}, text)
    with pytest.raises(ValueError, match="SEGMENT_QUESTION_COVERAGE_MISMATCH"):
        validate_batch(empty.model_copy(update={"segments": [empty.segments[0].model_copy(
            update={"status": "PLANNED"})]}), first, {"S1"}, text)
    in_context = question(start=text.index("민수", second.start)).model_copy(update={"target_spans": [
        Span(source_ref="T1", start=second.start, end=second.start + 2, quote="민수")]})
    planned = QuestionBatch(questions=[in_context], segments=[SegmentCoverage(
        segment_ref=first.ref, status="PLANNED", reason="질문")])
    # The span exists in the first segment's read-only context but not in its core.
    assert first.context_end > second.start
    with pytest.raises(ValueError, match="SPAN_OUTSIDE_SEGMENT"):
        validate_batch(planned, first, {"S1"}, text)
    validate_batch(planned.model_copy(update={"segments": [SegmentCoverage(
        segment_ref=second.ref, status="PLANNED", reason="질문")]}), second, {"S1"}, text)
    with pytest.raises(ValueError, match="UNKNOWN_SUBJECT_REFERENCE"):
        validate_batch(planned.model_copy(update={"segments": [SegmentCoverage(
            segment_ref=second.ref, status="PLANNED", reason="질문")]}), second, {"S2"}, text)


def test_follow_up_requests_are_validated_before_any_read():
    from app.validation.contracts import validate_context_request, validate_search_query
    assert validate_context_request(0, 3, 3) == (0, 3)
    for start, end in ((2, 2), (-1, 1), (0, 4)):
        with pytest.raises(ValueError, match="INVALID_TARGET_CONTEXT_REQUEST"):
            validate_context_request(start, end, 3)
    assert validate_search_query("민수가 의수를 얻은 장면")
    for query in ("", "   ", "가" * 1001, None):
        with pytest.raises(ValueError, match="INVALID_SEARCH_REQUEST"):
            validate_search_query(query)


def test_budget_exhaustion_and_invalid_values_fail_closed():
    from app.validation.contracts import BudgetState, IncompleteInspection, InvocationBudget
    budget = InvocationBudget(model_calls=1, lookups=1, seconds=10, input_tokens=1000, segment_chars=20,
                              context_chars=3, search_limit=3)
    state = BudgetState(budget)
    state.consume("model")
    with pytest.raises(IncompleteInspection, match="BUDGET_EXHAUSTED"):
        state.consume("model")
    for change in ({"model_calls": 0}, {"search_limit": 101}, {"context_chars": -1}, {"seconds": float("inf")}):
        with pytest.raises(ValueError):
            replace(budget, **change)
