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
        attribute_name="skills.화염검술",
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
