"""Worker fails closed before chunk/candidate writes and passes context to extraction."""

import asyncio
from types import SimpleNamespace

import pytest

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.semantic_current_context import prepare_semantic_current_context
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker
from tests.test_setting_book_source_contract import book_payload_data


def test_new_ordered_worker_advertises_context_without_changing_legacy_default():
    requests = []

    class Spring:
        async def claim(self, **kwargs):
            requests.append(kwargs)

    worker = object.__new__(AnalysisJobWorker)
    worker.spring_client = Spring()
    worker.extraction_model_name = "existing-model"
    asyncio.run(worker.claim_next())
    assert requests[0]["supports_semantic_current_context"] is True
    assert requests[0]["supported_source_kinds"] == ["EPISODE", "SETTING_BOOK"]


@pytest.mark.parametrize("mutation", ["missing", "source", "input", "owners"])
def test_invalid_carrier_stops_before_chunk_read_or_write(mutation):
    data = book_payload_data()
    if mutation == "missing":
        data.pop("semanticCurrentContext")
    elif mutation == "owners":
        data["semanticCurrentContext"]["characters"] = []
    elif mutation == "source":
        data["semanticCurrentContext"]["sourceHash"] = "c" * 64
    else:
        data["semanticCurrentContext"]["inputStateHash"] = "c" * 64
    payload = WorkerAnalysisJobPayload.model_validate(data)

    async def forbidden(*args):
        pytest.fail("Invalid carrier reached chunk persistence.")

    worker = SimpleNamespace(_run_chunk_stage=forbidden)
    with pytest.raises(OrderedInputContextError):
        asyncio.run(AnalysisJobWorker._run_analysis_steps(worker, payload))


def test_full_worker_stage_passes_prepared_context_only_to_extraction():
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
    from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult

    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    received = []

    class Stages:
        async def _run_chunk_stage(self, payload, checkpoint):
            return [], {"embeddedChunkCount": 0, "embeddingFailedChunkCount": 0, "embeddingSkippedChunkCount": 0}

        async def _run_character_stage(self, payload, chunks, checkpoint, known, hints, current):
            received.append(("character", current.character_prompt_json()))
            return {"candidateCount": 0, "subjectFallbackCallCount": 0,
                    "subjectFallbackResolvedCount": 0, "subjectFallbackUnresolvedCount": 0,
                    "statusContextCharacterCount": 0, "statusContextEntryCount": 0,
                    "statusInactiveCandidateCount": 0}

        async def _run_character_comparison_stage(self, payload, checkpoint):
            return CharacterFactComparisonRunResult(0, 0)

        async def _run_world_extraction_stage(self, payload, chunks, checkpoint, current):
            received.append(("world", current.world_prompt_json()))
            return 0

        async def _run_world_comparison_stage(self, payload, checkpoint):
            return WorldSettingComparisonRunResult(0, 0)

    asyncio.run(AnalysisJobWorker._run_analysis_steps(Stages(), payload))
    assert [name for name, _ in received] == ["character", "world"]
    assert "비요른 얀델" in received[0][1]
    assert "semanticCurrentContext" not in payload.analysis_context.model_dump(by_alias=True)


def test_reached_world_checkpoint_never_reextracts_or_republishes_current_context():
    from app.domain.enums import AnalysisJobCheckpointStage

    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    current = prepare_semantic_current_context(
        payload.semantic_current_context, analysis_context=payload.analysis_context,
        known_characters=payload.known_characters, provisional_characters=payload.provisional_characters,
        source_hash=payload.frozen_source().content_hash,
    )
    assert asyncio.run(AnalysisJobWorker._run_world_extraction_stage(
        object(), payload, [], AnalysisJobCheckpointStage.WORLD_CANDIDATES_PUBLISHED, current,
    )) == 0
