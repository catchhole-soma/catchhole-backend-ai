import logging
import unicodedata
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

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
    WorkerWorldSettingComparisonCompleteRequest,
    WorkerWorldSettingComparisonContextResponse,
    WorkerWorldSettingContextVersion,
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectPageResponse,
)

logger = logging.getLogger(__name__)
CONTEXT_STALE_ERROR_CODE = "WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE"


class WorldSettingComparisonSpringApi(Protocol):
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
class WorldSettingComparisonRunResult:
    completed_count: int
    failed_count: int
    first_failure_code: AnalysisFailureCode | None = None


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
            return [exact_matches[0].world_setting_id]
        return [
            reference.world_setting_id
            for reference in await self.subject_resolver.select_subjects(candidate, subjects)
        ]


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()
