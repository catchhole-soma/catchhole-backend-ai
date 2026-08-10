import logging
import unicodedata
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.domain.enums import WorldSettingCategory
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
    def claim_next_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
    ) -> WorkerWorldSettingCandidatePayload | None: ...

    def get_world_setting_subjects(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        category: WorldSettingCategory,
        page: int,
        size: int = 500,
    ) -> WorkerWorldSettingSubjectPageResponse: ...

    def get_world_setting_comparison_context(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        target_world_setting_ids: list[UUID],
    ) -> WorkerWorldSettingComparisonContextResponse: ...

    def complete_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        request: WorkerWorldSettingComparisonCompleteRequest,
    ) -> None: ...

    def fail_world_setting_comparison(
        self,
        analysis_job_id: UUID,
        candidate_id: UUID,
        lease_token: UUID,
        error_message: str,
    ) -> None: ...


@dataclass(frozen=True)
class WorldSettingComparisonRunResult:
    completed_count: int
    failed_count: int


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

    def process_all(
        self, analysis_job_id: UUID, lease_token: UUID
    ) -> WorldSettingComparisonRunResult:
        completed_count = 0
        failed_count = 0
        while True:
            candidate = self.spring_client.claim_next_world_setting_comparison(
                analysis_job_id,
                lease_token,
            )
            if candidate is None:
                return WorldSettingComparisonRunResult(completed_count, failed_count)
            if self._process_claimed_candidate(analysis_job_id, lease_token, candidate):
                completed_count += 1
            else:
                failed_count += 1

    def _process_claimed_candidate(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate: WorkerWorldSettingCandidatePayload,
    ) -> bool:
        try:
            self._compare_with_fresh_context(analysis_job_id, lease_token, candidate)
            return True
        except Exception as exc:
            error_message = (str(exc) or exc.__class__.__name__)[:1000]
            self.spring_client.fail_world_setting_comparison(
                analysis_job_id,
                candidate.candidate_id,
                lease_token,
                error_message,
            )
            logger.exception(
                "World-setting comparison failed. analysis_job_id=%s candidate_id=%s",
                analysis_job_id,
                candidate.candidate_id,
            )
            return False

    def _compare_with_fresh_context(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate: WorkerWorldSettingCandidatePayload,
    ) -> None:
        for attempt in range(1, self.max_context_attempts + 1):
            subjects = self._load_all_subjects(analysis_job_id, lease_token, candidate)
            target_ids = self._select_context_target_ids(candidate, subjects)
            context = self.spring_client.get_world_setting_comparison_context(
                analysis_job_id,
                candidate.candidate_id,
                lease_token,
                target_ids,
            )
            decision, raw_comparison_json = self.comparator.compare(
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
                self.spring_client.complete_world_setting_comparison(
                    analysis_job_id,
                    candidate.candidate_id,
                    lease_token,
                    request,
                )
                return
            except httpx.HTTPStatusError as exc:
                if not _has_error_code(exc, CONTEXT_STALE_ERROR_CODE) or (
                    attempt == self.max_context_attempts
                ):
                    raise
                logger.info(
                    "World-setting comparison context changed; rebuilding. "
                    "analysis_job_id=%s candidate_id=%s attempt=%s/%s",
                    analysis_job_id,
                    candidate.candidate_id,
                    attempt,
                    self.max_context_attempts,
                )

    def _load_all_subjects(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate: WorkerWorldSettingCandidatePayload,
    ) -> list[WorkerWorldSettingSubject]:
        subjects: list[WorkerWorldSettingSubject] = []
        page = 0
        while True:
            response = self.spring_client.get_world_setting_subjects(
                analysis_job_id,
                lease_token,
                candidate.category,
                page,
            )
            subjects.extend(response.subjects)
            if not response.has_next:
                return subjects
            page += 1

    def _select_context_target_ids(
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
            for reference in self.subject_resolver.select_subjects(candidate, subjects)
        ]


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _has_error_code(exc: httpx.HTTPStatusError, expected_code: str) -> bool:
    try:
        payload = exc.response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    return isinstance(error, dict) and error.get("code") == expected_code
