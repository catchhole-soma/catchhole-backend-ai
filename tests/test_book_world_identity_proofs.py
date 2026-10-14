"""Input-owned parent boundaries reject a self-asserted false SAME_ENTITY tag."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.domain.enums import WorldSettingCategory
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


@pytest.mark.parametrize("raw, is_book, expected_scope", [
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": "정비반"}, True, "정비반", id="maintenance"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": "조율반"}, True, "조율반", id="tuning"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": "기록실"}, True, "기록실", id="archive"),
    pytest.param({"epistemic_status": "ASSERTED"}, True, None, id="scope-missing"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": None}, True, None, id="scope-null"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": {}}, True, None, id="scope-object"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": []}, True, None, id="scope-list"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": 42}, True, None, id="scope-number"),
    pytest.param({"epistemic_status": "ASSERTED", "scope_name": "정비반"}, False, None, id="non-book"),
])
def test_ordered_book_scope_input_contract(monkeypatch, raw, is_book, expected_scope):
    quote = "부서는 시설을 관리한다."
    item = source_candidate("수로관리청", [quote]).model_copy(update={
        "category": WorldSettingCategory.FACTION, "raw_extraction_json": raw,
        **({} if is_book else {"source": None}),
    })
    subject = target("수로관리청", ["수로관리청은 세 부서를 둔다."])
    before = item.model_dump(mode="json")
    client = RecordingClient()

    class RequestCaptured(BaseException):
        pass

    async def capture(**kwargs):
        client.requests.append(kwargs)
        raise RequestCaptured

    monkeypatch.setattr(client, "create_text_response", capture)
    monkeypatch.setattr("app.analysis.world_setting_comparator.get_settings", lambda: SimpleNamespace())
    resolver = WorldSettingSubjectResolver(
        llm_client=client, model="test-model", max_attempts=1, max_output_tokens=2000,
    )
    with pytest.raises(RequestCaptured):
        asyncio.run(resolver.select_ordered_subjects(item, [subject]))
    request, = client.requests
    expected_candidate = {"category": "FACTION", "subject_name": "수로관리청"}
    if expected_scope is not None:
        expected_candidate["scope_name"] = expected_scope
    expected_candidate["evidence"] = [quote]
    if is_book:
        expected_candidate["source"] = {
            "source_kind": "SETTING_BOOK", "setting_book_name": SOURCE["settingBookName"],
            "revision_no": SOURCE["revisionNo"], "reference_time_kind": "UNSPECIFIED",
            "reference_episode_no": None,
        }
    expected = json.dumps({
        "candidate": expected_candidate,
        "subjects": [{
            "ref": "S1", "name": "수로관리청", "aliases": [],
            "identity_evidence": ["수로관리청은 세 부서를 둔다."],
            "confirmation_status": "PROVISIONAL", "source_episode_no": None,
        }],
        "unresolved_references": [],
    }, ensure_ascii=False)
    assert request["user_prompt"].encode("utf-8") == expected.encode("utf-8")
    assert item.model_dump(mode="json") == before
    assert request["model"] == "test-model" and request["max_output_tokens"] == 2000
    assert request["prompt_cache_key"] == (
        "ordered-world-subject-resolution:v4:book-grounded-identity-v1" if is_book
        else "ordered-world-subject-resolution:v4"
    )
    schema = request["response_schema"]
    assert schema.name == ("book_world_subject_resolution" if is_book else "ordered_world_subject_resolution")
    assert schema.strict is True
    assert set(schema.schema["properties"]) == (
        {"selected_subject_refs", "ambiguous", "identity_proofs"} if is_book
        else {"selected_subject_refs", "ambiguous"}
    )


def test_parent_conflict_exhaustion_remains_failure_not_a_new_identity():
    quote = "물문은 낮항 안에 있다."
    item = source_candidate("물문", ["[시설 / 물문]", quote])
    city = target("낮항", ["[지역 / 낮항]"])
    with pytest.raises(ComparisonValidationError):
        run(item, [city], [choice(["S1"], [proof("S1", quote)])], attempts=1)
