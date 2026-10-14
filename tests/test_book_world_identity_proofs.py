"""Input-owned parent boundaries reject a self-asserted false SAME_ENTITY tag."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import WorkerWorldSettingSubject, WorkerWorldSettingSubjectResolutionCandidate
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import SOURCE


def source_candidate(name, quotes):
    return WorkerWorldSettingSubjectResolutionCandidate(candidate_id=uuid4(), category="LOCATION",
        subject_name=name, evidence_spans=[{"quote": quote} for quote in quotes],
        source=WorkerAnalysisSource.model_validate(SOURCE), rawExtractionJson={"epistemic_status": "ASSERTED"})


def target(name, quotes, aliases=()):
    return WorkerWorldSettingSubject(provisional_subject_key=f"provisional-world:{uuid4()}",
        subject_name=name, aliases=list(aliases), identity_evidence=[{"quote": quote} for quote in quotes])


def choice(refs=(), proofs=(), ambiguous=False):
    return {"selected_subject_refs": list(refs), "ambiguous": ambiguous, "identity_proofs": list(proofs)}


def proof(ref, quote):
    return {"subject_ref": ref, "relation": "SAME_ENTITY", "quote": quote}


def run(item, subjects, outputs, attempts=2):
    client = RecordingClient(*outputs)
    result = asyncio.run(WorldSettingSubjectResolver(llm_client=client, model="test-model", max_attempts=attempts)
                         .select_ordered_subjects(item, subjects))
    return result, client


def test_exact_containment_quote_falsely_tagged_same_entity_retries_due_to_independent_parents():
    quote = "물문은 낮항 안에 있다."
    item = source_candidate("물문", ["[시설 / 물문]", quote, "가동 인원: 셋"])
    city = target("낮항", ["[지역 / 낮항]", "주교역품: 밀"])
    (selected, ambiguous), client = run(item, [city], [choice(["S1"], [proof("S1", quote)]), choice()])
    assert selected == [] and ambiguous is False
    assert len(client.requests) == 2
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason_code"] == "BOOK_PARENT_IDENTITY_CONFLICT"


def test_different_name_alias_bridge_can_use_the_selected_targets_supplied_alias():
    quote = "새뜰은 성림의 다른 이름이다."
    item = source_candidate("새뜰", [quote])
    park = target("서쪽 정원", ["이 정원을 성림이라고도 부른다."], aliases=["성림"])
    (selected, ambiguous), client = run(item, [park], [choice(["S1"], [proof("S1", quote)])])
    assert selected == [park] and ambiguous is False
    assert len(client.requests) == 1


@pytest.mark.parametrize("quote", [
    "큰탑",  # The short name inside the long name is not a second mention.
    "탑은 큰탑의 별칭이다. 없는 문장",  # Not a contiguous supplied quote.
    "탑은 이 장소의 별칭이다.",  # The selected target has no named mention.
])
def test_invalid_alias_evidence_cannot_connect_a_different_named_target(quote):
    item = source_candidate("탑", ["큰탑", "탑은 이 장소의 별칭이다."])
    building = target("큰탑", ["큰탑"], aliases=["큰탑"])
    (selected, ambiguous), client = run(item, [building], [choice(["S1"], [proof("S1", quote)]), choice(ambiguous=True)])
    assert selected == [] and ambiguous is True
    assert len(client.requests) == 2
    assert json.loads(client.requests[1]["user_prompt"])["validation_feedback"]["reason_code"] == "BOOK_IDENTITY_PROOF_INVALID"


def test_one_valid_proof_does_not_authorize_another_selected_target():
    quote = "새뜰은 성림의 다른 이름이다."
    item = source_candidate("새뜰", [quote])
    subjects = [target("성림", [quote]), target("남쪽 정원", ["관계는 미확인이다."])]
    (selected, ambiguous), client = run(item, subjects,
        [choice(["S1", "S2"], [proof("S1", quote)]), choice(ambiguous=True)])
    assert selected == [] and ambiguous is True and len(client.requests) == 2


def test_explicit_ambiguity_discards_selection_without_a_proof_or_evidence_accumulation():
    item = source_candidate("탑", ["탑의 위치는 불분명하다."])
    subject = target("큰탑", ["큰탑"])
    (selected, ambiguous), client = run(item, [subject], [choice(["S1"], ambiguous=True)])
    assert selected == [] and ambiguous is True and len(client.requests) == 1


def test_same_name_still_requires_model_choice_and_all_book_schema_objects_are_closed():
    item = source_candidate("물문", ["[시설 / 물문]", "가동 인원: 셋"])
    subject = target("물문", ["[시설 / 물문]"])
    (selected, ambiguous), client = run(item, [subject], [choice()])
    assert selected == [] and ambiguous is False
    from tests.test_book_world_provider_schema import _strict_object_violations
    assert _strict_object_violations(client.requests[0]["response_schema"].schema) == []


def test_parent_conflict_exhaustion_remains_failure_not_a_new_identity():
    quote = "물문은 낮항 안에 있다."
    item = source_candidate("물문", ["[시설 / 물문]", quote])
    city = target("낮항", ["[지역 / 낮항]"])
    with pytest.raises(ComparisonValidationError):
        run(item, [city], [choice(["S1"], [proof("S1", quote)])], attempts=1)
