import asyncio
import json
from uuid import uuid4

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import WorkerWorldSettingCandidatePayload, WorkerWorldSettingComparisonTarget
from tests.test_book_world_claims import candidate, decision
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import SOURCE


def test_past_concrete_output_gets_fixed_temporal_feedback_before_valid_review():
    item = candidate({"epistemic_status": "ASSERTED"}).model_copy(update={"temporal_scope": "PAST"})
    client = RecordingClient({"decisions": [decision("ADD")]}, {"decisions": [decision("REVIEW_REQUIRED")]})
    result, _ = asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=2).compare_batch(
        "WORLD_RULE_HISTORY", [item], [], ordered_context=True,
        analysis_source=WorkerAnalysisSource.model_validate(SOURCE)))
    assert result.decisions[0].operation == "REVIEW_REQUIRED"
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason_code"] == "BOOK_TIME_REQUIRES_REVIEW"
    assert feedback["issues"][0]["decision_index"] == 0


def test_singleton_past_retry_has_fixed_feedback_and_preserves_real_subject_and_value():
    item = WorkerWorldSettingCandidatePayload(candidate_id=uuid4(), work_id=uuid4(),
        source=WorkerAnalysisSource.model_validate(SOURCE), category="WORLD_RULE_HISTORY",
        subject_name="옛 규약", setting_name="통행세", extracted_value="돌 하나",
        evidence_spans=[{"quote": "옛 규약의 통행세는 돌 하나였다."}], temporal_scope="PAST",
        rawExtractionJson={"epistemic_status": "ASSERTED"})
    def response(operation):
        return {"operation": operation, "target_ref": "T1", "consolidation_status": "SINGLE",
            "proposed_setting_name": "통행세", "proposed_value": "돌 하나",
            "review_reason": "GENERAL_UNCERTAINTY" if operation == "REVIEW_REQUIRED" else None,
            "comparison_reason": "당시의 통행세를 과거 기록으로 보존합니다."}
    client = RecordingClient(response("ADD"), response("REVIEW_REQUIRED"))
    result, _ = asyncio.run(WorldSettingComparator(llm_client=client, model="test-model", max_attempts=2).compare(
        item, [WorkerWorldSettingComparisonTarget(world_setting_id=uuid4(), subject_name="옛 규약", properties=[], version=0)]))
    assert result.operation == "REVIEW_REQUIRED" and result.target_ref == "T1"
    assert result.proposed_setting_name == "통행세" and result.proposed_value == "돌 하나"
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason_code"] == "BOOK_TIME_REQUIRES_REVIEW"
