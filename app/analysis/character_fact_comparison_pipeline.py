import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.domain.enums import AnalysisFailureCode
from app.domain.setting_values import normalize_setting_display_value
from app.exceptions.failure_classification import comparison_failure_code
from app.schemas.worker import (
    WorkerCharacterFactComparisonClaimPayload,
    WorkerCharacterFactComparisonCompleteRequest,
    WorkerCharacterFactComparisonContextResponse,
    WorkerRemovedSnapshotEntry,
)

logger = logging.getLogger(__name__)
CONTEXT_STALE_ERROR_CODE = "SETTING_CANDIDATE_COMPARISON_STALE"


class CharacterFactComparisonSpringApi(Protocol):
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


@dataclass(frozen=True)
class CharacterFactComparisonRunResult:
    completed_count: int
    failed_count: int


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
        """후보를 하나씩 claim해 비교하며, 개별 실패는 다음 후보와 격리한다."""

        completed_count = 0
        failed_count = 0
        while True:
            claimed = await self.spring_client.claim_next_character_fact_comparison(
                analysis_job_id,
                lease_token,
            )
            if claimed is None:
                return CharacterFactComparisonRunResult(completed_count, failed_count)
            if await self._process_claimed_candidate(
                analysis_job_id,
                lease_token,
                claimed.candidate_id,
            ):
                completed_count += 1
            else:
                failed_count += 1

    async def _process_claimed_candidate(
        self,
        analysis_job_id: UUID,
        lease_token: UUID,
        candidate_id: UUID,
    ) -> bool:
        try:
            await self._compare_with_fresh_context(
                analysis_job_id,
                lease_token,
                candidate_id,
            )
            return True
        except AiTokenQuotaExhaustedError:
            raise
        except Exception as exc:
            error_message = (str(exc) or exc.__class__.__name__)[:1000]
            await self.spring_client.fail_character_fact_comparison(
                analysis_job_id,
                candidate_id,
                lease_token,
                error_message,
                comparison_failure_code(exc),
            )
            logger.exception(
                "Character-fact comparison failed. analysis_job_id=%s candidate_id=%s",
                analysis_job_id,
                candidate_id,
            )
            return False

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
                proposed_fact_value=normalize_setting_display_value(
                    context.candidate.value_type,
                    decision.proposed_value_json,
                    decision.proposed_fact_value,
                ),
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
            except httpx.HTTPStatusError as exc:
                if not _has_error_code(exc, CONTEXT_STALE_ERROR_CODE) or (
                    attempt == self.max_context_attempts
                ):
                    raise
                logger.info(
                    "Character snapshot changed; rebuilding comparison context. "
                    "analysis_job_id=%s candidate_id=%s attempt=%s/%s",
                    analysis_job_id,
                    candidate_id,
                    attempt,
                    self.max_context_attempts,
                )


def _has_error_code(exc: httpx.HTTPStatusError, expected_code: str) -> bool:
    if exc.response.status_code != 409:
        return False
    try:
        payload = exc.response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    return isinstance(error, dict) and error.get("code") == expected_code


def _without_request_local_refs(raw_comparison_json: dict) -> dict:
    return {
        key: value
        for key, value in raw_comparison_json.items()
        if key not in {"target_ref", "removed_snapshot_refs"}
    }
