"""BOOK grounding at the real extraction/mapper boundary; no model/network."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.exceptions import LlmExtractionError
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import WorkerAnalysisJobPayload
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import book_payload_data
from tests.test_world_setting_extractor import FakeTextClient


def output(quote, **overrides):
    return json.dumps({"candidates": [{
        "category": "IMPORTANT_ITEM", "subject_name": "기록등", "scope_name": None,
        "setting_name": "보관 장소", "extracted_value": "도서관 보관실",
        "confidence": 0.95, "temporal_scope": "PRESENT", "effective_from_episode_no": None,
        "epistemic_status": "ASSERTED", "evidence_spans": [{"quote": quote}],
        **overrides,
    }]}, ensure_ascii=False)


def extract(source, responses, *, book=True, ordered=True, attempts=2):
    client = FakeTextClient(responses)
    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    result = asyncio.run(WorldSettingExtractor(llm_client=client, max_attempts=attempts).extract_from_chunk(
        source, **({"setting_book": payload.setting_book} if book else {}),
        **({"analysis_context": payload.analysis_context} if ordered else {}),
    ))
    return result, client


@pytest.mark.parametrize("bad", [
    "보관 장소: 도서관 보관실 unresolved_references: []",
    "보관 장소: 도서관 보관실 사용 조건: 마른 손",
    "보관 장소: 관제실",
    " ",
])
def test_book_malformed_factual_quote_retries_without_accepting_header_as_replacement(bad):
    source = "[도구 / 기록등]\n보관 장소: 도서관 보관실\n주위 조건: 밝은 곳\n사용 조건: 마른 손\n"
    bad_output = output(bad, evidence_spans=[{"quote": "[도구 / 기록등]"}, {"quote": bad}])
    result, client = extract(source, [bad_output, output("보관 장소: 도서관 보관실")])
    assert len(client.requests) == 2
    assert "보관 장소: 도서관 보관실" in [span.quote for span in result.candidates[0].evidence_spans]
    assert bad not in client.requests[1]["user_prompt"] or bad in source


def test_book_malformed_quote_exhausts_bounded_retries_instead_of_empty_success():
    with pytest.raises(LlmExtractionError):
        extract("보관 장소: 도서관 보관실", [output("없는 원문"), output("없는 원문")])


def test_unique_whitespace_difference_is_restored_to_exact_unicode_source_slice():
    source = "[도구 / 기록등]\n보관 장소:  도서관\n보관실\n"
    result, client = extract(source, [output("보관 장소: 도서관 보관실")])
    assert len(client.requests) == 1
    candidate = result.candidates[0]
    assert "보관 장소:  도서관\n보관실" in [span.quote for span in candidate.evidence_spans]
    chunk = EpisodeChunk(id=uuid4(), episode_id=uuid4(), chunk_index=0, chunk_text=source,
                         start_offset=37, end_offset=37 + len(source))
    published = WorldSettingCandidateMapper.to_publish_item(candidate, chunk)
    for span in published.evidence_spans:
        assert source[span.start_offset - 37:span.end_offset - 37] == span.quote


def test_book_world_quote_recovery_reaches_publication_with_exact_unicode_source_slices():
    source = '[도구 / 기록등]\n🧭보관 장소: “도서관\t보관실” 안쪽\n'
    quote = '보관 장소: "도서관 보관실" 안쪽'
    expected_quote = '보관 장소: “도서관\t보관실” 안쪽'
    result, client = extract(source, [output(quote)], attempts=1)
    candidate = result.candidates[0]
    original = candidate.model_dump()
    chunk = EpisodeChunk(id=uuid4(), episode_id=uuid4(), chunk_index=0, chunk_text=source,
                         start_offset=37, end_offset=37 + len(source))

    published = WorldSettingCandidateMapper.to_publish_item(candidate, chunk)

    assert len(client.requests) == 1
    span = published.evidence_spans[0]
    assert span.quote == expected_quote
    assert span.start_offset == 37 + source.index(expected_quote)
    assert span.end_offset == span.start_offset + len(expected_quote)
    for span in published.evidence_spans:
        assert source[span.start_offset - 37:span.end_offset - 37] == span.quote
    assert candidate.model_dump() == original


def test_exact_repeated_source_quote_remains_valid_with_ambiguous_offsets():
    source = "보관 장소: 도서관 보관실\n보관 장소: 도서관 보관실\n"
    result, _ = extract(source, [output("보관 장소: 도서관 보관실")])
    chunk = EpisodeChunk(id=uuid4(), episode_id=uuid4(), chunk_index=0, chunk_text=source,
                         start_offset=0, end_offset=len(source))
    span = WorldSettingCandidateMapper.to_publish_item(result.candidates[0], chunk).evidence_spans[0]
    assert span.quote == "보관 장소: 도서관 보관실"
    assert span.start_offset is None and span.end_offset is None


def test_book_source_layout_adds_only_the_actual_parent_for_named_field():
    source = "[지역 / 낮항]\n주교역품: 밀\n\n[도구 / 기록등]\n보관 장소: 도서관 보관실\n"
    result, _ = extract(source, [output("보관 장소: 도서관 보관실")])
    quotes = [span.quote for span in result.candidates[0].evidence_spans]
    assert "[도구 / 기록등]" in quotes
    assert "[지역 / 낮항]" not in quotes


def test_book_source_and_metadata_are_separate_json_values_and_strict_marker_is_required():
    source = "보관 장소: 도서관 보관실"
    result, client = extract(source, [output(source)])
    request = client.requests[0]
    assert request["user_prompt"].startswith("{")
    data = json.loads(request["user_prompt"])
    assert data["chunk_text"] == source and data["unresolved_references"] == []
    schema = request["response_schema"].schema
    candidate = schema["$defs"]["BookExtractedWorldSettingCandidate"]
    assert "epistemic_status" in candidate["required"]
    assert set(candidate["properties"]["epistemic_status"]["enum"]) == {"ASSERTED", "UNRESOLVED_CLAIM"}
    assert getattr(result.candidates[0], "epistemic_status", None) == "ASSERTED"


def test_episode_extraction_does_not_gain_book_grounding_marker_or_request_shape():
    result, client = extract("도구가 빛났다.", [output("없는 원문")], book=False, ordered=False)
    assert result.candidates[0].evidence_spans[0].quote == "없는 원문"
    assert client.requests[0]["user_prompt"].startswith("metadata:\n")
    assert "epistemic_status" not in result.candidates[0].model_dump()


@pytest.mark.parametrize("repeat", [False, True])
def test_exact_quote_crossing_distinct_source_sections_is_not_accepted_as_identity_evidence(repeat):
    source = "[도구 / 기록등]\n보관 장소: 도서관 보관실\n[지역 / 낮항]\n주교역품: 밀\n"
    if repeat:
        source += source
    result, client = extract(source, [output(source[:len(source) // 2] if repeat else source), output("보관 장소: 도서관 보관실")])
    assert len(client.requests) == 2
    assert "[지역 / 낮항]" not in [span.quote for span in result.candidates[0].evidence_spans]


@pytest.mark.parametrize("marker", [None, "UNKNOWN", "asserted"])
def test_invalid_book_claim_marker_is_retried_without_inferring_asserted(marker):
    result, client = extract("보관 장소: 도서관 보관실", [
        output("보관 장소: 도서관 보관실", epistemic_status=marker), output("보관 장소: 도서관 보관실")])
    assert len(client.requests) == 2 and result.candidates[0].epistemic_status == "ASSERTED"
