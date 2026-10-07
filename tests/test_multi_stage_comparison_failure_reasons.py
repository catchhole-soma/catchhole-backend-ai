import asyncio
import copy
import json
from uuid import UUID

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_batch_recovery import compare_world_batch_with_recovery
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
)
from evals.multi_stage_setting.contracts import ScenarioPrediction, WorldStateEntry, world_state_ref
from evals.multi_stage_setting.runtime_adapter import (
    _run_world_batches,
    _WorldBatchSource,
    _WorldTargetSet,
)


class RecordedClient:
    def __init__(self, responses):
        self.responses = copy.deepcopy(responses)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text=json.dumps(self.responses.pop(0), ensure_ascii=False))


def _sources():
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=UUID(int=100), subject_name="산악족", version=1,
        properties=[WorkerWorldSettingProperty(
            scope_name="성인 의례", setting_name="기준 나이", value="열여덟 살",
        )],
    )
    target_set = _WorldTargetSet([target], {target.world_setting_id: [WorldStateEntry(
        ref=world_state_ref("RACE", "산악족", "성인 의례", "기준 나이"), category="RACE",
        subject_name="산악족", scope_name="성인 의례", setting_name="기준 나이", value="열여덟 살",
    )]})
    return [
        _WorldBatchSource(source_id, WorkerWorldSettingCandidatePayload(
            candidate_id=UUID(int=index), work_id=UUID(int=10), source_episode_id=UUID(int=11),
            category="RACE", subject_name="산악족", scope_name=None, setting_name=name,
            extracted_value=value, evidence_spans=[{"quote": value}], extraction_confidence=.9,
        ), target_set)
        for index, (source_id, name, value) in enumerate([
            ("first", "성인식 나이", "열여덟 살"), ("second", "언어", "산악어를 쓴다"),
        ], 1)
    ]


def _response(reason="범위를 정할 수 없어 성인 의례를 확인해야 합니다."):
    return {"decisions": [
        {"source_candidate_refs": ["C1"], "consolidation_status": "SINGLE",
         "operation": "REVIEW_REQUIRED", "review_reason": "SCOPE_UNRESOLVED",
         "target_ref": "T1", "matched_scope_name": "성인 의례",
         "matched_property_name": "기준 나이", "proposed_scope_name": "추론한 범위",
         "proposed_setting_name": "성인식 나이", "proposed_value": "열여덟 살",
         "comparison_reason": reason},
        {"source_candidate_refs": ["C2"], "consolidation_status": "SINGLE",
         "operation": "ADD", "target_ref": "T1", "proposed_scope_name": None,
         "proposed_setting_name": "언어", "proposed_value": "산악어를 쓴다",
         "comparison_reason": "기존에 없던 언어 정보입니다."},
    ]}


def _retry_response(reason="범위 선택을 다시 확인해야 합니다."):
    return {"decisions": [_response(reason)["decisions"][0]]}


def test_invalid_review_retains_proposals_and_independent_success_after_save_load():
    client = RecordedClient([_response(), _retry_response()])
    predictions, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=client, max_attempts=2),
    ))
    assert [item.source_candidate_id for item in predictions] == ["second"]
    assert [item.source_id for item in failures] == ["first"]
    saved = ScenarioPrediction(scenario_id="synthetic", failures=failures).model_dump_json()
    restored = ScenarioPrediction.model_validate_json(saved)
    attempts = getattr(restored.failures[0], "comparison_attempts", [])
    assert len(attempts) == 2
    assert attempts[0].rule_code == "SCOPE_UNRESOLVED_REVIEW_INVALID"
    assert attempts[0].rejected_source_ids == ["first"]
    assert attempts[0].decisions[0].comparison_reason == _response()["decisions"][0]["comparison_reason"]
    assert attempts[1].decisions[0].comparison_reason == "범위 선택을 다시 확인해야 합니다."
    assert len(attempts[1].decisions) == 1
    assert attempts[1].decisions[0].matched_path_verified is True
    assert "Authorization" not in saved and "evidence_spans" not in saved


def test_unknown_source_ref_does_not_assign_rejection_to_a_known_candidate():
    response = _response()
    response["decisions"][0]["source_candidate_refs"] = ["C999"]
    _, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=RecordedClient([response, _retry_response()]), max_attempts=1),
    ))
    attempts = getattr(failures[0], "comparison_attempts", [])
    assert len(attempts) == 2
    assert attempts[0].rejected_source_ids == []
    assert attempts[0].attribution == "UNKNOWN"


def test_schema_failure_keeps_batch_attribution_unknown():
    response = _response()
    response["decisions"][0]["operation"] = "invented"
    _, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=RecordedClient([response, _retry_response()]), max_attempts=1),
    ))
    attempts = getattr(failures[0], "comparison_attempts", [])
    assert len(attempts) == 2
    assert attempts[0].attribution == "UNKNOWN"
    assert attempts[0].rejected_source_ids == []


def test_observation_preserves_shared_recovery_requests_and_failure_metadata():
    observed_client = RecordedClient([_response(), _retry_response()])
    _, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=observed_client, max_attempts=2),
    ))
    plain_client = RecordedClient([_response(), _retry_response()])
    comparator = WorldSettingComparator(llm_client=plain_client, max_attempts=2)
    sources = _sources()
    from app.schemas.worker import WorkerWorldSettingComparisonBatchCandidate
    candidates = [WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=f"C{index}", candidate_id=item.candidate.candidate_id,
        subject_name=item.candidate.subject_name, scope_name=item.candidate.scope_name,
        setting_name=item.candidate.setting_name, extracted_value=item.candidate.extracted_value,
        evidence_spans=item.candidate.evidence_spans,
        extraction_confidence=item.candidate.extraction_confidence,
    ) for index, item in enumerate(sources, 1)]
    result, _ = asyncio.run(compare_world_batch_with_recovery(comparator, "RACE", candidates, sources[0].target_set.targets))
    assert failures[0].error_type == result.failures[0].failure_code.value
    assert failures[0].message == result.failures[0].error_message
    assert observed_client.requests == plain_client.requests


@pytest.mark.parametrize("operation", [{"unexpected": "container"}, [], None])
def test_malformed_operation_container_retains_unknown_failure_diagnostic(operation):
    response = _response()
    response["decisions"][0]["operation"] = operation
    _, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=RecordedClient([response, _retry_response()]), max_attempts=1),
    ))
    assert len(failures[0].comparison_attempts) == 2
    assert failures[0].comparison_attempts[0].attribution == "UNKNOWN"
    assert failures[0].comparison_attempts[0].decisions[0].operation is None


def test_conflicting_final_path_identifies_both_contributing_sources():
    response = _response()
    for decision in response["decisions"]:
        decision.update(operation="ADD", review_reason=None, matched_scope_name=None,
                        matched_property_name=None, proposed_scope_name=None, proposed_setting_name="신규 공통 정보")
    _, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=RecordedClient([response, response]), max_attempts=1),
    ))
    diagnostic = failures[0].comparison_attempts[0]
    assert diagnostic.rule_code == "FINAL_PATH_DUPLICATED"
    assert diagnostic.rejected_source_ids == ["first", "second"]


def _scoring_inputs(failures):
    from evals.multi_stage_setting.contracts import (
        EvaluationState,
        GoldSnapshotV3,
        PredictionBundleV3,
        ScenarioGold,
        WorldStage1Gold,
        WorldStage1Prediction,
        WorldStage2Gold,
        world_subject_ref,
    )
    scenario = ScenarioGold(
        scenario_id="synthetic", episode_no=1, source_identifier="synthetic.txt",
        target_domains={"WORLD"}, gold_version="synthetic", start_state_mode="SEED",
        cumulative_through_episode=0, seed_state=EvaluationState(), review_status="FINAL",
    )
    rows = [WorldStage1Gold(
        gold_id=item.source_id, scenario_id="synthetic", episode_no=1, sort_order=index,
        decision="EXTRACT", importance="MUST", evidence_quotes=[item.candidate.extracted_value],
        review_status="FINAL", domain="WORLD", candidate_kind="WORLD_SETTING",
        category="RACE", subject_name="산악족", setting_name=item.candidate.setting_name,
        source_values=[item.candidate.extracted_value],
    ) for index, item in enumerate(_sources(), 1)]
    gold = GoldSnapshotV3(dataset_version="synthetic", name="failure reasons", scenarios=[scenario],
        stage1=rows, stage2=[WorldStage2Gold(
            decision_id="d-" + row.gold_id, scenario_id="synthetic", episode_no=1,
            sort_order=index, source_gold_ids=[row.gold_id], domain="WORLD", operation="ADD",
            target_ref=world_subject_ref("RACE", "산악족") if index > 1 else None,
            consolidation_status="SINGLE", proposed_setting_name=row.setting_name,
            proposed_value=row.display_value, review_status="FINAL",
        ) for index, row in enumerate(rows, 1)]).with_fixture_hash()
    prediction = ScenarioPrediction(scenario_id="synthetic", failures=failures,
        stage1=[WorldStage1Prediction(candidate_id=row.gold_id, sort_order=row.sort_order,
            domain="WORLD", category="RACE", subject_name="산악족", setting_name=row.setting_name,
            source_values=row.source_values, evidence_spans=[{"quote": row.display_value}],
        ) for row in rows])
    bundle = PredictionBundleV3(fixture_hash=gold.fixture_hash, mode="FIXED", evaluation_domains={"WORLD"},
        scenarios=[prediction])
    return gold, PredictionBundleV3.model_validate_json(bundle.model_dump_json())


def test_failure_reasons_reach_scored_cases_without_changing_scores():
    from evals.multi_stage_setting.evaluator import evaluate_multi_stage
    from evals.multi_stage_setting.report_cli import render_markdown_summary
    _, failures = asyncio.run(_run_world_batches(
        _sources(), WorldSettingComparator(llm_client=RecordedClient([_response(), _retry_response()]), max_attempts=1),
    ))
    gold, bundle = _scoring_inputs(failures)
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    cases = report["scenarios"][0]["stage2"]
    assert cases[0].get("comparisonFailure", {}).get("status") == "REJECTED"
    assert cases[1].get("comparisonFailure") is None
    assert cases[0]["comparisonFailure"]["attempts"][0]["decision"]["matchedPath"] == "성인 의례 › 기준 나이"
    assert cases[0]["comparisonFailure"]["attempts"][0].get("rejectedSourceNames") == ["산악족 · 성인식 나이"]
    assert "범위 선택을 다시 확인해야 합니다." in render_markdown_summary(report)
    old_bundle = bundle.model_copy(update={"scenarios": [bundle.scenarios[0].model_copy(update={
        "failures": [failure.model_copy(update={"comparison_attempts": []}) for failure in failures],
    })]})
    old_report = asyncio.run(evaluate_multi_stage(gold, old_bundle))
    assert report["stages"] == old_report["stages"]
    assert report["endToEnd"] == old_report["endToEnd"]
