import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analysis.exceptions import LlmExtractionError, OrderedInputContextError
from app.analysis.relationship_observations import RelationshipExtractionResult
from app.analysis.relationship_pipeline import RelationshipAnalysisPipeline
from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
from app.llm.responses import LlmTextResponse
from tests.test_parallel_episode_extraction import ControlledExtractor, make_worker, started


@pytest.mark.parametrize("relation_fails", [False, True])
def test_three_extractions_overlap_and_bad_relation_output_keeps_core_results(relation_fails):
    async def scenario():
        character = ControlledExtractor("character", gated=True)
        world = ControlledExtractor("world", gated=True)
        worker, spring, _, candidates = make_worker(character, world)
        spring.payload = spring.payload.model_copy(update={"relationship_analysis_contract": {"formatVersion": 1}})
        spring.payload.episode = spring.payload.episode.model_copy(update={"content_hash": "a" * 64})
        relation_started, relation_release = asyncio.Event(), asyncio.Event()

        async def extract(**kwargs):
            relation_started.set()
            await relation_release.wait()
            if relation_fails:
                raise LlmExtractionError("private response")
            return RelationshipExtractionResult(
                source_chunk_id=kwargs["source_chunk_id"], raw_observations=[],
                observations=[], rejections=[], response=LlmTextResponse(text="{}"),
            )

        spring.get_relationship_analysis = AsyncMock(return_value=None)
        spring.prepare_relationship_analysis = AsyncMock(return_value={
            "formatVersion": 1, "contextToken": "frozen", "status": "PREPARED",
            "sourceEpisodeNo": 1, "sourceHash": "a" * 64, "observations": [],
        })
        spring.complete_relationship_analysis = AsyncMock()
        spring.fail_relationship_analysis = AsyncMock()
        worker._relationship_pipeline_factory = lambda payload: RelationshipAnalysisPipeline(
            spring_client=spring, payload=payload,
            extractor=SimpleNamespace(extract_from_chunk=extract), comparator=SimpleNamespace(compare=AsyncMock()),
        )
        task = asyncio.create_task(worker.run_once())
        try:
            await started(character)
            await started(world)
            await asyncio.wait_for(relation_started.wait(), 1)
            assert candidates.request is None
            character.release.put_nowait(None)
            world.release.put_nowait(None)
            relation_release.set()
            await asyncio.wait_for(task, 1)
            assert candidates.request is not None
            assert len(spring.published) == len(spring.complete_calls) == 1
            assert spring.fail_calls == []
            summary = json.loads(spring.complete_calls[0][1])
            assert summary["relationshipAnalysisStatus"] == ("FAILED" if relation_fails else "COMPLETED")
            assert spring.fail_relationship_analysis.await_count == int(relation_fails)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()
    asyncio.run(scenario())


def test_relationship_comparison_overlaps_world_comparison_after_core_preparation():
    async def scenario():
        worker, spring, _, _ = make_worker(ControlledExtractor("character"), ControlledExtractor("world"))
        spring.payload = spring.payload.model_copy(update={"relationship_analysis_contract": {"formatVersion": 1}})
        world_started, relation_started = asyncio.Event(), asyncio.Event()

        async def world_compare(*args):
            assert spring.published
            world_started.set()
            await relation_started.wait()
            return WorldSettingComparisonRunResult(0, 0)

        async def relation_finish(chunks):
            assert spring.published
            relation_started.set()
            await world_started.wait()
            return {"relationshipAnalysisStatus": "COMPLETED"}

        worker._run_world_comparison_stage = world_compare
        worker._relationship_pipeline_factory = lambda payload: SimpleNamespace(
            load=AsyncMock(), extract_chunk=AsyncMock(), finish=relation_finish,
        )
        try:
            await asyncio.wait_for(worker.run_once(), 1)
            assert len(spring.complete_calls) == 1
        finally:
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("version", [True, 2, None])
def test_unsupported_relationship_contract_stops_before_any_provider_or_progress(version):
    async def scenario():
        character, world = ControlledExtractor("character"), ControlledExtractor("world")
        worker, spring, _, _ = make_worker(character, world)
        spring.payload = spring.payload.model_copy(update={"relationship_analysis_contract": {"formatVersion": version}})
        try:
            with pytest.raises(OrderedInputContextError):
                await worker.run_once()
            assert spring.progress_calls == []
            assert character.requests == world.requests == []
            assert len(spring.fail_calls) == 1
        finally:
            await worker.aclose()
    asyncio.run(scenario())
