"""Source-owned quote recovery; no model/API/DB execution or semantic verdict."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.analysis.evidence_span_resolver import resolve_evidence_span_offsets
from app.analysis.schemas import ExtractedEvidenceSpan
from app.analysis.world_setting_schemas import ExtractedWorldSettingCandidate
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper


@pytest.fixture
def actual_counterexample():
    return json.loads(
        (Path(__file__).parent / "fixtures/ai_v5_actual_quote_counterexample.json").read_text()
    )


def test_actual_saved_episode_quote_restores_original_contiguous_slice(actual_counterexample):
    source = actual_counterexample["sourceText"]
    assert hashlib.sha256(source.encode()).hexdigest() == actual_counterexample["sourceSha256"]
    original = actual_counterexample["evidenceSpans"][0]
    assert original["quote"] not in source  # Observed whitespace error, not invented failure.
    span = ExtractedEvidenceSpan.model_validate(original)
    resolved = resolve_evidence_span_offsets(span, source, 0)

    assert resolved.start_offset == original["startOffset"]
    assert resolved.end_offset == original["endOffset"]
    assert resolved.quote == source[resolved.start_offset:resolved.end_offset]
    assert "\n" in resolved.quote
    assert span.quote == original["quote"]


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("가동 인원:\n  두 명", "가동 인원: 두 명"),
        ("✨그는\t\t검을 들었다", "그는 검을 들었다"),
        ("정상\r\n감각", "정상 감각"),
        ("앞말　뒷말", "앞말 뒷말"),
    ],
)
def test_unique_whitespace_match_returns_exact_quote_and_codepoint_offsets(source, quote):
    resolved = resolve_evidence_span_offsets(ExtractedEvidenceSpan(quote=quote), source, 51)
    assert resolved.start_offset is not None
    assert resolved.end_offset is not None
    assert resolved.quote == source[resolved.start_offset - 51:resolved.end_offset - 51]
    assert len(resolved.quote) == resolved.end_offset - resolved.start_offset


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("정상\n감각. 정상\t감각.", "정상 감각."),
        ("정상 감각. 정상 감각.", "정상 감각."),
        ("Ａ는 정상이다", "A는 정상이다"),
        ("앞말. 중간 말. 뒷말.", "앞말. 뒷말."),
        ("가동 인원: 두 명", "가동 인원: 한 명"),
        ("정상 감각", "정상 감각 unresolved_references: []"),
    ],
)
def test_ambiguous_or_changed_quote_is_never_reconstructed(source, quote):
    span = ExtractedEvidenceSpan(quote=quote, start_offset=999, end_offset=1000)
    resolved = resolve_evidence_span_offsets(span, source, 0)
    assert resolved.quote == quote
    assert resolved.start_offset is None
    assert resolved.end_offset is None


def test_exact_quote_is_preserved_with_matching_unique_offsets():
    source = "머리말\n정상 감각\n끝"
    quote = "정상 감각"
    start = source.index(quote)
    span = ExtractedEvidenceSpan(quote=quote, start_offset=start, end_offset=start + len(quote))
    assert resolve_evidence_span_offsets(span, source, 0) == span


def test_world_publication_and_raw_origin_both_preserve_exact_source_quote(actual_counterexample):
    source = actual_counterexample["sourceText"]
    candidate = ExtractedWorldSettingCandidate(
        category="IMPORTANT_ITEM", subject_name="잔향등", setting_name="작동 재료",
        extracted_value="받침의 홈에 소금을 넣으면 작동한다.", confidence=0.95,
        evidence_spans=[ExtractedEvidenceSpan.model_validate(actual_counterexample["evidenceSpans"][0])],
    )
    item = WorldSettingCandidateMapper.to_publish_item(
        candidate, SimpleNamespace(chunk_text=source, start_offset=0),
    )
    published = item.evidence_spans[0]
    raw = item.raw_extraction_json["evidence_spans"][0]
    exact = source[published.start_offset:published.end_offset]
    assert published.quote == exact
    assert raw["quote"] == exact
    assert raw["start_offset"] == published.start_offset
    assert raw["end_offset"] == published.end_offset
