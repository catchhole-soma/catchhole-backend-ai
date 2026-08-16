import asyncio
from uuid import UUID

import httpx
import pytest

from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.analysis.world_setting_schemas import WorldSettingComparisonDecision
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonContextResponse,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectPageResponse,
)

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000003")
TARGET_ID = UUID("00000000-0000-0000-0000-000000000004")


def test_pipeline_uses_normalized_exact_subject_and_completes_update() -> None:
    spring = FakeSpringApi(candidate=_candidate())
    spring.subjects = [_subject(TARGET_ID, "바바리안")]
    spring.context = _context()
    subject_resolver = FakeSubjectResolver([])
    comparator = FakeComparator(
        WorldSettingComparisonDecision(
            consolidation_status="SINGLE",
            operation="UPDATE",
            target_ref="T1",
            matched_property_name="서식지",
            proposed_setting_name="서식지",
            proposed_value="극지방",
            comparison_reason="새 근거가 기존 서식지를 구체화한다.",
        )
    )
    pipeline = WorldSettingComparisonPipeline(spring, subject_resolver, comparator)

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 1
    assert result.failed_count == 0
    assert subject_resolver.calls == []
    assert spring.context_target_ids == [[TARGET_ID]]
    request = spring.completions[0]
    assert request.target_world_setting_id == TARGET_ID
    assert request.context_versions[0].version == 3


def test_pipeline_forwards_scoped_property_path_to_backend() -> None:
    candidate = _candidate(scope_name="1층")
    spring = FakeSpringApi(candidate=candidate)
    spring.subjects = [_subject(TARGET_ID, "바바리안")]
    spring.context = WorkerWorldSettingComparisonContextResponse(
        candidate=candidate,
        exact_target_world_setting_id=TARGET_ID,
        targets=[
            WorkerWorldSettingComparisonTarget(
                world_setting_id=TARGET_ID,
                subject_name="바바리안",
                properties=[
                    WorkerWorldSettingProperty(
                        scope_name="1층",
                        setting_name="서식지",
                        value="혹한 지역",
                    )
                ],
                version=3,
            )
        ],
    )
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeSubjectResolver([]),
        FakeComparator(
            WorldSettingComparisonDecision(
                consolidation_status="SINGLE",
                operation="UPDATE",
                target_ref="T1",
                matched_scope_name="1층",
                matched_property_name="서식지",
                proposed_scope_name="1층",
                proposed_setting_name="서식지",
                proposed_value="극지방",
                comparison_reason="1층의 기존 서식지를 구체화한다.",
            )
        ),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 1
    request = spring.completions[0]
    assert request.matched_scope_name == "1층"
    assert request.proposed_scope_name == "1층"


def test_pipeline_rebuilds_context_after_stale_completion() -> None:
    spring = FakeSpringApi(candidate=_candidate(), stale_completion_count=1)
    spring.subjects = [_subject(TARGET_ID, "바바리안")]
    spring.context = _context()
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeSubjectResolver([]),
        FakeComparator(
            WorldSettingComparisonDecision(
                consolidation_status="SINGLE",
                operation="MERGE",
                target_ref="T1",
                matched_property_name="서식지",
                proposed_setting_name="서식지",
                proposed_value="혹한 지역과 극지방",
                comparison_reason="두 서술이 양립한다.",
            )
        ),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 1
    assert len(spring.context_target_ids) == 2
    assert len(spring.completions) == 2


def test_pipeline_keeps_duplicate_exclude_target_and_matched_property() -> None:
    spring = FakeSpringApi(candidate=_candidate())
    spring.subjects = [_subject(TARGET_ID, "바바리안")]
    spring.context = _context()
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeSubjectResolver([]),
        FakeComparator(
            WorldSettingComparisonDecision(
                consolidation_status="SINGLE",
                operation="EXCLUDE",
                target_ref="T1",
                matched_property_name="서식지",
                proposed_setting_name="서식지",
                proposed_value="극지방",
                comparison_reason="기존 서식지와 의미가 같아 별도로 반영하지 않는다.",
            )
        ),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 1
    request = spring.completions[0]
    assert request.target_world_setting_id == TARGET_ID
    assert request.matched_property_name == "서식지"


def test_pipeline_fails_only_claimed_candidate_when_comparator_fails() -> None:
    spring = FakeSpringApi(candidate=_candidate())
    spring.subjects = []
    spring.context = _context(targets=[])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeSubjectResolver([]),
        FailingComparator(),
    )

    result = asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert result.completed_count == 0
    assert result.failed_count == 1
    assert spring.failures == [
        (CANDIDATE_ID, "malformed LLM response", "COMPARISON_VALIDATION_FAILED")
    ]


def test_pipeline_bubbles_quota_failure_without_claiming_or_failing_next_candidate() -> None:
    second_candidate = _candidate().model_copy(
        update={"candidate_id": UUID("00000000-0000-0000-0000-000000000099")}
    )
    spring = FakeSpringApi(candidate=_candidate())
    spring.candidates.append(second_candidate)
    spring.subjects = []
    spring.context = _context(targets=[])
    pipeline = WorldSettingComparisonPipeline(
        spring,
        FakeSubjectResolver([]),
        QuotaFailingComparator(),
    )

    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(pipeline.process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))

    assert spring.claim_count == 1
    assert list(spring.candidates) == [second_candidate]
    assert spring.failures == []


class FakeSpringApi:
    def __init__(
        self,
        candidate: WorkerWorldSettingCandidatePayload,
        stale_completion_count: int = 0,
    ) -> None:
        self.candidates = [candidate]
        self.subjects: list[WorkerWorldSettingSubject] = []
        self.context = _context()
        self.context_target_ids: list[list[UUID]] = []
        self.completions = []
        self.failures: list[tuple[UUID, str, str]] = []
        self.claim_count = 0
        self.stale_completion_count = stale_completion_count

    async def claim_next_world_setting_comparison(self, analysis_job_id, lease_token):
        self.claim_count += 1
        return self.candidates.pop(0) if self.candidates else None

    async def get_world_setting_subjects(
        self, analysis_job_id, lease_token, category, page, size=500
    ):
        return WorkerWorldSettingSubjectPageResponse(
            subjects=self.subjects,
            page=page,
            has_next=False,
        )

    async def get_world_setting_comparison_context(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        target_world_setting_ids,
    ):
        self.context_target_ids.append(target_world_setting_ids)
        return self.context

    async def complete_world_setting_comparison(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        request,
    ):
        self.completions.append(request)
        if self.stale_completion_count:
            self.stale_completion_count -= 1
            http_request = httpx.Request("POST", "http://spring.local/comparison-complete")
            response = httpx.Response(
                409,
                request=http_request,
                json={"error": {"code": "WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE"}},
            )
            raise httpx.HTTPStatusError("stale", request=http_request, response=response)

    async def fail_world_setting_comparison(
        self,
        analysis_job_id,
        candidate_id,
        lease_token,
        error_message,
        failure_code,
    ):
        self.failures.append((candidate_id, error_message, failure_code.value))


class FakeSubjectResolver:
    def __init__(self, selected) -> None:
        self.selected = selected
        self.calls = []

    async def select_subjects(self, candidate, subjects):
        self.calls.append((candidate, subjects))
        return self.selected


class FakeComparator:
    def __init__(self, decision: WorldSettingComparisonDecision) -> None:
        self.decision = decision

    async def compare(self, candidate, targets):
        return self.decision, self.decision.model_dump(mode="json")


class FailingComparator:
    async def compare(self, candidate, targets):
        raise ValueError("malformed LLM response")


class QuotaFailingComparator:
    async def compare(self, candidate, targets):
        raise AiTokenQuotaExhaustedError()


def _candidate(scope_name: str | None = None) -> WorkerWorldSettingCandidatePayload:
    return WorkerWorldSettingCandidatePayload.model_validate(
        {
            "candidateId": str(CANDIDATE_ID),
            "workId": "00000000-0000-0000-0000-000000000010",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000011",
            "category": "RACE",
            "subjectName": " 바바리안 ",
            "scopeName": scope_name,
            "settingName": "서식지",
            "extractedValue": "극지방",
            "evidenceSpans": [{"quote": "바바리안은 극지방에 산다."}],
            "extractionConfidence": 0.95,
        }
    )


def _subject(world_setting_id: UUID, subject_name: str) -> WorkerWorldSettingSubject:
    return WorkerWorldSettingSubject(
        world_setting_id=world_setting_id,
        subject_name=subject_name,
    )


def _context(
    targets: list[WorkerWorldSettingComparisonTarget] | None = None,
) -> WorkerWorldSettingComparisonContextResponse:
    resolved_targets = targets
    if resolved_targets is None:
        resolved_targets = [
            WorkerWorldSettingComparisonTarget(
                world_setting_id=TARGET_ID,
                subject_name="바바리안",
                properties=[
                    WorkerWorldSettingProperty(
                        scope_name=None,
                        setting_name="서식지",
                        value="혹한 지역",
                    )
                ],
                version=3,
            )
        ]
    return WorkerWorldSettingComparisonContextResponse(
        candidate=_candidate(),
        exact_target_world_setting_id=(TARGET_ID if resolved_targets else None),
        targets=resolved_targets,
    )
