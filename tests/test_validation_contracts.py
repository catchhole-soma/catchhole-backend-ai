from dataclasses import replace

import pytest


def question(ref="q1", subject_refs=("S1",), mention="민수", start=0):
    from app.validation.contracts import QuestionDraft, Span, LookupIntent
    return QuestionDraft(
        local_ref=ref, target_spans=[Span(source_ref="T1", start=start, end=start + 2,
                                         quote="민수")],
        subject_refs=list(subject_refs), subject_mentions=[mention], property_or_rule="손 사용",
        time_context="현재", conditions=["검을 쥠"], assertion_mode="NARRATION",
        check_intent="양손 사용 가능 여부", reason="행동 전제 확인",
        lookup_intents=[LookupIntent(ref=ref + "-read", kind="CHANGES_EXCEPTIONS",
                                    operation="KEYWORD", purpose="의수 획득/사용 조건 확인",
                                    query="민수 의수")],
    )


def test_segmentation_covers_every_code_point_and_keeps_exact_repeated_quotes():
    from app.validation.contracts import split_segments, validate_span, Span
    text = "😀민수\n민수는 검을 쥐었다.\n" * 3
    segments = split_segments(text, core_chars=9, context_chars=4)
    assert "".join(text[s.start:s.end] for s in segments) == text
    assert [(s.start, s.end) for s in segments][:2] == [(0, 9), (9, 18)]
    assert all(s.context_start <= s.start < s.end <= s.context_end for s in segments)
    assert validate_span(Span(source_ref="T1", start=1, end=3, quote="민수"), text)
    with pytest.raises(ValueError):
        validate_span(Span(source_ref="T1", start=0, end=2, quote="민수"), text)


def test_merging_preserves_every_span_origin_and_lookup_obligation():
    from app.validation.contracts import merge_questions
    one, two = question(), question("q2", start=10)
    merged = merge_questions([(one, "PLANNER"), (two, "COVERAGE")])
    assert len(merged) == 1
    assert merged[0].original_refs == ("PLANNER:q1", "COVERAGE:q2")
    assert [s.start for s in merged[0].draft.target_spans] == [0, 10]
    assert {i.ref for i in merged[0].draft.lookup_intents} == {"q1-read", "q2-read"}
    # Unknown identities are never collapsed merely because both ID sets are empty.
    assert len(merge_questions([
        (question("q3", (), "낯선 남자"), "PLANNER"),
        (question("q4", (), "낯선 여자"), "COVERAGE"),
    ])) == 2
    assert len(merge_questions([
        (one, "PLANNER"),
        (two.model_copy(update={"time_context": "회상"}), "COVERAGE"),
    ])) == 2


def test_missing_processed_segment_is_different_from_normal_zero_questions():
    from app.validation.contracts import QuestionBatch, SegmentCoverage, validate_batch, split_segments
    segments = split_segments("민수가 쉬었다.", core_chars=4, context_chars=2)
    batch = QuestionBatch(questions=[], segments=[
        SegmentCoverage(segment_ref=s.ref, status="NO_CHECK", reason="비교할 사실 없음")
        for s in segments
    ], rules=[])
    validate_batch(batch, segments, [], [], "민수가 쉬었다.")
    with pytest.raises(ValueError, match="COVERAGE"):
        validate_batch(batch.model_copy(update={"segments": batch.segments[:-1]}),
                       segments, [], [], "민수가 쉬었다.")


def test_budget_exhaustion_and_invalid_values_fail_closed():
    from app.validation.contracts import InvocationBudget, BudgetState, IncompleteInspection
    budget = InvocationBudget(model_calls=1, lookups=1, seconds=10,
                              input_tokens=1000, output_tokens=100, segment_chars=20,
                              context_chars=3, catalogue_page_size=2, search_limit=3)
    state = BudgetState(budget)
    state.consume("model")
    with pytest.raises(IncompleteInspection, match="BUDGET_EXHAUSTED"):
        state.consume("model")
    with pytest.raises(ValueError):
        replace(budget, model_calls=0)
