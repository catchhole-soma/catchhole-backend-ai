import asyncio
import json
from uuid import uuid4
from hashlib import sha256

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import (
    OrderedSubjectTarget,
    resolve_ordered_character_subjects,
)
from app.analysis.ordered_context import OrderedExtractionContext
from app.analysis.schemas import ExtractedSettingCandidate
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.domain.enums import AnalysisMode
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_context import AnalysisStateProvenance, WorkerAnalysisContext
from app.schemas.worker import (
    WorkerAnalysisJobPayload,
    WorkerAnalysisProvisionalCharacterPayload,
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectResolutionCandidate,
)
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext
from app.services.setting_candidate_service import SettingCandidateSaveItem, SettingCandidateService
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService
from tests.test_analysis_job_worker import _payload
from tests.test_character_fact_batch import _batch_snapshot, _candidate, _decision_payload
from tests.test_setting_extractor import (
    CHUNK_ID,
    DEFAULT_SCHEMA_HINTS,
    _valid_discovery_payload,
    _valid_setting_payload,
)


class RecordingClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text=json.dumps(self.responses.pop(0), ensure_ascii=False))


def _input_context():
    return WorkerAnalysisContext(
        run_id=uuid4(),
        generation=1,
        input_state_hash="a" * 64,
        source_hash="b" * 64,
        format_version=1,
    )


def _ordered_payload():
    payload = _payload().model_dump()
    payload.update(analysis_mode="ORDERED_PROVISIONAL", analysis_context=_input_context())
    payload["episode"].update(content_hash="b" * 64, episode_no=2)
    return payload


def _temporary_character():
    return WorkerAnalysisProvisionalCharacterPayload(
        provisional_subject_key=f"provisional-character:{uuid4()}",
        name="세룸",
        source_episode_no=1,
        aliases=["넷째 아들"],
        identity_evidence=[{"quote": "케닉의 넷째 아들 세룸이 말했다.", "startOffset": 2}],
        active_statuses=[
            {
                "factKey": "status.부상",
                "factValue": "오른발을 다쳤다",
                "provenance": {
                    "confirmationStatus": "PROVISIONAL",
                    "sourceEpisodeNo": 1,
                    "sourceCandidateIds": [str(uuid4())],
                },
            }
        ],
    )


def test_mode_is_explicit_and_cannot_be_inferred_from_remaining_jobs():
    assert _payload().analysis_mode == AnalysisMode.CONFIRMED_ONLY
    ordered = WorkerAnalysisJobPayload.model_validate(_ordered_payload())
    assert ordered.analysis_mode == AnalysisMode.ORDERED_PROVISIONAL
    assert ordered.episode is not None
    assert ordered.analysis_context is not None


@pytest.mark.parametrize(
    "change", ["missing_context", "wrong_source", "future_character", "single_leak"]
)
def test_invalid_ordered_context_is_rejected_before_runtime(change):
    payload = _ordered_payload()
    if change == "missing_context":
        payload["analysis_context"] = None
    elif change == "wrong_source":
        payload["episode"]["content_hash"] = "c" * 64
    elif change == "future_character":
        payload["provisional_characters"] = [
            _temporary_character().model_copy(update={"source_episode_no": 2})
        ]
    else:
        payload["analysis_mode"] = "CONFIRMED_ONLY"
    with pytest.raises(ValidationError):
        WorkerAnalysisJobPayload.model_validate(payload)


def test_ordered_extraction_is_isolated_and_never_exposes_internal_identity():
    client = RecordingClient({"candidates": []}, {"candidates": []}, {"candidates": []})
    extractor = CharacterSettingExtractor(llm_client=client, model="test-model")
    temporary = _temporary_character()
    context = OrderedExtractionContext(_input_context(), (), (temporary,))
    args = dict(
        source_chunk_id=CHUNK_ID,
        chunk_text="세룸은 일어섰다.",
        episode_no=2,
        schema_hints=DEFAULT_SCHEMA_HINTS,
    )
    asyncio.run(extractor.extract_from_chunk(**args))
    asyncio.run(extractor.extract_from_chunk(**args, ordered_context=context))
    asyncio.run(extractor.extract_from_chunk(**args))
    assert client.requests[0] == client.requests[2]
    first, ordered = client.requests[:2]
    assert first["model"] == ordered["model"]
    assert "PROVISIONAL" not in first["user_prompt"]
    assert '"confirmation_status": "PROVISIONAL"' in ordered["user_prompt"]
    assert '"source_episode_no": 1' in ordered["user_prompt"]
    assert "status.부상" in ordered["user_prompt"]
    assert temporary.provisional_subject_key not in ordered["user_prompt"]
    assert str(context.analysis_context.run_id) not in ordered["user_prompt"]
    assert (
        str(temporary.active_statuses[0].provenance.source_candidate_ids[0])
        not in ordered["user_prompt"]
    )


def _resolve_subjects(response, *, subjects=(), candidates=None, previous=None):
    client = RecordingClient(response)
    if candidates is None:
        candidates = [
            ExtractedSettingCandidate.model_validate(_valid_discovery_payload()),
            ExtractedSettingCandidate.model_validate(_valid_setting_payload()),
        ]
    result = asyncio.run(
        resolve_ordered_character_subjects(
            client=client,
            model="test-model",
            max_attempts=1,
            max_output_tokens=2000,
            context=SubjectResolutionChunkContext(
                previous, "Synthetic Character appeared at level 12.", None
            ),
            candidates=candidates,
            subjects=list(subjects),
            episode_no=1,
        )
    )
    return result, client


def test_new_discovery_and_setting_share_a_provisional_identity_without_real_id():
    (resolved, discovered), client = _resolve_subjects(
        {
            "resolutions": [
                {"candidate_ref": "C1", "target_ref": "D1"},
                {"candidate_ref": "C2", "target_ref": "D1"},
            ]
        }
    )
    key = f"provisional-character:{resolved[0].binding.candidate_id}"
    assert len(discovered) == 1
    assert [item.binding.provisional_subject_key for item in resolved] == [key, key]
    assert all(item.binding.actual_character_id is None for item in resolved)
    assert client.requests == []


def test_competing_same_names_do_not_force_a_provisional_subject_match():
    prior = OrderedSubjectTarget(
        name="Synthetic Character", provisional_subject_key=f"provisional-character:{uuid4()}"
    )
    (resolved, discovered), _ = _resolve_subjects(
        {
            "resolutions": [
                {"candidate_ref": "C1", "target_ref": None},
                {"candidate_ref": "C2", "target_ref": None},
            ]
        },
        subjects=[prior, OrderedSubjectTarget(
            name=prior.name, provisional_subject_key=f"provisional-character:{uuid4()}"
        )],
    )
    assert discovered == []
    assert all(item.binding.provisional_subject_key is None for item in resolved)
    assert all(item.binding.match_status == "AMBIGUOUS" for item in resolved)


def test_later_episode_setting_can_bind_to_the_previous_discovery_key():
    prior = OrderedSubjectTarget(
        name="Synthetic Character",
        provisional_subject_key=f"provisional-character:{uuid4()}",
        source_episode_no=1,
        active_statuses=tuple(_temporary_character().active_statuses),
    )
    (resolved, _), client = _resolve_subjects(
        {
            "resolutions": [
                {"candidate_ref": "C1", "target_ref": "K1"},
            ]
        },
        subjects=[prior],
        candidates=[ExtractedSettingCandidate.model_validate(_valid_setting_payload())],
    )
    assert resolved[0].binding.provisional_subject_key == prior.provisional_subject_key
    assert client.requests == []


def test_subject_resolution_rejects_missing_coverage_and_unknown_identity():
    with pytest.raises(ComparisonValidationError):
        _resolve_subjects(
            {"resolutions": [{"candidate_ref": "C1", "target_ref": "K999"}]},
            subjects=[OrderedSubjectTarget(name="Synthetic Character", actual_character_id=uuid4())],
            candidates=[ExtractedSettingCandidate.model_validate(_valid_setting_payload()).model_copy(
                update={"entity_name": "미상", "raw_entity_mention": "그는"})],
            previous="Synthetic Character introduced himself.",
        )


class FenceSession:
    def __init__(self, allowed):
        self.allowed = allowed
        self.events = []
        self.statement = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement):
        self.events.append("fence")
        self.statement = statement
        return self

    def scalar_one_or_none(self):
        return uuid4() if self.allowed else None

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FenceRepository:
    def __init__(self, session):
        self.session = session

    def delete_by_analysis_job_id(self, job):
        self.session.events.append("delete")

    def save_all(self, candidates):
        self.session.events.append("save")
        return candidates


@pytest.mark.parametrize("allowed", [False, True])
def test_candidate_fence_precedes_replace_in_the_same_transaction(allowed):
    (resolved, _), _ = _resolve_subjects(
        {
            "resolutions": [
                {"candidate_ref": "C1", "target_ref": "D1"},
                {"candidate_ref": "C2", "target_ref": "D1"},
            ]
        }
    )
    session = FenceSession(allowed)
    service = SettingCandidateService(lambda: session, FenceRepository)
    ctx = OrderedCandidateWriteContext(
        uuid4(), _input_context(), uuid4(), "source-key", "source-version", 1
    )
    kwargs = dict(
        work_id=uuid4(),
        analysis_job_id=uuid4(),
        known_characters=[],
        ordered_write_context=ctx,
        save_items=[
            SettingCandidateSaveItem(ctx.episode_id, "source-key", item.candidate, item.binding)
            for item in resolved
        ],
    )
    if allowed:
        saved = service.replace_candidates_for_analysis_job(**kwargs)
        assert session.events == ["fence", "delete", "save", "commit"]
        assert saved[0].id == resolved[0].binding.candidate_id
        assert saved[1].comparison_status == "PENDING"
        assert saved[1].matched_character_id is None
        assert saved[1].provisional_subject_key == saved[0].provisional_subject_key
    else:
        with pytest.raises(ComparisonValidationError, match="stale"):
            service.replace_candidates_for_analysis_job(**kwargs)
        assert session.events == ["fence", "rollback"]
    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF analysis_jobs, episodes" in sql
    for column in (
        "lease_token",
        "lease_expires_at",
        "input_state_hash",
        "run_generation",
        "analysis_run_id",
        "journal_status",
        "source_content_s3_version",
    ):
        assert column in sql


def test_world_identity_cannot_impersonate_a_real_uuid():
    key = f"provisional-world:{uuid4()}"
    subject = WorkerWorldSettingSubject(provisional_subject_key=key, subject_name="백탑")
    assert subject.world_setting_id is None
    with pytest.raises(ValidationError):
        WorkerWorldSettingSubject(
            world_setting_id=uuid4(), provisional_subject_key=key, subject_name="백탑"
        )


def test_world_ambiguous_subject_is_separate_from_a_new_subject():
    client = RecordingClient({"selected_subject_refs": [], "ambiguous": True})
    resolver = WorldSettingSubjectResolver(llm_client=client, model="test-model")
    subject = WorkerWorldSettingSubject(
        provisional_subject_key=f"provisional-world:{uuid4()}",
        subject_name="백탑",
        identity_evidence=[{"quote": "동쪽의 백탑"}],
    )
    candidate = WorkerWorldSettingSubjectResolutionCandidate(
        candidate_id=uuid4(),
        source_episode_id=uuid4(),
        category="LOCATION",
        subject_name="백탑",
        evidence_spans=[{"quote": "서쪽의 백탑"}],
    )
    selected, ambiguous = asyncio.run(resolver.select_ordered_subjects(candidate, [subject]))
    assert selected == [] and ambiguous
    assert "서쪽의 백탑" in client.requests[0]["user_prompt"]
    assert "동쪽의 백탑" in client.requests[0]["user_prompt"]
    assert subject.provisional_subject_key not in client.requests[0]["user_prompt"]


def test_character_comparison_removes_a_provisional_status_and_keeps_single_input_unchanged():
    response = {
        "decisions": [
            _decision_payload(
                "C1",
                operation="REMOVE",
                resolved_key="status.다리_상태",
                removed_refs=["P1"],
                value=None,
            )
        ]
    }
    client = RecordingClient(response, response, response)
    comparator = CharacterFactComparator(llm_client=client, model="test-model")
    snapshot = _batch_snapshot("P1", "status.부상", "다친 다리")
    ordered_snapshot = snapshot.model_copy(
        update={
            "origin": "PROVISIONAL",
            "provenance": AnalysisStateProvenance(
                confirmation_status="PROVISIONAL",
                source_episode_no=1,
                source_candidate_ids=[uuid4()],
            ),
        }
    )
    kwargs = dict(
        matched_character_name="세룸",
        canonical_fact_type="STATUS",
        candidates=[_candidate("C1", "Q1", "다리가 나아 일어섰다")],
        snapshot_entries=[snapshot],
    )
    _, baseline_raw = asyncio.run(comparator.compare_batch(**kwargs))
    ordered_kwargs = {**kwargs, "snapshot_entries": [ordered_snapshot]}
    result, ordered_raw = asyncio.run(
        comparator.compare_batch(**ordered_kwargs, ordered_context=True)
    )
    asyncio.run(comparator.compare_batch(**kwargs))
    assert client.requests[0] == client.requests[2]
    assert result.decisions[0].removed_snapshot_refs == ["P1"]
    payload = json.loads(client.requests[1]["user_prompt"])
    assert payload["snapshot_entries"][0]["confirmation_status"] == "PROVISIONAL"
    assert payload["snapshot_entries"][0]["source_episode_no"] == 1
    assert (
        str(ordered_snapshot.provenance.source_candidate_ids[0])
        not in client.requests[1]["user_prompt"]
    )
    assert ordered_raw["estimated_input_tokens"] > baseline_raw["estimated_input_tokens"]
    comparator.batch_max_input_tokens = baseline_raw["estimated_input_tokens"]
    assert comparator.batch_fits(**kwargs)
    assert not comparator.batch_fits(**ordered_kwargs, ordered_context=True)
    with pytest.raises(ComparisonValidationError, match="explicit ordered"):
        asyncio.run(comparator.compare_batch(**ordered_kwargs))


@pytest.mark.parametrize("source_matches", [True, False])
def test_ordered_chunk_download_pins_s3_version_and_checks_hash_before_replace(source_matches):
    class Storage:
        def get_text(self, key, version_id=None):
            assert key == "source-key" and version_id == "version-1"
            return "회차 원문"

    class Chunks:
        calls = []

        def replace_episode_chunks(self, **kwargs):
            self.calls.append(kwargs)
            return []

    chunks = Chunks()
    ctx = _input_context().model_copy(
        update={
            "source_hash": sha256("회차 원문".encode()).hexdigest() if source_matches else "f" * 64,
        }
    )
    write = OrderedCandidateWriteContext(uuid4(), ctx, uuid4(), "source-key", "version-1", 1)
    service = EpisodeS3ChunkingService(Storage(), chunks)
    kwargs = dict(
        episode_id=write.episode_id,
        content_s3_key="source-key",
        ordered_write_context=write,
        analysis_job_id=uuid4(),
        work_id=uuid4(),
    )
    if source_matches:
        service.replace_chunks_from_s3_content(**kwargs)
        assert len(chunks.calls) == 1
        assert chunks.calls[0]["ordered_write_context"] is write
    else:
        with pytest.raises(ComparisonValidationError, match="hash"):
            service.replace_chunks_from_s3_content(**kwargs)
        assert chunks.calls == []


def test_world_batch_maps_local_target_refs_back_to_provisional_keys_and_context_token():
    from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
    from tests.test_world_setting_batch_pipeline import (
        FakeBatchSpringApi,
        FakeBatchComparator,
        _batch,
        _merged_decision,
    )

    ctx = _input_context()
    key = f"provisional-world:{uuid4()}"
    batch = _batch().model_copy(
        update={
            "resolved_target_world_setting_ids": [],
            "resolved_provisional_subject_keys": [key],
            "analysis_context": ctx,
        }
    )

    class Spring(FakeBatchSpringApi):
        async def get_world_setting_comparison_batch_context(
            self,
            analysis_job_id,
            comparison_batch_id,
            lease_token,
            target_world_setting_ids,
            provisional_subject_keys=None,
        ):
            assert target_world_setting_ids == [] and provisional_subject_keys == [key]
            context = await super().get_world_setting_comparison_batch_context(
                analysis_job_id,
                comparison_batch_id,
                lease_token,
                [],
            )
            return context.model_copy(
                update={
                    "analysis_context": ctx,
                    "context_token": "d" * 64,
                    "targets": [
                        target.model_copy(
                            update={"world_setting_id": None, "provisional_subject_key": key}
                        )
                        for target in context.targets
                    ],
                    "exact_targets": [
                        target.model_copy(
                            update={"world_setting_id": None, "provisional_subject_key": key}
                        )
                        for target in context.exact_targets
                    ],
                }
            )

    class Comparator(FakeBatchComparator):
        async def compare_batch(self, category, candidates, targets, ordered_context=False):
            assert ordered_context
            assert all(target.world_setting_id is None for target in targets)
            return await super().compare_batch(category, candidates, targets)

    spring = Spring([batch])
    pipeline = WorldSettingComparisonPipeline(spring, object(), Comparator(_merged_decision()))
    asyncio.run(pipeline._compare_batch_with_fresh_context(uuid4(), uuid4(), batch, 1, []))
    completion = spring.completions[0]
    assert completion.context_token == "d" * 64
    assert completion.decisions[0].target_world_setting_id is None
    assert completion.decisions[0].provisional_subject_key == key
    assert completion.context_versions[0].world_setting_id is None
    assert completion.context_versions[0].provisional_subject_key == key


def test_worker_character_stage_passes_explicit_context_and_fenced_semantic_bindings():
    from app.analysis.character_subject_resolver import CharacterSubjectResolver
    from app.worker.analysis_job_worker import AnalysisJobWorker
    from tests.test_analysis_job_worker import FakeSpringWorkerClient, _chunk

    payload = WorkerAnalysisJobPayload.model_validate(_ordered_payload())
    client = RecordingClient(
        {
            "candidates": [
                _valid_discovery_payload(provider_payload=True),
                _valid_setting_payload(provider_payload=True),
            ]
        },
        {
            "resolutions": [
                {"candidate_ref": "C1", "target_ref": "D1"},
                {"candidate_ref": "C2", "target_ref": "D1"},
            ]
        },
    )

    class Store:
        request = None

        def replace_candidates_for_analysis_job(self, **kwargs):
            self.request = kwargs
            return kwargs["save_items"]

    store = Store()
    spring = FakeSpringWorkerClient(payload)
    worker = AnalysisJobWorker(
        spring_client=spring,
        setting_extractor=CharacterSettingExtractor(llm_client=client, model="test-model"),
        subject_resolver=CharacterSubjectResolver(llm_client=client, model="test-model"),
        setting_candidate_service=store,
    )

    async def run():
        try:
            return await worker._run_character_stage(
                payload,
                [_chunk(0, "Synthetic Character appeared. Synthetic Character reached level 12.")],
                None,
                [],
                DEFAULT_SCHEMA_HINTS,
            )
        finally:
            await worker.aclose()

    summary = asyncio.run(run())
    assert summary["candidateCount"] == 2
    assert store.request["ordered_write_context"].context == payload.analysis_context
    discovery, setting = store.request["save_items"]
    assert (
        discovery.ordered_binding.provisional_subject_key
        == setting.ordered_binding.provisional_subject_key
    )
    assert '"characters"' in client.requests[0]["user_prompt"]
    assert str(payload.analysis_context.run_id) not in client.requests[0]["user_prompt"]
    assert len(client.requests) == 1
    assert summary["subjectFallbackCallCount"] == 0


@pytest.mark.parametrize("missing", ["provenance", "source_episode"])
def test_ordered_extraction_rejects_incomplete_item_provenance_before_provider(missing):
    client = RecordingClient()
    character = _temporary_character()
    provenance = (
        None
        if missing == "provenance"
        else AnalysisStateProvenance(confirmation_status="PROVISIONAL")
    )
    character = character.model_copy(
        update={
            "active_statuses": [
                character.active_statuses[0].model_copy(update={"provenance": provenance})
            ]
        }
    )
    extractor = CharacterSettingExtractor(llm_client=client, model="test-model")
    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            extractor.extract_from_chunk(
                source_chunk_id=CHUNK_ID,
                chunk_text="세룸은 회복했다.",
                schema_hints=DEFAULT_SCHEMA_HINTS,
                ordered_context=OrderedExtractionContext(_input_context(), (), (character,)),
            )
        )
    assert client.requests == []


@pytest.mark.parametrize("ordered", [True, False])
@pytest.mark.parametrize("completion_fails", [True, False])
def test_completed_checkpoints_resume_only_ordered_pending_comparisons(ordered, completion_fails):
    import httpx
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
    from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
    from app.domain.enums import AnalysisJobCheckpointStage
    from app.worker.analysis_job_worker import AnalysisJobWorker
    from app.clients.exceptions import SpringWorkerHttpError
    from tests.test_analysis_job_worker import FakeSpringWorkerClient, FakeEpisodeChunkingService, _chunk

    payload = WorkerAnalysisJobPayload.model_validate(_ordered_payload()) if ordered else _payload()
    checkpoint = AnalysisJobCheckpointStage.WORLD_COMPARISONS_FINISHED
    payload = payload.model_copy(update={"checkpoint_stage": checkpoint, "character_setting_schemas": []})
    calls = []

    class PendingOnlyPipeline:
        def __init__(self, name, result):
            self.name, self.result = name, result

        async def process_all(self, analysis_job_id, lease_token, analysis_context=None):
            assert analysis_context == payload.analysis_context
            calls.append(self.name)
            return self.result

    class Spring(FakeSpringWorkerClient):
        async def complete(self, *args, **kwargs):
            await super().complete(*args, **kwargs)
            if completion_fails:
                request = httpx.Request("POST", "https://synthetic.invalid/complete")
                raise SpringWorkerHttpError("synthetic persistence rejected", request=request,
                                           response=httpx.Response(409, request=request))

    spring = Spring(payload)
    chunks = FakeEpisodeChunkingService([_chunk(0, "synthetic stored chunk")])
    worker = AnalysisJobWorker(
        spring_client=spring,
        episode_chunk_service=chunks,
        chunking_service=object(),
        setting_extractor=object(),
        world_setting_extractor=object(),
        character_fact_comparison_pipeline=PendingOnlyPipeline(
            "character", CharacterFactComparisonRunResult(0, 0)
        ),
        world_setting_comparison_pipeline=PendingOnlyPipeline(
            "world", WorldSettingComparisonRunResult(0, 0)
        ),
    )

    async def run():
        try:
            if completion_fails:
                with pytest.raises(SpringWorkerHttpError):
                    await worker.run_once()
            else:
                await worker.run_once()
        finally:
            await worker.aclose()

    asyncio.run(run())
    assert calls == (["character", "world"] if ordered else [])
    assert [step for _, step, _ in spring.progress_calls] == (
        ["PERSISTING", "CHARACTER_FACT_COMPARISON", "WORLD_SETTING_COMPARISON", "PERSISTING"]
        if ordered else ["PERSISTING", "PERSISTING"]
    )
    assert all(value is None for value in spring.progress_checkpoints)
    assert payload.checkpoint_stage is checkpoint
    assert chunks.loaded_episode_ids == [payload.episode.episode_id]
    assert len(spring.complete_calls) == 1
    assert len(spring.fail_calls) == int(completion_fails)
    if completion_fails:
        assert spring.fail_calls[0][-1] == "UNEXPECTED_ERROR"


def test_ordered_character_batch_preserves_valid_decisions_and_does_not_claim_suffix():
    import httpx
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
    from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchResult
    from app.analysis.exceptions import OrderedAnalysisIncompleteError
    from app.exceptions.failure_classification import analysis_failure_code
    from tests.test_character_fact_batch import (
        FakeBatchComparator,
        FakeBatchSpring,
        _batch,
        _context,
        _decision,
    )

    ctx = _input_context()
    batch = _batch().model_copy(update={"analysis_context": ctx})
    context = _context(batch).model_copy(update={"analysis_context": ctx})
    comparator = FakeBatchComparator(
        [
            ComparisonValidationError("invalid batch"),
            CharacterFactComparisonBatchResult(
                decisions=[
                    _decision(
                        "C1", operation="ADD", resolved_key="status.부상", value="다리를 다침"
                    )
                ]
            ),
            httpx.TimeoutException("provider timeout"),
        ]
    )
    spring = FakeBatchSpring(batch, context)
    spring.batches.append(batch.model_copy(update={"comparison_batch_id": uuid4()}))
    with pytest.raises(OrderedAnalysisIncompleteError) as caught:
        asyncio.run(
            CharacterFactComparisonPipeline(spring, comparator).process_all(
                uuid4(), uuid4(), analysis_context=ctx
            )
        )
    assert analysis_failure_code(caught.value) == "LLM_NETWORK_ERROR"
    completion = spring.completions[0]
    assert [decision.candidate_ref for decision in completion.decisions] == ["C1"]
    assert [failure.candidate_ref for failure in completion.failures] == ["C2"]
    assert len(spring.batches) == 1


def test_ordered_world_failure_is_reported_before_stopping_suffix_claims():
    from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
    from tests.test_world_setting_batch_pipeline import FakeBatchSpringApi, _batch

    ctx = _input_context()
    batch = _batch().model_copy(update={"analysis_context": ctx})
    spring = FakeBatchSpringApi([batch, batch.model_copy(update={"comparison_batch_id": uuid4()})])

    class FailingBatchPipeline(WorldSettingComparisonPipeline):
        async def _compare_batch_with_fresh_context(self, *args, **kwargs):
            raise ComparisonValidationError("invalid ordered batch")

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            FailingBatchPipeline(spring, object(), object()).process_all(
                uuid4(), uuid4(), analysis_context=ctx
            )
        )
    assert spring.claim_count == 1
    assert len(spring.failures) == len(spring.batches) == 1


def test_ordered_failed_character_result_stops_before_world_extraction_and_checkpoint():
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
    from app.analysis.exceptions import OrderedAnalysisIncompleteError
    from app.domain.enums import AnalysisFailureCode
    from app.worker.analysis_job_worker import AnalysisJobWorker
    from tests.test_analysis_job_worker import FakeSpringWorkerClient

    payload = WorkerAnalysisJobPayload.model_validate(_ordered_payload())
    spring = FakeSpringWorkerClient(payload)
    calls = []

    class FailedComparison:
        async def process_all(self, *args, **kwargs):
            return CharacterFactComparisonRunResult(0, 1, AnalysisFailureCode.LLM_NETWORK_ERROR)

    class Worker(AnalysisJobWorker):
        async def _run_chunk_stage(self, *args):
            return [], {}

        async def _run_character_stage(self, *args):
            return {}

        async def _run_world_extraction_stage(self, *args):
            calls.append("world")
            return 0

    worker = Worker(spring_client=spring, character_fact_comparison_pipeline=FailedComparison())

    async def run():
        try:
            await worker._run_analysis_steps(payload)
        finally:
            await worker.aclose()

    with pytest.raises(OrderedAnalysisIncompleteError):
        asyncio.run(run())
    assert calls == spring.progress_calls == []


@pytest.mark.parametrize("ordered", [True, False])
def test_ordered_world_extraction_preserves_raw_same_name_subjects_until_resolution(ordered):
    from app.analysis.world_setting_extractor import WorldSettingExtractor
    from app.worker.analysis_job_worker import AnalysisJobWorker
    from tests.test_analysis_job_worker import FakeSpringWorkerClient, _chunk
    from tests.test_world_setting_extractor import _response

    payload = WorkerAnalysisJobPayload.model_validate(_ordered_payload()) if ordered else _payload()
    output = json.loads(_response(category="RACE"))
    client = RecordingClient(output, output)
    worker = AnalysisJobWorker(
        spring_client=FakeSpringWorkerClient(payload),
        world_setting_extractor=WorldSettingExtractor(llm_client=client, model="test-model"),
    )

    async def run():
        try:
            return await worker._run_world_extraction_stage(
                payload,
                [
                    _chunk(0, "바바리안은 혹한 지역에서 살아간다."),
                    _chunk(1, "바바리안은 혹한 지역에서 살아간다."),
                ],
                None,
            )
        finally:
            await worker.aclose()

    assert asyncio.run(run()) == (2 if ordered else 1)


def test_character_subject_input_keeps_prior_identity_evidence_and_settings_without_ids():
    from app.analysis.ordered_character_subjects import initial_ordered_subjects

    character = _temporary_character()
    targets = initial_ordered_subjects(OrderedExtractionContext(_input_context(), (), (character,)))
    (_, _), client = _resolve_subjects(
        {"resolutions": [{"candidate_ref": "C1", "target_ref": "K1"}]},
        subjects=targets,
        candidates=[ExtractedSettingCandidate.model_validate(_valid_setting_payload()).model_copy(
            update={"entity_name": "미상", "raw_entity_mention": "그는"})],
        previous="세룸이 앞서 자신을 소개했다.",
    )
    prompt = client.requests[0]["user_prompt"]
    assert character.identity_evidence[0].quote in prompt
    assert "status.부상" in prompt
    assert "넷째 아들" in prompt
    assert character.provisional_subject_key not in prompt


@pytest.mark.parametrize(
    "status,code",
    [
        (409, "SETTING_CANDIDATE_ORDERED_COMPARISON_FAILED"),
        (409, "WORLD_SETTING_ORDERED_COMPARISON_FAILED"),
        (422, "SETTING_CANDIDATE_COMPARISON_INPUT_LIMIT_EXCEEDED"),
        (422, "WORLD_SETTING_COMPARISON_INPUT_LIMIT_EXCEEDED"),
    ],
)
def test_ordered_claim_prevalidation_failure_is_never_a_deferrable_comparison(status, code):
    import httpx
    from app.clients.exceptions import SpringWorkerHttpError
    from app.exceptions.failure_classification import analysis_failure_code, comparison_failure_code

    request = httpx.Request("POST", "http://localhost/test-only")
    failure = SpringWorkerHttpError(
        "Ordered candidate prevalidation failed.",
        request=request,
        response=httpx.Response(status, request=request),
        status_code=status,
        spring_error_code=code,
    )
    assert analysis_failure_code(failure) == "UNEXPECTED_ERROR"
    assert comparison_failure_code(failure) == "UNEXPECTED_ERROR"
