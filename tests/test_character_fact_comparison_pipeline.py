import asyncio
from collections import deque
from uuid import UUID

import httpx
import pytest

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonDecision
from app.analysis.exceptions import ComparisonValidationError
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError
from app.domain.enums import AnalysisFailureCode
from app.llm.exceptions import LlmResponseValidationError
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


@pytest.mark.parametrize(
    ("value_type", "operation", "temporal_scope", "target_ref"),
    [
        ("NUMBER", "EXCLUDE", "PRESENT", None),
        ("NUMBER", "HISTORY_ONLY", "PAST", None),
        ("NUMBER", "REVIEW_REQUIRED", "UNKNOWN", None),
        ("NUMBER", "REMOVE", "PRESENT", "P1"),
        ("BOOLEAN", "EXCLUDE", "PRESENT", None),
        ("BOOLEAN", "HISTORY_ONLY", "PAST", None),
        ("BOOLEAN", "REVIEW_REQUIRED", "UNKNOWN", None),
        ("BOOLEAN", "REMOVE", "PRESENT", "P1"),
    ],
)
def test_pipeline_keeps_non_applying_scalar_proposals_null(
    value_type: str,
    operation: str,
    temporal_scope: str,
    target_ref: str | None,
) -> None:
    spring = FakeSpringApi([CANDIDATE_ID], candidate_value_type=value_type)
    comparator = FakeComparator(
        [
            CharacterFactComparisonDecision(
                operation=operation,
                target_ref=target_ref,
                proposed_fact_value=None,
                proposed_value_json=None,
                temporal_scope=temporal_scope,
                comparison_reason="현재 설정에 값을 반영하지 않는다.",
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
    assert spring.failures == []
    request = spring.completions[0]
    assert request.proposed_fact_value is None
    assert request.proposed_value_json is None


@pytest.mark.parametrize("operation", ["ADD", "UPDATE", "MERGE"])
def test_pipeline_requires_number_value_field_for_applying_operations(operation: str) -> None:
    spring = FakeSpringApi([CANDIDATE_ID], candidate_value_type="NUMBER")
    comparator = FakeComparator(
        [
            CharacterFactComparisonDecision(
                operation=operation,
                target_ref=None if operation == "ADD" else "P1",
                proposed_fact_value="2",
                proposed_value_json={},
                temporal_scope="PRESENT",
                comparison_reason="현재 숫자 설정을 반영한다.",
            )
        ]
    )

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 0
    assert result.failed_count == 1
    assert result.first_failure_code is AnalysisFailureCode.UNEXPECTED_ERROR
    assert spring.completions == []
    assert spring.failures[0][2] == AnalysisFailureCode.UNEXPECTED_ERROR.value
    assert "NUMBER value_json must contain a typed value field" in spring.failures[0][1]


@pytest.mark.parametrize(
    ("value_type", "proposed_value_json", "expected_error"),
    [
        ("NUMBER", {"value": "2"}, "NUMBER value_json.value must be a JSON number"),
        ("BOOLEAN", {"value": "true"}, "BOOLEAN value_json.value must be a JSON boolean"),
    ],
)
def test_pipeline_rejects_wrong_scalar_proposal_types(
    value_type: str,
    proposed_value_json: dict,
    expected_error: str,
) -> None:
    spring = FakeSpringApi([CANDIDATE_ID], candidate_value_type=value_type)
    comparator = FakeComparator(
        [
            CharacterFactComparisonDecision(
                operation="ADD",
                proposed_fact_value="2" if value_type == "NUMBER" else "true",
                proposed_value_json=proposed_value_json,
                temporal_scope="PRESENT",
                comparison_reason="현재 scalar 설정을 반영한다.",
            )
        ]
    )

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 0
    assert result.failed_count == 1
    assert result.first_failure_code is AnalysisFailureCode.UNEXPECTED_ERROR
    assert spring.completions == []
    assert expected_error in spring.failures[0][1]


@pytest.mark.parametrize(
    ("first_error", "expected_code"),
    [
        (
            ComparisonValidationError("malformed response"),
            AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
        ),
        (
            LlmResponseValidationError("malformed provider payload"),
            AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
        ),
        (RuntimeError("post-processing failed"), AnalysisFailureCode.UNEXPECTED_ERROR),
    ],
)
def test_pipeline_isolates_failed_candidate_and_processes_next_candidate(
    first_error: Exception,
    expected_code: AnalysisFailureCode,
) -> None:
    second_candidate_id = UUID("00000000-0000-0000-0000-000000000005")
    spring = FakeSpringApi([CANDIDATE_ID, second_candidate_id])
    comparator = FakeComparator([first_error, _add_decision()])

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 1
    assert result.failed_count == 1
    assert result.first_failure_code is expected_code
    assert spring.failures == [(CANDIDATE_ID, str(first_error), expected_code.value)]
    assert len(spring.completions) == 1


def test_pipeline_bubbles_quota_failure_before_claiming_next_candidate() -> None:
    second_candidate_id = UUID("00000000-0000-0000-0000-000000000005")
    spring = FakeSpringApi([CANDIDATE_ID, second_candidate_id])
    comparator = FakeComparator([AiTokenQuotaExhaustedError(), _add_decision()])

    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(
            CharacterFactComparisonPipeline(spring, comparator).process_all(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
        )

    assert spring.claim_count == 1
    assert list(spring.candidate_ids) == [second_candidate_id]
    assert spring.failures == [
        (CANDIDATE_ID, "AI token quota is exhausted.", "AI_TOKEN_QUOTA_EXHAUSTED")
    ]


class FakeSpringApi:
    def __init__(
        self,
        candidate_ids: list[UUID],
        stale_completion_count: int = 0,
        candidate_value_type: str = "JSON",
    ) -> None:
        self.candidate_ids = deque(candidate_ids)
        self.stale_completion_count = stale_completion_count
        self.candidate_value_type = candidate_value_type
        self.context_call_count = 0
        self.claim_count = 0
        self.completions = []
        self.failures: list[tuple[UUID, str, str]] = []

    async def claim_next_character_fact_comparison(self, analysis_job_id, lease_token):
        self.claim_count += 1
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
        return _context(
            candidate_id,
            f"snapshot-v{self.context_call_count}",
            self.candidate_value_type,
        )

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
            raise SpringWorkerHttpError(
                "stale",
                request=http_request,
                response=response,
                status_code=409,
                spring_error_code="SETTING_CANDIDATE_COMPARISON_STALE",
            )

    async def fail_character_fact_comparison(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        error_message,
        failure_code,
    ):
        self.failures.append((candidate_id, error_message, failure_code.value))


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
    candidate_value_type: str = "JSON",
) -> WorkerCharacterFactComparisonContextResponse:
    candidate_values = {
        "JSON": {
            "attributeName": "status.회복",
            "attributeValue": "완전히 회복됨",
            "valueJson": {"active": False},
            "canonicalFactType": "STATUS",
            "canonicalFactKey": "status.회복",
        },
        "NUMBER": {
            "attributeName": "level",
            "attributeValue": "1",
            "valueJson": {"value": 1},
            "canonicalFactType": "LEVEL",
            "canonicalFactKey": "level",
        },
        "BOOLEAN": {
            "attributeName": "profile.awake",
            "attributeValue": "true",
            "valueJson": {"value": True},
            "canonicalFactType": "PROFILE",
            "canonicalFactKey": "profile.awake",
        },
    }
    values = candidate_values[candidate_value_type]
    candidate = WorkerCharacterFactComparisonCandidatePayload.model_validate(
        {
            "candidateId": str(candidate_id),
            "workId": "00000000-0000-0000-0000-000000000020",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000021",
            "entityName": "비요른",
            "attributeName": values["attributeName"],
            "attributeValue": values["attributeValue"],
            "valueJson": values["valueJson"],
            "valueType": candidate_value_type,
            "evidenceSpans": [{"quote": "상처가 완전히 나았다."}],
            "matchedCharacterId": "00000000-0000-0000-0000-000000000010",
            "matchedCharacterName": "비요른",
            "canonicalFactType": values["canonicalFactType"],
            "canonicalFactKey": values["canonicalFactKey"],
            "confidence": 0.95,
        }
    )
    return WorkerCharacterFactComparisonContextResponse(
        candidate=candidate,
        snapshot_entries=[
            WorkerCharacterSnapshotEntry(
                fact_type=values["canonicalFactType"],
                fact_key=values["canonicalFactKey"],
                fact_value=values["attributeValue"],
                value_json=values["valueJson"],
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
