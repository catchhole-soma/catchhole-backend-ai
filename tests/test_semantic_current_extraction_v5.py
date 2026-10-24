"""Real extractor/request boundaries with offline responses; not model semantics."""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.analysis import ordered_context
from app.analysis.exceptions import OrderedInputContextError
from app.analysis.semantic_current_context import (
    CURRENT_STATUS_FORECAST_INSTRUCTIONS,
    SEMANTIC_CURRENT_EXTRACTION_INSTRUCTIONS,
)
from app.analysis.setting_extractor import (
    SETTING_EXTRACTION_RESPONSE_SCHEMA,
    CharacterSettingExtractor,
)
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerAnalysisJobPayload, WorkerWorldSettingCandidatePayload
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_semantic_current_context_v5 import prepare
from tests.test_setting_book_source_contract import book_payload_data
from tests.test_setting_extractor import DEFAULT_SCHEMA_HINTS


class OfflineClient:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = iter(responses or ['{"candidates": []}'])

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text=next(self.responses))


@pytest.mark.parametrize("book", [False, True])
def test_world_gets_all_category_current_structure_before_extraction(book):
    client = OfflineClient()
    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    result = asyncio.run(WorldSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
        "동문 낮 통행세는 여전히 두 닢이다.",
        analysis_context=payload.analysis_context,
        semantic_current_context=prepare(),
        **({"setting_book": payload.setting_book} if book else {}),
    ))
    assert result.candidates == []
    request = client.requests[0]
    assert CURRENT_STATUS_FORECAST_INSTRUCTIONS not in request["system_prompt"]
    if book:
        assert SEMANTIC_CURRENT_EXTRACTION_INSTRUCTIONS in request["system_prompt"]
        assert '"category":"LOCATION"' in request["user_prompt"]
        assert "은항" in request["user_prompt"] and "통행세" in request["user_prompt"]
        assert "semantic_current_state" in request["user_prompt"]
        assert json.loads(request["user_prompt"])["chunk_text"] == "동문 낮 통행세는 여전히 두 닢이다."
    else:
        assert SEMANTIC_CURRENT_EXTRACTION_INSTRUCTIONS not in request["system_prompt"]
        assert "semantic_current_state" not in request["user_prompt"]
        assert "은항" not in request["user_prompt"]


def test_character_sees_concrete_current_axis_and_keeps_schema_priority_instructions():
    client = OfflineClient()
    asyncio.run(CharacterSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
        source_chunk_id=UUID(int=8), chunk_text="백솔은 동문 직책을 내려놓고 새 직책을 맡았다.",
        schema_hints=DEFAULT_SCHEMA_HINTS, semantic_current_context=prepare(),
    ))
    request = client.requests[0]
    assert SEMANTIC_CURRENT_EXTRACTION_INSTRUCTIONS not in request["system_prompt"]
    assert "semantic_current_state" not in request["user_prompt"]
    assert "profile.직책" not in request["user_prompt"]
    assert "동문 경비대장" not in request["user_prompt"]
    assert "캐릭터에게 귀속" in request["system_prompt"]
    assert CURRENT_STATUS_FORECAST_INSTRUCTIONS in request["system_prompt"]


@pytest.mark.parametrize("retry", [False, True])
def test_character_actual_request_checks_strict_schema_and_retry_feedback(monkeypatch, retry):
    client = OfflineClient(['{"candidates": [{}]}', '{"candidates": []}'])
    observed = []

    def reject_at_boundary(system_prompt, user_prompt, max_tokens=64000, response_schema=None):
        observed.append((user_prompt, response_schema))
        assert response_schema == SETTING_EXTRACTION_RESPONSE_SCHEMA
        if not retry or len(observed) == 2:
            raise OrderedInputContextError("ordered_required_context_exceeds_input_limit")
        return 1000

    monkeypatch.setattr(ordered_context, "ensure_ordered_prompt_fits", reject_at_boundary)
    with pytest.raises(OrderedInputContextError):
        asyncio.run(CharacterSettingExtractor(llm_client=client, max_attempts=2).extract_from_chunk(
            source_chunk_id=UUID(int=8), chunk_text="명확한 원문", schema_hints=DEFAULT_SCHEMA_HINTS,
            semantic_current_context=prepare(),
        ))
    assert len(client.requests) == (1 if retry else 0)
    if retry:
        assert "previous_response_correction" in observed[1][0]


def test_book_current_invariant_guidance_reaches_extraction_and_shared_comparison_context():
    # Wiring of guidance only: the actual C01 semantic verdict belongs to fresh model runs/Astra.
    client = OfflineClient()
    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    source = "[시설 / 은문]\n가동 인원: 두 명 — 관리자가 바뀌어도 유지\n"
    asyncio.run(WorldSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
        source, setting_book=payload.setting_book, analysis_context=payload.analysis_context,
    ))
    assert "현재값의 불변" in client.requests[0]["system_prompt"]
    assert "현재값의 불변" in ordered_context.CURRENT_CHRONOLOGY_INSTRUCTIONS
    assert "조건은 HYPOTHETICAL" not in ordered_context.SETTING_BOOK_INSTRUCTIONS


def test_actual_current_invariant_source_and_observed_failure_are_preserved_in_regression():
    fixture = json.loads((Path(__file__).parent / "fixtures/ai_v5_actual_current_invariant.json").read_text())
    source = fixture["sourceText"]
    assert hashlib.sha256(source.encode()).hexdigest() == fixture["sourceSha256"]
    observed = fixture["candidate"]
    assert observed["temporalScope"] == "PRESENT"
    assert observed["suggestedOperation"] == "REVIEW_REQUIRED"  # Historical FAIL remains intact.
    assert observed["source"]["contentHash"] == fixture["sourceSha256"]
    for span in observed["evidenceSpans"]:
        assert span["quote"] == source[span["startOffset"]:span["endOffset"]]
    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    client = OfflineClient()
    asyncio.run(WorldSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
        source, setting_book=payload.setting_book, analysis_context=payload.analysis_context,
    ))
    assert json.loads(client.requests[0]["user_prompt"])["chunk_text"] == source
    assert "현재값의 불변" in client.requests[0]["system_prompt"]


@pytest.mark.parametrize("scope,source_text", [
    ("PRESENT", "현재 통행료는 두 닢이며 관리자가 바뀌어도 이 요금은 유지된다."),
    ("HYPOTHETICAL", "관리자가 바뀌면 통행료는 다섯 닢으로 바뀔 예정이다."),
    ("UNKNOWN", "관리자가 바뀔 때 통행료가 달라진다는 말은 있으나 시작 시점은 미정이다."),
])
def test_invariant_guidance_keeps_existing_noncurrent_review_validation(scope, source_text):
    # Supplied provider scopes exercise existing validation, not linguistic model judgment.
    claim = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    item = WorkerWorldSettingCandidatePayload(
        candidate_id=UUID(int=30), work_id=claim.work_id, source=claim.frozen_source(),
        category="LOCATION", subject_name="은항", setting_name="통행료", extracted_value="두 닢",
        evidence_spans=[{"quote": source_text}], temporal_scope=scope,
        rawExtractionJson={"epistemic_status": "ASSERTED"},
    )
    concrete = {"operation": "ADD", "target_ref": None, "consolidation_status": "SINGLE",
                "proposed_scope_name": None, "proposed_setting_name": "통행료", "proposed_value": "두 닢",
                "comparison_reason": "원문의 통행료 항목을 기록합니다."}
    review = {**concrete, "operation": "REVIEW_REQUIRED", "review_reason": "GENERAL_UNCERTAINTY"}
    client = RecordingClient(*([concrete] if scope == "PRESENT" else [concrete, review]))
    result, _ = asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=2).compare(item, []))
    assert result.operation == ("ADD" if scope == "PRESENT" else "REVIEW_REQUIRED")
    assert len(client.requests) == (1 if scope == "PRESENT" else 2)
    assert "현재값의 불변" in client.requests[0]["system_prompt"]
