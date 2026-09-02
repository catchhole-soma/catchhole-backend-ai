from uuid import UUID

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


def test_resolve_candidate_evidence_offsets_deduplicates_repeated_quote() -> None:
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

    assert len(resolved_candidate.evidence_spans) == 1
    assert resolved_candidate.evidence_spans[0].quote == quote
    assert resolved_candidate.evidence_spans[0].start_offset == 50
    assert resolved_candidate.evidence_spans[0].end_offset == 50 + len(quote)
    assert len(candidate.evidence_spans) == 2


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
