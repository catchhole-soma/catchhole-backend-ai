"""Optional same-name proofs cannot contradict a selected canonical identity."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.schemas.worker import (
    WorkerAnalysisJobPayload, WorkerWorldSettingSubjectPageResponse,
    WorkerWorldSettingSubjectResolutionPendingResponse, WorkerWorldSettingSubjectResolutionResponse,
)
from tests.test_book_world_identity_proofs import choice, proof, run, source_candidate, target
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import book_payload_data


def same_target_case(alias):
    name = "새문" if alias else "동문"
    quote = f"이 {name}은 항구의 동문과 다른 시설이다."
    item = source_candidate(name, [quote])
    subject = target("동문", ["동문의 개방은 일출이다."], aliases=[name] if alias else [])
    return item, subject, quote


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("relation", ["DISTINCT", "CONTAINS", "RELATED", "UNKNOWN"])
def test_same_primary_or_supplied_alias_cannot_ignore_a_non_same_relation(alias, relation):
    item, subject, quote = same_target_case(alias)
    before = subject.model_dump()
    bad = {**proof("S1", quote), "relation": relation}
    (selected, ambiguous), client = run(item, [subject], [choice(["S1"], [bad]), choice(ambiguous=True)])
    assert selected == [] and ambiguous and len(client.requests) == 2
    assert subject.model_dump() == before
    assert json.loads(client.requests[1]["user_prompt"])["validation_feedback"]["reason_code"] == "BOOK_IDENTITY_PROOF_INVALID"


@pytest.mark.parametrize("alias", [False, True])
def test_same_primary_or_supplied_alias_still_requires_supplied_quote_grounding(alias):
    item, subject, _ = same_target_case(alias)
    (selected, ambiguous), client = run(item, [subject], [
        choice(["S1"], [proof("S1", "원문에 없는 동일성 주장")]), choice(ambiguous=True)])
    assert selected == [] and ambiguous and len(client.requests) == 2


@pytest.mark.parametrize("alias", [False, True])
def test_proof_from_another_targets_evidence_cannot_authorize_the_selected_name_match(alias):
    item, subject, _ = same_target_case(alias)
    quote = "이 문은 앞서 나온 문과 같다."
    other = target("남문", [quote])
    (selected, ambiguous), client = run(item, [subject, other], [
        choice(["S1"], [proof("S1", quote)]), choice(ambiguous=True)])
    assert selected == [] and ambiguous and len(client.requests) == 2


@pytest.mark.parametrize("alias", [False, True])
def test_same_primary_and_supplied_alias_keep_explicit_selection_without_an_optional_proof(alias):
    item, subject, _ = same_target_case(alias)
    (selected, ambiguous), client = run(item, [subject], [choice(["S1"])])
    assert selected == [subject] and not ambiguous and len(client.requests) == 1


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("evidence_owner", ["candidate", "selected_target"])
def test_valid_optional_same_entity_proof_does_not_require_two_different_primary_names(alias, evidence_owner):
    name = "새문" if alias else "동문"
    quote = f"{name}은 항구의 동문을 가리킨다."
    item = source_candidate(name, [quote] if evidence_owner == "candidate" else ["관리 규칙: 일출 개방"])
    subject = target("동문", [quote] if evidence_owner == "selected_target" else [], aliases=[name] if alias else [])
    (selected, ambiguous), client = run(item, [subject], [choice(["S1"], [proof("S1", quote)])])
    assert selected == [subject] and not ambiguous and len(client.requests) == 1


@pytest.mark.parametrize("alias", [False, True])
def test_one_contradictory_optional_proof_rejects_the_entire_multiple_target_selection(alias):
    item, subject, quote = same_target_case(alias)
    second = target("동문", [quote], aliases=[item.subject_name] if alias else [])
    bad = {**proof("S2", quote), "relation": "DISTINCT"}
    (selected, ambiguous), client = run(item, [subject, second], [
        choice(["S1", "S2"], [bad]), choice(ambiguous=True)])
    assert selected == [] and ambiguous and len(client.requests) == 2


@pytest.mark.parametrize("alias", [False, True])
def test_optional_proof_never_bypasses_the_selected_reference_contract(alias):
    item, subject, quote = same_target_case(alias)
    (selected, ambiguous), client = run(item, [subject], [
        choice(["S1"], [proof("S9", quote)]), choice(ambiguous=True)])
    assert selected == [] and ambiguous and len(client.requests) == 2


def test_explicit_ambiguity_with_contradictory_proof_never_enriches_identity_memory():
    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    context = payload.analysis_context
    first, subject, quote = same_target_case(False)
    second = source_candidate("동문", ["개방 시간: 일출"])
    items = [first, second]
    original = subject.model_dump()
    saved = []
    class Spring:
        async def get_pending_world_setting_subject_resolutions(self, *_args):
            return WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[] if saved else items,
                analysis_context=context)
        async def get_world_setting_subjects(self, *_args):
            return WorkerWorldSettingSubjectPageResponse(subjects=[subject], page=0, has_next=False)
        async def complete_world_setting_subject_resolutions(self, _job, _lease, request):
            saved.append(request)
            return WorkerWorldSettingSubjectResolutionResponse(resolutions=[{
                "candidateId": row.candidate_id, "resolutionType": "AMBIGUOUS" if row.ambiguous else "EXISTING",
                "canonicalSubjectKey": "pending" if row.ambiguous else subject.provisional_subject_key,
                "canonicalSubjectName": "동문", "targetWorldSettingIds": row.target_world_setting_ids,
                "provisionalSubjectKeys": row.provisional_subject_keys,
            } for row in request.resolutions])
    bad = {**proof("S1", quote), "relation": "DISTINCT"}
    client = RecordingClient(choice(["S1"], [bad], ambiguous=True), choice(["S1"]))
    asyncio.run(WorldSettingComparisonPipeline(Spring(), WorldSettingSubjectResolver(llm_client=client,
        model="test-model", max_attempts=1), object())._prepare_subject_resolutions(
            uuid4(), uuid4(), context, analysis_source=first.source))
    assert len(client.requests) == 2 and saved[0].resolutions[0].ambiguous
    assert saved[0].resolutions[0].provisional_subject_keys == []
    assert saved[0].resolutions[1].provisional_subject_keys == [subject.provisional_subject_key]
    assert json.loads(client.requests[1]["user_prompt"])["subjects"][0]["identity_evidence"] == [
        span.quote for span in subject.identity_evidence]
    assert subject.model_dump() == original


def test_contradictory_optional_proof_exhaustion_is_a_typed_failure():
    item, subject, quote = same_target_case(False)
    with pytest.raises(ComparisonValidationError):
        run(item, [subject], [choice(["S1"], [{**proof("S1", quote), "relation": "DISTINCT"}])], attempts=1)
