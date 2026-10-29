from uuid import UUID

import pytest

from app.analysis.evidence_span_resolver import (
    resolve_candidate_evidence_offsets,
    resolve_evidence_span_offsets,
)
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate

CHUNK_ID = UUID("00000000-0000-0000-0000-000000000001")
LONG_CHUNK_TEXT = (
    "성벽 위로 붉은 달빛이 흘렀다.\n\n"
    "카엘은 숨을 고르고 검집에서 화염검을 뽑았다. "
    "그는 12레벨 검사답게 흔들림 없이 앞으로 나아갔다.\n"
    "불꽃은 칼날을 타고 번졌고, 적들은 그 기세에 물러섰다."
)


def test_resolve_evidence_span_offsets_with_exact_match() -> None:
    # 실제 LLM 호출을 검증하는 테스트가 아니라, LLM이 quote를 반환했다고 가정하고
    # Python 후처리가 해당 quote를 청크 원문에서 다시 찾아 offset을 보정하는지 확인한다.
    quote = "12레벨 검사답게"
    span = ExtractedEvidenceSpan(
        quote=quote,
        start_offset=None,
        end_offset=None,
    )

    chunk_start_offset = 100
    resolved = resolve_evidence_span_offsets(
        span,
        chunk_text=LONG_CHUNK_TEXT,
        chunk_start_offset=chunk_start_offset,
    )

    expected_start_offset = chunk_start_offset + LONG_CHUNK_TEXT.index(quote)
    assert resolved.quote == quote
    assert resolved.start_offset == expected_start_offset
    assert resolved.end_offset == expected_start_offset + len(quote)


def test_resolve_evidence_span_offsets_with_whitespace_normalized_match() -> None:
    # LLM이 원문 quote를 복사하되 줄바꿈/연속 공백만 다르게 반환한 경우를 보정한다.
    span = ExtractedEvidenceSpan(
        quote="카엘은 12레벨 검사",
        start_offset=None,
        end_offset=None,
    )

    resolved = resolve_evidence_span_offsets(
        span,
        chunk_text="카엘은\n12레벨   검사로, 화염검을 장비하고 있었다.",
        chunk_start_offset=20,
    )

    assert resolved.start_offset == 20
    assert resolved.end_offset == 33
    assert resolved.quote == "카엘은\n12레벨   검사"


@pytest.mark.parametrize(
    ("source", "quote", "expected_quote", "local_start"),
    [
        (
            '아린이 물었다. "지금 갈까?" 라온이 고개를 끄덕였다.',
            '아린이 물었다. "지금 갈까? 라온이 고개를 끄덕였다.',
            '아린이 물었다. "지금 갈까?" 라온이 고개를 끄덕였다.',
            0,
        ),
        (
            '그는 “문이 닫혔다”고 말했다.', '그는 "문이 닫혔다"고 말했다.',
            '그는 “문이 닫혔다”고 말했다.', 0,
        ),
        (
            '그는 "문이 닫혔다"고 말했다.', '그는 “문이 닫혔다”고 말했다.',
            '그는 "문이 닫혔다"고 말했다.', 0,
        ),
        (
            '그는 “안전하다”고 말했다.', "그는 안전하다고 말했다.",
            '그는 “안전하다”고 말했다.', 0,
        ),
        (
            "그는 안전하다고 말했다.", '그는 “안전하다”고 말했다.',
            "그는 안전하다고 말했다.", 0,
        ),
        (
            '그는 \t“\n문이  닫혔다”고 말했다.', '그는 "문이 닫혔다"고 말했다.',
            '그는 \t“\n문이  닫혔다”고 말했다.', 0,
        ),
        ("앞. “갈까?” 뒤.", '"갈까?"', "갈까?", 4),
        (
            '🧭 앞. 그는 “문이 닫혔다”고 말했다. 뒤.', '그는 "문이 닫혔다"고 말했다.',
            '그는 “문이 닫혔다”고 말했다.', 5,
        ),
    ],
    ids=[
        "missing-dialogue-close", "curly-source", "ascii-source", "missing-quotes",
        "extra-quotes", "whitespace-around-quotes", "minimal-source-range", "emoji-prefix",
    ],
)
def test_quote_differences_restore_exact_source_slice_and_unicode_offsets(
    source, quote, expected_quote, local_start,
) -> None:
    span = ExtractedEvidenceSpan(quote=quote, start_offset=999, end_offset=1000)
    original = span.model_dump()

    resolved = resolve_evidence_span_offsets(span, source, 100)

    assert resolved.quote == expected_quote
    assert resolved.start_offset == 100 + local_start
    assert resolved.end_offset == 100 + local_start + len(expected_quote)
    assert resolved.quote == source[resolved.start_offset - 100:resolved.end_offset - 100]
    assert span.model_dump() == original


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        (
            '그는 “문이 닫혔다”고 말했다. 그는 "문이 닫혔다"고 말했다.',
            "그는 문이 닫혔다고 말했다.",
        ),
        ('가“가”가', '"가가"'),
        ("문장.", '"“”'),
        ("문장.", '" \t“\n”"'),
        ('그는 “12층”에 있었다.', '그는 "13층"에 있었다.'),
        ('그는 “안전하지 않다”고 말했다.', '그는 "안전하다"고 말했다.'),
        ('그는 “빛”과 \'문\'을 보았다.', '그는 "빛"과 문을 보았다.'),
        ('그는 “빛”과 ‘문’을 보았다.', '그는 "빛"과 \'문\'을 보았다.'),
        ('그는 “가자!”고 말했다.', '그는 "가자?"고 말했다.'),
        ('그는 “빛, 문”을 보았다.', '그는 "빛 문"을 보았다.'),
        ('그는 “-3도”라고 말했다.', '그는 "3도"라고 말했다.'),
        ('그는 “마법사”였다.', '그는 "검사"였다.'),
        ('그는 “문이', '그는 "문이 닫혔다"고 말했다.'),
    ],
    ids=[
        "repeated", "overlapping", "quotes-only", "quotes-and-whitespace-only", "number",
        "negation", "missing-single-quotes", "single-quote-style", "punctuation",
        "missing-comma", "minus-sign", "changed-word", "crosses-chunk-end",
    ],
)
def test_quote_normalization_keeps_invalid_evidence_unlocated(source, quote) -> None:
    span = ExtractedEvidenceSpan(quote=quote, start_offset=999, end_offset=1000)
    original = span.model_dump()

    resolved = resolve_evidence_span_offsets(span, source, 100)

    assert resolved.quote == quote
    assert resolved.start_offset is None
    assert resolved.end_offset is None
    assert span.model_dump() == original


@pytest.mark.parametrize(
    ("source", "quote", "expected_quote"),
    [
        (
            '그는 "문이 닫혔다"고 말했다. 그는 “문이 닫혔다”고 말했다.',
            '그는 "문이 닫혔다"고 말했다.',
            '그는 "문이 닫혔다"고 말했다.',
        ),
        (
            '그는  “문이\n닫혔다”고 말했다. 그는 "문이 닫혔다"고 말했다.',
            '그는 “문이 닫혔다”고 말했다.',
            '그는  “문이\n닫혔다”고 말했다.',
        ),
    ],
    ids=["exact", "whitespace"],
)
def test_existing_matches_keep_priority_over_quote_normalization(
    source, quote, expected_quote,
) -> None:
    resolved = resolve_evidence_span_offsets(ExtractedEvidenceSpan(quote=quote), source, 20)

    assert resolved.quote == expected_quote
    assert resolved.start_offset == 20
    assert resolved.end_offset == 20 + len(expected_quote)


def test_resolve_evidence_span_offsets_keeps_null_offsets_when_quote_is_not_found() -> None:
    # quote를 찾지 못하면 LLM이 준 기존 offset을 믿지 않고 null로 되돌린다.
    span = ExtractedEvidenceSpan(
        quote="원문에 없는 근거",
        start_offset=1,
        end_offset=5,
    )

    resolved = resolve_evidence_span_offsets(
        span,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        chunk_start_offset=100,
    )

    assert resolved.start_offset is None
    assert resolved.end_offset is None


def test_resolve_candidate_evidence_offsets_resolves_all_candidate_spans() -> None:
    # Worker 저장 직전에 후보 객체 전체를 보정하는 흐름을 검증한다.
    # model_copy를 사용하므로 원본 LLM 결과 객체는 변경되지 않아야 한다.
    quote = "화염검을 뽑았다"
    candidate = ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name="카엘",
        attribute_name="skill.화염검술",
        attribute_value="화염검술",
        value_type="JSON",
        value_json={"name": "화염검술"},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote=quote,
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )

    resolved_candidates = resolve_candidate_evidence_offsets(
        [candidate],
        chunk_text=LONG_CHUNK_TEXT,
        chunk_start_offset=300,
    )

    expected_start_offset = 300 + LONG_CHUNK_TEXT.index(quote)
    resolved_span = resolved_candidates[0].evidence_spans[0]
    assert resolved_span.start_offset == expected_start_offset
    assert resolved_span.end_offset == expected_start_offset + len(quote)
    assert candidate.evidence_spans[0].start_offset is None


def test_resolve_candidate_evidence_offsets_orders_spans_by_source_position() -> None:
    first_quote = "카엘은 숨을 고르고"
    second_quote = "적들은 그 기세에 물러섰다"
    candidate = _candidate_with_spans(
        ExtractedEvidenceSpan(quote=second_quote),
        ExtractedEvidenceSpan(quote=first_quote),
    )

    resolved_candidate = resolve_candidate_evidence_offsets(
        [candidate],
        chunk_text=LONG_CHUNK_TEXT,
        chunk_start_offset=200,
    )[0]

    assert [span.quote for span in resolved_candidate.evidence_spans] == [
        first_quote,
        second_quote,
    ]
    assert [span.start_offset for span in resolved_candidate.evidence_spans] == [
        200 + LONG_CHUNK_TEXT.index(first_quote),
        200 + LONG_CHUNK_TEXT.index(second_quote),
    ]


def test_resolve_candidate_evidence_offsets_keeps_repeated_quote_without_position() -> None:
    quote = "상처가 나았다."
    chunk_text = f"{quote} 잠시 쉬었다. {quote}"
    candidate = _candidate_with_spans(
        ExtractedEvidenceSpan(quote=quote, start_offset=999, end_offset=1006),
        ExtractedEvidenceSpan(quote=quote, start_offset=None, end_offset=None),
    )

    resolved_candidate = resolve_candidate_evidence_offsets(
        [candidate],
        chunk_text=chunk_text,
        chunk_start_offset=50,
    )[0]

    assert len(resolved_candidate.evidence_spans) == 2
    assert all(span.quote == quote for span in resolved_candidate.evidence_spans)
    assert all(span.start_offset is None for span in resolved_candidate.evidence_spans)
    assert all(span.end_offset is None for span in resolved_candidate.evidence_spans)
    assert len(candidate.evidence_spans) == 2


def test_resolve_candidate_evidence_offsets_deduplicates_same_unique_span() -> None:
    quote = "상처가 나았다."
    candidate = _candidate_with_spans(
        ExtractedEvidenceSpan(quote=quote),
        ExtractedEvidenceSpan(quote=quote),
    )

    resolved_candidate = resolve_candidate_evidence_offsets(
        [candidate],
        chunk_text=f"전투가 끝났다. {quote}",
        chunk_start_offset=50,
    )[0]

    assert len(resolved_candidate.evidence_spans) == 1
    assert resolved_candidate.evidence_spans[0].start_offset == 59
    assert resolved_candidate.evidence_spans[0].end_offset == 59 + len(quote)


def test_resolve_candidate_evidence_offsets_keeps_unmatched_spans_in_input_order() -> None:
    candidate = _candidate_with_spans(
        ExtractedEvidenceSpan(quote="두 번째 미확인 근거", start_offset=10, end_offset=20),
        ExtractedEvidenceSpan(quote="첫 번째 미확인 근거", start_offset=30, end_offset=40),
    )

    resolved_candidate = resolve_candidate_evidence_offsets(
        [candidate],
        chunk_text=LONG_CHUNK_TEXT,
        chunk_start_offset=100,
    )[0]

    assert [span.quote for span in resolved_candidate.evidence_spans] == [
        "두 번째 미확인 근거",
        "첫 번째 미확인 근거",
    ]
    assert all(span.start_offset is None for span in resolved_candidate.evidence_spans)
    assert all(span.end_offset is None for span in resolved_candidate.evidence_spans)


def test_recovered_spans_are_ordered_and_deduplicated_without_changing_raw_input() -> None:
    source = '🌙 그는 “문이 닫혔다”고 말했다. 그녀가 "다시 확인할까?"라고 물었다.'
    first_quote = '그는 “문이 닫혔다”고 말했다.'
    second_quote = '그녀가 "다시 확인할까?"라고 물었다.'
    candidate = _candidate_with_spans(
        ExtractedEvidenceSpan(quote='그녀가 "다시 확인할까?라고 물었다.'),
        ExtractedEvidenceSpan(quote="원문에 없는 근거", start_offset=1, end_offset=5),
        ExtractedEvidenceSpan(quote='그는 "문이 닫혔다"고 말했다.'),
        ExtractedEvidenceSpan(quote="그는 문이 닫혔다고 말했다."),
    )
    original = candidate.model_dump()

    resolved = resolve_candidate_evidence_offsets([candidate], source, 70)[0]

    assert [span.quote for span in resolved.evidence_spans] == [
        first_quote, second_quote, "원문에 없는 근거",
    ]
    for span in resolved.evidence_spans[:2]:
        assert span.start_offset == 70 + source.index(span.quote)
        assert span.end_offset == span.start_offset + len(span.quote)
        assert source[span.start_offset - 70:span.end_offset - 70] == span.quote
    assert resolved.evidence_spans[2].start_offset is None
    assert resolved.evidence_spans[2].end_offset is None
    assert candidate.model_dump() == original


def _candidate_with_spans(
    *spans: ExtractedEvidenceSpan,
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name="카엘",
        attribute_name="status.회복",
        attribute_value="회복됨",
        value_type="JSON",
        value_json={"name": "회복", "active": False},
        evidence_spans=list(spans),
        confidence=0.9,
    )
