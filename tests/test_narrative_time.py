"""Located time metadata crosses chunk boundaries without inventing chronology."""
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.analysis.narrative_time import (
    NarrativeTimePoint,
    ground_extraction_narrative_time,
    time_context_prompt_data,
)
from app.analysis.schemas import CharacterSettingExtractionResult

CHUNK = UUID("10000000-0000-4000-8000-000000000001")
NEXT_CHUNK = UUID("10000000-0000-4000-8000-000000000002")


def _result(point, *, context=()):
    return CharacterSettingExtractionResult.model_validate({
        "time_context": list(context),
        "candidates": [{
            "candidate_kind": "SETTING", "source_chunk_id": NEXT_CHUNK, "entity_name": "서린", "attribute_name": "level",
            "attribute_value": "12", "value_type": "NUMBER", "value_json": {"value": 12},
            "evidence_spans": [{"quote": "12레벨"}],
            "narrative_time": {"fact_time": None, "reference_time": point},
        }],
    })


def test_time_grounding_recovers_exact_unicode_source_slice_and_global_offsets():
    result = _result({"description": "장마 첫날", "evidence_spans": [{"quote": "장마 첫날"}]})
    grounded = ground_extraction_narrative_time(
        result, chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="😀장마\n첫날에 12레벨", start_offset=70),
    )
    point = grounded.candidates[0].narrative_time.reference_time
    assert point.located()
    assert point.storage_data()["evidenceSpans"] == [{
        "quote": "장마\n첫날", "startOffset": 71, "endOffset": 76,
        "sourceChunkId": str(NEXT_CHUNK),
    }]
    assert result.candidates[0].narrative_time.reference_time.evidence_spans[0].quote == "장마 첫날"


def test_located_repeated_time_quote_uses_supplied_exact_occurrence():
    result = _result({"description": "둘째 그날", "evidence_spans": [
        {"quote": "그날", "start_offset": 84, "end_offset": 86}]})
    grounded = ground_extraction_narrative_time(
        result, chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="그날. 그날 12레벨", start_offset=80),
    )
    point = grounded.candidates[0].narrative_time.reference_time
    assert point.located()
    assert point.evidence_spans[0].start_offset == 84
    assert point.evidence_spans[0].end_offset == 86


def test_unlocated_repeated_time_quote_cannot_advance_or_be_carried_to_next_chunk():
    point = {"description": "그날", "evidence_spans": [{"quote": "그날"}]}
    grounded = ground_extraction_narrative_time(
        _result(point, context=[point]),
        chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="그날. 그날 12레벨", start_offset=80),
    )
    assert grounded.candidates[0].narrative_time.reference_time is None
    assert grounded.time_context == []


def test_previous_chunk_reference_resolves_original_point_without_relabeling_source():
    previous = NarrativeTimePoint.model_validate({
        "description": "장마 첫날", "evidenceSpans": [{
            "quote": "장마 첫날", "startOffset": 5, "endOffset": 10, "sourceChunkId": CHUNK,
        }],
    })
    result = _result({"description": "모델이 덧붙인 날짜", "evidence_spans": [], "context_ref": "T1"})
    grounded = ground_extraction_narrative_time(
        result, chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="치료 후 12레벨", start_offset=80),
        time_context=(previous,),
    )
    resolved = grounded.candidates[0].narrative_time.reference_time
    assert resolved.storage_data() == {
        "description": "장마 첫날", "evidenceSpans": [{
            "quote": "장마 첫날", "startOffset": 5, "endOffset": 10, "sourceChunkId": str(CHUNK),
        }],
    }
    assert resolved is not previous
    prompt = time_context_prompt_data((previous,))
    assert prompt[0]["ref"] == "T1"
    assert prompt[0]["evidenceSpans"][0]["sourceChunkRef"] == "T1"
    assert str(CHUNK) not in str(prompt)


def test_unknown_previous_chunk_reference_is_rejected():
    with pytest.raises(ValueError, match="NARRATIVE_TIME_CONTEXT_REF_INVALID"):
        ground_extraction_narrative_time(
            _result({"context_ref": "T2"}),
            chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="12레벨", start_offset=80),
            time_context=(),
        )


def test_time_context_without_candidate_is_grounded_without_creating_a_setting():
    result = CharacterSettingExtractionResult.model_validate({
        "candidates": [], "time_context": [{
            "description": "장마 첫날", "evidence_spans": [{"quote": "장마 첫날"}],
        }],
    })
    grounded = ground_extraction_narrative_time(
        result, chunk=SimpleNamespace(id=CHUNK, chunk_text="장마 첫날", start_offset=0),
    )
    assert grounded.candidates == []
    assert grounded.time_context[0].located()


def test_wrong_time_offsets_are_recovered_only_from_unique_original_quote():
    grounded = ground_extraction_narrative_time(
        _result({"description": "장마 첫날", "evidence_spans": [
            {"quote": "장마 첫날", "start_offset": 999, "end_offset": 1004}]}),
        chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="앞 장마 첫날 뒤", start_offset=80),
    )
    span = grounded.candidates[0].narrative_time.reference_time.evidence_spans[0]
    assert (span.start_offset, span.end_offset) == (82, 87)
    assert span.quote == "장마 첫날"


def test_time_quote_absent_from_original_is_not_kept_as_a_grounded_point():
    grounded = ground_extraction_narrative_time(
        _result({"description": "가을 첫날", "evidence_spans": [
            {"quote": "가을 첫날", "start_offset": 80, "end_offset": 85}]}),
        chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="장마 첫날 12레벨", start_offset=80),
    )
    assert grounded.candidates[0].narrative_time.reference_time is None


def test_candidate_storage_keeps_verified_time_separate_from_raw_provider_point():
    from app.analysis.schemas import CharacterSettingProviderResponse
    from app.mappers.setting_candidate_mapper import SettingCandidateMapper

    response = CharacterSettingProviderResponse.model_validate({
        "candidates": [{
            "candidate_kind": "SETTING", "entity_type": "CHARACTER", "entity_name": "서린",
            "raw_entity_mention": "서린", "attribute_name": "level", "attribute_value": "12",
            "value_type": "NUMBER", "value_json": {"value": 12, "extra_json": None},
            "confidence": 0.9, "evidence_spans": [{"quote": "12레벨", "start_offset": None, "end_offset": None}],
            "narrative_time": {"fact_time": None, "reference_time": {
                "description": "장마 첫날", "context_ref": None,
                "evidence_spans": [{"quote": "장마 첫날", "start_offset": 999, "end_offset": 1004}],
            }},
        }],
    })
    result = ground_extraction_narrative_time(
        response.to_extraction_result(NEXT_CHUNK),
        chunk=SimpleNamespace(id=NEXT_CHUNK, chunk_text="장마 첫날 12레벨", start_offset=80),
    )
    entity = SettingCandidateMapper.to_entity(
        work_id=UUID(int=10), episode_id=UUID(int=11), analysis_job_id=UUID(int=12),
        source_content_s3_key="immutable/test.txt", candidate=result.candidates[0],
    )
    assert entity.narrative_time["factTime"] is None
    assert entity.narrative_time["referenceTime"]["evidenceSpans"] == [{
        "quote": "장마 첫날", "startOffset": 80, "endOffset": 85, "sourceChunkId": str(NEXT_CHUNK),
    }]
    assert entity.raw_ai_result_json["narrative_time"]["reference_time"]["evidence_spans"][0]["start_offset"] == 999
    assert entity.value_json == {"value": 12}


def test_ambiguous_reference_point_is_null_without_losing_located_fact_point():
    result = _result({"description": "그날", "evidence_spans": [{"quote": "그날"}]})
    result.candidates[0].narrative_time.fact_time = NarrativeTimePoint.model_validate({
        "description": "208년 사고", "evidence_spans": [{"quote": "208년 사고"}],
    })
    grounded = ground_extraction_narrative_time(result, chunk=SimpleNamespace(
        id=NEXT_CHUNK, chunk_text="208년 사고. 그날. 그날 12레벨", start_offset=80,
    ))
    assert grounded.candidates[0].narrative_time.reference_time is None
    fact_point = grounded.candidates[0].narrative_time.fact_time
    assert fact_point.description == "208년 사고"
    assert fact_point.located()
    assert fact_point.evidence_spans[0].start_offset == 80
