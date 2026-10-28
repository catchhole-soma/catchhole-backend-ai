"""BOOK character evidence is checked before publishing; normal function stays positive."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_projection import CharacterProjectionState
from app.analysis.exceptions import LlmExtractionError
from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.mappers.setting_candidate_mapper import SettingCandidateMapper
from app.models.episode_chunk import EpisodeChunk
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import WorkerAnalysisJobPayload
from tests.test_character_fact_batch import _candidate, _decision_payload
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import SOURCE, book_payload_data
from tests.test_setting_extractor import _valid_setting_payload, CHUNK_ID


HINTS = (CharacterSettingSchemaHint(schema_key="profile.hand_function", display_name="손의 기능",
    attribute_pattern=None, aliases=("손의 기능",), value_type="STRING", canonical_fact_type="PROFILE"),)
TEXT = "[인물 / 아린]\n손의 기능:  정상 — 재진에서 회복 확인\n"
QUOTE = "손의 기능:  정상 — 재진에서 회복 확인"


def response(quote=QUOTE, *, kind="SETTING", spans=None):
    data = _valid_setting_payload(provider_payload=True)
    data.update(entity_name="아린", raw_entity_mention="아린", attribute_name="profile.hand_function",
        attribute_value="정상", value_type="STRING", value_json={"value": "정상", "extra_json": None},
        evidence_spans=[{"start_offset": None, "end_offset": None, **span}
                        for span in (spans or [{"quote": quote}])])
    if kind == "CHARACTER_DISCOVERY":
        data.update(candidate_kind=kind, attribute_name=None, attribute_value=None, value_type=None, value_json=None)
    return {"candidates": [data]}


def extract(client, *, attempts=2, book=True):
    return asyncio.run(CharacterSettingExtractor(llm_client=client, model="test-model", max_attempts=attempts).extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text=TEXT, schema_hints=HINTS,
        **({"setting_book": WorkerAnalysisJobPayload.model_validate(book_payload_data()).setting_book} if book else {})))


@pytest.mark.parametrize("kind", ["SETTING", "CHARACTER_DISCOVERY"])
@pytest.mark.parametrize("bad", ["없는 인용문", QUOTE + " metadata: {}", " "])
def test_book_character_grounding_retries_entire_invalid_candidate_without_header_substitution(kind, bad):
    client = RecordingClient(response(kind=kind, spans=[{"quote": "[인물 / 아린]"}, {"quote": bad}]), response(kind=kind))
    result = extract(client)
    assert len(client.requests) == 2
    assert [span.quote for span in result.candidates[0].evidence_spans] == [QUOTE]
    correction = client.requests[1]["user_prompt"]
    assert '"reasonCode":"BOOK_EVIDENCE_NOT_GROUNDED"' in correction
    assert bad not in correction or bad in TEXT


def test_character_book_grounding_exhaustion_is_failure_and_episode_behavior_stays_compatible():
    bad = response("없는 인용문")
    with pytest.raises(LlmExtractionError):
        extract(RecordingClient(bad), attempts=1)
    episode = extract(RecordingClient(bad), attempts=1, book=False)
    assert episode.candidates[0].evidence_spans[0].quote == "없는 인용문"


@pytest.mark.parametrize("quote", [
    '손의 기능: "정상" — 재진에서 회복 확인',
    '손의 기능: “정상 — 재진에서 회복 확인',
], ids=["extra-quotes", "unpaired-quote"])
def test_book_character_quote_recovery_publishes_exact_source_without_changing_provider_payload(quote):
    raw = response(quote)
    original = json.loads(json.dumps(raw))
    client = RecordingClient(raw)
    result = extract(client, attempts=1)
    resolved = resolve_candidate_evidence_offsets(result.candidates, TEXT, 29)[0]
    source = WorkerAnalysisSource.model_validate(SOURCE)
    published = SettingCandidateMapper.to_entity(work_id=uuid4(), episode_id=None, source_content_s3_key=None,
        analysis_job_id=uuid4(), candidate=resolved, source_kind=source.source_kind,
        setting_book_id=source.setting_book_id, setting_book_revision_id=source.revision_id)

    assert len(client.requests) == 1
    span = published.evidence_spans[0]
    assert span["quote"] == QUOTE
    assert span["start_offset"] == 29 + TEXT.index(QUOTE)
    assert span["end_offset"] == span["start_offset"] + len(QUOTE)
    assert TEXT[span["start_offset"] - 29:span["end_offset"] - 29] == span["quote"]
    assert raw == original


def test_positive_book_normal_function_survives_extraction_publish_comparison_and_empty_projection():
    result = extract(RecordingClient(response("손의 기능: 정상 — 재진에서 회복 확인")), attempts=1)
    chunk = EpisodeChunk(id=CHUNK_ID, episode_id=uuid4(), chunk_index=0, chunk_text=TEXT,
                         start_offset=29, end_offset=29 + len(TEXT))
    source = WorkerAnalysisSource.model_validate(SOURCE)
    resolved = resolve_candidate_evidence_offsets(result.candidates, TEXT, chunk.start_offset)[0]
    published = SettingCandidateMapper.to_entity(work_id=uuid4(), episode_id=None, source_content_s3_key=None,
        analysis_job_id=uuid4(), candidate=resolved, source_kind=source.source_kind,
        setting_book_id=source.setting_book_id, setting_book_revision_id=source.revision_id)
    assert published.value_json == {"value": "정상"}
    span = published.evidence_spans[0]
    assert span["quote"] == QUOTE
    assert TEXT[span["start_offset"] - 29:span["end_offset"] - 29] == QUOTE
    item = _candidate("C1", "Q1", "정상").model_copy(update={
        "source": source, "source_episode_no": None, "raw_fact_key": published.attribute_name,
        "initial_canonical_fact_key": published.attribute_name,
        "value_type": "STRING", "value_json": published.value_json, "evidence_spans": resolved.evidence_spans})
    client = RecordingClient({"decisions": [{**_decision_payload("C1", operation="ADD",
        resolved_key=published.attribute_name, value="정상"), "proposed_value_json": {"value": "정상"}}]})
    compared, _ = asyncio.run(CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1).compare_batch(
        matched_character_name="아린", canonical_fact_type="PROFILE", candidates=[item], snapshot_entries=[], ordered_context=True))
    state = CharacterProjectionState([])
    applied = state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="PROFILE",
        resolved_fact_key=published.attribute_name, value_type="STRING", candidate_value_json=published.value_json,
        source=source, decision=compared.decisions[0])
    assert applied.projected_entry.fact_value == "정상" and len(state.entries) == 1
