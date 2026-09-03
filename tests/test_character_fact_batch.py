import asyncio
import json
from collections import deque
from uuid import UUID

import httpx
import pytest

from app.analysis.character_fact_comparison_pipeline import (
    CharacterFactComparisonPipeline,
)
from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
)
from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
    validate_resolved_canonical_fact_key,
)
from app.analysis.exceptions import ComparisonValidationError
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import Settings
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchCompleteRequest,
    WorkerCharacterFactComparisonBatchContextResponse,
    WorkerCharacterFactComparisonBatchPayload,
    WorkerCharacterFactComparisonBatchSnapshotEntry,
)

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000003")
WORK_ID = UUID("00000000-0000-0000-0000-000000000004")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000005")
CONTEXT_TOKEN = "a" * 64


def test_projector_chains_q_refs_and_derives_dependencies() -> None:
    state = CharacterProjectionState(
        [
            _projection_entry("P1", "status.부상", "다리를 다침"),
            _projection_entry("P2", "status.마비독", "마비독에 중독됨"),
        ]
    )
    first = _decision(
        "C1",
        operation="UPDATE",
        resolved_key="status.부상",
        target_ref="P1",
        value="부상이 심해짐",
    )

    first_application = state.apply(
        candidate_ref="C1",
        projected_snapshot_ref="Q1",
        fact_type="STATUS",
        resolved_fact_key="status.부상",
        value_type="JSON",
        candidate_value_json={"active": True},
        decision=first,
    )

    assert first_application.dependency_candidate_refs == ()
    assert {entry.reference for entry in state.entries} == {"Q1", "P2"}

    second = _decision(
        "C2",
        operation="REMOVE",
        resolved_key="status.회복",
        removed_refs=["Q1", "P2"],
        value=None,
    )
    second_application = state.apply(
        candidate_ref="C2",
        projected_snapshot_ref="Q2",
        fact_type="STATUS",
        resolved_fact_key="status.회복",
        value_type="JSON",
        candidate_value_json={"active": False},
        decision=second,
    )

    assert second_application.dependency_candidate_refs == ("C1",)
    assert state.entries == []


def test_five_status_transitions_finish_with_empty_snapshot() -> None:
    state = CharacterProjectionState(
        [
            _projection_entry("P1", "status.오른발_부상", "오른발을 심하게 다침"),
            _projection_entry("P2", "status.마비독", "마비독에 중독됨"),
        ]
    )
    transitions = [
        (
            "C1",
            "Q1",
            "status.오른발_부상",
            _decision(
                "C1",
                operation="UPDATE",
                resolved_key="status.오른발_부상",
                target_ref="P1",
                value="오른발 부상이 악화됨",
            ),
            {"active": True},
        ),
        (
            "C2",
            "Q2",
            "status.출혈",
            _decision(
                "C2",
                operation="ADD",
                resolved_key="status.출혈",
                value="출혈 중",
            ),
            {"active": True},
        ),
        (
            "C3",
            "Q3",
            "status.생명력_저하",
            _decision(
                "C3",
                operation="ADD",
                resolved_key="status.생명력_저하",
                value="생명력이 5% 미만",
            ),
            {"active": True, "percent": 5},
        ),
        (
            "C4",
            "Q4",
            "status.생명력_저하",
            _decision(
                "C4",
                operation="UPDATE",
                resolved_key="status.생명력_저하",
                target_ref="Q3",
                value="생명력이 2% 이하",
            ),
            {"active": True, "percent": 2},
        ),
        (
            "C5",
            "Q5",
            "status.회복",
            _decision(
                "C5",
                operation="REMOVE",
                resolved_key="status.회복",
                removed_refs=["Q4", "P2", "Q2", "Q1"],
                value=None,
            ),
            {"active": False},
        ),
    ]

    applications = []
    for candidate_ref, projected_ref, key, decision, candidate_json in transitions:
        applications.append(
            state.apply(
                candidate_ref=candidate_ref,
                projected_snapshot_ref=projected_ref,
                fact_type="STATUS",
                resolved_fact_key=key,
                value_type="JSON",
                candidate_value_json=candidate_json,
                decision=decision,
            )
        )

    assert state.entries == []
    assert applications[3].dependency_candidate_refs == ("C3",)
    assert applications[4].dependency_candidate_refs == ("C1", "C2", "C3", "C4")


def test_independent_statuses_keep_separate_projected_slots() -> None:
    state = CharacterProjectionState([])
    for candidate_ref, projected_ref, key, value in [
        ("C1", "Q1", "status.출혈", "출혈 중"),
        ("C2", "Q2", "status.탈수", "수분이 부족함"),
    ]:
        state.apply(
            candidate_ref=candidate_ref,
            projected_snapshot_ref=projected_ref,
            fact_type="STATUS",
            resolved_fact_key=key,
            value_type="JSON",
            candidate_value_json={"active": True},
            decision=_decision(
                candidate_ref,
                operation="ADD",
                resolved_key=key,
                value=value,
            ),
        )

    assert [(entry.reference, entry.fact_key) for entry in state.entries] == [
        ("Q1", "status.출혈"),
        ("Q2", "status.탈수"),
    ]


def test_projector_rejects_future_q_ref() -> None:
    state = CharacterProjectionState([])
    decision = _decision(
        "C1",
        operation="UPDATE",
        resolved_key="status.부상",
        target_ref="Q2",
        value="부상이 심해짐",
    )

    with pytest.raises(ValueError, match="Unknown snapshot refs"):
        state.apply(
            candidate_ref="C1",
            projected_snapshot_ref="Q1",
            fact_type="STATUS",
            resolved_fact_key="status.부상",
            value_type="JSON",
            candidate_value_json={"active": True},
            decision=decision,
        )


def test_only_pattern_status_key_can_be_semantically_changed() -> None:
    with pytest.raises(ValueError, match="fixed canonical Fact key"):
        validate_resolved_canonical_fact_key(
            initial_fact_key="profile.species",
            resolved_fact_key="profile.race",
            canonical_key_resolution="ALIAS",
            fact_type="PROFILE",
        )
    with pytest.raises(ValueError, match="fixed canonical Fact key"):
        validate_resolved_canonical_fact_key(
            initial_fact_key="item.포션",
            resolved_fact_key="item.회복_포션",
            canonical_key_resolution="PATTERN",
            fact_type="ITEM",
        )

    validate_resolved_canonical_fact_key(
        initial_fact_key="item.포션",
        resolved_fact_key="item.포션",
        canonical_key_resolution="PATTERN",
        fact_type="ITEM",
    )

    validate_resolved_canonical_fact_key(
        initial_fact_key="status.오른발_골절상",
        resolved_fact_key="status.오른발_부상",
        canonical_key_resolution="PATTERN",
        fact_type="STATUS",
    )
    with pytest.raises(ValueError, match=r"status\.\* canonical form"):
        validate_resolved_canonical_fact_key(
            initial_fact_key="status.부상",
            resolved_fact_key="status.오른발 부상",
            canonical_key_resolution="PATTERN",
            fact_type="STATUS",
        )


def test_present_non_persistent_event_does_not_enter_projected_snapshot() -> None:
    state = CharacterProjectionState([])
    decision = _decision(
        "C1",
        operation="HISTORY_ONLY",
        resolved_key="item.포션",
        value=None,
    )

    application = state.apply(
        candidate_ref="C1",
        projected_snapshot_ref="Q1",
        fact_type="ITEM",
        resolved_fact_key="item.포션",
        value_type="JSON",
        candidate_value_json={"name": "포션"},
        decision=decision,
    )

    assert application.projected_entry is None
    assert state.entries == []


def test_batch_comparator_projects_in_order_and_hides_transport_ids() -> None:
    client = FakeTextClient(
        {
            "decisions": [
                _decision_payload(
                    "C1",
                    operation="UPDATE",
                    resolved_key="status.부상",
                    target_ref="P1",
                    value="부상이 심해짐",
                    reason="P1의 부상이 더 심해졌습니다.",
                ),
                _decision_payload(
                    "C2",
                    operation="REMOVE",
                    resolved_key="status.회복",
                    removed_refs=["Q1", "P2"],
                    value=None,
                    reason="Q1의 부상과 현재 마비 증상이 회복됐습니다.",
                ),
            ]
        }
    )
    candidates = _candidates()
    snapshots = [
        _batch_snapshot("P1", "status.부상", "다리를 다침"),
        _batch_snapshot("P2", "status.마비독", "마비독에 중독됨"),
    ]

    result, _ = asyncio.run(
        CharacterFactComparator(
            llm_client=client,
            max_attempts=1,
            batch_max_input_tokens=100_000,
        ).compare_batch(
            matched_character_name="비요른 얀델",
            canonical_fact_type="STATUS",
            candidates=candidates,
            snapshot_entries=snapshots,
        )
    )

    assert [decision.candidate_ref for decision in result.decisions] == ["C1", "C2"]
    assert result.decisions[1].removed_snapshot_refs == ["Q1", "P2"]
    assert "Q1" not in result.decisions[1].comparison_reason
    request = client.requests[0]
    prompt = json.loads(request["user_prompt"])
    serialized = request["user_prompt"]
    assert prompt["candidates"][0]["projected_snapshot_ref"] == "Q1"
    assert prompt["candidates"][0]["canonical_key_resolution"] == "PATTERN"
    assert str(WORK_ID) not in serialized
    assert str(EPISODE_ID) not in serialized
    assert request["prompt_cache_key"] == "character-fact-comparison-batch:v1"


def test_batch_pipeline_falls_back_to_singletons_without_losing_projection() -> None:
    batch = _batch()
    context = _context(batch)
    comparator = FakeBatchComparator(
        [
            ComparisonValidationError("invalid batch response"),
            CharacterFactComparisonBatchResult(
                decisions=[
                    _decision(
                        "C1",
                        operation="ADD",
                        resolved_key="status.부상",
                        value="다리를 다침",
                    )
                ]
            ),
            CharacterFactComparisonBatchResult(
                decisions=[
                    _decision(
                        "C2",
                        operation="REMOVE",
                        resolved_key="status.회복",
                        removed_refs=["Q1"],
                        value=None,
                    )
                ]
            ),
        ]
    )
    spring = FakeBatchSpring(batch, context)

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 2
    assert result.failed_count == 0
    assert result.batch_count == 1
    assert result.provider_segment_count == 3
    assert result.singleton_fallback_count == 2
    completion = spring.completions[0]
    assert completion.decisions[1].removed_snapshot_refs == ["Q1"]
    assert completion.decisions[1].dependency_candidate_refs == ["C1"]
    assert "target_ref" not in completion.decisions[1].raw_comparison_json
    summary = result.summary_metrics()
    assert summary["characterComparisonBatchFallbackCandidateCount"] == 2
    assert summary["characterComparisonMaxCandidatesPerBatch"] == 2


def test_batch_pipeline_splits_by_limit_and_keeps_q_projection() -> None:
    batch = _batch()
    comparator = FakeBatchComparator(
        [
            CharacterFactComparisonBatchResult(
                decisions=[
                    _decision(
                        "C1",
                        operation="ADD",
                        resolved_key="status.부상",
                        value="다리를 다침",
                    )
                ]
            ),
            CharacterFactComparisonBatchResult(
                decisions=[
                    _decision(
                        "C2",
                        operation="REMOVE",
                        resolved_key="status.회복",
                        removed_refs=["Q1"],
                        value=None,
                    )
                ]
            ),
        ],
        max_candidates_per_call=1,
    )
    spring = FakeBatchSpring(batch, _context(batch))

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert comparator.calls == [["C1"], ["C2"]]
    assert result.provider_segment_count == 2
    assert result.singleton_fallback_count == 0
    assert spring.completions[0].decisions[1].removed_snapshot_refs == ["Q1"]
    assert spring.completions[0].decisions[1].dependency_candidate_refs == ["C1"]


def test_batch_pipeline_rebuilds_every_decision_after_stale_context() -> None:
    batch = _batch()
    complete_result = CharacterFactComparisonBatchResult(
        decisions=[
            _decision(
                "C1",
                operation="ADD",
                resolved_key="status.부상",
                value="다리를 다침",
            ),
            _decision(
                "C2",
                operation="REMOVE",
                resolved_key="status.회복",
                removed_refs=["Q1"],
                value=None,
            ),
        ]
    )
    comparator = FakeBatchComparator([complete_result, complete_result])
    spring = FakeBatchSpring(batch, _context(batch), stale_completion_count=1)

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert comparator.calls == [["C1", "C2"], ["C1", "C2"]]
    assert spring.context_call_count == 2
    assert len(spring.completions) == 2
    assert result.stale_batch_retry_count == 1
    assert result.provider_segment_count == 2


def test_batch_pipeline_does_not_fallback_after_token_quota_failure() -> None:
    batch = _batch()
    comparator = FakeBatchComparator([AiTokenQuotaExhaustedError()])
    spring = FakeBatchSpring(batch, _context(batch))

    with pytest.raises(AiTokenQuotaExhaustedError):
        asyncio.run(
            CharacterFactComparisonPipeline(spring, comparator).process_all(
                ANALYSIS_JOB_ID,
                LEASE_TOKEN,
            )
        )

    assert comparator.calls == [["C1", "C2"]]
    assert spring.completions == []
    assert spring.failures[0][2] == "AI_TOKEN_QUOTA_EXHAUSTED"


def test_singleton_failure_is_not_projected_into_later_candidate() -> None:
    batch = _batch()
    comparator = FakeBatchComparator(
        [
            ComparisonValidationError("invalid batch"),
            ComparisonValidationError("invalid first singleton"),
            CharacterFactComparisonBatchResult(
                decisions=[
                    _decision(
                        "C2",
                        operation="EXCLUDE",
                        resolved_key="status.회복",
                        value=None,
                    )
                ]
            ),
        ]
    )
    spring = FakeBatchSpring(batch, _context(batch))

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert result.completed_count == 1
    assert result.failed_count == 1
    completion = spring.completions[0]
    assert [failure.candidate_ref for failure in completion.failures] == ["C1"]
    assert completion.decisions[0].candidate_ref == "C2"
    assert completion.decisions[0].dependency_candidate_refs == []


def test_batch_comparator_retries_fixed_key_change() -> None:
    candidate = _candidate("C1", "Q1", "바바리안").model_copy(
        update={
            "raw_fact_key": "종족",
            "initial_canonical_fact_key": "profile.species",
            "canonical_key_resolution": "ALIAS",
            "value_type": "STRING",
            "value_json": {"value": "바바리안"},
        }
    )
    invalid = _decision_payload(
        "C1",
        operation="ADD",
        resolved_key="profile.race",
        value="바바리안",
    )
    invalid["proposed_value_json"] = {"value": "바바리안"}
    valid = {**invalid, "resolved_canonical_fact_key": "profile.species"}
    client = SequencedTextClient([{"decisions": [invalid]}, {"decisions": [valid]}])

    result, _ = asyncio.run(
        CharacterFactComparator(
            llm_client=client,
            max_attempts=2,
            batch_max_input_tokens=100_000,
        ).compare_batch(
            matched_character_name="비요른 얀델",
            canonical_fact_type="PROFILE",
            candidates=[candidate],
            snapshot_entries=[],
        )
    )

    assert result.decisions[0].resolved_canonical_fact_key == "profile.species"
    assert len(client.requests) == 2
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert "fixed canonical Fact key" in feedback["reason"]


def test_batch_comparator_retries_incomplete_candidate_coverage() -> None:
    first = _decision_payload(
        "C1",
        operation="ADD",
        resolved_key="status.부상",
        value="다리를 다침",
    )
    second = _decision_payload(
        "C2",
        operation="REMOVE",
        resolved_key="status.회복",
        removed_refs=["Q1"],
        value=None,
    )
    client = SequencedTextClient(
        [
            {"decisions": [first]},
            {"decisions": [first, second]},
        ]
    )

    result, _ = asyncio.run(
        CharacterFactComparator(
            llm_client=client,
            max_attempts=2,
            batch_max_input_tokens=100_000,
        ).compare_batch(
            matched_character_name="비요른 얀델",
            canonical_fact_type="STATUS",
            candidates=_candidates(),
            snapshot_entries=[],
        )
    )

    assert [decision.candidate_ref for decision in result.decisions] == ["C1", "C2"]
    assert len(client.requests) == 2
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert "cover every candidate" in feedback["reason"]


def test_batch_comparator_rejects_input_limit_before_provider_call() -> None:
    client = FakeTextClient({"decisions": []})
    comparator = CharacterFactComparator(
        llm_client=client,
        max_attempts=1,
        batch_max_input_tokens=1,
    )

    with pytest.raises(
        ComparisonValidationError,
        match="character_batch_input_limit_exceeded",
    ):
        asyncio.run(
            comparator.compare_batch(
                matched_character_name="비요른 얀델",
                canonical_fact_type="STATUS",
                candidates=[_candidates()[0]],
                snapshot_entries=[],
            )
        )

    assert client.requests == []


def test_character_batch_config_defaults_and_metrics_are_namespaced() -> None:
    settings = Settings()

    assert settings.llm_character_fact_batch_comparison_max_output_tokens == 16000
    assert settings.llm_character_fact_batch_comparison_max_input_tokens == 64000
    assert settings.character_fact_comparison_batch_max_candidates == 10


def test_oversized_singleton_is_typed_failure_without_provider_or_fallback() -> None:
    batch = _batch().model_copy(update={"candidates": [_candidates()[0]]})
    context = _context(batch)
    comparator = FakeBatchComparator([], max_candidates_per_call=0)
    spring = FakeBatchSpring(batch, context)

    result = asyncio.run(
        CharacterFactComparisonPipeline(spring, comparator).process_all(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
    )

    assert comparator.calls == []
    assert result.completed_count == 0
    assert result.failed_count == 1
    assert result.provider_segment_count == 0
    assert result.singleton_fallback_count == 0
    assert result.batch_validation_failure_count == 1
    completion = spring.completions[0]
    assert completion.decisions == []
    assert completion.failures[0].failure_code == "COMPARISON_VALIDATION_FAILED"
    assert completion.failures[0].error_message == (
        "character_batch_input_limit_exceeded"
    )


def test_spring_client_batch_calls_match_java_contract() -> None:
    requests: list[httpx.Request] = []
    batch = _batch()
    context = _context(batch)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/claim-next"):
            return httpx.Response(
                200,
                request=request,
                json={"data": batch.model_dump(by_alias=True, mode="json")},
            )
        if request.url.path.endswith("/context"):
            return httpx.Response(
                200,
                request=request,
                json={"data": context.model_dump(by_alias=True, mode="json")},
            )
        return httpx.Response(200, request=request, json={"data": None})

    client = SpringWorkerClient(
        "http://spring.local",
        "secret",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    complete = WorkerCharacterFactComparisonBatchCompleteRequest(
        context_token=CONTEXT_TOKEN,
        decisions=[],
        failures=[
            {
                "candidateRef": "C1",
                "failureCode": "COMPARISON_VALIDATION_FAILED",
                "errorMessage": "invalid",
            }
        ],
    )

    async def call_all():
        claimed = await client.claim_next_character_fact_comparison_batch(
            ANALYSIS_JOB_ID,
            LEASE_TOKEN,
        )
        loaded = await client.get_character_fact_comparison_batch_context(
            ANALYSIS_JOB_ID,
            BATCH_ID,
            LEASE_TOKEN,
        )
        await client.complete_character_fact_comparison_batch(
            ANALYSIS_JOB_ID,
            BATCH_ID,
            LEASE_TOKEN,
            complete,
        )
        await client.fail_character_fact_comparison_batch(
            ANALYSIS_JOB_ID,
            BATCH_ID,
            LEASE_TOKEN,
            "failed",
        )
        await client.aclose()
        return claimed, loaded

    claimed, loaded = asyncio.run(call_all())

    assert claimed is not None and claimed.character_ref == "K1"
    assert loaded.base_snapshot_version == 7
    base = f"/api/internal/v1/analysis-jobs/{ANALYSIS_JOB_ID}/character-fact-comparison-batches"
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", f"{base}/claim-next"),
        ("POST", f"{base}/{BATCH_ID}/context"),
        ("POST", f"{base}/{BATCH_ID}/complete"),
        ("POST", f"{base}/{BATCH_ID}/fail"),
    ]
    complete_payload = json.loads(requests[2].content)
    assert complete_payload["contextToken"] == CONTEXT_TOKEN
    assert complete_payload["failures"][0]["candidateRef"] == "C1"


class FakeTextClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(text=json.dumps(self.response, ensure_ascii=False))


class SequencedTextClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(
            text=json.dumps(self.responses.popleft(), ensure_ascii=False)
        )


class FakeBatchComparator:
    def __init__(
        self,
        outcomes: list[CharacterFactComparisonBatchResult | Exception],
        max_candidates_per_call: int = 20,
    ) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[list[str]] = []
        self.max_candidates_per_call = max_candidates_per_call

    def batch_fits(self, *, candidates, **kwargs) -> bool:
        return 0 < len(candidates) <= self.max_candidates_per_call

    async def compare_batch(self, *, candidates, **kwargs):
        self.calls.append([candidate.candidate_ref for candidate in candidates])
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, outcome.model_dump(mode="json")


class FakeBatchSpring:
    def __init__(
        self,
        batch: WorkerCharacterFactComparisonBatchPayload,
        context: WorkerCharacterFactComparisonBatchContextResponse,
        stale_completion_count: int = 0,
    ) -> None:
        self.batches = deque([batch])
        self.context = context
        self.completions = []
        self.failures = []
        self.stale_completion_count = stale_completion_count
        self.context_call_count = 0

    async def claim_next_character_fact_comparison_batch(self, *args):
        return self.batches.popleft() if self.batches else None

    async def get_character_fact_comparison_batch_context(self, *args):
        self.context_call_count += 1
        return self.context

    async def complete_character_fact_comparison_batch(self, *args):
        self.completions.append(args[-1])
        if self.stale_completion_count:
            self.stale_completion_count -= 1
            request = httpx.Request("POST", "http://spring.local/complete")
            response = httpx.Response(409, request=request)
            raise SpringWorkerHttpError(
                "stale",
                request=request,
                response=response,
                status_code=409,
                spring_error_code="SETTING_CANDIDATE_COMPARISON_STALE",
            )

    async def fail_character_fact_comparison_batch(
        self,
        analysis_job_id,
        comparison_batch_id,
        lease_token,
        error_message,
        failure_code,
    ):
        self.failures.append((comparison_batch_id, error_message, failure_code.value))


def _candidate(
    ref: str,
    projected_ref: str,
    value: str,
) -> WorkerCharacterFactComparisonBatchCandidate:
    return WorkerCharacterFactComparisonBatchCandidate.model_validate(
        {
            "candidateRef": ref,
            "projectedSnapshotRef": projected_ref,
            "sourceEpisodeNo": 5,
            "rawFactKey": "status.다리_상태",
            "initialCanonicalFactKey": "status.다리_상태",
            "canonicalKeyResolution": "PATTERN",
            "attributeValue": value,
            "valueType": "JSON",
            "valueJson": {"name": "다리 상태", "active": True},
            "evidenceSpans": [{"quote": value}],
            "confidence": 0.95,
        }
    )


def _candidates() -> list[WorkerCharacterFactComparisonBatchCandidate]:
    return [
        _candidate("C1", "Q1", "부상이 심해짐"),
        _candidate("C2", "Q2", "완전히 회복됨").model_copy(
            update={"value_json": {"name": "회복", "active": False}}
        ),
    ]


def _batch() -> WorkerCharacterFactComparisonBatchPayload:
    return WorkerCharacterFactComparisonBatchPayload(
        comparison_batch_id=BATCH_ID,
        work_id=WORK_ID,
        source_episode_id=EPISODE_ID,
        character_ref="K1",
        matched_character_name="비요른 얀델",
        canonical_fact_type="STATUS",
        candidates=_candidates(),
    )


def _context(
    batch: WorkerCharacterFactComparisonBatchPayload,
) -> WorkerCharacterFactComparisonBatchContextResponse:
    return WorkerCharacterFactComparisonBatchContextResponse(
        comparison_batch_id=batch.comparison_batch_id,
        character_ref=batch.character_ref,
        matched_character_name=batch.matched_character_name,
        canonical_fact_type=batch.canonical_fact_type,
        base_snapshot_version=7,
        candidates=batch.candidates,
        snapshot_entries=[],
        context_token=CONTEXT_TOKEN,
    )


def _projection_entry(ref: str, key: str, value: str) -> CharacterProjectionEntry:
    return CharacterProjectionEntry(
        reference=ref,
        fact_type="STATUS",
        fact_key=key,
        fact_value=value,
        value_json={"active": True},
    )


def _batch_snapshot(ref: str, key: str, value: str):
    return WorkerCharacterFactComparisonBatchSnapshotEntry.model_validate(
        {
            "snapshotRef": ref,
            "origin": "PERSISTED",
            "sourceCandidateRef": None,
            "dependencyCandidateRefs": [],
            "factType": "STATUS",
            "factKey": key,
            "factValue": value,
            "valueJson": {"active": True},
        }
    )


def _decision(
    candidate_ref: str,
    *,
    operation: str,
    resolved_key: str,
    target_ref: str | None = None,
    removed_refs: list[str] | None = None,
    value: str | None,
) -> CharacterFactComparisonBatchDecision:
    return CharacterFactComparisonBatchDecision.model_validate(
        _decision_payload(
            candidate_ref,
            operation=operation,
            resolved_key=resolved_key,
            target_ref=target_ref,
            removed_refs=removed_refs,
            value=value,
        )
    )


def _decision_payload(
    candidate_ref: str,
    *,
    operation: str,
    resolved_key: str,
    target_ref: str | None = None,
    removed_refs: list[str] | None = None,
    value: str | None,
    reason: str = "현재 상태 변화를 반영합니다.",
) -> dict:
    return {
        "candidate_ref": candidate_ref,
        "resolved_canonical_fact_key": resolved_key,
        "operation": operation,
        "target_ref": target_ref,
        "removed_snapshot_refs": removed_refs or [],
        "proposed_fact_value": value,
        "proposed_value_json": None if value is None else {"active": True},
        "temporal_scope": "PRESENT",
        "comparison_reason": reason,
    }
