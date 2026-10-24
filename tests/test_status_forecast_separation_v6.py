"""Offline protocol/projection checks; supplied responses do not prove model semantics."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

import app.analysis.character_fact_comparator as comparator_module
import app.analysis.semantic_current_context as context_module
from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
)
from app.analysis.character_name_resolver import CharacterNameMatch
from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.ordered_character_subjects import OrderedCandidateBinding
from app.analysis.schemas import CharacterSettingProviderResponse
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.domain.enums import SettingCandidateMatchStatus
from app.llm.responses import LlmTextResponse
from app.mappers.setting_candidate_mapper import SettingCandidateMapper
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.worker import WorkerCharacterFactComparisonBatchCandidate
from app.services.setting_candidate_service import (
    SettingCandidateSaveItem,
    _prepare_ordered_setting_candidates,
)
from tests.test_semantic_current_context_v5 import prepare

CHUNK_ID, WORK_ID, EPISODE_ID, JOB_ID, CHARACTER_ID = (UUID(int=i) for i in range(1, 6))
STATUS_HINT = CharacterSettingSchemaHint("statuses.condition", "상태", "status.*", (), "JSON")
CACHE_SUFFIX = ":status-forecast-separation-v1"
SOURCE = "백솔은 왼발목 통증 때문에 걷지 못했다. 사흘 뒤 통증이 줄어들 수도 있으나 경과를 봐야 한다."
CURRENT_QUOTE = SOURCE.split(". ", 1)[0] + "."
FORECAST_QUOTE = SOURCE.split(". ", 1)[1]
CURRENT_VALUE = {"name": "왼발목 통증", "active": True, "description": "통증 때문에 걷지 못함"}
FORECAST_VALUE = {"name": "왼발목 통증", "description": "사흘 뒤 통증이 줄어들 수도 있으나 아직 경과를 봐야 하는 전망"}


class OfflineClient:
    def __init__(self, *payloads):
        self.payloads = iter(payloads)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text=json.dumps(next(self.payloads), ensure_ascii=False))


def provider_candidate(value, quote, key="status.왼발목_통증"):
    return {
        "candidate_kind": "SETTING", "entity_type": "CHARACTER", "entity_name": "백솔",
        "raw_entity_mention": "백솔", "attribute_name": key,
        "attribute_value": value["description"], "value_type": "JSON",
        "value_json": {"extra_json": json.dumps(value, ensure_ascii=False)},
        "evidence_spans": [{"quote": quote, "start_offset": None, "end_offset": None}],
        "confidence": 0.9,
    }


def split_entities(*, forecast_first=False, shared_quote=False):
    quote1, quote2 = (SOURCE, SOURCE) if shared_quote else (CURRENT_QUOTE, FORECAST_QUOTE)
    payloads = [provider_candidate(CURRENT_VALUE, quote1), provider_candidate(FORECAST_VALUE, quote2)]
    if forecast_first:
        payloads.reverse()
    parsed = CharacterSettingProviderResponse.model_validate({"candidates": payloads}).to_extraction_result(CHUNK_ID)
    located = resolve_candidate_evidence_offsets(parsed.candidates, SOURCE, 0)
    items = [SettingCandidateSaveItem(
        episode_id=EPISODE_ID, source_content_s3_key="frozen/episode.txt", candidate=candidate,
        ordered_binding=OrderedCandidateBinding(UUID(int=10+i), actual_character_id=CHARACTER_ID,
                                                match_status=SettingCandidateMatchStatus.MATCHED),
    ) for i, candidate in enumerate(located)]
    prepared = _prepare_ordered_setting_candidates(items)
    return [SettingCandidateMapper.to_entity(
        WORK_ID, EPISODE_ID, "frozen/episode.txt", JOB_ID, row.candidate,
        CharacterNameMatch(CHARACTER_ID, SettingCandidateMatchStatus.MATCHED),
    ) for row in prepared]


def batch_candidate(entity, index):
    return WorkerCharacterFactComparisonBatchCandidate(
        candidate_ref=f"C{index}", projected_snapshot_ref=f"Q{index}", source_episode_no=1,
        attribute_value=entity.attribute_value, value_json=entity.value_json, value_type="JSON",
        evidence_spans=entity.evidence_spans, raw_fact_key=entity.attribute_name,
        initial_canonical_fact_key=entity.attribute_name, canonical_key_resolution="PATTERN",
    )


def decision(candidate):
    forecast = "active" not in candidate.value_json
    return {
        "candidate_ref": candidate.candidate_ref,
        "resolved_canonical_fact_key": candidate.initial_canonical_fact_key,
        "operation": "REVIEW_REQUIRED" if forecast else "ADD", "target_ref": None,
        "removed_snapshot_refs": [],
        "proposed_fact_value": None if forecast else candidate.attribute_value,
        "proposed_value_json": None if forecast else candidate.value_json,
        "temporal_scope": "HYPOTHETICAL" if forecast else "PRESENT",
        "comparison_reason": "실현되지 않은 전망을 검토합니다." if forecast else "현재 통증 상태를 기록합니다.",
    }


def review_instructions():
    value = getattr(context_module, "STATUS_FORECAST_REVIEW_INSTRUCTIONS", "")
    assert value, "ORDERED STATUS needs explicit independent forecast review guidance."
    return value


def test_current_and_forecast_have_explicit_separate_extraction_contract():
    client = OfflineClient({"candidates": []})
    asyncio.run(CharacterSettingExtractor(llm_client=client).extract_from_chunk(
        CHUNK_ID, SOURCE, schema_hints=(STATUS_HINT,), semantic_current_context=prepare(),
    ))
    request = client.requests[0]
    assert "별도 SETTING 후보" in request["system_prompt"]
    assert "현재 후보" in request["system_prompt"] and "active=false" in request["system_prompt"]
    assert request["prompt_cache_key"].endswith(":episode-lean-pending-v1")


@pytest.mark.parametrize("ordered,fact_type", [(True, "STATUS"), (False, "STATUS"), (True, "PROFILE")])
def test_forecast_guidance_reaches_only_ordered_status_fit_and_request(monkeypatch, ordered, fact_type):
    entity = split_entities()[0]
    candidate = batch_candidate(entity, 1)
    item = decision(candidate)
    if fact_type == "PROFILE":
        candidate = candidate.model_copy(update={"initial_canonical_fact_key": "profile.기능", "raw_fact_key": "profile.기능", "value_type": "STRING", "value_json": {"value": "통증으로 걷지 못함"}})
        item.update(resolved_canonical_fact_key="profile.기능", proposed_value_json={"value": "통증으로 걷지 못함"})
    client = OfflineClient({"decisions": [item]})
    comparator = CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1)
    estimated_prompts = []
    original = comparator_module._estimate_prompt_tokens

    def observe(system, user, model):
        estimated_prompts.append(system)
        return original(system, user, model)

    monkeypatch.setattr(comparator_module, "_estimate_prompt_tokens", observe)
    args = {"matched_character_name": "백솔", "canonical_fact_type": fact_type, "candidates": [candidate],
            "snapshot_entries": [], "ordered_context": ordered}
    assert asyncio.run(comparator.batch_fits(**args))
    asyncio.run(comparator.compare_batch(**args))
    guidance = review_instructions()
    assert all((guidance in prompt) == (ordered and fact_type == "STATUS") for prompt in estimated_prompts)
    assert (guidance in client.requests[0]["system_prompt"]) == (ordered and fact_type == "STATUS")
    assert client.requests[0]["prompt_cache_key"].endswith(CACHE_SUFFIX) == (ordered and fact_type == "STATUS")


@pytest.mark.parametrize("forecast_first", [False, True])
@pytest.mark.parametrize("shared_quote", [False, True])
def test_parser_mapper_ordered_dedupe_batch_projection_preserve_two_claims(forecast_first, shared_quote):
    entities = split_entities(forecast_first=forecast_first, shared_quote=shared_quote)
    assert len(entities) == 2 and len({e.attribute_name for e in entities}) == 1
    assert entities[0].value_json != entities[1].value_json
    candidates = [batch_candidate(entity, i) for i, entity in enumerate(entities, 1)]
    client = OfflineClient({"decisions": [decision(c) for c in candidates]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=client, model="test-model").compare_batch(
        matched_character_name="백솔", canonical_fact_type="STATUS", candidates=candidates,
        snapshot_entries=[], ordered_context=True,
    ))
    assert review_instructions() in client.requests[0]["system_prompt"]
    state = CharacterProjectionState([])
    for candidate, outcome in zip(candidates, result.decisions, strict=True):
        before = list(state.entries)
        applied = state.apply(candidate_ref=candidate.candidate_ref, projected_snapshot_ref=candidate.projected_snapshot_ref,
                              fact_type="STATUS", resolved_fact_key=outcome.resolved_canonical_fact_key,
                              value_type="JSON", candidate_value_json=candidate.value_json, decision=outcome)
        if outcome.operation == "REVIEW_REQUIRED":
            assert state.entries == before and applied.projected_entry is None
            assert applied.dependency_candidate_refs == () and not outcome.removed_snapshot_refs
            assert outcome.proposed_value_json is None and outcome.target_ref is None
            assert candidate.value_json == FORECAST_VALUE
    assert len(state.entries) == 1 and state.entries[0].value_json == CURRENT_VALUE
    for entity in entities:
        assert entity.raw_ai_result_json["value_json"] == entity.value_json
        for span in entity.evidence_spans:
            assert span["quote"] == SOURCE[span["start_offset"]:span["end_offset"]]


@pytest.mark.parametrize("forecast_first", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
def test_retry_and_partial_recovery_cover_both_claims_once(forecast_first, recovery):
    candidates = [batch_candidate(e, i) for i, e in enumerate(split_entities(forecast_first=forecast_first), 1)]
    good = [decision(c) for c in candidates]
    bad = copy.deepcopy(good)
    forecast_index = next(i for i, d in enumerate(bad) if d["operation"] == "REVIEW_REQUIRED")
    bad[forecast_index].update(operation="ADD", proposed_fact_value="실현되지 않은 전망", proposed_value_json=FORECAST_VALUE)
    responses = [{"decisions": bad}]
    if recovery:
        responses.extend({"decisions": [d]} for d in (good if forecast_first else good[1:]))
    else:
        responses.append({"decisions": good})
    client = OfflineClient(*responses)
    comparator = CharacterFactComparator(llm_client=client, model="test-model", max_attempts=1 if recovery else 2)
    result = asyncio.run(execute_character_fact_comparison_batch(
        comparator, matched_character_name="백솔", canonical_fact_type="STATUS", candidates=candidates,
        snapshot_entries=[], ordered_context=True,
    ))
    assert not result.failures and [d.candidate_ref for d in result.decisions] == ["C1", "C2"]
    assert len(client.requests) == (3 if recovery and forecast_first else 2)
    assert all(review_instructions() in request["system_prompt"] and request["prompt_cache_key"].endswith(CACHE_SUFFIX)
               for request in client.requests)
    future = result.decisions[forecast_index]
    assert future.operation == "REVIEW_REQUIRED" and future.temporal_scope == "HYPOTHETICAL"
    assert future.proposed_value_json is None and future.removed_snapshot_refs == []
    assert future.dependency_candidate_refs == []


def test_legacy_extraction_and_existing_json_properties_are_preserved():
    value = {**CURRENT_VALUE, "prognosis": "의사가 현재 통증은 안정적이라고 평가함"}
    client = OfflineClient({"candidates": [provider_candidate(value, CURRENT_QUOTE)]})
    result = asyncio.run(CharacterSettingExtractor(llm_client=client).extract_from_chunk(CHUNK_ID, SOURCE, schema_hints=(STATUS_HINT,)))
    assert result.candidates[0].value_json == value
    assert context_module.CURRENT_STATUS_FORECAST_INSTRUCTIONS in client.requests[0]["system_prompt"]
    assert CACHE_SUFFIX not in client.requests[0]["prompt_cache_key"]


@pytest.mark.parametrize("forecast_only", [False, True])
def test_single_current_or_forecast_preserves_existing_projection_permissions(forecast_only):
    candidate = batch_candidate(split_entities()[int(forecast_only)], 1)
    client = OfflineClient({"decisions": [decision(candidate)]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=client, model="test-model").compare_batch(
        matched_character_name="백솔", canonical_fact_type="STATUS", candidates=[candidate],
        snapshot_entries=[], ordered_context=True,
    ))
    state = CharacterProjectionState([])
    outcome = result.decisions[0]
    state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="STATUS",
                resolved_fact_key=outcome.resolved_canonical_fact_key, value_type="JSON",
                candidate_value_json=candidate.value_json, decision=outcome)
    assert len(state.entries) == (0 if forecast_only else 1)
    assert review_instructions() in client.requests[0]["system_prompt"]


def test_realized_current_recovery_can_still_end_the_related_status():
    initial = CharacterProjectionEntry("P1", "STATUS", "status.왼발목_통증", CURRENT_VALUE["description"],
                                       CURRENT_VALUE, provenance=AnalysisStateProvenance(confirmation_status="CONFIRMED", source_episode_no=1))
    recovered = {"name": "왼발목 통증", "active": False, "description": "통증이 없어지고 혼자 걷는다"}
    candidate = batch_candidate(split_entities()[0], 1).model_copy(update={"value_json": recovered, "source_episode_no": 2})
    outcome = {**decision(candidate), "operation": "REMOVE", "removed_snapshot_refs": ["P1"],
               "proposed_fact_value": None, "proposed_value_json": None, "temporal_scope": "PRESENT",
               "comparison_reason": "통증이 사라지고 보행 기능이 회복되어 이전 통증이 끝났습니다."}
    client = OfflineClient({"decisions": [outcome]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=client, model="test-model").compare_batch(
        matched_character_name="백솔", canonical_fact_type="STATUS", candidates=[candidate],
        snapshot_entries=[initial], ordered_context=True,
    ))
    state = CharacterProjectionState([initial])
    state.apply(candidate_ref="C1", projected_snapshot_ref="Q1", fact_type="STATUS",
                resolved_fact_key="status.왼발목_통증", value_type="JSON",
                candidate_value_json=recovered, decision=result.decisions[0])
    assert state.entries == []
    assert review_instructions() in client.requests[0]["system_prompt"]


def test_different_body_part_and_independent_state_remain_separate():
    right_quote = "백솔의 오른팔에 새로운 화상이 생겼다."
    source = CURRENT_QUOTE + " " + right_quote
    right_value = {"name": "오른팔 화상", "active": True, "description": "오른팔에 새로운 화상"}
    parsed = CharacterSettingProviderResponse.model_validate({"candidates": [
        provider_candidate(CURRENT_VALUE, CURRENT_QUOTE),
        provider_candidate(right_value, right_quote, "status.오른팔_화상"),
    ]}).to_extraction_result(CHUNK_ID)
    located = resolve_candidate_evidence_offsets(parsed.candidates, source, 0)
    prepared = _prepare_ordered_setting_candidates([SettingCandidateSaveItem(
        EPISODE_ID, "frozen/episode.txt", candidate,
        OrderedCandidateBinding(UUID(int=20+i), actual_character_id=CHARACTER_ID, match_status=SettingCandidateMatchStatus.MATCHED),
    ) for i, candidate in enumerate(located)])
    assert {row.candidate.attribute_name for row in prepared} == {"status.왼발목_통증", "status.오른팔_화상"}
    assert len(prepared) == 2


def test_actual_observed_failure_quote_and_current_property_are_immutable():
    fixture = json.loads((Path(__file__).parent / "fixtures/ai_v6_actual_status_forecast.json").read_text())
    source = fixture["sourceText"]
    assert hashlib.sha256(source.encode()).hexdigest() == fixture["sourceSha256"]
    candidate = fixture["observedPublicCandidate"]
    assert candidate["temporalScope"] == "PRESENT" and candidate["reviewStatus"] == "CONFIRMED"
    assert candidate["suggestedOperation"] == "ADD" and "prognosis" in candidate["valueJson"]
    assert any(p["key"] == "prognosis" for p in fixture["observedPublicCurrentStatus"]["properties"])
    for span in candidate["evidenceSpans"]:
        assert span["quote"] == source[span["start_offset"]:span["end_offset"]]


@pytest.mark.parametrize("forecast_first", [False, True])
def test_authored_split_of_actual_quotes_reaches_review_without_current_pollution(forecast_first):
    """Authored offline responses prove wiring, not the original provider's behavior."""
    fixture = json.loads((Path(__file__).parent / "fixtures/ai_v6_actual_status_forecast.json").read_text())
    observed = fixture["observedPublicCandidate"]
    source = fixture["sourceText"]
    current = {key: observed["valueJson"][key] for key in ("name", "active", "description")}
    forecast = {"name": current["name"], "description": observed["valueJson"]["prognosis"]}
    payloads = [provider_candidate(value, observed["evidenceSpans"][i]["quote"], observed["attributeName"])
                for i, value in enumerate((current, forecast))]
    for payload in payloads:
        payload.update(entity_name=observed["entityName"], raw_entity_mention=observed["rawEntityMention"])
    if forecast_first:
        payloads.reverse()
    parsed = CharacterSettingProviderResponse.model_validate({"candidates": payloads}).to_extraction_result(CHUNK_ID)
    located = resolve_candidate_evidence_offsets(parsed.candidates, source, 0)
    prepared = _prepare_ordered_setting_candidates([SettingCandidateSaveItem(
        EPISODE_ID, "frozen/episode.txt", candidate,
        OrderedCandidateBinding(UUID(int=30+i), actual_character_id=CHARACTER_ID,
                                match_status=SettingCandidateMatchStatus.MATCHED),
    ) for i, candidate in enumerate(located)])
    entities = [SettingCandidateMapper.to_entity(
        WORK_ID, EPISODE_ID, "frozen/episode.txt", JOB_ID, row.candidate,
        CharacterNameMatch(CHARACTER_ID, SettingCandidateMatchStatus.MATCHED),
    ) for row in prepared]
    assert len(entities) == 2 and len({entity.attribute_name for entity in entities}) == 1
    candidates = [batch_candidate(entity, i) for i, entity in enumerate(entities, 1)]
    client = OfflineClient({"decisions": [decision(candidate) for candidate in candidates]})
    result, _ = asyncio.run(CharacterFactComparator(llm_client=client, model="test-model").compare_batch(
        matched_character_name=observed["entityName"], canonical_fact_type="STATUS", candidates=candidates,
        snapshot_entries=[], ordered_context=True,
    ))
    state = CharacterProjectionState([])
    for candidate, outcome in zip(candidates, result.decisions, strict=True):
        before = list(state.entries)
        applied = state.apply(candidate_ref=candidate.candidate_ref, projected_snapshot_ref=candidate.projected_snapshot_ref,
                              fact_type="STATUS", resolved_fact_key=outcome.resolved_canonical_fact_key,
                              value_type="JSON", candidate_value_json=candidate.value_json, decision=outcome)
        span = candidate.evidence_spans[0]
        assert span.quote == source[span.start_offset:span.end_offset]
        if outcome.temporal_scope == "HYPOTHETICAL":
            assert state.entries == before and applied.dependency_candidate_refs == ()
            assert outcome.operation == "REVIEW_REQUIRED" and outcome.proposed_value_json is None
    assert len(state.entries) == 1 and state.entries[0].value_json == current
    assert "prognosis" in observed["valueJson"]  # The immutable observed failure remains unchanged.
