"""Explicit uncertain decisions remain reviewable without relaxing factual writes."""

import asyncio
from copy import deepcopy
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_schemas import WorldSettingComparisonDecision
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.worker import WorkerWorldSettingComparisonBatchCandidate
from tests.test_world_setting_comparator import FakeTextClient, _candidate, _target as _legacy_target


REASON = "대상이나 내용을 확실히 판단하기 어려워 확인이 필요합니다."


def _target():
    target = _legacy_target()
    target.properties[0].provenance = AnalysisStateProvenance(
        confirmation_status="CONFIRMED", source_episode_no=1, review_source="AUTOMATIC",
    )
    return target


def source(ref="C1"):
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=ref, candidate_id=UUID(int=int(ref[1:])), subject_name="얼음 새",
        scope_name=None, setting_name="이동 특징", extracted_value="물 위에서도 움직인다.",
        evidence_spans=[{"quote": "그 새는 물 위에서도 움직인다."}],
    )


def response(*, ordered=True, batch=True, matched=True):
    result = dict(
        consolidation_status="SINGLE", operation="REVIEW_REQUIRED", review_reason="GENERAL_UNCERTAINTY",
        target_ref="T1", proposed_scope_name=None, proposed_setting_name="이동 특징",
        proposed_value="물 위에서도 움직인다.", comparison_reason=REASON,
    )
    if batch:
        result.update(source_candidate_refs=["C1"], existing_root_property_names_to_move=[])
    if ordered:
        result["matched_property_ref"] = "T1.P1" if matched else None
    else:
        result.update(matched_scope_name=None, matched_property_name="서식지" if matched else None)
    return {"decisions": [result]} if batch else result


def compare_batch(body, *, ordered=True, targets=None, sources=None):
    client = FakeTextClient([body])
    result, raw = asyncio.run(WorldSettingComparator(llm_client=client, max_attempts=1).compare_batch(
        "RACE", sources or [source()], [_target()] if targets is None else targets,
        ordered_context=ordered,
    ))
    return result, raw, client


@pytest.mark.parametrize("ordered", [False, True])
@pytest.mark.parametrize("matched", [False, True])
def test_general_review_of_unscoped_content_completes_once_without_changing_source_or_target(ordered, matched):
    candidate = source()
    target = _target()
    before = (candidate.model_dump(), target.model_dump())
    body = response(ordered=ordered, matched=matched)
    # As with existing single-source review, deterministic fields are copied from
    # the source instead of requesting another call for paraphrased text.
    body["decisions"][0].update(proposed_scope_name="임의 분류", proposed_setting_name="임의 이름",
                                 proposed_value="추측을 보탠 문장")
    result, raw, client = compare_batch(body, ordered=ordered, targets=[target], sources=[candidate])
    decision = result.decisions[0]
    assert decision.operation == "REVIEW_REQUIRED" and decision.review_reason == "GENERAL_UNCERTAINTY"
    assert (decision.proposed_scope_name, decision.proposed_setting_name, decision.proposed_value) == (
        candidate.scope_name, candidate.setting_name, candidate.extracted_value,
    )
    assert decision.comparison_reason == REASON
    assert decision.matched_property_name == ("서식지" if matched else None)
    assert decision.existing_root_property_names_to_move == []
    assert (candidate.model_dump(), target.model_dump()) == before
    assert raw["decisions"][0]["review_reason"] == "GENERAL_UNCERTAINTY"
    assert len(client.requests) == 1
    assert client.requests[0]["prompt_cache_key"] == "world-setting-comparison-batch:v8" + (
        ":ordered-provisional-v6" if ordered else ""
    )


def test_ordered_response_schema_exposes_general_reason_and_only_actual_property_choices():
    _, _, client = compare_batch(response())
    schema = client.requests[0]["response_schema"].schema
    assert "GENERAL_UNCERTAINTY" in schema["$defs"]["WorldSettingComparisonReviewReason"]["enum"]
    assert schema["$defs"]["OrderedWorldPropertyDecision"]["properties"]["matched_property_ref"] == {
        "anyOf": [{"type": "string", "enum": ["T1.P1"]}, {"type": "null"}],
    }


@pytest.mark.parametrize("ordered", [False, True])
def test_general_review_can_hold_new_identity_without_claiming_a_nonexistent_match(ordered):
    body = response(ordered=ordered, matched=False)
    body["decisions"][0]["target_ref"] = None
    result, _, _ = compare_batch(body, ordered=ordered, targets=[])
    assert result.decisions[0].target_ref is None
    assert result.decisions[0].review_reason == "GENERAL_UNCERTAINTY"


@pytest.mark.parametrize("ordered", [False, True])
@pytest.mark.parametrize("change", ["unknown_target", "missing_target", "unknown_property"])
def test_general_review_still_rejects_invalid_existing_targets_and_properties(ordered, change):
    body = response(ordered=ordered)
    decision = body["decisions"][0]
    if change == "unknown_target":
        decision["target_ref"] = "T999"
    elif change == "missing_target":
        decision["target_ref"] = None
    elif ordered:
        decision["matched_property_ref"] = "T1.P999"
    else:
        decision["matched_property_name"] = "없는 속성"
    with pytest.raises(ComparisonValidationError):
        compare_batch(body, ordered=ordered)


@pytest.mark.parametrize("change", ["multiple_sources", "property_move"])
@pytest.mark.parametrize("ordered", [False, True])
def test_general_review_does_not_merge_sources_or_move_stored_properties(change, ordered):
    body = response(ordered=ordered)
    sources = [source()]
    if change == "multiple_sources":
        sources.append(source("C2"))
        body["decisions"][0].update(source_candidate_refs=["C1", "C2"], consolidation_status="MERGED")
    else:
        body["decisions"][0]["existing_root_property_names_to_move"] = ["서식지"]
    with pytest.raises(ComparisonValidationError):
        compare_batch(body, ordered=ordered, sources=sources)


@pytest.mark.parametrize("ordered", [False, True])
def test_scope_review_of_unscoped_property_is_still_invalid_and_never_rewritten_to_general(ordered):
    body = response(ordered=ordered)
    body["decisions"][0]["review_reason"] = "SCOPE_UNRESOLVED"
    with pytest.raises(ComparisonValidationError):
        compare_batch(body, ordered=ordered)


@pytest.mark.parametrize("matched", [False, True])
def test_single_general_review_preserves_raw_content_and_optional_existing_path(matched):
    candidate = _candidate("이동 특징", "물 위에서도 움직인다.")
    body = response(ordered=False, batch=False, matched=matched)
    body.update(proposed_scope_name="임의", proposed_setting_name="임의", proposed_value="다듬은 문장")
    client = FakeTextClient([body])
    result, raw = asyncio.run(WorldSettingComparator(llm_client=client, max_attempts=1).compare(candidate, [_target()]))
    assert result.review_reason == "GENERAL_UNCERTAINTY" and result.operation == "REVIEW_REQUIRED"
    assert (result.proposed_scope_name, result.proposed_setting_name, result.proposed_value) == (
        None, candidate.setting_name, candidate.extracted_value,
    )
    assert raw["proposed_value"] == candidate.extracted_value
    assert client.requests[0]["prompt_cache_key"] == "world-setting-comparison:v11"


@pytest.mark.parametrize("change", ["target_null", "unknown_target", "unknown_property", "scope_without_property"])
def test_single_general_review_rejects_invalid_optional_match(change):
    body = response(ordered=False, batch=False)
    if change == "target_null":
        body["target_ref"] = None
    elif change == "unknown_target":
        body["target_ref"] = "T999"
    elif change == "unknown_property":
        body["matched_property_name"] = "없는 속성"
    else:
        body.update(matched_scope_name="범위", matched_property_name=None)
    with pytest.raises(ComparisonValidationError):
        asyncio.run(WorldSettingComparator(llm_client=FakeTextClient([body]), max_attempts=1).compare(
            _candidate("이동 특징", "원본"), [_target()],
        ))


def test_explicit_general_review_is_not_changed_into_scoped_review():
    target = _target()
    target.properties[0].scope_name = "거주 환경"
    candidate = source().model_copy(update={"setting_name": "서식지"})
    result, _, _ = compare_batch(response(), targets=[target], sources=[candidate])
    assert result.decisions[0].review_reason == "GENERAL_UNCERTAINTY"
    assert result.decisions[0].matched_scope_name == "거주 환경"


@pytest.mark.parametrize("ordered", [False, True])
def test_general_review_preserves_every_line_of_a_preconsolidated_source(ordered):
    candidate = source().model_copy(update={"extracted_value": "물 위에서도 움직인다.\n깊은 물은 피한다."})
    body = response(ordered=ordered)
    body["decisions"][0].update(consolidation_status="MERGED", proposed_value="임의로 합친 값")
    result, _, _ = compare_batch(body, ordered=ordered, sources=[candidate])
    assert result.decisions[0].proposed_value == candidate.extracted_value


def test_general_review_reason_must_not_expose_the_new_internal_enum():
    body = response()
    body["decisions"][0]["comparison_reason"] = "GENERAL_UNCERTAINTY 때문에 확인이 필요합니다."
    with pytest.raises(ComparisonValidationError):
        compare_batch(body)


@pytest.mark.parametrize("operation", ["ADD", "UPDATE", "MERGE", "EXCLUDE"])
def test_general_reason_cannot_mark_a_concrete_write(operation):
    body = response(ordered=False, batch=False)
    body["operation"] = operation
    with pytest.raises(ValidationError):
        WorldSettingComparisonDecision.model_validate(body)


def test_valid_concrete_decision_survives_beside_general_review():
    body = response()
    normal = deepcopy(body["decisions"][0])
    normal.update(source_candidate_refs=["C2"], operation="ADD", review_reason=None,
                  matched_property_ref=None, proposed_scope_name=None, proposed_setting_name="먹이",
                  proposed_value="작은 물고기")
    body["decisions"].append(normal)
    result, _, _ = compare_batch(body, sources=[source(), source("C2").model_copy(update={
        "setting_name": "먹이", "extracted_value": "작은 물고기",
    })])
    assert [row.operation for row in result.decisions] == ["REVIEW_REQUIRED", "ADD"]
    assert json.loads(json.dumps(result.model_dump(mode="json")))["decisions"][0]["comparison_reason"] == REASON
