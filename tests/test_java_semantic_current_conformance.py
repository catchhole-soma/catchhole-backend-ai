"""Consume frozen actual MVC responses; never contact Java, DB or a provider.

Supply GH221_JAVA_SEMANTIC_CURRENT_WIRE for release verification. A missing
export is explicitly skipped, and is not evidence that the wire contract passed.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from app.analysis.semantic_current_context import prepare_semantic_current_context
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.domain.enums import WorldSettingCategory
from app.llm.responses import LlmTextResponse
from app.mappers.setting_candidate_mapper import SettingCandidateMapper
from app.schemas.worker import WorkerAnalysisJobPayload
from evals.multi_stage_setting.ordered_journal import journal_state_hash, load_journal_json

EXPORT_PATH = os.environ.get("GH221_JAVA_SEMANTIC_CURRENT_WIRE")
pytestmark = pytest.mark.skipif(
    not EXPORT_PATH, reason="Actual Java semantic CURRENT wire export is not supplied.",
)


@pytest.fixture(scope="module")
def actual_wire():
    return load_journal_json(Path(EXPORT_PATH).read_text(encoding="utf-8"))


def prepared(claim):
    return prepare_semantic_current_context(
        claim.semantic_current_context,
        analysis_context=claim.analysis_context,
        known_characters=claim.known_characters,
        provisional_characters=claim.provisional_characters,
        source_hash=claim.frozen_source().content_hash,
    )


@pytest.mark.parametrize("key", ["bookClaim", "episodeClaim", "emptyClaim", "resumedBookClaim"])
def test_actual_claim_full_parse_preserves_carrier_and_advisory_serialization(actual_wire, key):
    raw = actual_wire[key]
    claim = WorkerAnalysisJobPayload.model_validate(raw)
    assert claim.semantic_current_context.model_dump(mode="json", by_alias=True) == raw["semanticCurrentContext"]
    assert "semanticCurrentContext" not in claim.analysis_context.model_dump(by_alias=True)
    advisory = prepared(claim)
    chars = json.loads(advisory.character_prompt_json())["characters"]
    worlds = json.loads(advisory.world_prompt_json())["world_subjects"]
    assert len(chars) == len(raw["semanticCurrentContext"]["characters"])
    assert len(worlds) == len(raw["semanticCurrentContext"]["worldSubjects"])
    internal_ids = [raw["semanticCurrentContext"]["runId"]]
    internal_ids += [owner["characterId"] or owner["provisionalSubjectKey"]
                     for owner in raw["semanticCurrentContext"]["characters"]]
    internal_ids += [owner["subjectId"] or owner["provisionalSubjectKey"]
                     for owner in raw["semanticCurrentContext"]["worldSubjects"]]
    prompt = advisory.character_prompt_json() + advisory.world_prompt_json()
    assert all(identifier not in prompt for identifier in internal_ids)
    assert raw["semanticCurrentContext"]["inputStateHash"] not in prompt
    assert raw["semanticCurrentContext"]["sourceHash"] not in prompt
    assert "provenance" not in prompt and "sourceCandidates" not in prompt


def test_actual_current_all_types_values_and_world_paths_match_full_frozen_input(actual_wire):
    raw = actual_wire["bookClaim"]
    carrier = raw["semanticCurrentContext"]
    frozen = actual_wire["originalInputState"]
    assert journal_state_hash(frozen) == carrier["inputStateHash"]
    character_types = set()
    nullable_items = []
    for owner in carrier["characters"]:
        ref = f'character:{owner["characterId"]}' if owner["characterId"] else owner["provisionalSubjectKey"]
        source_owner = frozen["characters"][ref]
        assert owner["characterId"] == source_owner["actualCharacterId"]
        assert owner["provisionalSubjectKey"] == source_owner["provisionalSubjectKey"]
        assert {f'{slot["factType"]}:{slot["factKey"]}': slot
                for slot in owner["currentSlots"]} == source_owner["slots"]
        character_types.update(slot["factType"] for slot in owner["currentSlots"])
        nullable_items += [slot for slot in owner["currentSlots"]
                           if slot["factType"] == "ITEM" and slot["factValue"] is None]
    assert character_types == {"PROFILE", "AGE", "LEVEL", "STAT", "SKILL", "ITEM", "STATUS", "TIME"}
    assert nullable_items and all(slot["valueJson"] is None for slot in nullable_items)
    assert len(carrier["characters"]) == len(frozen["characters"])
    assert len(carrier["worldSubjects"]) == len(frozen["worldSettings"])
    assert {owner["category"] for owner in carrier["worldSubjects"]} == {category.value for category in WorldSettingCategory}
    for owner in carrier["worldSubjects"]:
        ref = f'world:{owner["subjectId"]}' if owner["subjectId"] else owner["provisionalSubjectKey"]
        source_owner = frozen["worldSettings"][ref]
        assert owner["category"] == source_owner["category"]
        assert owner["subjectName"] == source_owner["subjectName"]
        expected = {}
        for name, value in source_owner["propertiesJson"].items():
            if isinstance(value, dict):
                expected.update({(name, leaf): text for leaf, text in value.items()})
            else:
                expected[(None, name)] = value
        assert {(prop["scopeName"], prop["settingName"]): prop["value"]
                for prop in owner["properties"]} == expected


def test_actual_empty_is_complete_and_resume_keeps_input_with_fresh_lease(actual_wire):
    empty = WorkerAnalysisJobPayload.model_validate(actual_wire["emptyClaim"])
    assert empty.semantic_current_context.complete is True
    assert json.loads(prepared(empty).character_prompt_json()) == {"characters": []}
    assert json.loads(prepared(empty).world_prompt_json()) == {"world_subjects": []}
    before = WorkerAnalysisJobPayload.model_validate(actual_wire["bookClaim"])
    resumed = WorkerAnalysisJobPayload.model_validate(actual_wire["resumedBookClaim"])
    assert before.semantic_current_context == resumed.semantic_current_context
    assert before.lease_token != resumed.lease_token
    assert resumed.checkpoint_stage == "CHUNKS_READY"
    episode = WorkerAnalysisJobPayload.model_validate(actual_wire["episodeClaim"])
    assert episode.episode.episode_no == 3
    assert all(person.source_episode_no < episode.episode.episode_no for person in episode.provisional_characters)


@pytest.mark.parametrize("key", [
    "profile.직책", "profile.왼발_감각", "profile.작업_담당", "profile.키",  # Concrete PATTERN axes.
    "profile.occupation", "profile.직업",  # Exact/alias remain authoritative downstream.
])
def test_actual_claim_schemas_allow_parser_to_keep_concrete_keys_without_current_rewrite(actual_wire, key):
    # Offline supplied provider choice, not a claim that a model chose the correct axis.
    claim = WorkerAnalysisJobPayload.model_validate(actual_wire["episodeClaim"])
    hints = tuple(CharacterSettingSchemaHint(
        schema_key=schema.schema_key, display_name=schema.display_name,
        attribute_pattern=schema.attribute_pattern, aliases=tuple(schema.aliases),
        value_type=schema.value_type,
    ) for schema in claim.character_setting_schemas)
    occupation = next(hint for hint in hints if hint.schema_key == "profile.occupation")
    assert "직책" not in occupation.aliases
    assert "직업" in occupation.aliases
    assert any(hint.attribute_pattern == "profile.*" and hint.value_type == "STRING" for hint in hints)
    source = "청류의 현재 담당 업무와 왼발 감각이 확인되었다."
    candidate = {
        "candidate_kind": "SETTING", "entity_type": "CHARACTER", "entity_name": "청류",
        "raw_entity_mention": "청류", "attribute_name": key, "attribute_value": "확인한 현재값",
        "value_type": "STRING", "value_json": {"value": "확인한 현재값", "extra_json": None},
        "evidence_spans": [{"quote": source, "start_offset": None, "end_offset": None}],
        "confidence": 0.9,
    }

    class OfflineClient:
        async def create_text_response(self, **kwargs):
            return LlmTextResponse(text=json.dumps({"candidates": [candidate]}, ensure_ascii=False))

    # The actual carrier's profile.키 cannot force supplied exact/alias keys into it.
    context = prepared(claim)
    result = asyncio.run(CharacterSettingExtractor(llm_client=OfflineClient(), max_attempts=1).extract_from_chunk(
        source_chunk_id=UUID(int=17), chunk_text=source, schema_hints=hints,
        semantic_current_context=context,
    ))
    parsed = result.candidates[0]
    assert parsed.attribute_name == key
    assert parsed.value_json == {"value": "확인한 현재값"}
    entity = SettingCandidateMapper.to_entity(
        work_id=claim.work_id, episode_id=claim.episode.episode_id,
        source_content_s3_key=claim.episode.content_s3_key,
        analysis_job_id=claim.analysis_job_id, candidate=parsed,
    )
    assert entity.attribute_name == key
    assert entity.raw_ai_result_json["attribute_name"] == key
