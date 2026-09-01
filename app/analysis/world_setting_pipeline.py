import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError
from app.domain.enums import AnalysisFailureCode, WorldSettingCategory
from app.exceptions.failure_classification import (
    comparison_failure_code,
    spring_failure_source,
)
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonBatchCompleteRequest,
    WorkerWorldSettingComparisonBatchContextResponse,
    WorkerWorldSettingComparisonBatchDecision,
    WorkerWorldSettingComparisonBatchPayload,
    WorkerWorldSettingComparisonCompleteRequest,
    WorkerWorldSettingComparisonContextResponse,
    WorkerWorldSettingContextVersion,
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectPageResponse,
    WorkerWorldSettingSubjectResolutionPendingResponse,
    WorkerWorldSettingSubjectResolutionRequest,
    WorkerWorldSettingSubjectResolutionRequestItem,
    WorkerWorldSettingSubjectResolutionResponse,
)
from app.usage.metering import TextGenerationUsageSnapshot

logger = logging.getLogger(__name__)
CONTEXT_STALE_ERROR_CODE = "WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE"
SUBJECT_RESOLUTION_STALE_ERROR_CODE = "WORLD_SETTING_SUBJECT_RESOLUTION_STALE"
MAX_SUBJECT_RESOLUTION_TARGETS = 20


class WorldSettingComparisonSpringApi(Protocol):
    async def get_pending_world_setting_subject_resolutions(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerWorldSettingSubjectResolutionPendingResponse: ...

    async def complete_world_setting_subject_resolutions(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        request: WorkerWorldSettingSubjectResolutionRequest,
    ) -> WorkerWorldSettingSubjectResolutionResponse: ...

    async def reset_stale_world_setting_subject_resolution(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
    ) -> None: ...

    async def claim_next_world_setting_comparison_batch(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerWorldSettingComparisonBatchPayload | None: ...

    async def get_world_setting_comparison_batch_context(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
        target_world_setting_ids: list[UUID],
    ) -> WorkerWorldSettingComparisonBatchContextResponse: ...

    async def complete_world_setting_comparison_batch(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
        request: WorkerWorldSettingComparisonBatchCompleteRequest,
    ) -> None: ...

    async def fail_world_setting_comparison_batch(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode,
        source_error_code: str | None = None,
        source_reason_code: str | None = None,
    ) -> None: ...

    async def claim_next_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerWorldSettingCandidatePayload | None: ...

    async def get_world_setting_subjects(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        category: WorldSettingCategory,
        page: int,
        size: int = 500,
    ) -> WorkerWorldSettingSubjectPageResponse: ...

    async def get_world_setting_comparison_context(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        target_world_setting_ids: list[UUID],
    ) -> WorkerWorldSettingComparisonContextResponse: ...

    async def complete_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        request: WorkerWorldSettingComparisonCompleteRequest,
    ) -> None: ...

    async def fail_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode,
        source_error_code: str | None = None,
        source_reason_code: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class WorldSettingComparisonClusterUsage:
    batch_sequence: int
    context_attempt: int
    cluster_sequence: int
    source_candidate_count: int
    usage_attribution: str
    provider_request_count: int
    provider_latency_ms: int
    input_token_count: int
    cached_input_token_count: int
    output_token_count: int

    def as_summary(self) -> dict[str, int | str]:
        return {
            "batchSequence": self.batch_sequence,
            "contextAttempt": self.context_attempt,
            "clusterSequence": self.cluster_sequence,
            "sourceCandidateCount": self.source_candidate_count,
            "usageAttribution": self.usage_attribution,
            "providerRequestCount": self.provider_request_count,
            "providerLatencyMs": self.provider_latency_ms,
            "inputTokenCount": self.input_token_count,
            "cachedInputTokenCount": self.cached_input_token_count,
            "outputTokenCount": self.output_token_count,
        }


@dataclass(frozen=True)
class WorldSettingComparisonBatchUsage:
    batch_sequence: int
    candidate_count: int
    cluster_count: int
    provider_request_count: int
    provider_latency_ms: int
    input_token_count: int
    cached_input_token_count: int
    output_token_count: int

    def as_summary(self) -> dict[str, int]:
        return {
            "batchSequence": self.batch_sequence,
            "candidateCount": self.candidate_count,
            "clusterCount": self.cluster_count,
            "providerRequestCount": self.provider_request_count,
            "providerLatencyMs": self.provider_latency_ms,
            "inputTokenCount": self.input_token_count,
            "cachedInputTokenCount": self.cached_input_token_count,
            "outputTokenCount": self.output_token_count,
        }


@dataclass(frozen=True)
class WorldSettingComparisonRunResult:
    completed_count: int
    failed_count: int
    first_failure_code: AnalysisFailureCode | None = None
    batch_count: int = 0
    decision_count: int = 0
    cluster_count: int = 0
    clustered_candidate_count: int = 0
    singleton_candidate_count: int = 0
    batch_validation_failure_count: int = 0
    stale_batch_retry_count: int = 0
    overflow_or_review_required_count: int | None = None
    subject_resolution_usage: TextGenerationUsageSnapshot = field(
        default_factory=TextGenerationUsageSnapshot
    )
    batch_usages: tuple[WorldSettingComparisonBatchUsage, ...] = ()
    cluster_usages: tuple[WorldSettingComparisonClusterUsage, ...] = ()

    def summary_metrics(self) -> dict[str, object]:
        candidate_count = self.completed_count + self.failed_count
        average_candidates_per_batch = (
            candidate_count / self.batch_count if self.batch_count else 0.0
        )
        clustered_or_singleton_count = (
            self.clustered_candidate_count + self.singleton_candidate_count
        )
        average_candidates_per_cluster = (
            clustered_or_singleton_count / self.cluster_count
            if self.cluster_count
            else 0.0
        )
        provider_request_count = self.subject_resolution_usage.provider_request_count + sum(
            usage.provider_request_count for usage in self.batch_usages
        )
        provider_latency_ms = self.subject_resolution_usage.provider_latency_ms + sum(
            usage.provider_latency_ms for usage in self.batch_usages
        )
        input_token_count = self.subject_resolution_usage.input_token_count + sum(
            usage.input_token_count for usage in self.batch_usages
        )
        cached_input_token_count = self.subject_resolution_usage.cached_input_token_count + sum(
            usage.cached_input_token_count for usage in self.batch_usages
        )
        output_token_count = self.subject_resolution_usage.output_token_count + sum(
            usage.output_token_count for usage in self.batch_usages
        )
        return {
            "worldComparisonBatchCount": self.batch_count,
            "worldComparisonDecisionCount": self.decision_count,
            "worldComparisonClusterCount": self.cluster_count,
            "averageCandidatesPerBatch": round(average_candidates_per_batch, 4),
            "averageCandidatesPerCluster": round(average_candidates_per_cluster, 4),
            "clusteredCandidateCount": self.clustered_candidate_count,
            "singletonCandidateCount": self.singleton_candidate_count,
            "batchValidationFailureCount": self.batch_validation_failure_count,
            "staleBatchRetryCount": self.stale_batch_retry_count,
            "clusterOverflowOrReviewRequiredCount": (
                self.overflow_or_review_required_count
            ),
            "worldComparisonProviderRequestCount": provider_request_count,
            "worldComparisonProviderLatencyMs": provider_latency_ms,
            "worldComparisonInputTokenCount": input_token_count,
            "worldComparisonCachedInputTokenCount": cached_input_token_count,
            "worldComparisonOutputTokenCount": output_token_count,
            "worldComparisonSubjectResolutionUsage": {
                "providerRequestCount": self.subject_resolution_usage.provider_request_count,
                "providerLatencyMs": self.subject_resolution_usage.provider_latency_ms,
                "inputTokenCount": self.subject_resolution_usage.input_token_count,
                "cachedInputTokenCount": (
                    self.subject_resolution_usage.cached_input_token_count
                ),
                "outputTokenCount": self.subject_resolution_usage.output_token_count,
            },
            "worldComparisonBatchUsages": [
                usage.as_summary() for usage in self.batch_usages
            ],
            "worldComparisonClusterUsages": [
                usage.as_summary() for usage in self.cluster_usages
            ],
        }


@dataclass(frozen=True)
class _BatchComparisonStats:
    candidate_count: int
    decision_count: int
    cluster_count: int
    clustered_candidate_count: int
    singleton_candidate_count: int
    stale_retry_count: int


class WorldSettingComparisonPipeline:
    def __init__(
        self,
        spring_client: WorldSettingComparisonSpringApi,
        subject_resolver: WorldSettingSubjectResolver,
        comparator: WorldSettingComparator,
        max_context_attempts: int = 3,
    ) -> None:
        if max_context_attempts < 1:
            raise ValueError("max_context_attempts must be at least 1.")
        self.spring_client = spring_client
        self.subject_resolver = subject_resolver
        self.comparator = comparator
        self.max_context_attempts = max_context_attempts

    async def process_all(
        self, analysis_job_id: UUID, lease_token: UUID
    ) -> WorldSettingComparisonRunResult:
        if hasattr(self.spring_client, "claim_next_world_setting_comparison_batch"):
            resolution_api_names = (
                "get_pending_world_setting_subject_resolutions",
                "complete_world_setting_subject_resolutions",
                "reset_stale_world_setting_subject_resolution",
            )
            implemented_resolution_apis = [
                name for name in resolution_api_names if hasattr(self.spring_client, name)
            ]
            if implemented_resolution_apis and len(implemented_resolution_apis) != len(
                resolution_api_names
            ):
                raise TypeError(
                    "World-setting subject-resolution APIs must all be implemented together."
                )
            if implemented_resolution_apis:
                usage_before = _component_usage_snapshot(self.subject_resolver)
                await self._prepare_subject_resolutions(analysis_job_id, lease_token)
                subject_resolution_usage = _component_usage_snapshot(
                    self.subject_resolver
                ).since(usage_before)
            else:
                subject_resolution_usage = TextGenerationUsageSnapshot()
            return await self._process_all_batches(
                analysis_job_id,
                lease_token,
                subject_resolution_usage,
            )
        return await self._process_all_legacy_candidates(analysis_job_id, lease_token)

    async def _prepare_subject_resolutions(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> None:
        subjects_by_category: dict[WorldSettingCategory, list[WorkerWorldSettingSubject]] = {}
        while True:
            pending = await self.spring_client.get_pending_world_setting_subject_resolutions(
                analysis_job_id,
                lease_token,
            )
            if not pending.candidates:
                return
            resolutions: list[WorkerWorldSettingSubjectResolutionRequestItem] = []
            for candidate in pending.candidates:
                subjects = subjects_by_category.get(candidate.category)
                if subjects is None:
                    subjects = await self._load_all_subjects_for_category(
                        analysis_job_id,
                        lease_token,
                        candidate.category,
                    )
                    subjects_by_category[candidate.category] = subjects
                candidate_key = _normalized_name(candidate.subject_name)
                exact_matches = [
                    subject
                    for subject in subjects
                    if _normalized_name(subject.subject_name) == candidate_key
                ]
                if exact_matches:
                    if len(exact_matches) > MAX_SUBJECT_RESOLUTION_TARGETS:
                        raise ComparisonValidationError(
                            "World-setting subject resolution found more than 20 "
                            "normalized exact targets."
                        )
                    target_ids = [
                        subject.world_setting_id for subject in exact_matches
                    ]
                else:
                    try:
                        selected_subjects = await self.subject_resolver.select_subjects(
                            candidate,
                            subjects,
                        )
                    except AiTokenQuotaExhaustedError as exc:
                        source_error_code, source_reason_code = spring_failure_source(exc)
                        await self.spring_client.fail_world_setting_comparison(
                            analysis_job_id,
                            candidate.candidate_id,
                            lease_token,
                            (str(exc) or exc.__class__.__name__)[:1000],
                            AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED,
                            source_error_code=source_error_code,
                            source_reason_code=source_reason_code,
                        )
                        raise
                    target_ids = [
                        reference.world_setting_id for reference in selected_subjects
                    ]
                resolutions.append(
                    WorkerWorldSettingSubjectResolutionRequestItem(
                        candidate_id=candidate.candidate_id,
                        target_world_setting_ids=target_ids,
                    )
                )
            request = WorkerWorldSettingSubjectResolutionRequest(resolutions=resolutions)
            response = await self.spring_client.complete_world_setting_subject_resolutions(
                analysis_job_id,
                lease_token,
                request,
            )
            _validate_subject_resolution_response(request, response)

    async def _process_all_legacy_candidates(
        self, analysis_job_id: UUID, lease_token: UUID
    ) -> WorldSettingComparisonRunResult:
        completed_count = 0
        failed_count = 0
        first_failure_code: AnalysisFailureCode | None = None
        while True:
            candidate = await self.spring_client.claim_next_world_setting_comparison(
                analysis_job_id,
                lease_token,
            )
            if candidate is None:
                return WorldSettingComparisonRunResult(
                    completed_count,
                    failed_count,
                    first_failure_code,
                )
            failure_code = await self._process_claimed_candidate(
                analysis_job_id,
                lease_token,
                candidate,
            )
            if failure_code is None:
                completed_count += 1
            else:
                failed_count += 1
                if first_failure_code is None:
                    first_failure_code = failure_code

    async def _process_all_batches(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        subject_resolution_usage: TextGenerationUsageSnapshot,
    ) -> WorldSettingComparisonRunResult:
        completed_count = 0
        failed_count = 0
        first_failure_code: AnalysisFailureCode | None = None
        batch_count = 0
        decision_count = 0
        cluster_count = 0
        clustered_candidate_count = 0
        singleton_candidate_count = 0
        batch_validation_failure_count = 0
        stale_batch_retry_count = 0
        batch_usages: list[WorldSettingComparisonBatchUsage] = []
        cluster_usages: list[WorldSettingComparisonClusterUsage] = []
        subject_resolution_stale_attempts: dict[tuple[UUID, ...], int] = {}
        while True:
            batch = await self.spring_client.claim_next_world_setting_comparison_batch(
                analysis_job_id,
                lease_token,
            )
            if batch is None:
                return WorldSettingComparisonRunResult(
                    completed_count=completed_count,
                    failed_count=failed_count,
                    first_failure_code=first_failure_code,
                    batch_count=batch_count,
                    decision_count=decision_count,
                    cluster_count=cluster_count,
                    clustered_candidate_count=clustered_candidate_count,
                    singleton_candidate_count=singleton_candidate_count,
                    batch_validation_failure_count=batch_validation_failure_count,
                    stale_batch_retry_count=stale_batch_retry_count,
                    subject_resolution_usage=subject_resolution_usage,
                    batch_usages=tuple(batch_usages),
                    cluster_usages=tuple(cluster_usages),
                )
            batch_count += 1
            usage_before = self._usage_snapshot()
            try:
                stats = await self._compare_batch_with_fresh_context(
                    analysis_job_id,
                    lease_token,
                    batch,
                    batch_count,
                    cluster_usages,
                )
            except AiTokenQuotaExhaustedError as exc:
                source_error_code, source_reason_code = spring_failure_source(exc)
                await self.spring_client.fail_world_setting_comparison_batch(
                    analysis_job_id,
                    batch.comparison_batch_id,
                    lease_token,
                    (str(exc) or exc.__class__.__name__)[:1000],
                    AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED,
                    source_error_code=source_error_code,
                    source_reason_code=source_reason_code,
                )
                raise
            except Exception as exc:
                stale_resolution_key = tuple(
                    sorted(
                        (candidate.candidate_id for candidate in batch.candidates),
                        key=str,
                    )
                )
                stale_resolution_attempts = subject_resolution_stale_attempts.get(
                    stale_resolution_key,
                    0,
                )
                is_subject_resolution_stale = (
                    isinstance(exc, SpringWorkerHttpError)
                    and exc.status_code == 409
                    and exc.spring_error_code == SUBJECT_RESOLUTION_STALE_ERROR_CODE
                )
                if (
                    is_subject_resolution_stale
                    and stale_resolution_attempts < self.max_context_attempts - 1
                ):
                    usage = self._usage_snapshot().since(usage_before)
                    await self.spring_client.reset_stale_world_setting_subject_resolution(
                        analysis_job_id,
                        batch.comparison_batch_id,
                        lease_token,
                    )
                    subject_resolution_stale_attempts[stale_resolution_key] = (
                        stale_resolution_attempts + 1
                    )
                    stale_batch_retry_count += 1
                    resolution_usage_before = _component_usage_snapshot(
                        self.subject_resolver
                    )
                    await self._prepare_subject_resolutions(
                        analysis_job_id,
                        lease_token,
                    )
                    subject_resolution_usage += _component_usage_snapshot(
                        self.subject_resolver
                    ).since(resolution_usage_before)
                    batch_usages.append(
                        _batch_usage(
                            batch_count,
                            len(batch.candidates),
                            0,
                            usage,
                        )
                    )
                    logger.info(
                        "World-setting canonical subject changed; resolution and batch "
                        "will be rebuilt. analysis_job_id=%s comparison_batch_id=%s "
                        "attempt=%s/%s",
                        analysis_job_id,
                        batch.comparison_batch_id,
                        stale_resolution_attempts + 1,
                        self.max_context_attempts,
                    )
                    continue
                error_message = (str(exc) or exc.__class__.__name__)[:1000]
                failure_code = comparison_failure_code(exc)
                source_error_code, source_reason_code = spring_failure_source(exc)
                await self.spring_client.fail_world_setting_comparison_batch(
                    analysis_job_id,
                    batch.comparison_batch_id,
                    lease_token,
                    error_message,
                    failure_code,
                    source_error_code=source_error_code,
                    source_reason_code=source_reason_code,
                )
                failed_count += len(batch.candidates)
                if failure_code is AnalysisFailureCode.COMPARISON_VALIDATION_FAILED:
                    batch_validation_failure_count += 1
                if first_failure_code is None:
                    first_failure_code = failure_code
                usage = self._usage_snapshot().since(usage_before)
                batch_usages.append(
                    _batch_usage(
                        batch_count,
                        len(batch.candidates),
                        0,
                        usage,
                    )
                )
                logger.exception(
                    "World-setting batch comparison failed. "
                    "analysis_job_id=%s comparison_batch_id=%s candidate_count=%s",
                    analysis_job_id,
                    batch.comparison_batch_id,
                    len(batch.candidates),
                )
                continue

            completed_count += stats.candidate_count
            decision_count += stats.decision_count
            cluster_count += stats.cluster_count
            clustered_candidate_count += stats.clustered_candidate_count
            singleton_candidate_count += stats.singleton_candidate_count
            stale_batch_retry_count += stats.stale_retry_count
            usage = self._usage_snapshot().since(usage_before)
            batch_usages.append(
                _batch_usage(
                    batch_count,
                    stats.candidate_count,
                    stats.cluster_count,
                    usage,
                )
            )

    def _usage_snapshot(self) -> TextGenerationUsageSnapshot:
        return _component_usage_snapshot(self.subject_resolver) + _component_usage_snapshot(
            self.comparator
        )

    async def _process_claimed_candidate(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate: WorkerWorldSettingCandidatePayload,
    ) -> AnalysisFailureCode | None:
        try:
            await self._compare_with_fresh_context(analysis_job_id, lease_token, candidate)
            return None
        except AiTokenQuotaExhaustedError as exc:
            source_error_code, source_reason_code = spring_failure_source(exc)
            await self.spring_client.fail_world_setting_comparison(
                analysis_job_id,
                candidate.candidate_id,
                lease_token,
                (str(exc) or exc.__class__.__name__)[:1000],
                AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED,
                source_error_code=source_error_code,
                source_reason_code=source_reason_code,
            )
            raise
        except Exception as exc:
            error_message = (str(exc) or exc.__class__.__name__)[:1000]
            failure_code = comparison_failure_code(exc)
            source_error_code, source_reason_code = spring_failure_source(exc)
            await self.spring_client.fail_world_setting_comparison(
                analysis_job_id,
                candidate.candidate_id,
                lease_token,
                error_message,
                failure_code,
                source_error_code=source_error_code,
                source_reason_code=source_reason_code,
            )
            logger.exception(
                "World-setting comparison failed. analysis_job_id=%s candidate_id=%s",
                analysis_job_id,
                candidate.candidate_id,
            )
            return failure_code

    async def _compare_with_fresh_context(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate: WorkerWorldSettingCandidatePayload,
    ) -> None:
        for attempt in range(1, self.max_context_attempts + 1):
            subjects = await self._load_all_subjects(analysis_job_id, lease_token, candidate)
            target_ids = await self._select_context_target_ids(candidate, subjects)
            context = await self.spring_client.get_world_setting_comparison_context(
                analysis_job_id,
                candidate.candidate_id,
                lease_token,
                target_ids,
            )
            decision, raw_comparison_json = await self.comparator.compare(
                context.candidate,
                context.targets,
            )
            targets_by_ref = {
                f"T{index}": target for index, target in enumerate(context.targets, start=1)
            }
            selected_target = (
                None if decision.target_ref is None else targets_by_ref[decision.target_ref]
            )
            request = WorkerWorldSettingComparisonCompleteRequest(
                target_world_setting_id=(
                    None if selected_target is None else selected_target.world_setting_id
                ),
                matched_scope_name=decision.matched_scope_name,
                matched_property_name=decision.matched_property_name,
                consolidation_status=decision.consolidation_status,
                suggested_operation=decision.operation,
                comparison_review_reason=decision.review_reason,
                proposed_scope_name=decision.proposed_scope_name,
                proposed_setting_name=decision.proposed_setting_name,
                proposed_value=decision.proposed_value,
                comparison_reason=decision.comparison_reason,
                exact_target_world_setting_id=context.exact_target_world_setting_id,
                context_versions=[
                    WorkerWorldSettingContextVersion(
                        world_setting_id=target.world_setting_id,
                        version=target.version,
                    )
                    for target in context.targets
                ],
                raw_comparison_json=raw_comparison_json,
            )
            try:
                await self.spring_client.complete_world_setting_comparison(
                    analysis_job_id,
                    candidate.candidate_id,
                    lease_token,
                    request,
                )
                return
            except SpringWorkerHttpError as exc:
                is_stale = (
                    exc.status_code == 409
                    and exc.spring_error_code == CONTEXT_STALE_ERROR_CODE
                )
                if not is_stale or attempt == self.max_context_attempts:
                    raise
                logger.info(
                    "World-setting comparison context changed; rebuilding. "
                    "analysis_job_id=%s candidate_id=%s attempt=%s/%s",
                    analysis_job_id,
                    candidate.candidate_id,
                    attempt,
                    self.max_context_attempts,
                )

    async def _load_all_subjects(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate: WorkerWorldSettingCandidatePayload,
    ) -> list[WorkerWorldSettingSubject]:
        subjects: list[WorkerWorldSettingSubject] = []
        page = 0
        while True:
            response = await self.spring_client.get_world_setting_subjects(
                analysis_job_id,
                lease_token,
                candidate.category,
                page,
            )
            subjects.extend(response.subjects)
            if not response.has_next:
                return subjects
            page += 1

    async def _compare_batch_with_fresh_context(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        batch: WorkerWorldSettingComparisonBatchPayload,
        batch_sequence: int,
        cluster_usage_sink: list[WorldSettingComparisonClusterUsage],
    ) -> _BatchComparisonStats:
        stale_retry_count = 0
        for attempt in range(1, self.max_context_attempts + 1):
            target_ids = batch.resolved_target_world_setting_ids
            context = await self.spring_client.get_world_setting_comparison_batch_context(
                analysis_job_id,
                batch.comparison_batch_id,
                lease_token,
                target_ids,
            )
            _validate_batch_context_response(batch, context)
            targets_by_id = {target.world_setting_id: target for target in context.targets}
            if set(target_ids) != set(targets_by_id):
                raise ComparisonValidationError(
                    "Backend batch context does not cover every selected target."
                )

            completion_decisions: list[WorkerWorldSettingComparisonBatchDecision] = []
            cluster_targets = [targets_by_id[target_id] for target_id in target_ids]
            usage_before = _component_usage_snapshot(self.comparator)
            try:
                comparison_result, raw_comparison = await self.comparator.compare_batch(
                    batch.category,
                    batch.candidates,
                    cluster_targets,
                )
            except BaseException:
                usage = _component_usage_snapshot(self.comparator).since(usage_before)
                cluster_usage_sink.append(
                    _unassigned_cluster_usage(
                        batch_sequence,
                        attempt,
                        len(batch.candidates),
                        usage,
                    )
                )
                raise
            usage = _component_usage_snapshot(self.comparator).since(usage_before)
            targets_by_ref = {
                f"T{index}": target
                for index, target in enumerate(cluster_targets, start=1)
            }
            try:
                for decision in comparison_result.decisions:
                    selected_target = (
                        None
                        if decision.target_ref is None
                        else targets_by_ref.get(decision.target_ref)
                    )
                    if decision.target_ref is not None and selected_target is None:
                        raise ComparisonValidationError(
                            "Unknown target ref in normalized batch decision: "
                            f"{decision.target_ref}"
                        )
                    canonical_subject_name = (
                        selected_target.subject_name
                        if selected_target is not None
                        else batch.canonical_subject_name
                    )
                    completion_decisions.append(
                        WorkerWorldSettingComparisonBatchDecision(
                            decision_ref=f"D{len(completion_decisions) + 1}",
                            source_candidate_refs=decision.source_candidate_refs,
                            existing_root_property_names_to_move=(
                                decision.existing_root_property_names_to_move
                            ),
                            canonical_subject_name=canonical_subject_name,
                            target_world_setting_id=(
                                None
                                if selected_target is None
                                else selected_target.world_setting_id
                            ),
                            matched_scope_name=decision.matched_scope_name,
                            matched_property_name=decision.matched_property_name,
                            consolidation_status=decision.consolidation_status,
                            suggested_operation=decision.operation,
                            comparison_review_reason=decision.review_reason,
                            proposed_scope_name=decision.proposed_scope_name,
                            proposed_setting_name=decision.proposed_setting_name,
                            proposed_value=decision.proposed_value,
                            comparison_reason=decision.comparison_reason,
                            raw_comparison_json=decision.model_dump(mode="json"),
                        )
                    )
                _validate_completion_coverage(batch.candidates, completion_decisions)
            except Exception:
                cluster_usage_sink.append(
                    _unassigned_cluster_usage(
                        batch_sequence,
                        attempt,
                        len(batch.candidates),
                        usage,
                    )
                )
                raise
            cluster_usage_sink.extend(
                _attributed_cluster_usages(
                    batch_sequence,
                    attempt,
                    completion_decisions,
                    usage,
                )
            )
            request = WorkerWorldSettingComparisonBatchCompleteRequest(
                context_versions=[
                    WorkerWorldSettingContextVersion(
                        world_setting_id=target.world_setting_id,
                        version=target.version,
                    )
                    for target in context.targets
                ],
                decisions=completion_decisions,
                raw_comparison_json={
                    "schemaVersion": "world-comparison-batch-v1",
                    "clusters": [raw_comparison],
                },
            )
            try:
                await self.spring_client.complete_world_setting_comparison_batch(
                    analysis_job_id,
                    batch.comparison_batch_id,
                    lease_token,
                    request,
                )
            except SpringWorkerHttpError as exc:
                is_stale = (
                    exc.status_code == 409
                    and exc.spring_error_code == CONTEXT_STALE_ERROR_CODE
                )
                if not is_stale or attempt == self.max_context_attempts:
                    raise
                stale_retry_count += 1
                logger.info(
                    "World-setting comparison batch context changed; rebuilding. "
                    "analysis_job_id=%s comparison_batch_id=%s attempt=%s/%s",
                    analysis_job_id,
                    batch.comparison_batch_id,
                    attempt,
                    self.max_context_attempts,
                )
                continue

            clustered_candidate_count = sum(
                len(decision.source_candidate_refs)
                for decision in completion_decisions
                if len(decision.source_candidate_refs) > 1
            )
            singleton_candidate_count = sum(
                1
                for decision in completion_decisions
                if len(decision.source_candidate_refs) == 1
            )
            return _BatchComparisonStats(
                candidate_count=len(batch.candidates),
                decision_count=len(completion_decisions),
                cluster_count=len(completion_decisions),
                clustered_candidate_count=clustered_candidate_count,
                singleton_candidate_count=singleton_candidate_count,
                stale_retry_count=stale_retry_count,
            )
        raise AssertionError("Unreachable batch comparison attempt loop.")

    async def _load_all_subjects_for_category(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        category: WorldSettingCategory,
    ) -> list[WorkerWorldSettingSubject]:
        subjects: list[WorkerWorldSettingSubject] = []
        page = 0
        while True:
            response = await self.spring_client.get_world_setting_subjects(
                analysis_job_id,
                lease_token,
                category,
                page,
            )
            subjects.extend(response.subjects)
            if not response.has_next:
                return subjects
            page += 1

    async def _select_context_target_ids(
        self,
        candidate: WorkerWorldSettingCandidatePayload,
        subjects: list[WorkerWorldSettingSubject],
    ) -> list[UUID]:
        candidate_key = _normalized_name(candidate.subject_name)
        exact_matches = [
            subject
            for subject in subjects
            if _normalized_name(subject.subject_name) == candidate_key
        ]
        if exact_matches:
            return [subject.world_setting_id for subject in exact_matches]
        return [
            reference.world_setting_id
            for reference in await self.subject_resolver.select_subjects(candidate, subjects)
        ]


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _component_usage_snapshot(component: object) -> TextGenerationUsageSnapshot:
    llm_client = getattr(component, "llm_client", None)
    snapshot = getattr(llm_client, "usage_snapshot", None)
    if not callable(snapshot):
        return TextGenerationUsageSnapshot()
    value = snapshot()
    return value if isinstance(value, TextGenerationUsageSnapshot) else TextGenerationUsageSnapshot()


def _batch_usage(
    batch_sequence: int,
    candidate_count: int,
    cluster_count: int,
    usage: TextGenerationUsageSnapshot,
) -> WorldSettingComparisonBatchUsage:
    return WorldSettingComparisonBatchUsage(
        batch_sequence=batch_sequence,
        candidate_count=candidate_count,
        cluster_count=cluster_count,
        provider_request_count=usage.provider_request_count,
        provider_latency_ms=usage.provider_latency_ms,
        input_token_count=usage.input_token_count,
        cached_input_token_count=usage.cached_input_token_count,
        output_token_count=usage.output_token_count,
    )


def _unassigned_cluster_usage(
    batch_sequence: int,
    context_attempt: int,
    source_candidate_count: int,
    usage: TextGenerationUsageSnapshot,
) -> WorldSettingComparisonClusterUsage:
    return WorldSettingComparisonClusterUsage(
        batch_sequence=batch_sequence,
        context_attempt=context_attempt,
        cluster_sequence=0,
        source_candidate_count=source_candidate_count,
        usage_attribution="UNASSIGNED_FAILED_BATCH_REQUEST",
        provider_request_count=usage.provider_request_count,
        provider_latency_ms=usage.provider_latency_ms,
        input_token_count=usage.input_token_count,
        cached_input_token_count=usage.cached_input_token_count,
        output_token_count=usage.output_token_count,
    )


def _attributed_cluster_usages(
    batch_sequence: int,
    context_attempt: int,
    decisions: list[WorkerWorldSettingComparisonBatchDecision],
    usage: TextGenerationUsageSnapshot,
) -> list[WorldSettingComparisonClusterUsage]:
    weights = [len(decision.source_candidate_refs) for decision in decisions]
    request_counts = _allocate_integer_total(usage.provider_request_count, weights)
    latencies = _allocate_integer_total(usage.provider_latency_ms, weights)
    input_tokens = _allocate_integer_total(usage.input_token_count, weights)
    cached_input_tokens = _allocate_integer_total(
        usage.cached_input_token_count,
        weights,
    )
    output_tokens = _allocate_integer_total(usage.output_token_count, weights)
    return [
        WorldSettingComparisonClusterUsage(
            batch_sequence=batch_sequence,
            context_attempt=context_attempt,
            cluster_sequence=index + 1,
            source_candidate_count=weight,
            usage_attribution="PROPORTIONAL_SHARED_BATCH_REQUEST",
            provider_request_count=request_counts[index],
            provider_latency_ms=latencies[index],
            input_token_count=input_tokens[index],
            cached_input_token_count=cached_input_tokens[index],
            output_token_count=output_tokens[index],
        )
        for index, weight in enumerate(weights)
    ]


def _allocate_integer_total(total: int, weights: list[int]) -> list[int]:
    if not weights:
        return []
    weight_sum = sum(weights)
    if total <= 0 or weight_sum <= 0:
        return [0] * len(weights)
    allocations = [(total * weight) // weight_sum for weight in weights]
    remainder = total - sum(allocations)
    remainder_order = sorted(
        range(len(weights)),
        key=lambda index: (-(total * weights[index] % weight_sum), index),
    )
    for index in remainder_order[:remainder]:
        allocations[index] += 1
    return allocations


def _validate_batch_context_response(
    batch: WorkerWorldSettingComparisonBatchPayload,
    context: WorkerWorldSettingComparisonBatchContextResponse,
) -> None:
    if context.comparison_batch_id != batch.comparison_batch_id:
        raise ComparisonValidationError(
            "Backend batch context belongs to a different comparison batch."
        )

    expected_candidates = {
        candidate.candidate_ref: candidate.candidate_id for candidate in batch.candidates
    }
    actual_candidates = {
        candidate.candidate_ref: candidate.candidate_id for candidate in context.candidates
    }
    if len(actual_candidates) != len(context.candidates):
        raise ComparisonValidationError(
            "Backend batch context contains duplicated candidate refs."
        )
    if actual_candidates != expected_candidates:
        raise ComparisonValidationError(
            "Backend batch context candidates do not match the claimed batch."
        )

    exact_targets = {
        exact_target.candidate_ref: exact_target.world_setting_id
        for exact_target in context.exact_targets
    }
    if len(exact_targets) != len(context.exact_targets):
        raise ComparisonValidationError(
            "Backend batch context contains duplicated exact-target refs."
        )
    if set(exact_targets) != set(expected_candidates):
        raise ComparisonValidationError(
            "Backend batch context must include one exact-target entry per candidate."
        )

    target_ids = [target.world_setting_id for target in context.targets]
    if len(set(target_ids)) != len(target_ids):
        raise ComparisonValidationError(
            "Backend batch context contains duplicated comparison targets."
        )
    missing_exact_target_ids = {
        target_id
        for target_id in exact_targets.values()
        if target_id is not None and target_id not in set(target_ids)
    }
    if missing_exact_target_ids:
        raise ComparisonValidationError(
            "Backend batch context omits an exact comparison target."
        )


def _validate_subject_resolution_response(
    request: WorkerWorldSettingSubjectResolutionRequest,
    response: WorkerWorldSettingSubjectResolutionResponse,
) -> None:
    requested = {
        resolution.candidate_id: resolution.target_world_setting_ids
        for resolution in request.resolutions
    }
    actual = {
        resolution.candidate_id: resolution.target_world_setting_ids
        for resolution in response.resolutions
    }
    if len(requested) != len(request.resolutions):
        raise ComparisonValidationError(
            "World-setting subject-resolution request contains duplicated candidates."
        )
    if len(actual) != len(response.resolutions):
        raise ComparisonValidationError(
            "Backend subject-resolution response contains duplicated candidates."
        )
    if actual != requested:
        raise ComparisonValidationError(
            "Backend subject-resolution response does not match the submitted targets."
        )


def _validate_completion_coverage(
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    decisions: list[WorkerWorldSettingComparisonBatchDecision],
) -> None:
    expected_refs = {candidate.candidate_ref for candidate in candidates}
    if len(expected_refs) != len(candidates):
        raise ComparisonValidationError("Claimed batch contains duplicated candidate refs.")

    decision_refs = [decision.decision_ref for decision in decisions]
    if len(set(decision_refs)) != len(decision_refs):
        raise ComparisonValidationError("Batch completion contains duplicated decision refs.")

    source_refs = [
        source_ref
        for decision in decisions
        for source_ref in decision.source_candidate_refs
    ]
    unknown_refs = set(source_refs) - expected_refs
    if unknown_refs:
        raise ComparisonValidationError(
            f"Batch completion contains unknown candidate refs: {sorted(unknown_refs)}"
        )
    duplicated_refs = sorted(
        source_ref for source_ref in set(source_refs) if source_refs.count(source_ref) > 1
    )
    if duplicated_refs:
        raise ComparisonValidationError(
            f"Batch completion uses candidate refs more than once: {duplicated_refs}"
        )
    missing_refs = expected_refs - set(source_refs)
    if missing_refs:
        raise ComparisonValidationError(
            f"Batch completion omits candidate refs: {sorted(missing_refs)}"
        )
