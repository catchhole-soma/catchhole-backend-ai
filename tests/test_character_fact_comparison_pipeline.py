import asyncio
from collections import deque
from uuid import UUID

import httpx

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonDecision
from app.schemas.worker import (
    WorkerCharacterFactComparisonCandidatePayload,
    WorkerCharacterFactComparisonClaimPayload,
    WorkerCharacterFactComparisonContextResponse,
    WorkerCharacterSnapshotEntry,
)

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000003")


def test_pipeline_maps_refs_to_fact_identity_without_source_ids() -> None:
    spring = FakeSpringApi([CANDIDATE_ID])
    comparator = FakeComparator(
        [
            CharacterFactComparisonDecision(
                operation="UPDATE",
                target_ref="P1",
                removed_snapshot_refs=["P2"],
                proposed_fact_value="완전히 회복됨",
                proposed_value_json={"active": False},
                temporal_scope="PRESENT",
                comparison_reason="명시적으로 회복했다.",
            )
        ]
    )

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 1
    assert result.failed_count == 0
    request = spring.completions[0]
    assert request.target_fact_type == "STATUS"
    assert request.target_fact_key == "status.회복"
    assert request.proposed_fact_value == "완전히 회복됨"
    assert request.removed_snapshot_entries[0].fact_key == "status.출혈"
    payload = request.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "sourceFactIds" not in str(payload)
    assert "evidenceSpans" not in str(payload)
    assert "quote" not in str(payload)
    assert "target_ref" not in request.raw_comparison_json
    assert "removed_snapshot_refs" not in request.raw_comparison_json
    assert payload["contextToken"] == "snapshot-v1"


def test_pipeline_rebuilds_context_on_stale_409_up_to_success() -> None:
    spring = FakeSpringApi([CANDIDATE_ID], stale_completion_count=2)
    comparator = FakeComparator([_add_decision(), _add_decision(), _add_decision()])

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 1
    assert spring.context_call_count == 3
    assert len(spring.completions) == 3


def test_pipeline_maps_same_slot_remove_without_proposed_value() -> None:
    spring = FakeSpringApi([CANDIDATE_ID])
    comparator = FakeComparator(
        [
            CharacterFactComparisonDecision(
                operation="REMOVE",
                target_ref="P1",
                removed_snapshot_refs=[],
                proposed_fact_value=None,
                proposed_value_json=None,
                temporal_scope="PRESENT",
                comparison_reason="회복이 완료되어 현재 회복 상태를 종료한다.",
            )
        ]
    )

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 1
    request = spring.completions[0]
    assert request.operation == "REMOVE"
    assert request.target_fact_type == "STATUS"
    assert request.target_fact_key == "status.회복"
    assert request.proposed_fact_value is None
    assert request.proposed_value_json is None
    assert request.removed_snapshot_entries == []


def test_pipeline_isolates_failed_candidate_and_processes_next_candidate() -> None:
    second_candidate_id = UUID("00000000-0000-0000-0000-000000000005")
    spring = FakeSpringApi([CANDIDATE_ID, second_candidate_id])
    comparator = FakeComparator([ValueError("malformed response"), _add_decision()])

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 1
    assert result.failed_count == 1
    assert spring.failures == [(CANDIDATE_ID, "malformed response")]
    assert len(spring.completions) == 1


class FakeSpringApi:
    def __init__(
        self,
        candidate_ids: list[UUID],
        stale_completion_count: int = 0,
    ) -> None:
        self.candidate_ids = deque(candidate_ids)
        self.stale_completion_count = stale_completion_count
        self.context_call_count = 0
        self.completions = []
        self.failures: list[tuple[UUID, str]] = []

    async def claim_next_character_fact_comparison(self, analysis_job_id, lease_token):
        if not self.candidate_ids:
            return None
        return WorkerCharacterFactComparisonClaimPayload(candidate_id=self.candidate_ids.popleft())

    async def get_character_fact_comparison_context(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
    ):
        self.context_call_count += 1
        return _context(candidate_id, f"snapshot-v{self.context_call_count}")

    async def complete_character_fact_comparison(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        request,
    ):
        self.completions.append(request)
        if self.stale_completion_count:
            self.stale_completion_count -= 1
            http_request = httpx.Request("POST", "http://spring.local/complete")
            response = httpx.Response(
                409,
                request=http_request,
                json={"error": {"code": "SETTING_CANDIDATE_COMPARISON_STALE"}},
            )
            raise httpx.HTTPStatusError("stale", request=http_request, response=response)

    async def fail_character_fact_comparison(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        error_message,
    ):
        self.failures.append((candidate_id, error_message))


class FakeComparator:
    def __init__(self, outcomes: list[CharacterFactComparisonDecision | Exception]) -> None:
        self.outcomes = deque(outcomes)

    async def compare(self, candidate, snapshot_entries, prior_candidates=None):
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, outcome.model_dump(mode="json")


def _add_decision() -> CharacterFactComparisonDecision:
    return CharacterFactComparisonDecision(
        operation="ADD",
        proposed_fact_value="완전히 회복됨",
        proposed_value_json={"active": False},
        temporal_scope="PRESENT",
        comparison_reason="새 상태를 추가한다.",
    )


def _context(
    candidate_id: UUID,
    context_token: str,
) -> WorkerCharacterFactComparisonContextResponse:
    candidate = WorkerCharacterFactComparisonCandidatePayload.model_validate(
        {
            "candidateId": str(candidate_id),
            "workId": "00000000-0000-0000-0000-000000000020",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000021",
            "entityName": "비요른",
            "attributeName": "status.회복",
            "attributeValue": "완전히 회복됨",
            "valueJson": {"active": False},
            "valueType": "JSON",
            "evidenceSpans": [{"quote": "상처가 완전히 나았다."}],
            "matchedCharacterId": "00000000-0000-0000-0000-000000000010",
            "matchedCharacterName": "비요른",
            "canonicalFactType": "STATUS",
            "canonicalFactKey": "status.회복",
            "confidence": 0.95,
        }
    )
    return WorkerCharacterFactComparisonContextResponse(
        candidate=candidate,
        snapshot_entries=[
            WorkerCharacterSnapshotEntry(
                fact_type="STATUS",
                fact_key="status.회복",
                fact_value="회복 중",
                value_json={"active": True},
            ),
            WorkerCharacterSnapshotEntry(
                fact_type="STATUS",
                fact_key="status.출혈",
                fact_value="출혈 중",
                value_json={"active": True},
            ),
        ],
        context_token=context_token,
    )
