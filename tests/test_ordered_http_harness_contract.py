"""Keep the offline HTTP provider fixture valid against the real ordered response schema."""

import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.ordered_world_batch_contract import (
    OrderedWorldBatchValidationError,
    OrderedWorldPropertyBatchResult,
)
from app.analysis.ordered_world_diagnostics import restore_selected_properties
from app.analysis.world_setting_comparator import (
    ComparisonTargetReference,
    _validate_batch_comparison_result,
)
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)
from tests.ordered_analysis_http_harness import (
    AutomaticIsolationProvider,
    CharacterPartialRecoveryProvider,
    DeterministicProvider,
    GeneralUncertaintyProvider,
    ISOLATED_WORLD_VALUES,
    PreparationFailureProvider,
    source_text,
)


def test_general_uncertainty_harness_preserves_original_identity_beside_a_valid_decision():
    from app.analysis.world_setting_comparator import WorldSettingComparator
    from app.analysis.world_setting_schemas import WorldSettingExtractionResult

    provider = GeneralUncertaintyProvider()
    provider.episode_no = 1
    provider.dual_world_sources = True
    extracted = WorldSettingExtractionResult.model_validate_json(asyncio.run(provider.create_text_response(
        prompt_cache_key="world-setting-extraction:test", user_prompt="{}",
    )).text).candidates
    assert len(extracted) == 3
    assert all(span.quote in source_text(1, True) for row in extracted for span in row.evidence_spans)
    sources = [WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=f"C{index}", candidate_id=UUID(int=index), subject_name=row.subject_name,
        scope_name=row.scope_name, setting_name=row.setting_name, extracted_value=row.extracted_value,
        evidence_spans=[{"quote": span.quote} for span in row.evidence_spans],
    ) for index, row in enumerate(extracted, 1)]
    target = WorkerWorldSettingComparisonTarget(
        provisional_subject_key=f"provisional-world:{UUID(int=8)}", subject_name="백탑", version=0, properties=[],
    )
    result, _ = asyncio.run(WorldSettingComparator(llm_client=provider, max_attempts=1).compare_batch(
        "LOCATION", sources, [target], ordered_context=True,
    ))
    assert [row.operation for row in result.decisions] == ["ADD", "REVIEW_REQUIRED"]
    assert result.decisions[1].review_reason == "GENERAL_UNCERTAINTY"
    assert result.decisions[1].source_candidate_refs == ["C3"]
    assert sources[2].subject_name == "미궁" and result.decisions[1].proposed_value == "복잡하다."


def test_preparation_failure_harness_uses_two_real_chunks_and_only_fails_unlinked_subjects():
    from app.analysis.schemas import CharacterSettingProviderResponse
    from app.analysis.world_setting_schemas import WorldSettingExtractionResult
    from app.chunking.chunk_splitter import split_into_chunks

    chunks = split_into_chunks(source_text(1, True, preparation_failure=True))
    assert len(chunks) == 2 and chunks[1].chunk_text == "고요" * 3500
    provider = PreparationFailureProvider()
    provider.episode_no = 1
    provider.dual_world_sources = True
    for chunk in chunks:
        for cache, schema in [("setting-extraction:test", CharacterSettingProviderResponse),
                              ("world-setting-extraction:test", WorldSettingExtractionResult)]:
            response = asyncio.run(provider.create_text_response(
                prompt_cache_key=cache, user_prompt="chunk_text:\n" + chunk.chunk_text,
            ))
            parsed = schema.model_validate_json(response.text)
            if chunk.chunk_index == 1:
                assert parsed.candidates == []
                continue
            assert len(parsed.candidates) == 3
            for candidate in parsed.candidates:
                assert all(span.quote in chunk.chunk_text for span in candidate.evidence_spans)
            if cache.startswith("world-setting"):
                assert parsed.candidates[-1].confidence == 0.9
    for _ in range(3):
        for cache, payload in [
            ("ordered-character-subject-resolution:test", {"candidates": [{"ref": "C3", "entity_name": "미상"}]}),
            ("ordered-world-subject-resolution:test", {"candidate": {"subject_name": "미궁"}}),
        ]:
            response = asyncio.run(provider.create_text_response(
                prompt_cache_key=cache, user_prompt=json.dumps(payload) + "\nresponse_format_feedback:\n{}",
            ))
            assert "999" in response.text
    assert provider.failed_character_attempts == provider.failed_world_attempts == 3


def test_character_partial_provider_uses_existing_source_and_preserves_the_valid_prefix():
    from app.analysis import character_fact_comparator as comparator
    from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchResult
    from tests.test_ordered_character_slot_feedback import candidate

    provider = CharacterPartialRecoveryProvider()
    provider.episode_no = 1
    response = asyncio.run(provider.create_text_response(
        prompt_cache_key="setting-extraction:test", user_prompt="{}",
    ))
    extracted = [row for row in json.loads(response.text)["candidates"] if row["candidate_kind"] == "SETTING"]
    assert len(extracted) == 2
    offsets = [source_text(1, True).index(row["evidence_spans"][0]["quote"]) for row in extracted]
    assert offsets[0] < offsets[1]
    sources = [candidate("C1", "status.부상"), candidate("C2", "status.오른발_부상")]
    payload = comparator._batch_prompt_payload("세룸", "STATUS", sources, [])
    response = asyncio.run(provider.create_text_response(
        prompt_cache_key="character-fact-comparison-batch:test", user_prompt=json.dumps(payload),
    ))
    result = CharacterFactComparisonBatchResult.model_validate_json(response.text)
    with pytest.raises(ValueError, match="Unknown snapshot refs"):
        comparator._validate_batch_comparison_result(result, "STATUS", sources, [], ordered_context=True)
    retained = comparator._independent_ordered_decisions(json.loads(response.text), "STATUS", sources, [])
    assert retained[0].candidate_ref == "C1" and retained[1] is None


@pytest.mark.parametrize("episode_no", [1, 2])
def test_http_harness_world_response_uses_complete_strict_wire_schema(episode_no):
    provider = DeterministicProvider()
    provider.calls = []
    provider.episode_no = episode_no
    payload = {
        "candidates": [{"ref": "C1"}, {"ref": "C2"}] if episode_no == 1 else [{"ref": "C1"}],
        "targets": [{"ref": "T1", "properties": [] if episode_no == 1 else [
            {"ref": "T1.P1", "scope_name": None, "setting_name": "색상", "value": "색1"},
        ]}],
    }

    response = asyncio.run(provider.create_text_response(
        prompt_cache_key="world-setting-comparison-batch:v5", user_prompt=json.dumps(payload),
    ))
    result = restore_selected_properties(OrderedWorldPropertyBatchResult.model_validate_json(response.text), payload)
    assert result.decisions[0].operation == ("ADD" if episode_no == 1 else "UPDATE")
    assert result.decisions[0].source_candidate_refs == [item["ref"] for item in payload["candidates"]]


def test_automatic_harness_failure_has_four_grounded_sources_and_reaches_target_validator():
    provider = AutomaticIsolationProvider()
    provider.episode_no = 1
    extracted = json.loads(asyncio.run(provider.create_text_response(
        prompt_cache_key="world-setting-extraction:test", user_prompt="{}",
    )).text)["candidates"]
    assert len(extracted) == 5
    assert {item["subject_name"] for item in extracted[:4]} == {"미궁"}
    assert extracted[-1]["subject_name"] == "백탑"
    for item in extracted:
        assert item["evidence_spans"][0]["quote"] in source_text(1, True)
    candidates = [WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=f"C{index}", candidate_id=UUID(int=index), subject_name="미궁",
        setting_name=key, extracted_value=value, evidence_spans=[{"quote": f"미궁의 {key}는 {value}."}],
    ) for index, (key, value) in enumerate(ISOLATED_WORLD_VALUES.items(), 1)]
    payload = {
        "candidates": [{"ref": candidate.candidate_ref, "subject_name": "미궁",
                        "setting_name": candidate.setting_name, "extracted_value": candidate.extracted_value}
                       for candidate in candidates],
        "targets": [{"ref": "T1", "confirmation_status": "PROVISIONAL", "properties": []}],
    }
    response = asyncio.run(provider.create_text_response(
        prompt_cache_key="world-setting-comparison-batch:test", user_prompt=json.dumps(payload),
    ))
    result = restore_selected_properties(OrderedWorldPropertyBatchResult.model_validate_json(response.text), payload)
    target = WorkerWorldSettingComparisonTarget(
        provisional_subject_key=f"provisional-world:{UUID(int=8)}",
        subject_name="미궁", version=0, properties=[],
    )
    with pytest.raises(OrderedWorldBatchValidationError, match="CANONICAL_TARGET_REQUIRED"):
        _validate_batch_comparison_result(result, candidates,
                                         [ComparisonTargetReference("T1", target)], ordered_context=True)
