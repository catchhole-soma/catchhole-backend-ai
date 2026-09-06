import asyncio
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import SubjectReference, WorldSettingComparator
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.analysis.world_setting_schemas import (
    WorldSettingComparisonBatchDecision,
    WorldSettingComparisonBatchResult,
)
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import Settings
from app.domain.enums import AnalysisFailureCode, WorldSettingConsolidationStatus
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchContextResponse,
    WorkerWorldSettingComparisonBatchPayload,
    WorkerWorldSettingComparisonExactTarget,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectPageResponse,
    WorkerWorldSettingSubjectResolutionCandidate,
    WorkerWorldSettingSubjectResolutionPendingResponse,
    WorkerWorldSettingSubjectResolutionResponse,
    WorkerWorldSettingSubjectResolutionResult,
)
from app.usage.metering import TextGenerationUsageSnapshot

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
SECOND_BATCH_ID = UUID("00000000-0000-0000-0000-000000000004")
TARGET_ID = UUID("00000000-0000-0000-0000-000000000005")
SECOND_TARGET_ID = UUID("00000000-0000-0000-0000-000000000006")


def test_batch_pipeline_links_two_source_candidates_to_one_decision() -> None:
    batch = _batch()
    spring = FakeBatchSpringApi([batch])
    comparator = FakeBatchComparator(
        _merged_decision().model_copy(
            update={"existing_root_property_names_to_move": ["기존 사냥 습성"]}
        )
    )
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        comparator,
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 2
    assert result.failed_count == 0
    assert result.batch_count == 1
    assert result.decision_count == 1
    assert result.cluster_count == 1
    assert result.clustered_candidate_count == 2
    assert result.singleton_candidate_count == 0
    assert spring.context_target_ids == [[TARGET_ID]]
    request = spring.completions[0]
    assert len(request.decisions) == 1
    assert request.decisions[0].decision_ref == "D1"
    assert request.decisions[0].source_candidate_refs == ["C1", "C2"]
    assert request.decisions[0].existing_root_property_names_to_move == ["기존 사냥 습성"]
    assert request.decisions[0].canonical_subject_name == "고블린"
    assert request.decisions[0].target_world_setting_id == TARGET_ID
    assert request.context_versions[0].version == 7
    summary = result.summary_metrics()
    assert summary["worldComparisonProviderRequestCount"] == 1
    assert summary["worldComparisonInputTokenCount"] == 20
    assert summary["worldComparisonOutputTokenCount"] == 4
    assert summary["worldComparisonBatchUsages"][0]["candidateCount"] == 2
    assert summary["worldComparisonClusterUsages"][0]["sourceCandidateCount"] == 2
    assert summary["worldComparisonClusterUsages"][0]["usageAttribution"] == (
        "PROPORTIONAL_SHARED_BATCH_REQUEST"
    )


def test_batch_pipeline_resolves_raw_aliases_before_claiming_canonical_batch() -> None:
    original = _batch()
    batch = original.model_copy(
        update={
            "candidates": [
                original.candidates[0].model_copy(update={"subject_name": "고블린 떼"}),
                original.candidates[1].model_copy(update={"subject_name": "고블린 무리"}),
            ]
        }
    )
    spring = PreparingBatchSpringApi([batch])
    resolver = FakeBatchSubjectResolver(TARGET_ID, "고블린")
    pipeline = WorldSettingComparisonPipeline(
        spring,
        resolver,
        FakeBatchComparator(_merged_decision()),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 2
    assert spring.claim_count == 2
    assert len(spring.resolution_requests) == 1
    submitted = spring.resolution_requests[0].resolutions
    assert [item.candidate_id for item in submitted] == [
        candidate.candidate_id for candidate in batch.candidates
    ]
    assert [item.target_world_setting_ids for item in submitted] == [
        [TARGET_ID],
        [TARGET_ID],
    ]
    summary = result.summary_metrics()
    assert summary["worldComparisonProviderRequestCount"] == 3
    assert summary["worldComparisonProviderLatencyMs"] == 0
    assert summary["worldComparisonInputTokenCount"] == 40
    assert summary["worldComparisonOutputTokenCount"] == 8
    assert summary["worldComparisonSubjectResolutionUsage"] == {
        "providerRequestCount": 2,
        "providerLatencyMs": 0,
        "inputTokenCount": 20,
        "cachedInputTokenCount": 0,
        "outputTokenCount": 4,
    }


def test_batch_pipeline_counts_final_decisions_as_clusters() -> None:
    batch = _batch()
    spring = FakeBatchSpringApi([batch])
    comparator = FakeBatchComparator(
        [
            _merged_decision().model_copy(
                update={
                    "source_candidate_refs": ["C1"],
                    "consolidation_status": WorldSettingConsolidationStatus.SINGLE,
                    "proposed_setting_name": "사냥 방식",
                    "proposed_value": "무리를 지어 사냥한다.",
                }
            ),
            _merged_decision().model_copy(
                update={
                    "source_candidate_refs": ["C2"],
                    "consolidation_status": WorldSettingConsolidationStatus.SINGLE,
                }
            ),
        ]
    )
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        comparator,
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.decision_count == 2
    assert result.cluster_count == 2
    assert len(result.cluster_usages) == 2
    assert [usage.cluster_sequence for usage in result.cluster_usages] == [1, 2]
    assert [usage.source_candidate_count for usage in result.cluster_usages] == [1, 1]
    assert sum(usage.provider_request_count for usage in result.cluster_usages) == 1
    assert sum(usage.input_token_count for usage in result.cluster_usages) == 20
    assert sum(usage.output_token_count for usage in result.cluster_usages) == 4
    assert {
        usage.usage_attribution for usage in result.cluster_usages
    } == {"PROPORTIONAL_SHARED_BATCH_REQUEST"}


def test_batch_pipeline_submits_every_normalized_exact_subject_match() -> None:
    spring = DuplicateExactSubjectPreparingSpringApi([_batch()])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        FakeBatchComparator(_merged_decision()),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 0
    assert len(spring.resolution_requests) == 1
    assert [
        item.target_world_setting_ids
        for item in spring.resolution_requests[0].resolutions
    ] == [[TARGET_ID, SECOND_TARGET_ID], [TARGET_ID, SECOND_TARGET_ID]]


def test_batch_pipeline_submits_up_to_twenty_normalized_exact_subject_matches() -> None:
    spring = ManyExactSubjectPreparingSpringApi([_batch()], exact_match_count=20)
    resolver = FakeBatchSubjectResolver(TARGET_ID, "고블린")
    pipeline = WorldSettingComparisonPipeline(
        spring,
        resolver,
        FakeBatchComparator(_merged_decision()),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 0
    assert resolver.llm_client.provider_request_count == 0
    assert len(spring.resolution_requests) == 1
    assert all(
        len(item.target_world_setting_ids) == 20
        for item in spring.resolution_requests[0].resolutions
    )


def test_batch_pipeline_rejects_more_than_twenty_exact_subject_matches_explicitly() -> None:
    spring = ManyExactSubjectPreparingSpringApi([_batch()], exact_match_count=21)
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        FakeBatchComparator(_merged_decision()),
    )

    with pytest.raises(ComparisonValidationError, match="more than 20"):
        asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert spring.resolution_requests == []
    assert spring.claim_count == 0


def test_batch_pipeline_rebuilds_whole_batch_after_stale_completion() -> None:
    spring = FakeBatchSpringApi([_batch()], stale_completion_count=1)
    comparator = FakeBatchComparator(_merged_decision())
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        comparator,
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 2
    assert result.stale_batch_retry_count == 1
    assert len(spring.context_target_ids) == 2
    assert len(spring.completions) == 2
    assert comparator.call_count == 2
    assert comparator.target_versions == [[7], [8]]
    assert comparator.target_values == [
        [["기존 무기 정보 v7"]],
        [["기존 무기 정보 v8"]],
    ]
    assert [request.context_versions[0].version for request in spring.completions] == [7, 8]


def test_batch_pipeline_re_resolves_subject_and_claims_new_batch_when_target_is_stale(
) -> None:
    spring = StaleSubjectResolutionSpringApi(_batch())
    comparator = FakeBatchComparator(_merged_decision())
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(SECOND_TARGET_ID, "고블린"),
        comparator,
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 2
    assert result.failed_count == 0
    assert result.batch_count == 2
    assert result.stale_batch_retry_count == 1
    assert spring.reset_batch_ids == [BATCH_ID]
    assert len(spring.resolution_requests) == 1
    assert [item.target_world_setting_ids for item in spring.resolution_requests[0].resolutions] == [
        [SECOND_TARGET_ID],
        [SECOND_TARGET_ID],
    ]
    assert comparator.target_ids == [[SECOND_TARGET_ID]]
    assert spring.failures == []
    summary = result.summary_metrics()
    assert summary["worldComparisonProviderRequestCount"] == 2
    assert summary["worldComparisonSubjectResolutionUsage"]["providerRequestCount"] == 1


def test_batch_pipeline_rejects_partial_subject_resolution_adapter() -> None:
    spring = PartialSubjectResolutionSpringApi([_batch()])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        FakeBatchComparator(_merged_decision()),
    )

    with pytest.raises(TypeError, match="must all be implemented together"):
        asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))


def test_batch_pipeline_fails_all_sources_when_completion_coverage_is_missing() -> None:
    spring = FakeBatchSpringApi([_batch()])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        FakeBatchComparator(
            _merged_decision().model_copy(update={"source_candidate_refs": ["C1"]})
        ),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 0
    assert result.failed_count == 2
    assert result.batch_validation_failure_count == 1
    assert result.first_failure_code is AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert len(result.cluster_usages) == 1
    assert result.cluster_usages[0].provider_request_count == 1
    assert result.cluster_usages[0].cluster_sequence == 0
    assert result.cluster_usages[0].usage_attribution == (
        "UNASSIGNED_FAILED_BATCH_REQUEST"
    )
    assert spring.completions == []
    assert spring.failures[0][0] == BATCH_ID
    assert "omits candidate refs" in spring.failures[0][1]


@pytest.mark.parametrize(
    ("invalid_fields", "validator"),
    [
        ({"target_ref": "SECRET_PROVIDER_VALUE"}, "_validate_batch_comparison_result"),
        ({"proposed_scope_name": "SECRET_PROVIDER_VALUE"}, "_validate_batch_scope_plan"),
    ],
)
def test_real_validation_origin_reaches_logs_and_spring_failure_payload(
    invalid_fields, validator, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.analysis.world_setting_comparator.get_settings",
        lambda: Settings(_env_file=None),
    )
    decision = _merged_decision().model_copy(update=invalid_fields)
    response = WorldSettingComparisonBatchResult(decisions=[decision])
    create_response = AsyncMock(return_value=LlmTextResponse(text=response.model_dump_json()))
    comparator = WorldSettingComparator(
        llm_client=SimpleNamespace(create_text_response=create_response), max_attempts=3
    )
    spring = FakeBatchSpringApi([_batch()])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": None})

    async def run_pipeline():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = SpringWorkerClient(
                base_url="http://spring.local",
                internal_api_key="SECRET_API_KEY",
                http_client=http_client,
            )
            # claim/context는 합성 fixture, 실패 보고는 실제 HTTP 직렬화 경계를 통과한다.
            monkeypatch.setattr(
                spring, "fail_world_setting_comparison_batch",
                client.fail_world_setting_comparison_batch,
            )
            pipeline = WorldSettingComparisonPipeline(
                spring, FakeBatchSubjectResolver(TARGET_ID, "고블린"), comparator
            )
            return await pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(run_pipeline())

    assert create_response.await_count == 3
    assert result.completed_count == 0
    assert result.failed_count == 2
    assert result.first_failure_code is AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert spring.completions == []
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == (
        f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}"
        f"/world-setting-comparison-batches/{BATCH_ID}/fail"
    )
    payload = json.loads(requests[0].content)
    error_message = payload["errorMessage"]
    assert re.fullmatch(
        r"World-setting batch comparison failed after 3 attempts: "
        rf"ValueError\(origin=app\.analysis\.world_setting_comparator\.{validator}:\d+\)",
        error_message,
    )
    assert payload == {
        "errorMessage": error_message,
        "failureCode": "COMPARISON_VALIDATION_FAILED",
    }
    retry_logs = [record.message for record in caplog.records if "retrying attempt=" in record.message]
    assert len(retry_logs) == 2
    assert all(f".{validator}:" in message for message in retry_logs)
    assert error_message in caplog.text
    assert len(error_message) < 1000
    assert "/" not in error_message
    for private_value in ("SECRET_PROVIDER_VALUE", "SECRET_API_KEY", "무리를 지어"):
        assert private_value not in error_message
        assert private_value not in caplog.text


def test_batch_pipeline_stops_after_quota_failure_without_claiming_next_batch() -> None:
    second_batch = _batch().model_copy(update={"comparison_batch_id": SECOND_BATCH_ID})
    spring = FakeBatchSpringApi([_batch(), second_batch])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeBatchSubjectResolver(TARGET_ID, "고블린"),
        QuotaFailingBatchComparator(),
    )

    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert spring.claim_count == 1
    assert spring.batches == [second_batch]
    assert spring.failures == [
        (
            BATCH_ID,
            "AI token quota is exhausted.",
            "AI_TOKEN_QUOTA_EXHAUSTED",
            None,
            None,
        )
    ]


def test_batch_pipeline_reports_subject_resolution_quota_failure_before_stopping() -> None:
    original = _batch()
    batch = original.model_copy(
        update={
            "candidates": [
                candidate.model_copy(update={"subject_name": "고블린 무리"})
                for candidate in original.candidates
            ]
        }
    )
    spring = PreparingBatchSpringApi([batch])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        QuotaFailingSubjectResolver(),
        FakeBatchComparator(_merged_decision()),
    )

    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert spring.candidate_failures == [
        (
            batch.candidates[0].candidate_id,
            "AI token quota is exhausted.",
            AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED,
            None,
            None,
        )
    ]
    assert spring.resolution_requests == []
    assert spring.claim_count == 0


class FakeBatchSpringApi:
    def __init__(
        self,
        batches: list[WorkerWorldSettingComparisonBatchPayload],
        stale_completion_count: int = 0,
    ) -> None:
        self.batches = list(batches)
        self.all_batches = {
            batch.comparison_batch_id: batch for batch in batches
        }
        self.claim_count = 0
        self.stale_completion_count = stale_completion_count
        self.context_target_ids: list[list[UUID]] = []
        self.completions = []
        self.failures: list[tuple[UUID, str, str, str | None, str | None]] = []

    async def claim_next_world_setting_comparison_batch(self, analysis_job_id, lease_token):
        self.claim_count += 1
        return self.batches.pop(0) if self.batches else None

    async def get_world_setting_subjects(
        self,
        analysis_job_id,
        lease_token,
        category,
        page,
        size=500,
    ):
        return WorkerWorldSettingSubjectPageResponse(
            subjects=[
                WorkerWorldSettingSubject(
                    world_setting_id=TARGET_ID,
                    subject_name="고블린",
                )
            ],
            page=page,
            has_next=False,
        )

    async def get_world_setting_comparison_batch_context(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
        target_world_setting_ids,
    ):
        self.context_target_ids.append(target_world_setting_ids)
        batch = self.all_batches[comparison_batch_id]
        target_version = 6 + len(self.context_target_ids)
        return WorkerWorldSettingComparisonBatchContextResponse(
            comparison_batch_id=comparison_batch_id,
            candidates=batch.candidates,
            exact_targets=[
                WorkerWorldSettingComparisonExactTarget(
                    candidate_ref="C1",
                    world_setting_id=TARGET_ID,
                ),
                WorkerWorldSettingComparisonExactTarget(
                    candidate_ref="C2",
                    world_setting_id=None,
                ),
            ],
            targets=[
                WorkerWorldSettingComparisonTarget(
                    world_setting_id=TARGET_ID,
                    subject_name="고블린",
                    properties=[
                        WorkerWorldSettingProperty(
                            scope_name="전투 특성",
                            setting_name="무기",
                            value=f"기존 무기 정보 v{target_version}",
                        )
                    ],
                    version=target_version,
                )
            ],
        )

    async def complete_world_setting_comparison_batch(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
        request,
    ):
        self.completions.append(request)
        if self.stale_completion_count:
            self.stale_completion_count -= 1
            http_request = httpx.Request(
                "POST",
                "http://spring.local/world-setting-comparison-batches/complete",
            )
            response = httpx.Response(409, request=http_request)
            raise SpringWorkerHttpError(
                "backend request failed",
                request=http_request,
                response=response,
                spring_error_code="WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE",
            )

    async def fail_world_setting_comparison_batch(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
        error_message,
        failure_code,
        source_error_code=None,
        source_reason_code=None,
    ):
        self.failures.append(
            (
                comparison_batch_id,
                error_message,
                failure_code.value,
                source_error_code,
                source_reason_code,
            )
        )


class PreparingBatchSpringApi(FakeBatchSpringApi):
    def __init__(self, batches: list[WorkerWorldSettingComparisonBatchPayload]) -> None:
        super().__init__(batches)
        self.resolution_completed = False
        self.resolution_requests = []
        self.reset_batch_ids: list[UUID] = []
        self.candidate_failures = []

    async def get_pending_world_setting_subject_resolutions(
        self,
        analysis_job_id,
        lease_token,
    ):
        if self.resolution_completed:
            return WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[])
        batch = next(iter(self.all_batches.values()))
        return WorkerWorldSettingSubjectResolutionPendingResponse(
            candidates=[
                WorkerWorldSettingSubjectResolutionCandidate(
                    candidate_id=candidate.candidate_id,
                    source_episode_id=batch.source_episode_id,
                    category=batch.category,
                    subject_name=candidate.subject_name,
                )
                for candidate in batch.candidates
            ]
        )

    async def complete_world_setting_subject_resolutions(
        self,
        analysis_job_id,
        lease_token,
        request,
    ):
        self.resolution_requests.append(request)
        self.resolution_completed = True
        return WorkerWorldSettingSubjectResolutionResponse(
            resolutions=[
                WorkerWorldSettingSubjectResolutionResult(
                    candidate_id=item.candidate_id,
                    resolution_type="EXISTING",
                    canonical_subject_key=f"TARGET:{TARGET_ID}",
                    canonical_subject_name="고블린",
                    target_world_setting_ids=item.target_world_setting_ids,
                )
                for item in request.resolutions
            ]
        )

    async def reset_stale_world_setting_subject_resolution(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
    ):
        self.reset_batch_ids.append(comparison_batch_id)

    async def fail_world_setting_comparison(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        error_message,
        failure_code,
        source_error_code=None,
        source_reason_code=None,
    ):
        self.candidate_failures.append(
            (
                candidate_id,
                error_message,
                failure_code,
                source_error_code,
                source_reason_code,
            )
        )


class PartialSubjectResolutionSpringApi(FakeBatchSpringApi):
    async def get_pending_world_setting_subject_resolutions(
        self,
        analysis_job_id,
        lease_token,
    ):
        return WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[])


class DuplicateExactSubjectPreparingSpringApi(PreparingBatchSpringApi):
    async def get_world_setting_subjects(
        self,
        analysis_job_id,
        lease_token,
        category,
        page,
        size=500,
    ):
        return WorkerWorldSettingSubjectPageResponse(
            subjects=[
                WorkerWorldSettingSubject(
                    world_setting_id=TARGET_ID,
                    subject_name="Goblin",
                ),
                WorkerWorldSettingSubject(
                    world_setting_id=SECOND_TARGET_ID,
                    subject_name="GOBLIN",
                ),
            ],
            page=page,
            has_next=False,
        )

    async def get_pending_world_setting_subject_resolutions(
        self,
        analysis_job_id,
        lease_token,
    ):
        if self.resolution_completed:
            return WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[])
        batch = next(iter(self.all_batches.values()))
        return WorkerWorldSettingSubjectResolutionPendingResponse(
            candidates=[
                WorkerWorldSettingSubjectResolutionCandidate(
                    candidate_id=candidate.candidate_id,
                    source_episode_id=batch.source_episode_id,
                    category=batch.category,
                    subject_name="goblin",
                )
                for candidate in batch.candidates
            ]
        )

    async def claim_next_world_setting_comparison_batch(
        self,
        analysis_job_id,
        lease_token,
    ):
        self.claim_count += 1


class ManyExactSubjectPreparingSpringApi(DuplicateExactSubjectPreparingSpringApi):
    def __init__(
        self,
        batches: list[WorkerWorldSettingComparisonBatchPayload],
        exact_match_count: int,
    ) -> None:
        super().__init__(batches)
        self.exact_match_count = exact_match_count

    async def get_world_setting_subjects(
        self,
        analysis_job_id,
        lease_token,
        category,
        page,
        size=500,
    ):
        return WorkerWorldSettingSubjectPageResponse(
            subjects=[
                WorkerWorldSettingSubject(
                    world_setting_id=UUID(int=index + 100),
                    subject_name="GOBLIN",
                )
                for index in range(self.exact_match_count)
            ],
            page=page,
            has_next=False,
        )


class StaleSubjectResolutionSpringApi(PreparingBatchSpringApi):
    def __init__(self, stale_batch: WorkerWorldSettingComparisonBatchPayload) -> None:
        super().__init__([stale_batch])
        self.resolution_completed = True
        self.resolution_requests = []
        self.allow_resolution = False

    async def get_pending_world_setting_subject_resolutions(
        self,
        analysis_job_id,
        lease_token,
    ):
        if not self.allow_resolution or self.resolution_completed:
            return WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[])
        batch = self.all_batches[BATCH_ID]
        return WorkerWorldSettingSubjectResolutionPendingResponse(
            candidates=[
                WorkerWorldSettingSubjectResolutionCandidate(
                    candidate_id=candidate.candidate_id,
                    source_episode_id=batch.source_episode_id,
                    category=batch.category,
                    subject_name=candidate.subject_name,
                )
                for candidate in batch.candidates
            ]
        )

    async def get_world_setting_subjects(
        self,
        analysis_job_id,
        lease_token,
        category,
        page,
        size=500,
    ):
        return WorkerWorldSettingSubjectPageResponse(
            subjects=[
                WorkerWorldSettingSubject(
                    world_setting_id=SECOND_TARGET_ID,
                    subject_name="고블린",
                )
            ],
            page=page,
            has_next=False,
        )

    async def complete_world_setting_subject_resolutions(
        self,
        analysis_job_id,
        lease_token,
        request,
    ):
        self.resolution_requests.append(request)
        self.resolution_completed = True
        return WorkerWorldSettingSubjectResolutionResponse(
            resolutions=[
                WorkerWorldSettingSubjectResolutionResult(
                    candidate_id=item.candidate_id,
                    resolution_type="EXISTING",
                    canonical_subject_key=f"TARGET:{SECOND_TARGET_ID}",
                    canonical_subject_name="고블린",
                    target_world_setting_ids=item.target_world_setting_ids,
                )
                for item in request.resolutions
            ]
        )

    async def reset_stale_world_setting_subject_resolution(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
    ):
        self.reset_batch_ids.append(comparison_batch_id)
        replacement = _batch().model_copy(
            update={
                "comparison_batch_id": SECOND_BATCH_ID,
                "canonical_subject_key": f"TARGET:{SECOND_TARGET_ID}",
                "resolved_target_world_setting_ids": [SECOND_TARGET_ID],
            }
        )
        self.batches.append(replacement)
        self.all_batches[SECOND_BATCH_ID] = replacement
        self.allow_resolution = True
        self.resolution_completed = False

    async def get_world_setting_comparison_batch_context(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
        target_world_setting_ids,
    ):
        self.context_target_ids.append(target_world_setting_ids)
        if comparison_batch_id == BATCH_ID:
            http_request = httpx.Request(
                "POST",
                "http://spring.local/world-setting-comparison-batches/context",
            )
            response = httpx.Response(409, request=http_request)
            raise SpringWorkerHttpError(
                "canonical subject target changed",
                request=http_request,
                response=response,
                spring_error_code="WORLD_SETTING_SUBJECT_RESOLUTION_STALE",
            )
        batch = self.all_batches[comparison_batch_id]
        return WorkerWorldSettingComparisonBatchContextResponse(
            comparison_batch_id=comparison_batch_id,
            candidates=batch.candidates,
            exact_targets=[
                WorkerWorldSettingComparisonExactTarget(
                    candidate_ref=candidate.candidate_ref,
                    world_setting_id=SECOND_TARGET_ID,
                )
                for candidate in batch.candidates
            ],
            targets=[
                WorkerWorldSettingComparisonTarget(
                    world_setting_id=SECOND_TARGET_ID,
                    subject_name="고블린",
                    properties=[],
                    version=11,
                )
            ],
        )


class FakeBatchSubjectResolver:
    def __init__(self, target_id: UUID, subject_name: str) -> None:
        self.reference = SubjectReference("S1", target_id, subject_name)
        self.llm_client = FakeUsageClient()

    async def select_subjects(self, candidate, subjects):
        self.llm_client.record(input_tokens=10, output_tokens=2)
        return [self.reference]


class QuotaFailingSubjectResolver:
    async def select_subjects(self, candidate, subjects):
        raise AiTokenQuotaExhaustedError()


class FakeBatchComparator:
    def __init__(
        self,
        decisions: WorldSettingComparisonBatchDecision
        | list[WorldSettingComparisonBatchDecision],
    ) -> None:
        self.decisions = decisions if isinstance(decisions, list) else [decisions]
        self.call_count = 0
        self.target_ids: list[list[UUID]] = []
        self.target_versions: list[list[int]] = []
        self.target_values: list[list[list[str]]] = []
        self.llm_client = FakeUsageClient()

    async def compare_batch(self, category, candidates, targets):
        self.call_count += 1
        self.target_ids.append([target.world_setting_id for target in targets])
        self.target_versions.append([target.version for target in targets])
        self.target_values.append(
            [[property.value for property in target.properties] for target in targets]
        )
        self.llm_client.record(input_tokens=20, output_tokens=4)
        result = WorldSettingComparisonBatchResult(decisions=self.decisions)
        return result, result.model_dump(mode="json")


class QuotaFailingBatchComparator:
    async def compare_batch(self, category, candidates, targets):
        raise AiTokenQuotaExhaustedError()


class FakeUsageClient:
    def __init__(self) -> None:
        self.provider_request_count = 0
        self.input_token_count = 0
        self.output_token_count = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.provider_request_count += 1
        self.input_token_count += input_tokens
        self.output_token_count += output_tokens

    def usage_snapshot(self) -> TextGenerationUsageSnapshot:
        return TextGenerationUsageSnapshot(
            provider_request_count=self.provider_request_count,
            input_token_count=self.input_token_count,
            output_token_count=self.output_token_count,
        )


def _batch() -> WorkerWorldSettingComparisonBatchPayload:
    return WorkerWorldSettingComparisonBatchPayload.model_validate(
        {
            "comparisonBatchId": str(BATCH_ID),
            "workId": "00000000-0000-0000-0000-000000000010",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000011",
            "category": "RACE",
            "resolutionType": "EXISTING",
            "canonicalSubjectKey": f"TARGET:{TARGET_ID}",
            "canonicalSubjectName": "고블린",
            "resolvedTargetWorldSettingIds": [str(TARGET_ID)],
            "rawScopeName": "전투 특성",
            "candidates": [
                {
                    "candidateRef": "C1",
                    "candidateId": "00000000-0000-0000-0000-000000000021",
                    "subjectName": "고블린",
                    "scopeName": "전투 특성",
                    "settingName": "사냥 방식",
                    "extractedValue": "무리를 지어 사냥한다.",
                    "evidenceSpans": [{"quote": "고블린은 무리를 지어 사냥했다."}],
                    "extractionConfidence": 0.95,
                },
                {
                    "candidateRef": "C2",
                    "candidateId": "00000000-0000-0000-0000-000000000022",
                    "subjectName": "고블린족",
                    "scopeName": "전투 특성",
                    "settingName": "사냥 전술",
                    "extractedValue": "여럿이 목표를 포위한다.",
                    "evidenceSpans": [{"quote": "고블린족은 여럿이 목표를 포위했다."}],
                    "extractionConfidence": 0.8,
                },
            ],
        }
    )


def _merged_decision() -> WorldSettingComparisonBatchDecision:
    return WorldSettingComparisonBatchDecision(
        source_candidate_refs=["C1", "C2"],
        consolidation_status="MERGED",
        operation="ADD",
        target_ref="T1",
        proposed_scope_name="전투 특성",
        proposed_setting_name="사냥 전술",
        proposed_value="무리를 지어 목표를 포위해 사냥한다.",
        comparison_reason="두 후보가 같은 사냥 전술을 보완해서 설명한다.",
    )
