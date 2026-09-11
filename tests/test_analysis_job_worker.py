import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionResult,
)
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.domain.enums import AnalysisJobCheckpointStage, EpisodeProcessingStatus
from app.embeddings.exceptions import (
    EmbeddingDataIntegrityError,
    RecoverableEmbeddingProviderError,
)
from app.embeddings.services.episode_chunk_embedding import EpisodeChunkEmbeddingResult
from app.llm.responses import LlmTextResponse
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import WorkerAnalysisEpisodePayload, WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import (
    AnalysisJobWorker,
    WorkerRunResult,
    WorkerRunSummary,
    _is_explicit_inactive_status_candidate,
)

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000006")
ZERO_CHARACTER_COMPARISON_METRICS = {
    "characterComparisonRequestCount": 0,
    "characterComparisonBatchCount": 0,
    "characterComparisonAverageCandidatesPerBatch": 0.0,
    "characterComparisonMaxCandidatesPerBatch": 0,
    "characterComparisonProviderSegmentCount": 0,
    "characterComparisonBatchFallbackCandidateCount": 0,
    "characterComparisonBatchValidationFailureCount": 0,
    "characterComparisonStaleBatchRetryCount": 0,
    "characterComparisonProviderRequestCount": 0,
    "characterComparisonProviderLatencyMs": 0,
    "characterComparisonInputTokenCount": 0,
    "characterComparisonCachedInputTokenCount": 0,
    "characterComparisonOutputTokenCount": 0,
    "characterComparisonAverageInputTokensPerCandidate": 0.0,
    "characterComparisonAverageOutputTokensPerCandidate": 0.0,
}
ZERO_WORLD_COMPARISON_METRICS = {
    "worldComparisonBatchCount": 0,
    "worldComparisonDecisionCount": 0,
    "worldComparisonClusterCount": 0,
    "averageCandidatesPerBatch": 0.0,
    "averageCandidatesPerCluster": 0.0,
    "clusteredCandidateCount": 0,
    "singletonCandidateCount": 0,
    "batchValidationFailureCount": 0,
    "staleBatchRetryCount": 0,
    "clusterOverflowOrReviewRequiredCount": None,
    "worldComparisonProviderRequestCount": 0,
    "worldComparisonProviderLatencyMs": 0,
    "worldComparisonInputTokenCount": 0,
    "worldComparisonCachedInputTokenCount": 0,
    "worldComparisonOutputTokenCount": 0,
    "worldComparisonSubjectResolutionUsage": {
        "providerRequestCount": 0,
        "providerLatencyMs": 0,
        "inputTokenCount": 0,
        "cachedInputTokenCount": 0,
        "outputTokenCount": 0,
    },
    "worldComparisonBatchUsages": [],
    "worldComparisonClusterUsages": [],
}
SCHEMA_HINTS = (
    CharacterSettingSchemaHint(
        schema_key="stats.strength",
        display_name="근력",
        attribute_pattern=None,
        aliases=("근력", "힘", "strength"),
        value_type="NUMBER",
    ),
    CharacterSettingSchemaHint(
        schema_key="stats.strength",
        display_name="작품 근력",
        attribute_pattern=None,
        aliases=("완력",),
        value_type="NUMBER",
    ),
)


def test_worker_returns_without_error_when_claimable_job_does_not_exist() -> None:
    spring_client = FakeSpringWorkerClient(payload=None)
    worker = SuccessfulAnalysisJobWorker(spring_client=spring_client)

    result = _run_once(worker)

    assert result.claimed is False
    assert result.analysis_job_id is None
    assert spring_client.claim_called is True
    assert spring_client.progress_calls == []
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == []


def test_worker_reports_progress_and_complete_to_spring() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = SuccessfulAnalysisJobWorker(
        spring_client=spring_client,
        extraction_model_name="extraction-model",
        subject_resolution_model_name="subject-resolution-model",
        comparison_model_name="comparison-model",
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert result.analysis_job_id == ANALYSIS_JOB_ID
    assert spring_client.claim_model_name == "extraction-model"
    assert spring_client.progress_calls == [
        (ANALYSIS_JOB_ID, "SETTING_EXTRACTION", EpisodeProcessingStatus.ANALYZING)
    ]
    assert spring_client.complete_calls == [
        (ANALYSIS_JOB_ID, '{"candidateCount": 0}', None, None),
    ]
    assert spring_client.fail_calls == []


def test_worker_routes_each_llm_stage_to_its_configured_model() -> None:
    async def scenario() -> None:
        worker = AnalysisJobWorker(
            spring_client=FakeSpringWorkerClient(payload=None),
            extraction_model_name="extraction-model",
            subject_resolution_model_name="subject-resolution-model",
            comparison_model_name="comparison-model",
            llm_provider_client=object(),
        )
        try:
            setting_extractor = worker._get_setting_extractor(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
            character_subject_resolver = worker._get_subject_resolver(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
            world_setting_extractor = worker._get_world_setting_extractor(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
            world_setting_pipeline = worker._get_world_setting_comparison_pipeline(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
            character_fact_pipeline = worker._get_character_fact_comparison_pipeline(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )

            assert setting_extractor.model == "extraction-model"
            assert setting_extractor.llm_client.default_model == "extraction-model"
            assert character_subject_resolver.model == "subject-resolution-model"
            assert character_subject_resolver.llm_client.default_model == "subject-resolution-model"
            assert world_setting_extractor.model == "extraction-model"
            assert world_setting_extractor.llm_client.default_model == "extraction-model"
            assert world_setting_pipeline.subject_resolver.model == "subject-resolution-model"
            assert (
                world_setting_pipeline.subject_resolver.llm_client.default_model
                == "subject-resolution-model"
            )
            assert world_setting_pipeline.comparator.model == "comparison-model"
            assert world_setting_pipeline.comparator.llm_client.default_model == "comparison-model"
            assert character_fact_pipeline.comparator.model == "comparison-model"
            assert character_fact_pipeline.comparator.llm_client.default_model == "comparison-model"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_worker_reports_fail_to_spring_when_analysis_fails() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = FailingAnalysisJobWorker(spring_client=spring_client)

    with pytest.raises(RuntimeError):
        _run_once(worker)

    assert spring_client.progress_calls == [
        (ANALYSIS_JOB_ID, "SETTING_EXTRACTION", EpisodeProcessingStatus.ANALYZING)
    ]
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [
        (ANALYSIS_JOB_ID, "LLM response parse failed.", "UNEXPECTED_ERROR")
    ]


def test_worker_reports_token_quota_failure_with_typed_code() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = QuotaFailingAnalysisJobWorker(spring_client=spring_client)

    with pytest.raises(AiTokenQuotaExhaustedError):
        _run_once(worker)

    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [
        (
            ANALYSIS_JOB_ID,
            "AI token quota is exhausted.",
            "AI_TOKEN_QUOTA_EXHAUSTED",
        )
    ]


@pytest.mark.parametrize("missing_field", ["batch_id", "episode"])
def test_setting_extraction_rejects_payload_without_batch_or_episode(missing_field: str) -> None:
    payload = _payload().model_copy(update={missing_field: None})
    spring_client = FakeSpringWorkerClient(payload=payload)
    worker = SuccessfulAnalysisJobWorker(spring_client=spring_client)

    with pytest.raises(ValueError, match="must include batchId and episode"):
        _run_once(worker)

    assert spring_client.progress_calls == []
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [
        (
            ANALYSIS_JOB_ID,
            "Setting-extraction job must include batchId and episode.",
            "UNEXPECTED_ERROR",
        )
    ]


def test_worker_cancellation_leaves_job_for_lease_recovery_instead_of_reporting_failure() -> None:
    async def scenario() -> FakeSpringWorkerClient:
        payload = _payload()
        spring_client = FakeSpringWorkerClient(payload=payload)
        worker = CancellableAnalysisJobWorker(spring_client=spring_client)
        task = asyncio.create_task(worker.process_claimed(payload))
        await worker.started.wait()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await worker.aclose()
        return spring_client

    spring_client = asyncio.run(scenario())

    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == []


def test_worker_close_attempts_every_owned_resource_after_one_close_fails() -> None:
    async def scenario() -> tuple[
        FailingCloseResource, RecordingCloseResource, RecordingCloseResource
    ]:
        provider = FailingCloseResource()
        spring_client = FakeSpringWorkerClient(payload=None)
        spring_close = RecordingCloseResource()
        blocking_executor = RecordingCloseResource()
        spring_client.aclose = spring_close.aclose
        worker = AnalysisJobWorker(
            spring_client=spring_client,
            llm_provider_client=provider,
            blocking_io_executor=blocking_executor,
        )
        # 주입 객체는 기본적으로 caller 소유다. 이 테스트에서는 production 소유 경로를 재현한다.
        worker._owns_llm_provider_client = True
        worker._owns_spring_client = True
        worker._owns_blocking_io_executor = True

        with pytest.raises(RuntimeError, match="provider close failed"):
            await worker.aclose()
        return provider, spring_close, blocking_executor

    provider, spring_close, blocking_executor = asyncio.run(scenario())

    assert provider.closed is True
    assert spring_close.closed is True
    assert blocking_executor.closed is True


def test_worker_injects_bounded_executor_into_embedding_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unused_session_factory() -> None:
        raise AssertionError("This test must not access the database.")

    monkeypatch.setattr(
        "app.worker.analysis_job_worker.get_session_maker",
        lambda: unused_session_factory,
    )

    async def scenario() -> None:
        worker = AnalysisJobWorker(
            spring_client=FakeSpringWorkerClient(payload=None),
            embedding_generation_enabled=True,
        )
        try:
            service = worker._get_episode_chunk_embedding_service(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
            assert service.blocking_runner.__self__ is worker._blocking_io_executor
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_worker_fails_before_data_changes_when_claim_has_no_character_schemas() -> None:
    # 이전 Spring payload도 job ID까지는 파싱하되, Schema 없이 빈 후보로 기존 데이터를
    # 교체하지 않도록 청킹·임베딩·추출·후보 저장 전에 분석 실패로 보고한다.
    payload = _payload().model_copy(update={"character_setting_schemas": []})
    spring_client = FakeSpringWorkerClient(payload=payload)
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")])
    embedding_service = FakeEpisodeChunkEmbeddingService()
    setting_extractor = FakeSettingExtractor(candidate_groups=[[]])
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=embedding_service,
        setting_extractor=setting_extractor,
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
    )

    with pytest.raises(
        ValueError,
        match="claim must include at least one characterSettingSchemas entry",
    ):
        _run_once(worker)

    assert spring_client.progress_calls == [
        (ANALYSIS_JOB_ID, "SETTING_EXTRACTION", EpisodeProcessingStatus.ANALYZING)
    ]
    assert chunking_service.requested_episode_ids == []
    assert chunking_service.requested_content_s3_keys == []
    assert embedding_service.requested_chunk_ids == []
    assert setting_extractor.requests == []
    assert setting_candidate_service.request is None
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [
        (
            ANALYSIS_JOB_ID,
            "Analysis job claim must include at least one characterSettingSchemas entry.",
            "UNEXPECTED_ERROR",
        )
    ]


def test_worker_chunks_episode_content_and_extracts_candidates() -> None:
    # 실제 OpenAI 호출은 FakeSettingExtractor로 대체한다.
    # 여기서는 "LLM 결과가 이미 나왔다"는 가정 아래 Worker가 저장 전에 quote offset을 보정하는지 본다.
    chunk_text = (
        "던전의 입구에는 축축한 안개가 내려앉아 있었다.\n\n"
        "비요른은 1레벨 바바리안이다. 그는 낡은 도끼를 고쳐 쥐고 통로 안쪽을 노려보았다."
    )
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, chunk_text, start_offset=100)])
    extracted_candidates = [
        _candidate(chunking_service.chunks[0].id, attribute_name="level", raw_entity_mention="비요른"),
        _candidate(chunking_service.chunks[0].id, attribute_name="class", raw_entity_mention="비요른"),
    ]
    setting_extractor = FakeSettingExtractor(candidate_groups=[extracted_candidates])
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService()
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
        embedding_generation_enabled=True,
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert result.analysis_job_id == ANALYSIS_JOB_ID
    assert result.work_id == WORK_ID
    assert result.work_title == "빛나는 검사 로맨스"
    assert result.episode_count == 1
    assert chunking_service.requested_episode_ids == [EPISODE_ID]
    assert chunking_service.requested_content_s3_keys == ["works/work-id/episodes/episode-id.txt"]
    assert episode_chunk_embedding_service.requested_chunk_ids == [[chunking_service.chunks[0].id]]
    assert setting_extractor.requests == [
        {
            "source_chunk_id": chunking_service.chunks[0].id,
            "chunk_text": chunk_text,
            "analysis_job_id": ANALYSIS_JOB_ID,
            "episode_no": 1,
            "episode_title": "첫 번째 회차",
            "schema_hints": SCHEMA_HINTS,
            "prior_status_observations": (),
            "narrative_context": {
                "previous_episode": None, "previous_chunk": None, "next_chunk": None,
            },
            "known_characters": (
                KnownCharacter(
                    character_id=UUID("00000000-0000-0000-0000-000000000005"),
                    name="비요른 얀델",
                ),
            ),
        }
    ]
    assert setting_candidate_service.request == {
        "work_id": WORK_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "episode_ids": [EPISODE_ID, EPISODE_ID],
        "known_character_names": ["비요른 얀델"],
        "candidate_count": 2,
    }
    saved_candidate = setting_candidate_service.saved_candidates[0]
    expected_start_offset = 100 + chunk_text.index("비요른은 1레벨 바바리안이다.")
    assert saved_candidate.attribute_name == "level"
    assert saved_candidate.evidence_spans[0].start_offset == expected_start_offset
    assert saved_candidate.evidence_spans[0].end_offset == expected_start_offset + len(
        "비요른은 1레벨 바바리안이다."
    )
    assert extracted_candidates[0].evidence_spans[0].start_offset is None
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 1,
        "embeddedChunkCount": 1,
        "embeddingFailedChunkCount": 0,
        "embeddingSkippedChunkCount": 0,
        "candidateCount": 2,
        "subjectFallbackCallCount": 0,
        "subjectFallbackResolvedCount": 0,
        "subjectFallbackUnresolvedCount": 0,
        "statusContextCharacterCount": 0,
        "statusContextEntryCount": 0,
        "statusInactiveCandidateCount": 0,
        "characterFactComparisonCompletedCount": 0,
        "characterFactComparisonFailedCount": 0,
        **ZERO_CHARACTER_COMPARISON_METRICS,
        "worldSettingCandidateCount": 0,
        "worldSettingComparisonCompletedCount": 0,
        "worldSettingComparisonFailedCount": 0,
        **ZERO_WORLD_COMPARISON_METRICS,
    }
    assert spring_client.fail_calls == []


def test_worker_passes_all_active_statuses_to_each_chunk_and_reports_metrics() -> None:
    payload_json = _payload().model_dump(by_alias=True, mode="json")
    payload_json["knownCharacters"][0]["activeStatuses"] = [
        {
            "factKey": "status.오른발_부상",
            "factValue": "오른발이 크게 다쳐 걷기 어려움",
        },
        {
            "factKey": "status.마비독",
            "factValue": None,
        },
    ]
    payload = WorkerAnalysisJobPayload.model_validate(payload_json)
    chunks = [
        _chunk(0, "포션을 마신 뒤 오른발로 디딜 수 있었다."),
        _chunk(1, "그는 다시 두 발로 걸었다."),
    ]
    inactive_candidate = _candidate(
        chunks[0].id,
        attribute_name="status.회복",
        quote="오른발로 디딜 수 있었다.",
    ).model_copy(
        update={
            "attribute_value": "오른발 기능이 회복됨",
            "value_type": "JSON",
            "value_json": {"name": "회복", "active": False},
        }
    )
    setting_extractor = FakeSettingExtractor(candidate_groups=[[inactive_candidate], []])
    worker = AnalysisJobWorker(
        spring_client=FakeSpringWorkerClient(payload=payload),
        chunking_service=FakeEpisodeChunkingService(chunks=chunks),
        setting_extractor=setting_extractor,
        subject_resolver=FakeSubjectResolver(
            result=SubjectResolutionResult(candidates=[inactive_candidate])
        ),
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=FakeSettingCandidateService(),
    )

    result = _run_once(worker)

    assert result.claimed is True
    expected_character = KnownCharacter(
        character_id=UUID("00000000-0000-0000-0000-000000000005"),
        name="비요른 얀델",
        active_statuses=(
            ActiveCharacterStatus(
                fact_key="status.오른발_부상",
                fact_value="오른발이 크게 다쳐 걷기 어려움",
            ),
            ActiveCharacterStatus(
                fact_key="status.마비독",
                fact_value=None,
            ),
        ),
    )
    assert [request["known_characters"] for request in setting_extractor.requests] == [
        (expected_character,),
        (expected_character,),
    ]
    spring_client = worker.spring_client
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary["statusContextCharacterCount"] == 1
    assert summary["statusContextEntryCount"] == 2
    assert summary["statusInactiveCandidateCount"] == 1


def test_inactive_status_metric_requires_json_boolean_false() -> None:
    base = _candidate(
        UUID("00000000-0000-0000-0000-000000000100"),
        attribute_name="status.회복",
    ).model_copy(update={"value_type": "JSON"})

    assert _is_explicit_inactive_status_candidate(
        base.model_copy(update={"value_json": {"active": False}})
    )
    assert not _is_explicit_inactive_status_candidate(
        base.model_copy(update={"value_json": {"active": "false"}})
    )
    assert not _is_explicit_inactive_status_candidate(
        base.model_copy(update={"attribute_name": "item.포션", "value_json": {"active": False}})
    )


def test_chunks_ready_retry_reuses_stored_chunks_after_zero_candidate_failure() -> None:
    """후보 저장 전 실패한 재현 Job은 청크를 교체하지 않고 추출부터 안전하게 재개한다."""

    chunk = _chunk(0, "비요른은 1레벨 바바리안이다.")
    payload = _payload().model_copy(
        update={"checkpoint_stage": AnalysisJobCheckpointStage.CHUNKS_READY}
    )
    spring_client = FakeSpringWorkerClient(payload=payload)
    chunk_store = FakeEpisodeChunkingService(chunks=[chunk])
    candidate = _candidate(chunk.id, attribute_name="level")
    setting_candidate_service = FakeSettingCandidateService()
    assert setting_candidate_service.saved_candidates == []

    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunk_store,
        episode_chunk_service=chunk_store,
        episode_chunk_embedding_service=FakeEpisodeChunkEmbeddingService(),
        setting_extractor=FakeSettingExtractor(candidate_groups=[[candidate]]),
        subject_resolver=FakeSubjectResolver(
            result=SubjectResolutionResult(candidates=[candidate])
        ),
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert chunk_store.loaded_episode_ids == [EPISODE_ID]
    assert chunk_store.requested_episode_ids == []
    assert chunk_store.requested_content_s3_keys == []
    assert setting_candidate_service.saved_candidates == [candidate]
    assert setting_candidate_service.request["candidate_count"] == 1
    assert spring_client.fail_calls == []


def test_worker_applies_subject_resolution_before_saving_candidates() -> None:
    current_chunk_text = "나는 1레벨 바바리안으로 깨어났다."
    resolved_candidate = _candidate(
        UUID("00000000-0000-0000-0000-000000000100"),
        attribute_name="level",
        entity_name="비요른 얀델",
        raw_entity_mention="나",
        quote="나는 1레벨 바바리안으로 깨어났다.",
    )
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(
        chunks=[
            _chunk(0, "비요른 얀델은 낡은 도끼를 들고 있었다."),
            _chunk(1, current_chunk_text),
            _chunk(2, "주변에는 다른 인물이 없었다."),
        ]
    )
    setting_extractor = FakeSettingExtractor(
        candidate_groups=[
            [],
            [
                _candidate(
                    chunking_service.chunks[1].id,
                    attribute_name="level",
                    entity_name="미상",
                    raw_entity_mention="나",
                    quote="나는 1레벨 바바리안으로 깨어났다.",
                )
            ],
            [],
        ]
    )
    subject_resolver = FakeSubjectResolver(
        result=SubjectResolutionResult(
            candidates=[resolved_candidate],
            fallback_call_count=1,
            fallback_resolved_count=1,
            fallback_unresolved_count=0,
        )
    )
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService()
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        subject_resolver=subject_resolver,
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
        embedding_generation_enabled=True,
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert subject_resolver.requests == [
        {
            "previous_chunk_text": "비요른 얀델은 낡은 도끼를 들고 있었다.",
            "current_chunk_text": current_chunk_text,
            "next_chunk_text": "주변에는 다른 인물이 없었다.",
            "previous_episode_text": None,
            "candidate_count": 1,
            "known_character_names": ["비요른 얀델"],
        }
    ]
    assert setting_candidate_service.saved_candidates == [resolved_candidate]
    assert all(request["schema_hints"] == SCHEMA_HINTS for request in setting_extractor.requests)
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 3,
        "embeddedChunkCount": 3,
        "embeddingFailedChunkCount": 0,
        "embeddingSkippedChunkCount": 0,
        "candidateCount": 1,
        "subjectFallbackCallCount": 1,
        "subjectFallbackResolvedCount": 1,
        "subjectFallbackUnresolvedCount": 0,
        "statusContextCharacterCount": 0,
        "statusContextEntryCount": 0,
        "statusInactiveCandidateCount": 0,
        "characterFactComparisonCompletedCount": 0,
        "characterFactComparisonFailedCount": 0,
        **ZERO_CHARACTER_COMPARISON_METRICS,
        "worldSettingCandidateCount": 0,
        "worldSettingComparisonCompletedCount": 0,
        "worldSettingComparisonFailedCount": 0,
        **ZERO_WORLD_COMPARISON_METRICS,
    }


def test_worker_reconciles_names_after_all_chunks_and_preserves_candidate_source_metadata() -> None:
    first_text = "세나는 검을 들었다."
    alias_text = "레이의 딸 세나는 세나라고 불리며 마을을 지켰다."
    chunks = [
        _chunk(0, first_text, start_offset=100),
        _chunk(1, alias_text, start_offset=300),
        _chunk(2, "그날 저녁 마을은 조용했다.", start_offset=500),
    ]
    originals = [
        _candidate(chunks[0].id, "level", entity_name="세나", raw_entity_mention="세나", quote=first_text),
        _candidate(chunks[1].id, "status.발_부상", entity_name="레이의 딸 세나",
                   raw_entity_mention="레이의 딸 세나", quote=alias_text).model_copy(update={
                       "attribute_value": "발 부상이 회복됨", "value_type": "JSON",
                       "value_json": {"name": "발 부상", "active": False},
                   }),
        ExtractedSettingCandidate(
            source_chunk_id=chunks[1].id, candidate_kind="CHARACTER_DISCOVERY",
            entity_name="레이의 딸 세나", raw_entity_mention="레이의 딸 세나",
            evidence_spans=[ExtractedEvidenceSpan(quote=alias_text)], confidence=0.8,
        ),
    ]
    original_dumps = [candidate.model_dump() for candidate in originals]
    events = []
    identity_inputs = []

    class RecordingExtractor(FakeSettingExtractor):
        async def extract_from_chunk(self, **kwargs):
            events.append(("extract", kwargs["source_chunk_id"]))
            return await super().extract_from_chunk(**kwargs)

    class IdentityClient:
        def __init__(self):
            self.requests = []

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            if kwargs.get("prompt_cache_key") == "subject-resolution:v5":
                subject_input = json.loads(kwargs["user_prompt"].split("\n\n", 1)[1])
                return LlmTextResponse(text=json.dumps({"resolutions": [
                    {"candidate_id": item["candidate_id"],
                     "resolved_entity_name": item["draft_entity_name"],
                     "reason": "현재 청크의 명시된 신규 이름을 확인했다."}
                    for item in subject_input["candidates"]
                ]}, ensure_ascii=False))
            return LlmTextResponse(text=json.dumps({"decisions": [
                {"pair_ref": "R1", "relation": "SAME_PERSON",
                 "evidence": [{"source": "current_episode", "quote": alias_text}]},
            ]}, ensure_ascii=False))

    class RecordingSubjectResolver(CharacterSubjectResolver):
        async def resolve_candidates(self, context, candidates, known_characters):
            events.append(("subject", context.current_chunk_text))
            return await super().resolve_candidates(context, candidates, known_characters)

        async def reconcile_episode_names(self, context, candidates, known_characters):
            events.append(("identity", len(candidates)))
            identity_inputs.append((context, [candidate.model_dump() for candidate in candidates]))
            return await super().reconcile_episode_names(context, candidates, known_characters)

    class RecordingCandidateService(FakeSettingCandidateService):
        def replace_candidates_for_analysis_job(self, **kwargs):
            events.append(("save", len(kwargs["save_items"])))
            self.save_items = kwargs["save_items"]
            return super().replace_candidates_for_analysis_job(**kwargs)

    payload = _payload().model_copy(update={"known_characters": []})
    payload.previous_episode = payload.episode.model_copy(update={
        "episode_id": UUID(int=99), "content_s3_key": "previous-source.txt",
    })
    payload.episode = payload.episode.model_copy(update={"episode_no": 2})
    storage = FakeEpisodeChunkingService(chunks)
    storage.context_text = "레이는 세나의 아버지였다."
    client = IdentityClient()
    saved = RecordingCandidateService()
    spring = FakeSpringWorkerClient(payload)
    worker = AnalysisJobWorker(
        spring_client=spring, chunking_service=storage,
        setting_extractor=RecordingExtractor([[originals[0]], originals[1:], []]),
        subject_resolver=RecordingSubjectResolver(llm_client=client, model="gpt-5.6-luna"),
        setting_candidate_service=saved, world_setting_extractor=FakeWorldSettingExtractor(),
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert spring.fail_calls == []
    assert events == [
        ("extract", chunks[0].id), ("subject", first_text),
        ("extract", chunks[1].id), ("subject", alias_text),
        ("extract", chunks[2].id), ("subject", chunks[2].chunk_text),
        ("identity", 3), ("save", 3),
    ]
    assert len(client.requests) == 3
    context, candidates_before_identity = identity_inputs[0]
    assert context.current_chunk_text == "\n\n".join(chunk.chunk_text for chunk in chunks)
    assert context.previous_episode_text == storage.context_text
    assert context.previous_chunk_text is context.next_chunk_text is None
    assert storage.context_keys == ["previous-source.txt"]
    assert [candidate.entity_name for candidate in saved.saved_candidates] == ["세나", "세나", "세나"]
    for before, after in zip(candidates_before_identity, saved.saved_candidates, strict=True):
        assert after.model_dump(exclude={"entity_name"}) == {
            key: value for key, value in before.items() if key != "entity_name"
        }
    assert [item.candidate.source_chunk_id for item in saved.save_items] == [
        chunks[0].id, chunks[1].id, chunks[1].id,
    ]
    assert all(item.episode_id == EPISODE_ID for item in saved.save_items)
    assert all(item.source_content_s3_key == payload.episode.content_s3_key for item in saved.save_items)
    assert [candidate.evidence_spans[0].start_offset for candidate in saved.saved_candidates] == [100, 300, 300]
    assert [candidate.evidence_spans[0].end_offset for candidate in saved.saved_candidates] == [
        100 + len(first_text), 300 + len(alias_text), 300 + len(alias_text),
    ]
    assert [candidate.model_dump() for candidate in originals] == original_dumps
    summary = json.loads(spring.complete_calls[0][1])
    assert summary["candidateCount"] == 3
    assert summary["subjectFallbackCallCount"] == 3
    assert summary["subjectFallbackResolvedCount"] == 4
    assert summary["statusInactiveCandidateCount"] == 1


def test_previous_episode_is_read_once_without_rechunking_and_used_only_as_context() -> None:
    payload = _payload()
    payload.previous_episode = payload.episode.model_copy()
    payload.episode = payload.episode.model_copy(update={
        "episode_no": 2, "episode_id": UUID(int=999),
        "content_s3_key": "current-source.txt",
    })
    chunk = _chunk(0, "나는 검을 들었다.")
    storage = FakeEpisodeChunkingService([chunk])
    storage.context_text = "폐기할 앞부분" * 3000 + "카엘, 가자. 나는 대답했다."
    extractor = FakeSettingExtractor([[_candidate(
        chunk.id, attribute_name="level", entity_name="미상",
        raw_entity_mention="나는", quote=chunk.chunk_text,
    )]])
    resolver = FakeSubjectResolver(SubjectResolutionResult(candidates=[]))
    spring = FakeSpringWorkerClient(payload)
    worker = AnalysisJobWorker(
        spring_client=spring, chunking_service=storage,
        setting_extractor=extractor, subject_resolver=resolver,
        setting_candidate_service=FakeSettingCandidateService(),
        world_setting_extractor=FakeWorldSettingExtractor(),
    )

    _run_once(worker)

    assert spring.fail_calls == []
    assert storage.context_keys == [payload.previous_episode.content_s3_key]
    assert storage.requested_episode_ids == [payload.episode.episode_id]
    reference = storage.context_text[-14000:]
    assert extractor.requests[0]["narrative_context"]["previous_episode"] == reference
    assert resolver.requests[0]["previous_episode_text"] == reference
    assert extractor.requests[0]["chunk_text"] == chunk.chunk_text
    assert len(reference) == 14000


@pytest.mark.parametrize("reference_no", [1, 3, 4])
def test_previous_episode_context_rejects_nonadjacent_or_future_source(reference_no) -> None:
    payload = _payload()
    payload.episode = payload.episode.model_copy(update={"episode_no": 3})
    payload.previous_episode = payload.episode.model_copy(update={
        "episode_no": reference_no, "episode_id": UUID(int=999),
    })
    storage = FakeEpisodeChunkingService([])
    worker = AnalysisJobWorker(spring_client=FakeSpringWorkerClient(payload), chunking_service=storage)
    with pytest.raises(ValueError, match="immediately preceding"):
        asyncio.run(worker._read_previous_episode_context(payload))
    assert storage.context_keys == []


def test_worker_preserves_subject_fallback_unresolved_candidate() -> None:
    # fallback이 주체를 특정하지 못해도 후보를 버리지 않고 저장 Service까지 전달한다.
    current_chunk_text = "의사는 아니지만 내겐 블랙아웃 증상이 있다."
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, current_chunk_text)])
    unresolved_candidate = _candidate(
        chunking_service.chunks[0].id,
        attribute_name="status.블랙아웃",
        entity_name="미상",
        raw_entity_mention="내려다 본 손",
        quote="내겐 블랙아웃 증상이 있다.",
    )
    subject_resolver = FakeSubjectResolver(
        result=SubjectResolutionResult(
            candidates=[unresolved_candidate],
            fallback_call_count=1,
            fallback_resolved_count=0,
            fallback_unresolved_count=1,
        )
    )
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=FakeEpisodeChunkEmbeddingService(),
        setting_extractor=FakeSettingExtractor(candidate_groups=[[unresolved_candidate]]),
        subject_resolver=subject_resolver,
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
        embedding_generation_enabled=True,
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert setting_candidate_service.saved_candidates == [unresolved_candidate]
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary["candidateCount"] == 1
    assert summary["subjectFallbackCallCount"] == 1
    assert summary["subjectFallbackResolvedCount"] == 0
    assert summary["subjectFallbackUnresolvedCount"] == 1


def test_worker_skips_chunk_embedding_by_default_and_completes_extraction() -> None:
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")])
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService()
    extracted_candidate = _candidate(
        chunking_service.chunks[0].id,
        attribute_name="class",
        quote="비요른은 전사다.",
    )
    setting_extractor = FakeSettingExtractor(candidate_groups=[[extracted_candidate]])
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        subject_resolver=FakeSubjectResolver(
            result=SubjectResolutionResult(candidates=[extracted_candidate])
        ),
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert episode_chunk_embedding_service.requested_chunk_ids == []
    assert len(setting_extractor.requests) == 1
    assert setting_candidate_service.saved_candidates == [extracted_candidate]
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 1,
        "embeddedChunkCount": 0,
        "embeddingFailedChunkCount": 0,
        "embeddingSkippedChunkCount": 1,
        "candidateCount": 1,
        "subjectFallbackCallCount": 0,
        "subjectFallbackResolvedCount": 0,
        "subjectFallbackUnresolvedCount": 0,
        "statusContextCharacterCount": 0,
        "statusContextEntryCount": 0,
        "statusInactiveCandidateCount": 0,
        "characterFactComparisonCompletedCount": 0,
        "characterFactComparisonFailedCount": 0,
        **ZERO_CHARACTER_COMPARISON_METRICS,
        "worldSettingCandidateCount": 0,
        "worldSettingComparisonCompletedCount": 0,
        "worldSettingComparisonFailedCount": 0,
        **ZERO_WORLD_COMPARISON_METRICS,
    }
    assert spring_client.fail_calls == []


def test_worker_continues_setting_extraction_when_embedding_provider_temporarily_fails() -> None:
    # 일시적인 provider 장애만 요약에 기록하고 설정 후보 추출과 작업 완료를 계속하는지 검증한다.
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")])
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService(
        error=RecoverableEmbeddingProviderError("embedding API failed temporarily")
    )
    setting_extractor = FakeSettingExtractor(candidate_groups=[[]])
    subject_resolver = FakeSubjectResolver(result=SubjectResolutionResult(candidates=[]))
    setting_candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        subject_resolver=subject_resolver,
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=setting_candidate_service,
        embedding_generation_enabled=True,
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert len(setting_extractor.requests) == 1
    assert setting_candidate_service.request["candidate_count"] == 0
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary == {
        "episodeCount": 1,
        "chunkCount": 1,
        "embeddedChunkCount": 0,
        "embeddingFailedChunkCount": 1,
        "embeddingSkippedChunkCount": 0,
        "candidateCount": 0,
        "subjectFallbackCallCount": 0,
        "subjectFallbackResolvedCount": 0,
        "subjectFallbackUnresolvedCount": 0,
        "statusContextCharacterCount": 0,
        "statusContextEntryCount": 0,
        "statusInactiveCandidateCount": 0,
        "characterFactComparisonCompletedCount": 0,
        "characterFactComparisonFailedCount": 0,
        **ZERO_CHARACTER_COMPARISON_METRICS,
        "worldSettingCandidateCount": 0,
        "worldSettingComparisonCompletedCount": 0,
        "worldSettingComparisonFailedCount": 0,
        **ZERO_WORLD_COMPARISON_METRICS,
    }


def test_initial_analysis_isolates_character_comparison_failure() -> None:
    # 한 후보의 2차 비교 실패는 후보 FAILED로 남지만 같은 회차의 세계관 단계와 Job 완료는 계속된다.
    spring_client = FakeSpringWorkerClient(payload=_payload())
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")]),
        setting_extractor=FakeSettingExtractor(candidate_groups=[[]]),
        subject_resolver=FakeSubjectResolver(result=SubjectResolutionResult(candidates=[])),
        character_fact_comparison_pipeline=FakeCharacterFactComparisonPipeline(
            CharacterFactComparisonRunResult(completed_count=0, failed_count=1)
        ),
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=FakeSettingCandidateService(),
    )

    result = _run_once(worker)

    assert result.claimed is True
    assert spring_client.fail_calls == []
    summary = json.loads(spring_client.complete_calls[0][1])
    assert summary["characterFactComparisonCompletedCount"] == 0
    assert summary["characterFactComparisonFailedCount"] == 1
    assert summary["worldSettingCandidateCount"] == 0
    assert spring_client.fail_calls == []


def test_worker_fails_analysis_when_chunk_embedding_data_is_inconsistent() -> None:
    # 중복·누락 청크 같은 정합성 오류를 삼키지 않고 Spring 실패 보고까지 전파하는지 검증한다.
    spring_client = FakeSpringWorkerClient(payload=_payload())
    chunking_service = FakeEpisodeChunkingService(chunks=[_chunk(0, "비요른은 전사다.")])
    episode_chunk_embedding_service = FakeEpisodeChunkEmbeddingService(
        error=EmbeddingDataIntegrityError("embedding update target is missing")
    )
    setting_extractor = FakeSettingExtractor(candidate_groups=[[]])
    worker = AnalysisJobWorker(
        spring_client=spring_client,
        chunking_service=chunking_service,
        episode_chunk_embedding_service=episode_chunk_embedding_service,
        setting_extractor=setting_extractor,
        world_setting_extractor=FakeWorldSettingExtractor(),
        embedding_generation_enabled=True,
    )

    with pytest.raises(EmbeddingDataIntegrityError, match="target is missing"):
        _run_once(worker)

    assert setting_extractor.requests == []
    assert spring_client.complete_calls == []
    assert spring_client.fail_calls == [
        (ANALYSIS_JOB_ID, "embedding update target is missing", "UNEXPECTED_ERROR")
    ]


class SuccessfulAnalysisJobWorker(AnalysisJobWorker):
    async def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        return WorkerRunSummary(summary_json='{"candidateCount": 0}')


class FailingAnalysisJobWorker(AnalysisJobWorker):
    async def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        raise RuntimeError("LLM response parse failed.")


class QuotaFailingAnalysisJobWorker(AnalysisJobWorker):
    async def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        raise AiTokenQuotaExhaustedError()


class CancellableAnalysisJobWorker(AnalysisJobWorker):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started = asyncio.Event()

    async def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class RecordingCloseResource:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FailingCloseResource(RecordingCloseResource):
    async def aclose(self) -> None:
        await super().aclose()
        raise RuntimeError("provider close failed")


class FakeSpringWorkerClient:
    def __init__(self, payload: WorkerAnalysisJobPayload | None) -> None:
        self.payload = payload
        self.claim_called = False
        self.claim_model_name: str | None = None
        self.progress_calls: list[tuple[UUID, str, EpisodeProcessingStatus]] = []
        self.complete_calls: list[tuple[UUID, str | None, int | None, int | None]] = []
        self.fail_calls: list[tuple[UUID, str, str]] = []

    async def claim(
        self,
        allowed_job_types,
        model_name: str | None = None,
        current_step: str | None = None,
    ) -> WorkerAnalysisJobPayload | None:
        self.claim_called = True
        self.claim_model_name = model_name
        return self.payload

    async def report_progress(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        current_step: str,
        episode_status: EpisodeProcessingStatus | None = None,
        checkpoint_stage=None,
    ) -> None:
        self.progress_calls.append((analysis_job_id, current_step, episode_status))

    async def complete(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        self.complete_calls.append(
            (analysis_job_id, summary_json, input_token_count, output_token_count)
        )

    async def fail(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code,
    ) -> None:
        self.fail_calls.append((analysis_job_id, error_message, failure_code.value))

    async def heartbeat(self, analysis_job_id: UUID, lease_token: UUID) -> None:
        return None

    async def publish_world_setting_candidates(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidates,
    ):
        return candidates

    async def claim_next_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ):
        return None

    async def claim_next_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ):
        return None


class FakeEpisodeChunkingService:
    # 실제 S3/DB 청킹 대신 Worker가 claim payload의 episode_id/content_s3_key를 넘겼는지 기록
    def __init__(self, chunks: list[EpisodeChunk]) -> None:
        self.chunks = chunks
        self.requested_episode_ids: list[UUID] = []
        self.requested_content_s3_keys: list[str] = []
        self.loaded_episode_ids: list[UUID] = []
        self.context_text = ""
        self.context_keys: list[str] = []

    def read_source_text(self, content_s3_key: str) -> str:
        self.context_keys.append(content_s3_key)
        return self.context_text

    def replace_chunks_from_s3_content(
        self,
        episode_id: UUID,
        content_s3_key: str,
    ) -> list[EpisodeChunk]:
        self.requested_episode_ids.append(episode_id)
        self.requested_content_s3_keys.append(content_s3_key)
        return self.chunks

    def get_episode_chunks(self, episode_id: UUID) -> list[EpisodeChunk]:
        self.loaded_episode_ids.append(episode_id)
        return self.chunks


class FakeEpisodeChunkEmbeddingService:
    # 실제 OpenAI/DB 호출 대신 Worker가 청킹 직후 임베딩을 요청했는지 기록한다.
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requested_chunk_ids: list[list[UUID]] = []

    async def embed_chunks(self, chunks: list[EpisodeChunk]) -> EpisodeChunkEmbeddingResult:
        self.requested_chunk_ids.append([chunk.id for chunk in chunks])
        if self.error is not None:
            raise self.error
        return EpisodeChunkEmbeddingResult(embedded_chunk_count=len(chunks))


class FakeSettingExtractor:
    # 실제 OpenAI 호출 대신 chunk별 추출 후보 목록만 흉내
    def __init__(self, candidate_groups: list[list[ExtractedSettingCandidate]]) -> None:
        self.candidate_groups = candidate_groups
        self.requests = []

    async def extract_from_chunk(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        analysis_job_id: UUID | None = None,
        episode_no: int | None = None,
        episode_title: str | None = None,
        schema_hints: tuple[CharacterSettingSchemaHint, ...] = (),
        known_characters: tuple[KnownCharacter, ...] = (),
        narrative_context: dict[str, str | None] | None = None,
        prior_status_observations: tuple[ExtractedSettingCandidate, ...] = (),
    ):
        self.requests.append(
            {
                "source_chunk_id": source_chunk_id,
                "chunk_text": chunk_text,
                "analysis_job_id": analysis_job_id,
                "episode_no": episode_no,
                "episode_title": episode_title,
                "schema_hints": schema_hints,
                "known_characters": known_characters,
                "narrative_context": narrative_context,
                "prior_status_observations": prior_status_observations,
            }
        )
        candidates = self.candidate_groups.pop(0)
        return FakeExtractionResult(candidates=candidates)


class FakeExtractionResult:
    def __init__(self, candidates: list[ExtractedSettingCandidate]) -> None:
        self.candidates = candidates


class FakeWorldSettingExtractor:
    async def extract_from_chunk(self, chunk_text: str, episode_no=None, episode_title=None):
        return WorldSettingExtractionResult(candidates=[])


class FakeCharacterFactComparisonPipeline:
    def __init__(self, result: CharacterFactComparisonRunResult) -> None:
        self.result = result

    async def process_all(self, analysis_job_id, lease_token):
        return self.result


class FakeSettingCandidateService:
    # 실제 DB 저장 대신 Worker가 전달한 저장 요청을 기록
    def __init__(self) -> None:
        self.request = None
        self.saved_candidates: list[ExtractedSettingCandidate] = []

    def replace_candidates_for_analysis_job(
        self,
        work_id,
        analysis_job_id,
        save_items,
        known_characters,
    ):
        self.saved_candidates = [item.candidate for item in save_items]
        self.request = {
            "work_id": work_id,
            "analysis_job_id": analysis_job_id,
            "episode_ids": [item.episode_id for item in save_items],
            "known_character_names": [character.name for character in known_characters],
            "candidate_count": len(save_items),
        }
        return self.saved_candidates


class FakeSubjectResolver:
    def __init__(self, result: SubjectResolutionResult) -> None:
        self.result = result
        self.requests = []

    async def reconcile_episode_names(self, context, candidates, known_characters):
        return SubjectResolutionResult(candidates=candidates)

    async def resolve_candidates(
        self,
        context,
        candidates,
        known_characters,
    ) -> SubjectResolutionResult:
        if not candidates:
            return SubjectResolutionResult(candidates=[])

        self.requests.append(
            {
                "previous_chunk_text": context.previous_chunk_text,
                "current_chunk_text": context.current_chunk_text,
                "next_chunk_text": context.next_chunk_text,
                "previous_episode_text": context.previous_episode_text,
                "candidate_count": len(candidates),
                "known_character_names": [character.name for character in known_characters],
            }
        )
        return self.result


def _payload() -> WorkerAnalysisJobPayload:
    return WorkerAnalysisJobPayload(
        analysis_job_id=ANALYSIS_JOB_ID,
        job_type="SETTING_EXTRACTION",
        work_id=WORK_ID,
        work_title="빛나는 검사 로맨스",
        batch_id=BATCH_ID,
        model_name="gpt-4.1-mini",
        current_step="SETTING_EXTRACTION",
        lease_token=LEASE_TOKEN,
        lease_expires_at="2026-08-06T12:05:00",
        claim_attempt_count=1,
        character_setting_schemas=[
            {
                "schemaKey": "stats.strength",
                "displayName": "근력",
                "attributePattern": None,
                "aliases": ["근력", "힘", "strength"],
                "valueType": "NUMBER",
            },
            {
                "schemaKey": "stats.strength",
                "displayName": "작품 근력",
                "attributePattern": None,
                "aliases": ["완력"],
                "valueType": "NUMBER",
            },
        ],
        known_characters=[
            {
                "characterId": "00000000-0000-0000-0000-000000000005",
                "name": "비요른 얀델",
            }
        ],
        episode=WorkerAnalysisEpisodePayload(
            episode_id=EPISODE_ID,
            episode_no=1,
            title="첫 번째 회차",
            content_s3_key="works/work-id/episodes/episode-id.txt",
            content_s3_version=None,
            content_hash="hash",
            char_count=1234,
        ),
    )


def _run_once(worker: AnalysisJobWorker) -> WorkerRunResult:
    async def scenario() -> WorkerRunResult:
        try:
            return await worker.run_once()
        finally:
            await worker.aclose()

    return asyncio.run(scenario())


def _candidate(
    source_chunk_id: UUID,
    attribute_name: str,
    entity_name: str = "비요른",
    raw_entity_mention: str | None = None,
    quote: str = "비요른은 1레벨 바바리안이다.",
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=source_chunk_id,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name=attribute_name,
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote=quote,
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )


def _chunk(chunk_index: int, chunk_text: str, start_offset: int = 0) -> EpisodeChunk:
    return EpisodeChunk(
        id=UUID(f"00000000-0000-0000-0000-00000000010{chunk_index}"),
        episode_id=EPISODE_ID,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
        start_offset=start_offset,
        end_offset=start_offset + len(chunk_text),
        paragraph_start_index=0,
        paragraph_end_index=0,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )


def test_prior_status_observations_use_resolved_job_local_history_without_unknown_subjects():
    text = "리안은 목소리를 낼 수 없었다. 이름 모를 사람도 말을 못 했다."
    chunks = [_chunk(0, text, start_offset=100), _chunk(1, "리안이 또렷하게 대답했다.", start_offset=200)]
    status = _candidate(chunks[0].id, "status.발성_장애", entity_name="그는",
                        quote="리안은 목소리를 낼 수 없었다.").model_copy(update={
        "attribute_value": "발성 불능", "value_type": "JSON",
        "value_json": {"name": "발성 장애", "active": True},
    })
    unknown = status.model_copy(update={"entity_name": "미상"})
    level = _candidate(chunks[0].id, "level", quote="리안은 목소리를 낼 수 없었다.")
    extractor = FakeSettingExtractor(candidate_groups=[[status, unknown, level], [], [], []])

    class EchoResolver(FakeSubjectResolver):
        async def resolve_candidates(self, context, candidates, known_characters):
            return SubjectResolutionResult(candidates=[
                item.model_copy(update={"entity_name": "리안"}) if item.entity_name == "그는" else item
                for item in candidates
            ])

    spring = FakeSpringWorkerClient(payload=_payload())
    candidate_service = FakeSettingCandidateService()
    worker = AnalysisJobWorker(
        spring_client=spring,
        chunking_service=FakeEpisodeChunkingService(chunks=chunks),
        setting_extractor=extractor,
        subject_resolver=EchoResolver(SubjectResolutionResult(candidates=[])),
        world_setting_extractor=FakeWorldSettingExtractor(),
        setting_candidate_service=candidate_service,
    )

    async def scenario():
        try:
            first = await worker.run_once()
            first_saved = list(candidate_service.saved_candidates)
            spring.payload = _payload().model_copy(update={"analysis_job_id": UUID(int=900)})
            second = await worker.run_once()
            return first, second, first_saved
        finally:
            await worker.aclose()

    first, second, first_saved = asyncio.run(scenario())
    assert first.claimed and second.claimed
    prior = [request["prior_status_observations"] for request in extractor.requests]
    assert prior[0] == prior[2] == prior[3] == ()
    assert len(prior[1]) == 1
    assert prior[1][0].entity_name == "리안"
    assert prior[1][0].source_chunk_id == chunks[0].id
    assert prior[1][0].evidence_spans[0].start_offset == 100
    assert prior[1][0].value_json == {"name": "발성 장애", "active": True}
    assert any(item.entity_name == "미상" for item in first_saved)
    assert any(item.attribute_name == "level" for item in first_saved)
