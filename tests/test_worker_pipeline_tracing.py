"""Trace the real Worker orchestration with only external I/O replaced."""

import asyncio
import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
from app.analysis.character_subject_resolver import SubjectResolutionResult
from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.core.config import Settings
from app.domain.enums import AnalysisFailureCode
from app.domain.enums import AnalysisJobCheckpointStage as Stage
from app.embeddings.exceptions import RecoverableEmbeddingProviderError
from app.monitoring.worker_tracing import WorkerTracingRuntime, set_process_tracing
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker
from app.worker.character_fact_comparison_worker import CharacterFactComparisonWorker
from app.worker.world_setting_comparison_worker import WorldSettingComparisonWorker
from scripts.run_analysis_worker import _run_claimed_with_metrics
from tests.test_analysis_job_worker import (
    FakeEpisodeChunkEmbeddingService,
    FakeEpisodeChunkingService,
    FakeExtractionResult,
    FakeSettingCandidateService,
    FakeSpringWorkerClient,
    FakeSubjectResolver,
    _chunk,
    _payload,
)
from tests.test_character_fact_comparison_worker import _payload as character_payload
from tests.test_ordered_analysis_runtime import _ordered_payload
from tests.test_world_setting_comparison_worker import _payload as world_payload


@pytest.fixture
def spans(monkeypatch):
    settings = Settings(_env_file=None)
    for module in (
        "analysis_job_worker", "character_fact_comparison_worker", "world_setting_comparison_worker",
    ):
        monkeypatch.setattr(f"app.worker.{module}.get_settings", lambda: settings)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = WorkerTracingRuntime(provider)
    previous = set_process_tracing(runtime)
    try:
        yield exporter
    finally:
        set_process_tracing(previous)
        runtime.shutdown()


class Extractor:
    def __init__(self, kind, *, gated=False, error=None):
        self.kind = kind
        self.gated = gated
        self.error = error
        self.started = asyncio.Queue()
        self.release = asyncio.Queue()
        self.finished = asyncio.Queue()
        self.calls = []

    async def extract_from_chunk(self, **kwargs):
        self.calls.append(kwargs)
        self.started.put_nowait(kwargs["chunk_text"])
        try:
            if self.gated:
                await self.release.get()
            if self.error is not None:
                raise self.error
            if self.kind == "character":
                return FakeExtractionResult([])
            return WorldSettingExtractionResult(candidates=[])
        finally:
            self.finished.put_nowait(kwargs["chunk_text"])


class Comparison:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def process_all(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self.result


class RecordingSpring(FakeSpringWorkerClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.finalization_span_ids = []

    async def report_progress(self, *args, **kwargs):
        step = kwargs.get("current_step", args[2] if len(args) > 2 else None)
        if step == "PERSISTING":
            self.finalization_span_ids.append(trace.get_current_span().get_span_context().span_id)
        await super().report_progress(*args, **kwargs)

    async def complete(self, *args, **kwargs):
        self.finalization_span_ids.append(trace.get_current_span().get_span_context().span_id)
        await super().complete(*args, **kwargs)


def make_worker(*, payload=None, character=None, world=None, chunks=None, **kwargs):
    payload = payload or _payload()
    spring = RecordingSpring(payload)
    chunking = FakeEpisodeChunkingService(chunks or [_chunk(0, "PRIVATE_SOURCE_TEXT")])
    worker = AnalysisJobWorker(
        spring_client=spring,
        chunking_service=chunking,
        episode_chunk_service=chunking,
        setting_extractor=character or Extractor("character"),
        world_setting_extractor=world or Extractor("world"),
        subject_resolver=FakeSubjectResolver(SubjectResolutionResult(candidates=[])),
        setting_candidate_service=FakeSettingCandidateService(),
        **kwargs,
    )
    return worker, spring, chunking


async def execute(worker, payload):
    try:
        return await _run_claimed_with_metrics(worker, payload, None)
    finally:
        await worker.aclose()


def named(exporter, name):
    return [span for span in exporter.get_finished_spans() if span.name == name]


def only(exporter, name):
    matches = named(exporter, name)
    assert len(matches) == 1
    return matches[0]


def assert_no_private_trace_data(exporter, *secrets):
    serialized = "\n".join(span.to_json() for span in exporter.get_finished_spans())
    for secret in secrets:
        assert secret not in serialized
    assert all(not span.events for span in exporter.get_finished_spans())


def test_parallel_extractors_are_overlapping_siblings_and_chunks_remain_sequential(spans):
    async def scenario():
        character = Extractor("character", gated=True)
        world = Extractor("world", gated=True)
        chunks = [_chunk(0, "PRIVATE_CHUNK_ONE"), _chunk(1, "PRIVATE_CHUNK_TWO", 30)]
        worker, spring, _ = make_worker(character=character, world=world, chunks=chunks)
        task = asyncio.create_task(execute(worker, spring.payload))
        try:
            for chunk in chunks:
                assert await asyncio.wait_for(character.started.get(), 2) == chunk.chunk_text
                assert await asyncio.wait_for(world.started.get(), 2) == chunk.chunk_text
                world.release.put_nowait(None)
                assert await asyncio.wait_for(world.finished.get(), 2) == chunk.chunk_text
                assert character.finished.empty()
                assert character.started.empty() and world.started.empty()
                character.release.put_nowait(None)
                assert await asyncio.wait_for(character.finished.get(), 2) == chunk.chunk_text
            result = await asyncio.wait_for(task, 2)
            assert result.claimed
            assert spring.fail_calls == []
            return spring, chunks
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    spring, chunks = asyncio.run(scenario())
    root = only(spans, "analysis.job.execute")
    parent = only(spans, "analysis.character.prepare")
    assert parent.parent.span_id == root.context.span_id
    previous_end = 0
    for index, chunk in enumerate(chunks):
        pair = [span for span in spans.get_finished_spans()
                if span.name in {"analysis.character.extract", "analysis.world.extract"}
                and span.attributes["analysis.chunk.index"] == index]
        assert len(pair) == 2
        assert all(span.parent.span_id == parent.context.span_id for span in pair)
        assert max(span.start_time for span in pair) < min(span.end_time for span in pair)
        assert previous_end <= min(span.start_time for span in pair)
        previous_end = max(span.end_time for span in pair)
        assert all(span.attributes["analysis.chunk.char_count"] == len(chunk.chunk_text) for span in pair)
    assert only(spans, "analysis.world.prepare").attributes["analysis.world.reused_chunk_count"] == 2
    assert only(spans, "analysis.character.compare").attributes["outcome"] == "handed_off"
    finalize = only(spans, "analysis.finalize")
    assert spring.finalization_span_ids == [finalize.context.span_id] * 2
    assert root.attributes["outcome"] == "success"
    assert_no_private_trace_data(
        spans, *(chunk.chunk_text for chunk in chunks), spring.payload.work_title,
        spring.payload.episode.content_s3_key, str(spring.payload.lease_token),
    )


@pytest.mark.parametrize("ordered", [False, True])
def test_checkpoint_reuse_is_distinct_from_running_pending_ordered_comparisons(spans, ordered):
    async def scenario():
        payload = WorkerAnalysisJobPayload.model_validate(_ordered_payload()) if ordered else _payload()
        payload = payload.model_copy(update={"checkpoint_stage": Stage.WORLD_COMPARISONS_FINISHED})
        character = Extractor("character")
        world = Extractor("world")
        character_comparison = Comparison(CharacterFactComparisonRunResult(0, 0))
        world_comparison = Comparison(WorldSettingComparisonRunResult(0, 0))
        worker, spring, chunking = make_worker(
            payload=payload, character=character, world=world,
            character_fact_comparison_pipeline=character_comparison,
            world_setting_comparison_pipeline=world_comparison,
        )
        await execute(worker, payload)
        assert not character.calls and not world.calls
        assert chunking.loaded_episode_ids == [payload.episode.episode_id]
        assert not chunking.requested_episode_ids
        assert len(character_comparison.calls) == int(ordered)
        assert len(world_comparison.calls) == int(ordered)
        assert all(checkpoint is None for checkpoint in spring.checkpoint_calls)

    asyncio.run(scenario())
    assert not named(spans, "analysis.character.extract")
    assert not named(spans, "analysis.world.extract")
    for name in ("analysis.character.prepare", "analysis.world.prepare"):
        stage = only(spans, name)
        assert stage.attributes["outcome"] == "skipped"
        assert "analysis.candidate.count" not in stage.attributes
    for name in ("analysis.character.compare", "analysis.world.compare"):
        assert (only(spans, name).attributes.get("outcome") == "skipped") is not ordered


def test_returned_automatic_job_keeps_known_partial_comparison_results_on_child_spans(spans):
    async def scenario():
        payload = WorkerAnalysisJobPayload.model_validate({
            **_ordered_payload(), "review_mode": "AUTOMATIC",
            "checkpoint_stage": Stage.WORLD_CANDIDATES_PUBLISHED,
        })
        character = Comparison(CharacterFactComparisonRunResult(
            2, 1, AnalysisFailureCode.COMPARISON_VALIDATION_FAILED, batch_count=2,
        ))
        world = Comparison(WorldSettingComparisonRunResult(
            3, 2, AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR, batch_count=3,
        ))
        worker, spring, _ = make_worker(
            payload=payload, character_fact_comparison_pipeline=character,
            world_setting_comparison_pipeline=world,
        )
        await execute(worker, payload)
        assert not spring.fail_calls
        assert len(spring.complete_calls) == 1
        assert character.calls[0]["continue_on_candidate_failure"]
        assert world.calls[0]["continue_on_candidate_failure"]
        summary = json.loads(spring.complete_calls[0][1])
        assert summary["characterFactComparisonFailedCount"] == 1
        assert summary["worldSettingComparisonFailedCount"] == 2

    asyncio.run(scenario())
    root = only(spans, "analysis.job.execute")
    assert root.attributes["outcome"] == "success"
    assert root.status.status_code == StatusCode.UNSET
    for name, completed, failed in (("character", 2, 1), ("world", 3, 2)):
        span = only(spans, f"analysis.{name}.compare")
        assert span.parent.span_id == root.context.span_id
        assert span.attributes["outcome"] == "partial"
        assert span.attributes["analysis.comparison.completed_count"] == completed
        assert span.attributes["analysis.comparison.failed_count"] == failed
        assert span.status.status_code == StatusCode.ERROR


@pytest.mark.parametrize("optional", ["embedding", "retrieval"])
def test_recoverable_optional_failure_is_visible_without_failing_analysis(spans, optional):
    class FailingRetrieval:
        def prepare(self, scope):
            raise RuntimeError("PRIVATE_OPTIONAL_FAILURE")

    async def scenario():
        options = ({
            "embedding_generation_enabled": True,
            "episode_chunk_embedding_service": FakeEpisodeChunkEmbeddingService(
                RecoverableEmbeddingProviderError("PRIVATE_OPTIONAL_FAILURE"),
            ),
        } if optional == "embedding" else {
            "retrieval_preparation_enabled": True, "retrieval_preparer": FailingRetrieval(),
        })
        worker, spring, _ = make_worker(**options)
        await execute(worker, spring.payload)
        assert not spring.fail_calls and len(spring.complete_calls) == 1

    asyncio.run(scenario())
    root = only(spans, "analysis.job.execute")
    prepare = only(spans, "analysis.prepare")
    optional_span = only(spans, "analysis.embedding" if optional == "embedding" else "analysis.retrieval.prepare")
    assert optional_span.parent.span_id == prepare.context.span_id
    assert optional_span.attributes["outcome"] == "failure"
    assert optional_span.status.status_code == StatusCode.ERROR
    assert prepare.status.status_code == root.status.status_code == StatusCode.UNSET
    assert root.attributes["outcome"] == "success"
    assert_no_private_trace_data(spans, "PRIVATE_OPTIONAL_FAILURE", "PRIVATE_SOURCE_TEXT")


@pytest.mark.parametrize("domain", ["character", "world"])
@pytest.mark.parametrize("failed", [False, True])
def test_hidden_comparison_workers_trace_only_their_actual_stages(spans, domain, failed):
    async def scenario():
        if domain == "character":
            worker_type, payload, result_type = (
                CharacterFactComparisonWorker, character_payload(), CharacterFactComparisonRunResult,
            )
        else:
            worker_type, payload, result_type = (
                WorldSettingComparisonWorker, world_payload(), WorldSettingComparisonRunResult,
            )
        spring = RecordingSpring(payload)
        worker = worker_type(spring_client=spring, comparison_pipeline=Comparison(result_type(
            1, int(failed), AnalysisFailureCode.COMPARISON_VALIDATION_FAILED if failed else None,
        )))
        if failed:
            with pytest.raises(RuntimeError, match="recomparison failed"):
                await execute(worker, payload)
            assert len(spring.fail_calls) == 1 and not spring.complete_calls
        else:
            await execute(worker, payload)
            assert len(spring.complete_calls) == 1 and not spring.fail_calls

    asyncio.run(scenario())
    names = {span.name for span in spans.get_finished_spans()}
    assert names == {"analysis.job.execute", f"analysis.{domain}.compare"} | (
        set() if failed else {"analysis.finalize"}
    )
    root = only(spans, "analysis.job.execute")
    comparison = only(spans, f"analysis.{domain}.compare")
    assert comparison.parent.span_id == root.context.span_id
    assert comparison.attributes["analysis.comparison.failed_count"] == int(failed)
    assert root.attributes["outcome"] == ("failure" if failed else "success")


def test_finalization_failure_is_not_reported_as_successful_execution(spans):
    async def scenario():
        payload = _payload().model_copy(update={"checkpoint_stage": Stage.WORLD_COMPARISONS_FINISHED})
        worker, spring, _ = make_worker(payload=payload)

        async def fail_complete(*args, **kwargs):
            raise RuntimeError("PRIVATE_COMPLETION_FAILURE")

        spring.complete = fail_complete
        with pytest.raises(RuntimeError, match="PRIVATE_COMPLETION_FAILURE"):
            await execute(worker, payload)
        assert len(spring.fail_calls) == 1

    asyncio.run(scenario())
    for name in ("analysis.job.execute", "analysis.finalize"):
        span = only(spans, name)
        assert span.attributes["outcome"] == "failure"
        assert span.status.status_code == StatusCode.ERROR
    assert_no_private_trace_data(spans, "PRIVATE_COMPLETION_FAILURE")
