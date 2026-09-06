import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
)
from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
)
from app.analysis.exceptions import ComparisonValidationError
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError
from app.domain.enums import AnalysisFailureCode, CharacterFactComparisonOperation
from app.domain.setting_values import normalize_setting_display_value
from app.exceptions.failure_classification import comparison_failure_code
from app.schemas.worker import (
    WorkerCharacterFactComparisonClaimPayload,
    WorkerCharacterFactComparisonBatchCompleteRequest,
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchContextResponse,
    WorkerCharacterFactComparisonBatchDecision,
    WorkerCharacterFactComparisonBatchFailure,
    WorkerCharacterFactComparisonBatchPayload,
    WorkerCharacterFactComparisonBatchSnapshotEntry,
    WorkerCharacterFactComparisonCompleteRequest,
    WorkerCharacterFactComparisonContextResponse,
    WorkerRemovedSnapshotEntry,
)
from app.usage.metering import TextGenerationUsageSnapshot

logger = logging.getLogger(__name__)
CONTEXT_STALE_ERROR_CODE = "SETTING_CANDIDATE_COMPARISON_STALE"
SINGLETON_ISOLATABLE_FAILURE_CODES = frozenset(
    {
        AnalysisFailureCode.LLM_NETWORK_ERROR,
        AnalysisFailureCode.LLM_PROVIDER_ERROR,
        AnalysisFailureCode.LLM_OUTPUT_TRUNCATED,
        AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
        AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
    }
)


class CharacterFactComparisonSpringApi(Protocol):
    async def claim_next_character_fact_comparison_batch(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerCharacterFactComparisonBatchPayload | None: ...

    async def get_character_fact_comparison_batch_context(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
    ) -> WorkerCharacterFactComparisonBatchContextResponse: ...

    async def complete_character_fact_comparison_batch(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
        request: WorkerCharacterFactComparisonBatchCompleteRequest,
    ) -> None: ...

    async def fail_character_fact_comparison_batch(
        self,
        analysis_job_id: UUID,
        comparison_batch_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode,
    ) -> None: ...

    async def claim_next_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerCharacterFactComparisonClaimPayload | None: ...

    async def get_character_fact_comparison_context(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
    ) -> WorkerCharacterFactComparisonContextResponse: ...

    async def complete_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        request: WorkerCharacterFactComparisonCompleteRequest,
    ) -> None: ...

    async def fail_character_fact_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        error_message: str,
        failure_code: AnalysisFailureCode,
    ) -> None: ...


class CharacterFactBatchComparator(Protocol):
    """Minimal comparator surface shared by production and evaluation runtimes."""

    def batch_fits(
        self,
        *,
        matched_character_name: str,
        canonical_fact_type: str,
        candidates: list[WorkerCharacterFactComparisonBatchCandidate],
        snapshot_entries: list[CharacterProjectionEntry],
    ) -> bool: ...

    async def compare_batch(
        self,
        *,
        matched_character_name: str,
        canonical_fact_type: str,
        candidates: list[WorkerCharacterFactComparisonBatchCandidate],
        snapshot_entries: list[CharacterProjectionEntry],
    ) -> tuple[CharacterFactComparisonBatchResult, dict]: ...


@dataclass(frozen=True)
class CharacterFactComparisonRunResult:
    completed_count: int
    failed_count: int
    first_failure_code: AnalysisFailureCode | None = None
    batch_count: int = 0
    max_candidates_per_batch: int = 0
    provider_segment_count: int = 0
    singleton_fallback_count: int = 0
    batch_validation_failure_count: int = 0
    stale_batch_retry_count: int = 0
    usage: TextGenerationUsageSnapshot = field(default_factory=TextGenerationUsageSnapshot)

    def summary_metrics(self) -> dict[str, object]:
        candidate_count = self.completed_count + self.failed_count
        average_candidates_per_batch = (
            candidate_count / self.batch_count if self.batch_count else 0.0
        )
        average_input_tokens_per_candidate = (
            self.usage.input_token_count / candidate_count if candidate_count else 0.0
        )
        average_output_tokens_per_candidate = (
            self.usage.output_token_count / candidate_count if candidate_count else 0.0
        )
        return {
            "characterComparisonRequestCount": self.usage.provider_request_count,
            "characterComparisonBatchCount": self.batch_count,
            "characterComparisonAverageCandidatesPerBatch": round(
                average_candidates_per_batch,
                4,
            ),
            "characterComparisonMaxCandidatesPerBatch": self.max_candidates_per_batch,
            "characterComparisonProviderSegmentCount": self.provider_segment_count,
            "characterComparisonBatchFallbackCandidateCount": self.singleton_fallback_count,
            "characterComparisonBatchValidationFailureCount": (
                self.batch_validation_failure_count
            ),
            "characterComparisonStaleBatchRetryCount": self.stale_batch_retry_count,
            "characterComparisonProviderRequestCount": self.usage.provider_request_count,
            "characterComparisonProviderLatencyMs": self.usage.provider_latency_ms,
            "characterComparisonInputTokenCount": self.usage.input_token_count,
            "characterComparisonCachedInputTokenCount": self.usage.cached_input_token_count,
            "characterComparisonOutputTokenCount": self.usage.output_token_count,
            "characterComparisonAverageInputTokensPerCandidate": round(
                average_input_tokens_per_candidate,
                4,
            ),
            "characterComparisonAverageOutputTokensPerCandidate": round(
                average_output_tokens_per_candidate,
                4,
            ),
        }


@dataclass(frozen=True)
class _CharacterBatchStats:
    completed_count: int
    failed_count: int
    first_failure_code: AnalysisFailureCode | None
    provider_segment_count: int
    singleton_fallback_count: int
    batch_validation_failure_count: int
    stale_retry_count: int


@dataclass(frozen=True)
class CharacterFactBatchExecutionResult:
    """Pure provider/projection result before Spring atomic completion."""

    decisions: list[WorkerCharacterFactComparisonBatchDecision]
    failures: list[WorkerCharacterFactComparisonBatchFailure]
    provider_segment_count: int
    singleton_fallback_count: int
    validation_failure_count: int


async def execute_character_fact_comparison_batch(
    comparator: CharacterFactBatchComparator,
    *,
    matched_character_name: str,
    canonical_fact_type: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    snapshot_entries: list[
        WorkerCharacterFactComparisonBatchSnapshotEntry | CharacterProjectionEntry
    ],
) -> CharacterFactBatchExecutionResult:
    """Split, compare, and project one ordered character+FactType batch.

    Spring claim/context/stale/complete ownership intentionally stays outside this
    function. Evaluation can therefore reuse the exact provider fallback and Q-ref
    projection behavior without constructing transport UUIDs or writing state.
    """

    state = CharacterProjectionState(
        [_to_projection_entry(entry) for entry in snapshot_entries]
    )
    decisions: list[WorkerCharacterFactComparisonBatchDecision] = []
    failures: list[WorkerCharacterFactComparisonBatchFailure] = []
    provider_segment_count = 0
    singleton_fallback_count = 0
    validation_failure_count = 0
    cursor = 0
    while cursor < len(candidates):
        segment = _largest_fitting_segment(
            comparator,
            matched_character_name=matched_character_name,
            canonical_fact_type=canonical_fact_type,
            candidates=candidates,
            state=state,
            start=cursor,
        )
        if not segment:
            # Spring도 문자 수로 batch를 자르지만 tokenizer 기준으로 단일 후보가
            # 여전히 너무 클 수 있다. Provider나 의미 없는 singleton fallback을
            # 호출하지 않고 typed candidate failure로 원자적 complete에 포함한다.
            failures.append(
                _candidate_failure(
                    candidates[cursor].candidate_ref,
                    ComparisonValidationError("character_batch_input_limit_exceeded"),
                )
            )
            validation_failure_count += 1
            cursor += 1
            continue
        provider_segment_count += 1
        try:
            result, _ = await comparator.compare_batch(
                matched_character_name=matched_character_name,
                canonical_fact_type=canonical_fact_type,
                candidates=segment,
                snapshot_entries=state.entries,
            )
        except ComparisonValidationError:
            validation_failure_count += 1
            if len(segment) == 1:
                failures.append(_candidate_failure(segment[0].candidate_ref))
                cursor += 1
                continue
            singleton_fallback_count += len(segment)
            for candidate in segment:
                provider_segment_count += 1
                try:
                    singleton_result, _ = await comparator.compare_batch(
                        matched_character_name=matched_character_name,
                        canonical_fact_type=canonical_fact_type,
                        candidates=[candidate],
                        snapshot_entries=state.entries,
                    )
                    decisions.append(
                        _apply_and_map_decision(
                            state,
                            canonical_fact_type,
                            candidate,
                            singleton_result.decisions[0],
                        )
                    )
                except AiTokenQuotaExhaustedError:
                    # Quota exhaustion is job-scoped. Continuing with later singletons would
                    # only create more rejected reservations and violate the no-fallback rule.
                    raise
                except Exception as exc:
                    failure_code = comparison_failure_code(exc)
                    if failure_code not in SINGLETON_ISOLATABLE_FAILURE_CODES:
                        # Lease/context/transport invariants are not candidate-local provider
                        # failures and must remain visible to the outer batch/job boundary.
                        raise
                    failures.append(_candidate_failure(candidate.candidate_ref, exc))
            cursor += len(segment)
            continue

        by_ref = {decision.candidate_ref: decision for decision in result.decisions}
        for candidate in segment:
            decisions.append(
                _apply_and_map_decision(
                    state,
                    canonical_fact_type,
                    candidate,
                    by_ref[candidate.candidate_ref],
                )
            )
        cursor += len(segment)
    return CharacterFactBatchExecutionResult(
        decisions=decisions,
        failures=failures,
        provider_segment_count=provider_segment_count,
        singleton_fallback_count=singleton_fallback_count,
        validation_failure_count=validation_failure_count,
    )


class CharacterFactComparisonPipeline:
    def __init__(
        self,
        spring_client: CharacterFactComparisonSpringApi,
        comparator: CharacterFactComparator,
        max_context_attempts: int = 3,
    ) -> None:
        if max_context_attempts < 1:
            raise ValueError("max_context_attempts must be at least 1.")
        self.spring_client = spring_client
        self.comparator = comparator
        self.max_context_attempts = max_context_attempts

    async def process_all(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> CharacterFactComparisonRunResult:
        """Prefer atomic ordered batches, retaining the legacy endpoint for rollout."""

        if hasattr(self.spring_client, "claim_next_character_fact_comparison_batch"):
            return await self._process_all_batches(analysis_job_id, lease_token)
        return await self._process_all_legacy_candidates(analysis_job_id, lease_token)

    async def _process_all_legacy_candidates(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> CharacterFactComparisonRunResult:

        completed_count = 0
        failed_count = 0
        first_failure_code: AnalysisFailureCode | None = None
        while True:
            claimed = await self.spring_client.claim_next_character_fact_comparison(
                analysis_job_id,
                lease_token,
            )
            if claimed is None:
                return CharacterFactComparisonRunResult(
                    completed_count,
                    failed_count,
                    first_failure_code,
                )
            failure_code = await self._process_claimed_candidate(
                analysis_job_id,
                lease_token,
                claimed.candidate_id,
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
    ) -> CharacterFactComparisonRunResult:
        completed_count = 0
        failed_count = 0
        first_failure_code: AnalysisFailureCode | None = None
        batch_count = 0
        max_candidates_per_batch = 0
        provider_segment_count = 0
        singleton_fallback_count = 0
        batch_validation_failure_count = 0
        stale_batch_retry_count = 0
        usage_before = _component_usage_snapshot(self.comparator)
        while True:
            batch = await self.spring_client.claim_next_character_fact_comparison_batch(
                analysis_job_id,
                lease_token,
            )
            if batch is None:
                return CharacterFactComparisonRunResult(
                    completed_count=completed_count,
                    failed_count=failed_count,
                    first_failure_code=first_failure_code,
                    batch_count=batch_count,
                    max_candidates_per_batch=max_candidates_per_batch,
                    provider_segment_count=provider_segment_count,
                    singleton_fallback_count=singleton_fallback_count,
                    batch_validation_failure_count=batch_validation_failure_count,
                    stale_batch_retry_count=stale_batch_retry_count,
                    usage=_component_usage_snapshot(self.comparator).since(usage_before),
                )
            batch_count += 1
            max_candidates_per_batch = max(max_candidates_per_batch, len(batch.candidates))
            try:
                stats = await self._compare_batch_with_fresh_context(
                    analysis_job_id,
                    lease_token,
                    batch,
                )
            except AiTokenQuotaExhaustedError as exc:
                await self.spring_client.fail_character_fact_comparison_batch(
                    analysis_job_id,
                    batch.comparison_batch_id,
                    lease_token,
                    (str(exc) or exc.__class__.__name__)[:1000],
                    AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED,
                )
                raise
            except Exception as exc:
                failure_code = comparison_failure_code(exc)
                await self.spring_client.fail_character_fact_comparison_batch(
                    analysis_job_id,
                    batch.comparison_batch_id,
                    lease_token,
                    (str(exc) or exc.__class__.__name__)[:1000],
                    failure_code,
                )
                failed_count += len(batch.candidates)
                if first_failure_code is None:
                    first_failure_code = failure_code
                if failure_code is AnalysisFailureCode.COMPARISON_VALIDATION_FAILED:
                    batch_validation_failure_count += 1
                logger.exception(
                    "Character-fact batch comparison failed. analysis_job_id=%s "
                    "comparison_batch_id=%s candidate_count=%s",
                    analysis_job_id,
                    batch.comparison_batch_id,
                    len(batch.candidates),
                )
                continue

            completed_count += stats.completed_count
            failed_count += stats.failed_count
            provider_segment_count += stats.provider_segment_count
            singleton_fallback_count += stats.singleton_fallback_count
            batch_validation_failure_count += stats.batch_validation_failure_count
            stale_batch_retry_count += stats.stale_retry_count
            if first_failure_code is None:
                first_failure_code = stats.first_failure_code

    async def _compare_batch_with_fresh_context(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        batch: WorkerCharacterFactComparisonBatchPayload,
    ) -> _CharacterBatchStats:
        stale_retry_count = 0
        total_provider_segment_count = 0
        total_singleton_fallback_count = 0
        total_validation_failure_count = 0
        for attempt in range(1, self.max_context_attempts + 1):
            context = await self.spring_client.get_character_fact_comparison_batch_context(
                analysis_job_id,
                batch.comparison_batch_id,
                lease_token,
            )
            _validate_batch_context(batch, context)
            execution = await execute_character_fact_comparison_batch(
                self.comparator,
                matched_character_name=context.matched_character_name,
                canonical_fact_type=context.canonical_fact_type,
                candidates=context.candidates,
                snapshot_entries=context.snapshot_entries,
            )
            decisions = execution.decisions
            failures = execution.failures
            provider_segment_count = execution.provider_segment_count
            singleton_fallback_count = execution.singleton_fallback_count
            validation_failure_count = execution.validation_failure_count
            total_provider_segment_count += provider_segment_count
            total_singleton_fallback_count += singleton_fallback_count
            total_validation_failure_count += validation_failure_count
            _validate_batch_completion_coverage(context, decisions, failures)
            request = WorkerCharacterFactComparisonBatchCompleteRequest(
                context_token=context.context_token,
                decisions=decisions,
                failures=failures,
                raw_comparison_json={
                    "schemaVersion": "character-comparison-batch-v1",
                    "providerSegmentCount": provider_segment_count,
                    "singletonFallbackCount": singleton_fallback_count,
                },
            )
            try:
                await self.spring_client.complete_character_fact_comparison_batch(
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
                    "Character snapshot changed; rebuilding the full batch. "
                    "analysis_job_id=%s comparison_batch_id=%s attempt=%s/%s",
                    analysis_job_id,
                    batch.comparison_batch_id,
                    attempt,
                    self.max_context_attempts,
                )
                continue
            first_failure_code = failures[0].failure_code if failures else None
            return _CharacterBatchStats(
                completed_count=len(decisions),
                failed_count=len(failures),
                first_failure_code=first_failure_code,
                provider_segment_count=total_provider_segment_count,
                singleton_fallback_count=total_singleton_fallback_count,
                batch_validation_failure_count=total_validation_failure_count,
                stale_retry_count=stale_retry_count,
            )
        raise AssertionError("Unreachable character batch comparison attempt loop.")

    async def _process_claimed_candidate(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate_id: UUID,
    ) -> AnalysisFailureCode | None:
        try:
            await self._compare_with_fresh_context(
                analysis_job_id,
                lease_token,
                candidate_id,
            )
            return None
        except AiTokenQuotaExhaustedError as exc:
            await self.spring_client.fail_character_fact_comparison(
                analysis_job_id,
                candidate_id,
                lease_token,
                (str(exc) or exc.__class__.__name__)[:1000],
                AnalysisFailureCode.AI_TOKEN_QUOTA_EXHAUSTED,
            )
            raise
        except Exception as exc:
            error_message = (str(exc) or exc.__class__.__name__)[:1000]
            failure_code = comparison_failure_code(exc)
            await self.spring_client.fail_character_fact_comparison(
                analysis_job_id,
                candidate_id,
                lease_token,
                error_message,
                failure_code,
            )
            logger.exception(
                "Character-fact comparison failed. analysis_job_id=%s candidate_id=%s",
                analysis_job_id,
                candidate_id,
            )
            return failure_code

    async def _compare_with_fresh_context(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate_id: UUID,
    ) -> None:
        for attempt in range(1, self.max_context_attempts + 1):
            context = await self.spring_client.get_character_fact_comparison_context(
                analysis_job_id,
                candidate_id,
                lease_token,
            )
            decision, raw_comparison_json = await self.comparator.compare(
                context.candidate,
                context.snapshot_entries,
                context.prior_candidates,
            )
            entries_by_ref = {
                f"P{index}": entry for index, entry in enumerate(context.snapshot_entries, start=1)
            }
            target = None if decision.target_ref is None else entries_by_ref[decision.target_ref]
            proposed_fact_value = decision.proposed_fact_value
            if decision.operation in {
                CharacterFactComparisonOperation.ADD,
                CharacterFactComparisonOperation.UPDATE,
                CharacterFactComparisonOperation.MERGE,
            }:
                proposed_fact_value = normalize_setting_display_value(
                    context.candidate.value_type,
                    decision.proposed_value_json,
                    decision.proposed_fact_value,
                )
            request = WorkerCharacterFactComparisonCompleteRequest(
                operation=decision.operation,
                target_fact_type=None if target is None else target.fact_type,
                target_fact_key=None if target is None else target.fact_key,
                removed_snapshot_entries=[
                    WorkerRemovedSnapshotEntry(
                        fact_type=entries_by_ref[reference].fact_type,
                        fact_key=entries_by_ref[reference].fact_key,
                    )
                    for reference in decision.removed_snapshot_refs
                ],
                proposed_fact_value=proposed_fact_value,
                proposed_value_json=decision.proposed_value_json,
                temporal_scope=decision.temporal_scope,
                comparison_reason=decision.comparison_reason,
                context_token=context.context_token,
                # P1/P2는 한 번의 provider 요청에서만 유효한 임시 참조다. DB 감사 원문에는
                # 최종 판단만 남기고 재생성할 수 없는 요청 로컬 식별자는 저장하지 않는다.
                raw_comparison_json=_without_request_local_refs(raw_comparison_json),
            )
            try:
                await self.spring_client.complete_character_fact_comparison(
                    analysis_job_id,
                    candidate_id,
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
                    "Character snapshot changed; rebuilding comparison context. "
                    "analysis_job_id=%s candidate_id=%s attempt=%s/%s",
                    analysis_job_id,
                    candidate_id,
                    attempt,
                    self.max_context_attempts,
                )


def _without_request_local_refs(raw_comparison_json: dict) -> dict:
    local_ref_fields = {
        "candidate_ref",
        "projected_snapshot_ref",
        "target_ref",
        "removed_snapshot_refs",
    }
    return {
        key: _without_request_local_refs_value(value)
        for key, value in raw_comparison_json.items()
        if key not in local_ref_fields
    }


def _without_request_local_refs_value(value):
    if isinstance(value, dict):
        return _without_request_local_refs(value)
    if isinstance(value, list):
        return [_without_request_local_refs_value(item) for item in value]
    return value


def _validate_batch_context(
    batch: WorkerCharacterFactComparisonBatchPayload,
    context: WorkerCharacterFactComparisonBatchContextResponse,
) -> None:
    if context.comparison_batch_id != batch.comparison_batch_id:
        raise ComparisonValidationError("Character batch context ID does not match the claim.")
    if context.character_ref != batch.character_ref:
        raise ComparisonValidationError("Character batch context character does not match.")
    if context.matched_character_name != batch.matched_character_name:
        raise ComparisonValidationError("Character batch context display name does not match.")
    if context.canonical_fact_type != batch.canonical_fact_type:
        raise ComparisonValidationError("Character batch context Fact type does not match.")
    if context.candidates != batch.candidates:
        raise ComparisonValidationError(
            "Character batch context candidates changed after the claim."
        )


def _to_projection_entry(
    entry: WorkerCharacterFactComparisonBatchSnapshotEntry | CharacterProjectionEntry,
) -> CharacterProjectionEntry:
    if isinstance(entry, CharacterProjectionEntry):
        return entry
    return CharacterProjectionEntry(
        reference=entry.snapshot_ref,
        origin=entry.origin,
        source_candidate_ref=entry.source_candidate_ref,
        dependency_candidate_refs=tuple(entry.dependency_candidate_refs),
        fact_type=entry.fact_type,
        fact_key=entry.fact_key,
        fact_value=entry.fact_value,
        value_json=entry.value_json,
    )


def _largest_fitting_segment(
    comparator: CharacterFactBatchComparator,
    *,
    matched_character_name: str,
    canonical_fact_type: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    state: CharacterProjectionState,
    start: int,
) -> list[WorkerCharacterFactComparisonBatchCandidate]:
    segment: list[WorkerCharacterFactComparisonBatchCandidate] = []
    for candidate in candidates[start:]:
        proposed = [*segment, candidate]
        if not comparator.batch_fits(
            matched_character_name=matched_character_name,
            canonical_fact_type=canonical_fact_type,
            candidates=proposed,
            snapshot_entries=state.entries,
        ):
            break
        segment = proposed
    return segment


def _apply_and_map_decision(
    state: CharacterProjectionState,
    canonical_fact_type: str,
    candidate: WorkerCharacterFactComparisonBatchCandidate,
    decision: CharacterFactComparisonBatchDecision,
) -> WorkerCharacterFactComparisonBatchDecision:
    application = state.apply(
        candidate_ref=candidate.candidate_ref,
        projected_snapshot_ref=candidate.projected_snapshot_ref,
        fact_type=canonical_fact_type,
        resolved_fact_key=decision.resolved_canonical_fact_key,
        value_type=candidate.value_type,
        candidate_value_json=candidate.value_json,
        decision=decision,
    )
    return WorkerCharacterFactComparisonBatchDecision(
        candidate_ref=candidate.candidate_ref,
        operation=decision.operation,
        resolved_canonical_fact_key=decision.resolved_canonical_fact_key,
        target_snapshot_ref=decision.target_ref,
        removed_snapshot_refs=decision.removed_snapshot_refs,
        dependency_candidate_refs=list(application.dependency_candidate_refs),
        proposed_fact_value=decision.proposed_fact_value,
        proposed_value_json=decision.proposed_value_json,
        temporal_scope=decision.temporal_scope,
        comparison_reason=decision.comparison_reason,
        raw_comparison_json=_without_request_local_refs(
            decision.model_dump(mode="json")
        ),
    )


def _candidate_failure(
    candidate_ref: str,
    exc: Exception | None = None,
) -> WorkerCharacterFactComparisonBatchFailure:
    failure = exc or ComparisonValidationError(
        "Character comparison response failed schema validation."
    )
    return WorkerCharacterFactComparisonBatchFailure(
        candidate_ref=candidate_ref,
        failure_code=comparison_failure_code(failure),
        error_message=(str(failure) or failure.__class__.__name__)[:1000],
    )


def _validate_batch_completion_coverage(
    context: WorkerCharacterFactComparisonBatchContextResponse,
    decisions: list[WorkerCharacterFactComparisonBatchDecision],
    failures: list[WorkerCharacterFactComparisonBatchFailure],
) -> None:
    expected = [candidate.candidate_ref for candidate in context.candidates]
    actual = [decision.candidate_ref for decision in decisions] + [
        failure.candidate_ref for failure in failures
    ]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ComparisonValidationError(
            "Character batch completion must cover every candidate exactly once."
        )


def _component_usage_snapshot(component: object) -> TextGenerationUsageSnapshot:
    llm_client = getattr(component, "llm_client", None)
    snapshot = getattr(llm_client, "usage_snapshot", None)
    if not callable(snapshot):
        return TextGenerationUsageSnapshot()
    value = snapshot()
    return value if isinstance(value, TextGenerationUsageSnapshot) else TextGenerationUsageSnapshot()
