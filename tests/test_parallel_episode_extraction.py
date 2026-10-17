"""Worker orchestration tests; only external extraction/storage are doubled."""
import asyncio
from uuid import UUID

import pytest

from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.analysis.narrative_time import NarrativeTimePoint
from app.domain.enums import AnalysisJobCheckpointStage as Stage
from app.worker.analysis_job_worker import AnalysisJobWorker
from app.llm.responses import LlmTextResponse
from app.usage import metering
from tests.test_ai_token_metering import FakeEncoding, FakeLedger
from tests.test_analysis_job_worker import (
    FakeEpisodeChunkingService,
    FakeExtractionResult,
    FakeSettingCandidateService,
    FakeSpringWorkerClient,
    _chunk,
    _payload,
)


class RecordingSpring(FakeSpringWorkerClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.published = []

    async def publish_world_setting_candidates(self, job_id, lease_token, candidates):
        assert Stage.CHARACTER_COMPARISONS_HANDED_OFF in self.checkpoint_calls or (
            self.payload.checkpoint_stage == Stage.CHARACTER_COMPARISONS_HANDED_OFF
        )
        self.published.append(candidates)
        return candidates


class ControlledExtractor:
    def __init__(self, kind, *, gated=False, error=None):
        self.kind = kind
        self.gated = gated
        self.error = error
        self.requests = []
        self.started = asyncio.Queue()
        self.release = asyncio.Queue()
        self.completed = asyncio.Queue()
        self.finished = []

    async def extract_from_chunk(self, **kwargs):
        text = kwargs["chunk_text"]
        self.requests.append(kwargs)
        self.started.put_nowait(text)
        try:
            if self.gated:
                await self.release.get()
            if self.error:
                raise self.error
            if self.kind == "character":
                result = FakeExtractionResult([])
                result.time_context = [NarrativeTimePoint(
                    description="현재", evidence_spans=[{"quote": text}],
                )]
                return result
            return WorldSettingExtractionResult.model_validate({"candidates": [{
                "category": "RACE", "subject_name": text, "setting_name": "특징",
                "extracted_value": "강하다", "evidence_spans": [{"quote": text}],
                "confidence": 0.9,
            }]})
        finally:
            self.finished.append(text)
            self.completed.put_nowait(text)


def make_worker(character, world, *, checkpoint=None, chunks=None):
    payload = _payload().model_copy(update={"checkpoint_stage": checkpoint})
    spring = RecordingSpring(payload)
    chunking = FakeEpisodeChunkingService(chunks or [_chunk(0, "거인은 강하다.")])
    candidates = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring, chunking_service=chunking, episode_chunk_service=chunking,
        setting_extractor=character, world_setting_extractor=world,
        setting_candidate_service=candidates,
    )
    return worker, spring, chunking, candidates


async def started(extractor):
    return await asyncio.wait_for(extractor.started.get(), timeout=1)


def test_each_chunk_extracts_both_domains_together_but_next_chunk_waits():
    async def scenario():
        character = ControlledExtractor("character", gated=True)
        world = ControlledExtractor("world", gated=True)
        worker, spring, _, _ = make_worker(character, world, chunks=[
            _chunk(0, "거인은 강하다.", 20), _chunk(1, "엘프는 빠르다.", 80),
        ])
        task = asyncio.create_task(worker.run_once())
        try:
            for text in ("거인은 강하다.", "엘프는 빠르다."):
                assert await started(character) == text
                assert await started(world) == text
                assert spring.published == []
                assert character.started.empty() and world.started.empty()
                world.release.put_nowait(None)
                assert await asyncio.wait_for(world.completed.get(), timeout=1) == text
                assert text not in character.finished
                assert character.started.empty() and world.started.empty()
                character.release.put_nowait(None)
            await task
            assert len(world.requests) == 2  # Publishing must reuse the early results.
            assert "time_context" not in character.requests[0]
            assert character.requests[1]["time_context"][0].evidence_spans[0].start_offset == 20
            assert all("time_context" not in request for request in world.requests)
            assert len(spring.published) == 1
            assert [item.subject_name for item in spring.published[0]] == [
                "거인은 강하다.", "엘프는 빠르다.",
            ]
            assert [item.evidence_spans[0].start_offset for item in spring.published[0]] == [20, 80]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [1, 2])
def test_real_extractors_share_existing_generation_limit_and_meter_each_request(monkeypatch, limit):
    monkeypatch.setattr(metering, "_encoding_for_model", lambda _: FakeEncoding())

    class SpringWithLedger(RecordingSpring, FakeLedger):
        def __init__(self):
            RecordingSpring.__init__(self, _payload())
            FakeLedger.__init__(self)

    async def scenario():
        spring = SpringWithLedger()
        started_at_limit = asyncio.Event()
        release = asyncio.Event()
        active = peak = calls = 0

        class Provider:
            async def create_text_response(self, **kwargs):
                nonlocal active, peak, calls
                active += 1
                calls += 1
                peak = max(peak, active)
                if active == limit:
                    started_at_limit.set()
                try:
                    await release.wait()
                    await asyncio.sleep(0)
                    return LlmTextResponse(text='{"candidates":[],"time_context":[]}',
                                           input_token_count=10, output_token_count=5)
                finally:
                    active -= 1

        semaphore = asyncio.Semaphore(limit)
        worker = AnalysisJobWorker(
            spring_client=spring,
            chunking_service=FakeEpisodeChunkingService([_chunk(0, "거인은 강하다.")]),
            setting_candidate_service=FakeSettingCandidateService(),
            llm_provider_client=Provider(), llm_request_semaphore=semaphore,
            extraction_model_name="gpt-6.1-sol",
        )
        payloads = [_payload().model_copy(update={"analysis_job_id": UUID(int=i)}) for i in (101, 102)]
        tasks = [asyncio.create_task(worker.process_claimed(payload)) for payload in payloads]
        try:
            await asyncio.wait_for(started_at_limit.wait(), timeout=1)
            assert calls == active == limit
            release.set()
            await asyncio.gather(*tasks)
            assert peak == limit and active == 0 and calls == 4
            assert len(spring.reservations) == len(spring.settlements) == 4
            assert spring.releases == []
            assert {row["purpose"] for row in spring.reservations} == {
                "SETTING_EXTRACTION", "WORLD_SETTING_EXTRACTION",
            }
            for payload in payloads:
                assert sum(row["analysis_job_id"] == payload.analysis_job_id
                           for row in spring.reservations) == 2
            assert not semaphore.locked()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failing_kind", ["character", "world"])
def test_one_extraction_failure_drains_peer_and_restart_reuses_stored_chunks(failing_kind):
    async def scenario():
        character = ControlledExtractor("character", gated=True)
        world = ControlledExtractor("world", gated=True)
        failing = character if failing_kind == "character" else world
        peer = world if failing_kind == "character" else character
        failure = RuntimeError("extraction failed")
        failing.error = failure
        worker, spring, chunking, candidates = make_worker(character, world)
        task = asyncio.create_task(worker.run_once())
        try:
            await started(character)
            await started(world)
            failing.release.put_nowait(None)
            await asyncio.wait_for(failing.completed.get(), timeout=1)
            assert not task.done()  # No paid sibling request outlives the job.
            peer.release.put_nowait(None)
            with pytest.raises(RuntimeError) as raised:
                await task
            assert raised.value is failure
            assert character.finished and world.finished
            assert candidates.request is None and spring.published == []
            assert Stage.CHARACTER_CANDIDATES_SAVED not in spring.checkpoint_calls
            assert len(spring.fail_calls) == 1

            # A new lease has only CHUNKS_READY: neither extraction was committed.
            spring.payload = spring.payload.model_copy(update={"checkpoint_stage": Stage.CHUNKS_READY})
            failing.error = None
            character.gated = world.gated = False
            await worker.run_once()
            assert len(chunking.requested_episode_ids) == 1
            assert len(chunking.loaded_episode_ids) == 1
            assert len(character.requests) == len(world.requests) == 2
            assert len(spring.published) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()
    asyncio.run(scenario())


def test_shutdown_cancels_both_extractions_and_leaves_job_for_lease_recovery():
    async def scenario():
        character = ControlledExtractor("character", gated=True)
        world = ControlledExtractor("world", gated=True)
        worker, spring, _, candidates = make_worker(character, world)
        task = asyncio.create_task(worker.run_once())
        try:
            await started(character)
            await started(world)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert character.finished and world.finished
            assert spring.fail_calls == spring.complete_calls == spring.published == []
            assert candidates.request is None

            spring.payload = spring.payload.model_copy(update={"checkpoint_stage": Stage.CHUNKS_READY})
            character.gated = world.gated = False
            await worker.run_once()
            assert len(spring.complete_calls) == len(spring.published) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("checkpoint,world_calls", [
    (Stage.CHARACTER_CANDIDATES_SAVED, 1),
    (Stage.CHARACTER_COMPARISONS_HANDED_OFF, 1),
    (Stage.WORLD_CANDIDATES_PUBLISHED, 0),
])
def test_restart_only_extracts_domains_without_saved_checkpoint(checkpoint, world_calls):
    async def scenario():
        character = ControlledExtractor("character")
        world = ControlledExtractor("world")
        worker, spring, chunking, candidates = make_worker(character, world, checkpoint=checkpoint)
        try:
            await worker.run_once()
            assert character.requests == []
            assert candidates.request is None
            assert len(world.requests) == len(spring.published) == world_calls
            assert chunking.requested_episode_ids == []
            assert len(chunking.loaded_episode_ids) == 1
        finally:
            await worker.aclose()
    asyncio.run(scenario())


def test_restart_after_world_publish_failure_keeps_character_checkpoint():
    async def scenario():
        character = ControlledExtractor("character")
        world = ControlledExtractor("world")
        worker, spring, chunking, _ = make_worker(character, world)
        publish = spring.publish_world_setting_candidates

        async def unavailable(*args):
            raise ConnectionError("Spring publish unavailable")

        spring.publish_world_setting_candidates = unavailable
        try:
            with pytest.raises(ConnectionError):
                await worker.run_once()
            checkpoint = spring.checkpoint_calls[-1]
            assert checkpoint == Stage.CHARACTER_COMPARISONS_HANDED_OFF
            assert len(character.requests) == len(world.requests) == 1
            assert spring.published == []

            spring.payload = spring.payload.model_copy(update={
                "checkpoint_stage": checkpoint, "lease_token": UUID(int=300),
            })
            spring.publish_world_setting_candidates = publish
            await worker.run_once()
            assert len(character.requests) == 1
            assert len(world.requests) == 2  # An uncommitted response is not a durable checkpoint.
            assert len(spring.published) == len(spring.complete_calls) == 1
            assert len(chunking.requested_episode_ids) == len(chunking.loaded_episode_ids) == 1
        finally:
            await worker.aclose()
    asyncio.run(scenario())
