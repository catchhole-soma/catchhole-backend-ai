import asyncio
import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.character_status_observation_review import review_status_observations
from app.analysis.exceptions import LlmExtractionError
from app.analysis.schemas import ExtractedCharacterSettingCandidate, ExtractedEvidenceSpan
from app.analysis.status_evidence import StatusEvidenceDocument
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.usage.metering import MeteredTextGenerationClient

CHUNK_ID = UUID("00000000-0000-0000-0000-000000000123")
CHARACTER_ID = UUID("00000000-0000-0000-0000-000000000456")
SOURCE = "리안은 말을 하지 못했다. 약을 마셨다. 리안은 또렷하게 대답했다."
KNOWN = (KnownCharacter(CHARACTER_ID, "리안", ()),)


class Client:
    def __init__(self, *responses, repeat_last=True):
        self.responses = list(responses)
        self.calls = []
        self.repeat_last = repeat_last
        self.last = None

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses:
            item = self.responses.pop(0)
            self.last = item
        elif self.repeat_last and self.last is not None:
            item = self.last
        else:
            raise RuntimeError("test provider has no remaining response")
        if isinstance(item, Exception):
            raise item
        return LlmTextResponse(text=json.dumps(item, ensure_ascii=False))


def draft(document, index=0, *, key="status.발화_불능", active=True, name="리안"):
    unit = document.units[index]
    value = {"name": key.removeprefix("status.")}
    if active is not None:
        value["active"] = active
    return ExtractedCharacterSettingCandidate(
        source_chunk_id=CHUNK_ID, candidate_kind="SETTING", entity_name=name,
        raw_entity_mention=None, attribute_name=key, attribute_value="해당 상태 관찰",
        value_type="JSON", value_json=value,
        evidence_spans=[ExtractedEvidenceSpan(quote=unit.text, start_offset=unit.start_offset,
                                             end_offset=unit.end_offset)], confidence=0.8,
    )


def response(*, draft_count=1, target_count=1, end_refs=None, draft_refs=None):
    refs = [f"D{i}" for i in range(1, draft_count + 1)] if draft_refs is None else draft_refs
    return {
        "draft_reviews": [{"draft_ref": ref, "classification": "STATE_OBSERVATION",
                           "reason": "원문 상태 관찰이다.", "evidence_refs": []}
                          for ref in refs],
        "target_reviews": [{"target_ref": f"T{i}", "end_state": "ENDED" if end_refs else "UNKNOWN",
                            "reason": "현재 원문의 후속 기능을 검토했다.",
                            "end_observations": [{"summary": "후속 관찰에서 기능이 회복됨",
                                                  "evidence_refs": list(end_refs)}] if end_refs else []}
                           for i in range(1, target_count + 1)],
    }


def run(client, candidates, *, source=SOURCE, known=KNOWN, **kwargs):
    options = {"llm_client": client, "model": "gpt-5.6-terra", "max_output_tokens": 6000,
               "truncation_retry_max_output_tokens": 12000, "max_attempts": 1,
               "source_chunk_id": CHUNK_ID, "chunk_text": source,
               "evidence_document": StatusEvidenceDocument(source),
               "status_candidates": candidates, "known_characters": known}
    options.update(kwargs)
    return asyncio.run(review_status_observations(**options))


def test_adds_exact_end_and_preserves_source_observation_and_metadata():
    document = StatusEvidenceDocument(SOURCE)
    onset = draft(document)
    before = onset.model_dump(mode="json")
    client = Client(response(end_refs=["E2", "E3"]))

    result = run(client, [onset], narrative_context={"previous_episode": "다른 과거의 상태"})

    assert len(result) == 2
    assert result[0] is onset
    assert onset.model_dump(mode="json") == before
    end = result[1]
    assert isinstance(end, ExtractedCharacterSettingCandidate)
    assert end.source_chunk_id == CHUNK_ID
    assert (end.entity_name, end.attribute_name) == (onset.entity_name, onset.attribute_name)
    assert end.value_json["active"] is False
    assert end.raw_entity_mention is None
    assert [span.quote for span in end.evidence_spans] == [u.text for u in document.units[1:]]
    assert all(SOURCE[s.start_offset:s.end_offset] == s.quote for s in end.evidence_spans)
    call = client.calls[0]
    payload = json.loads(call["user_prompt"])
    assert str(CHUNK_ID) not in call["user_prompt"]
    assert payload["targets"][0]["draft_refs"] == ["D1"]
    assert call["response_schema"].strict
    assert call["response_schema"].schema["$defs"]["CurrentEvidenceRef"]["enum"] == ["E1", "E2", "E3"]
    assert call["prompt_cache_key"] == "status-observation-review:v9"
    assert len(client.calls) == 3
    assert all(request == call for request in client.calls)


def test_active_status_without_draft_can_receive_grounded_end_without_identifier_leak():
    known = (KnownCharacter(CHARACTER_ID, "리안", (ActiveCharacterStatus("status.발화_불능", None),)),)
    client = Client(response(draft_count=0, end_refs=["E3"]))
    result = run(client, [], known=known)
    assert len(result) == 1 and result[0].value_json["active"] is False
    assert str(CHARACTER_ID) not in client.calls[0]["user_prompt"]
    assert client.calls[0]["response_schema"].schema["properties"]["draft_reviews"]["maxItems"] == 0


def test_one_off_process_excluded_but_independent_effect_and_injury_preserved():
    source = ("리안은 다쳤다. 한 번의 치료로 기존 상처가 아물었다. "
              "다음 한 시간 동안 수호 문양은 계속 빛나며 새로 생긴 상처도 반복해서 아물게 했다.")
    document = StatusEvidenceDocument(source)
    candidates = [draft(document), draft(document, 1, key="status.처치_효과", active=None),
                  draft(document, 2, key="status.지속_재생")]
    before = [c.model_dump(mode="json") for c in candidates]
    payload = response(draft_count=3, target_count=3)
    payload["draft_reviews"][1].update(classification="ONE_OFF_TREATMENT_PROCESS", evidence_refs=["E2"])
    client = Client(payload)
    result = run(client, candidates, source=source)
    assert result == [candidates[0], candidates[2]]
    assert [c.model_dump(mode="json") for c in candidates] == before
    # Verify classification/application rather than wording of the model prompt.
    sent = json.loads(client.calls[0]["user_prompt"])
    assert [row["draft_ref"] for row in sent["draft_observations"]] == ["D1", "D2", "D3"]
    assert sent["preserved_state_observations"] == []


@pytest.mark.parametrize("mutation", [
    lambda p: p["draft_reviews"].clear(),
    lambda p: p["draft_reviews"].append(deepcopy(p["draft_reviews"][0])),
    lambda p: p["draft_reviews"][0].update(draft_ref="D99"),
    lambda p: p["target_reviews"].clear(),
    lambda p: p["target_reviews"].append(deepcopy(p["target_reviews"][0])),
    lambda p: p["target_reviews"][0].update(target_ref="T99"),
    lambda p: p["target_reviews"][0]["end_observations"][0].update(evidence_refs=["PREVIOUS:E3"]),
    lambda p: p["target_reviews"][0]["end_observations"][0].update(evidence_refs=["E99"]),
    lambda p: p["target_reviews"][0]["end_observations"][0].update(evidence_refs=["E3", "E3"]),
    lambda p: p["target_reviews"][0]["end_observations"][0].update(evidence_refs=["E1"]),
    lambda p: p["target_reviews"][0]["end_observations"][0].update(entity_name="다른 인물"),
    lambda p: p["target_reviews"][0]["end_observations"][0].update(active=True),
    lambda p: p["target_reviews"][0].update(end_state="UNKNOWN"),
    lambda p: p["target_reviews"][0].update(end_observations=[]),
])
def test_invalid_coverage_ref_shape_or_chronology_fails_atomically(mutation):
    candidate = draft(StatusEvidenceDocument(SOURCE))
    before = candidate.model_dump(mode="json")
    payload = response(end_refs=["E3"])
    mutation(payload)
    with pytest.raises(LlmExtractionError):
        run(Client(payload), [candidate])
    assert candidate.model_dump(mode="json") == before


def test_recurrence_after_original_end_requires_later_new_end():
    source = "리안은 다쳤다. 나았다. 다시 다쳤다. 치료 뒤 다시 나았다."
    doc = StatusEvidenceDocument(source)
    candidates = [draft(doc, 0), draft(doc, 1, active=False), draft(doc, 2)]
    bad = response(draft_refs=("D1", "D3"), end_refs=["E2"])
    with pytest.raises(LlmExtractionError):
        run(Client(bad), candidates, source=source)
    good = response(draft_refs=("D1", "D3"), end_refs=["E4"])
    result = run(Client(good), candidates, source=source)
    assert result[:3] == candidates
    assert result[-1].evidence_spans[0].start_offset == doc.units[3].start_offset


def test_existing_end_is_preserved_without_adding_duplicate():
    doc = StatusEvidenceDocument(SOURCE)
    candidates = [draft(doc), draft(doc, 2, active=False)]
    payload = response(draft_refs=("D1",))
    payload["target_reviews"][0]["end_state"] = "ENDED"
    assert run(Client(payload), candidates) == candidates
    payload["draft_reviews"].append({"draft_ref": "D2", "classification": "ONE_OFF_TREATMENT_PROCESS",
                                     "reason": "종료 원형을 분류하려는 잘못된 응답", "evidence_refs": ["E3"]})
    with pytest.raises(LlmExtractionError):
        run(Client(payload), candidates)


def test_repeated_sentence_end_keeps_selected_occurrence_with_crlf():
    source = "리안은 쓰러졌다.\r\n리안은 일어났다.\r\n리안은 쓰러졌다.\r\n리안은 일어났다."
    doc = StatusEvidenceDocument(source)
    candidates = [draft(doc, 0), draft(doc, 1, active=False), draft(doc, 2)]
    result = run(Client(response(draft_refs=("D1", "D3"), end_refs=["E4"])), candidates, source=source)
    end = result[-1].evidence_spans[0]
    assert end.start_offset == source.rindex("리안은 일어났다.")
    assert end.quote == source[end.start_offset:end.end_offset]


def test_invalid_response_retries_with_safe_code_without_losing_original_source():
    bad = response(end_refs=["E99"])
    client = Client(bad, response(end_refs=["E3"]))
    assert len(run(client, [draft(StatusEvidenceDocument(SOURCE))], max_attempts=2)) == 2
    retry = json.loads(client.calls[1]["user_prompt"])
    original = json.loads(client.calls[0]["user_prompt"])
    feedback = retry.pop("validation_feedback")
    assert feedback["reason_code"] == "STATUS_REVIEW_UNKNOWN_EVIDENCE_REF"
    assert retry == original


def test_no_targets_and_empty_source_do_not_call_provider():
    client = Client()
    assert run(client, []) == []
    known = (KnownCharacter(CHARACTER_ID, "리안", (ActiveCharacterStatus("status.부상", "다침"),)),)
    assert run(client, [], source="\r\n ", known=known) == []
    assert client.calls == []


def test_input_limit_prevents_provider_call():
    client = Client()
    with pytest.raises(LlmExtractionError, match="INPUT_LIMIT"):
        run(client, [draft(StatusEvidenceDocument(SOURCE))], max_input_tokens=1)
    assert client.calls == []


def test_provider_failure_is_not_replaced_by_drafts():
    error = RuntimeError("quota stop")
    client = Client(error)
    with pytest.raises(RuntimeError) as caught:
        run(client, [draft(StatusEvidenceDocument(SOURCE))])
    assert caught.value is error
    assert len(client.calls) == 1


def test_output_expands_once_and_second_truncation_propagates():
    def truncated(limit):
        return LlmOutputTruncatedError("output truncated", incomplete_reason="max_output_tokens",
                                       max_output_tokens=limit, output_token_count=limit)
    candidate = draft(StatusEvidenceDocument(SOURCE))
    client = Client(truncated(6000), response())
    assert run(client, [candidate]) == [candidate]
    assert [call["max_output_tokens"] for call in client.calls] == [6000, 12000, 6000, 6000]
    assert client.calls[0]["user_prompt"] == client.calls[1]["user_prompt"]
    client = Client(truncated(6000), truncated(12000))
    with pytest.raises(LlmOutputTruncatedError):
        run(client, [candidate], max_attempts=3)
    assert len(client.calls) == 2


def test_one_off_process_requires_current_evidence():
    payload = response()
    payload["draft_reviews"][0]["classification"] = "ONE_OFF_TREATMENT_PROCESS"
    with pytest.raises(LlmExtractionError):
        run(Client(payload), [draft(StatusEvidenceDocument(SOURCE))])


@pytest.mark.parametrize("value_type,value,display", [("NUMBER", 3, "3"), ("BOOLEAN", True, "true")])
def test_scalar_status_end_preserves_declared_type(value_type, value, display):
    original = draft(StatusEvidenceDocument(SOURCE)).model_copy(update={
        "value_type": value_type, "value_json": {"value": value, "active": True},
        "attribute_value": display,
    })
    result = run(Client(response(end_refs=["E3"])), [original])
    assert result[0] is original
    assert result[1].value_type == value_type
    assert result[1].attribute_value == display
    assert result[1].value_json["value"] == value
    assert result[1].value_json["active"] is False


def test_wrong_document_is_rejected_before_provider_call():
    client = Client()
    with pytest.raises(ValueError, match="DOCUMENT_MISMATCH"):
        run(client, [], evidence_document=StatusEvidenceDocument("다른 원문"))
    assert client.calls == []


def test_prior_chunk_onset_is_readonly_and_current_end_uses_only_current_evidence():
    previous = draft(StatusEvidenceDocument("리안은 말을 하지 못했다.")).model_copy(update={
        "source_chunk_id": UUID("00000000-0000-0000-0000-000000000999"),
        "raw_entity_mention": "리안",
        "evidence_spans": [ExtractedEvidenceSpan(quote="리안은 말을 하지 못했다.",
                                                  start_offset=800, end_offset=815)],
    })
    before = previous.model_dump(mode="json")
    source = "리안은 또렷하게 대답했다."
    client = Client(response(draft_count=0, end_refs=["E1"]))
    result = run(client, [], source=source, known=(), prior_status_observations=(previous,))
    assert len(result) == 1
    assert result[0].value_json["active"] is False
    assert result[0].source_chunk_id == CHUNK_ID
    assert result[0].raw_entity_mention == "리안"
    assert result[0].evidence_spans[0].start_offset == 0
    assert result[0].evidence_spans[0].quote == source
    assert previous.model_dump(mode="json") == before
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["draft_observations"] == []
    assert payload["targets"][0]["prior_observation_refs"] == ["H1"]
    assert payload["prior_observations"][0]["evidence_spans"][0]["start_offset"] == 800
    assert str(previous.source_chunk_id) not in client.calls[0]["user_prompt"]
    assert "_prior_value_json" not in client.calls[0]["user_prompt"]


def test_prior_end_without_current_recurrence_needs_no_duplicate_end():
    previous = draft(StatusEvidenceDocument(SOURCE), 2, active=False)
    payload = response(draft_count=0)
    payload["target_reviews"][0]["end_state"] = "ENDED"
    assert run(Client(payload), [], prior_status_observations=(previous,)) == []


@pytest.mark.parametrize("prior_text,current_text,key", [
    ("리안은 발바닥의 감각을 잃었다.", "리안은 손으로 밧줄을 단단히 움켜쥐었다.", "status.발_감각_상실"),
    ("리안은 전날의 일을 기억하지 못했다.", "리안은 새로 들은 규칙을 이해했다.", "status.기억_공백"),
])
def test_unknown_keeps_prior_observation_without_turning_another_function_into_an_end(
    prior_text, current_text, key,
):
    prior = draft(StatusEvidenceDocument(prior_text), key=key)
    before = prior.model_dump(mode="json")
    client = Client(response(draft_count=0))

    result = run(client, [], source=current_text, prior_status_observations=(prior,))

    assert result == [] and prior.model_dump(mode="json") == before
    sent = json.loads(client.calls[0]["user_prompt"])
    assert sent["targets"][0]["fact_key"] == key
    assert sent["current_evidence"][0]["text"] == current_text
    assert sent["prior_observations"][0]["evidence_spans"][0]["quote"] == prior_text
    # The fake response verifies the contract; live semantic inference is tested separately.
    assert len(client.calls) == 3
    assert all(call == client.calls[0] for call in client.calls)


def test_grounded_recovery_of_the_injured_function_still_creates_an_end():
    source = "리안은 발목을 다쳐 걷지 못했다. 상처를 치료했다. 리안은 통증 없이 두 발로 걸었다."
    doc = StatusEvidenceDocument(source)
    onset = draft(doc, key="status.발목_부상")
    client = Client(response(end_refs=["E2", "E3"]))

    result = run(client, [onset], source=source)

    assert result[0] is onset
    assert result[1].attribute_name == onset.attribute_name
    assert result[1].value_json["active"] is False
    assert [span.quote for span in result[1].evidence_spans] == [unit.text for unit in doc.units[1:]]
    assert "치료 후 실제 신체·기능 회복으로 원인 부상" in client.calls[0]["system_prompt"]


def test_missing_active_current_recurrence_blocks_earlier_end_even_after_prior_end():
    doc = StatusEvidenceDocument(SOURCE)
    previous = draft(doc, 0, active=False)
    current = draft(doc, 2, active=None)
    with pytest.raises(LlmExtractionError):
        run(Client(response(end_refs=["E2"])), [current], prior_status_observations=(previous,))


def test_existing_end_and_later_recollection_are_preserved_when_review_adds_nothing():
    source = "리안은 다시 말할 수 있었다. 그는 예전에 목소리가 나오지 않았던 때를 떠올렸다."
    doc = StatusEvidenceDocument(source)
    candidates = [draft(doc, 0, active=False), draft(doc, 1, active=None)]
    before = [candidate.model_dump(mode="json") for candidate in candidates]
    payload = response(draft_refs=("D2",))
    payload["target_reviews"][0]["end_state"] = "ENDED"

    result = run(Client(payload), candidates, source=source)

    assert all(actual is original for actual, original in zip(result, candidates, strict=True))
    assert [candidate.model_dump(mode="json") for candidate in result] == before


def test_prior_end_and_current_recollection_are_preserved_when_review_adds_nothing():
    previous = draft(StatusEvidenceDocument(SOURCE), 2, active=False)
    source = "리안은 예전에 목소리가 나오지 않았던 때를 떠올렸다."
    recollection = draft(StatusEvidenceDocument(source), active=None)
    before = recollection.model_dump(mode="json")
    payload = response()
    payload["target_reviews"][0]["end_state"] = "ENDED"

    result = run(Client(payload), [recollection], source=source,
                 prior_status_observations=(previous,))

    assert len(result) == 1 and result[0] is recollection
    assert result[0].model_dump(mode="json") == before


def test_unresolved_subject_drafts_have_separate_targets_and_keep_original_mentions():
    source = "첫 사람은 목소리가 나오지 않았다. 첫 사람이 대답했다. 둘째 사람은 목소리가 나오지 않았다."
    doc = StatusEvidenceDocument(source)
    first = draft(doc, 0, name="미상").model_copy(update={"raw_entity_mention": "첫 사람"})
    second = draft(doc, 2, name="미상").model_copy(update={"raw_entity_mention": "둘째 사람"})
    before = [c.model_dump(mode="json") for c in (first, second)]
    payload = response(draft_count=2, target_count=2)
    payload["target_reviews"][0].update(end_state="ENDED", end_observations=[{
        "summary": "목소리를 내어 대답함", "evidence_refs": ["E2"],
    }])
    client = Client(payload)
    result = run(client, [first, second], source=source, known=())
    assert len(result) == 3 and result[0] is first and result[2] is second
    assert result[1].entity_name == "미상" and result[1].raw_entity_mention == "첫 사람"
    assert [c.model_dump(mode="json") for c in (first, second)] == before
    sent = json.loads(client.calls[0]["user_prompt"])
    assert [t["draft_refs"] for t in sent["targets"]] == [["D1"], ["D2"]]
    assert [d["raw_entity_mention"] for d in sent["draft_observations"]] == ["첫 사람", "둘째 사람"]


def test_unregistered_concrete_name_drafts_are_not_assumed_one_identity():
    doc = StatusEvidenceDocument(SOURCE)
    client = Client(response(draft_count=2, target_count=2))
    candidates = [draft(doc), draft(doc, 2)]
    assert run(client, candidates, known=()) == candidates
    assert len(json.loads(client.calls[0]["user_prompt"])["targets"]) == 2


def test_existing_end_anchor_is_preserved_when_new_end_adds_a_cause_ref():
    doc = StatusEvidenceDocument(SOURCE)
    candidates = [draft(doc), draft(doc, 2, active=False)]
    before = candidates[1].model_dump(mode="json")
    result = run(Client(response(draft_refs=("D1",), end_refs=["E2", "E3"])), candidates)
    assert len(result) == 2 and result[1] is candidates[1]
    assert result[1].model_dump(mode="json") == before


@pytest.mark.parametrize("value_type,fact_value,expected", [
    ("STRING", "음성이 나오지 않음", "후속 관찰에서 기능이 회복됨"),
    ("NUMBER", "3", 3),
    ("BOOLEAN", "true", True),
])
def test_known_only_target_uses_exact_schema_type_before_pattern(value_type, fact_value, expected):
    known = (KnownCharacter(CHARACTER_ID, "리안", (ActiveCharacterStatus("status.발화_불능", fact_value),)),)
    result = run(Client(response(draft_count=0, end_refs=["E3"])), [], known=known,
                 status_value_types={"status.*": "JSON", "status.발화_불능": value_type})
    assert result[0].value_type == value_type
    assert result[0].value_json["value"] == expected
    assert result[0].value_json["active"] is False


def test_known_only_target_uses_pattern_schema_type():
    known = (KnownCharacter(CHARACTER_ID, "리안", (ActiveCharacterStatus("status.발화_불능", None),)),)
    result = run(Client(response(draft_count=0, end_refs=["E3"])), [], known=known,
                 status_value_types={"status.*": "JSON"})
    assert result[0].value_type == "JSON"


@pytest.mark.parametrize("types,fact_value", [
    ({}, None), ({"status.다른_상태": "JSON"}, None),
    ({"status.*": "NUMBER"}, None), ({"status.*": "NUMBER"}, "세 단계"),
    ({"status.*": "NUMBER"}, "true"), ({"status.*": "BOOLEAN"}, "1"),
])
def test_known_only_missing_schema_or_invalid_scalar_fails_without_inventing_value(types, fact_value):
    known = (KnownCharacter(CHARACTER_ID, "리안", (ActiveCharacterStatus("status.발화_불능", fact_value),)),)
    with pytest.raises(LlmExtractionError):
        run(Client(response(draft_count=0, end_refs=["E3"])), [], known=known, status_value_types=types)


def test_two_end_votes_outvote_unknown_and_keep_one_complete_evidence_selection():
    first = response(end_refs=["E3"])
    second = response(end_refs=["E2", "E3"])
    second["target_reviews"][0]["end_observations"][0]["summary"] = "다른 응답의 회복 요약"
    client = Client(first, response(), second, repeat_last=False)
    onset = draft(StatusEvidenceDocument(SOURCE))

    result = run(client, [onset])

    assert result[0] is onset and len(result) == 2
    assert result[1].attribute_value == first["target_reviews"][0]["end_observations"][0]["summary"]
    assert [s.quote for s in result[1].evidence_spans] == [StatusEvidenceDocument(SOURCE).units[2].text]
    assert len(client.calls) == 3 and client.calls[0] == client.calls[1] == client.calls[2]


def test_one_incorrect_end_vote_cannot_supply_evidence_to_non_end_majority():
    onset = draft(StatusEvidenceDocument(SOURCE))
    continues = response()
    continues["target_reviews"][0]["end_state"] = "CONTINUES"
    client = Client(response(end_refs=["E2"]), response(), continues, repeat_last=False)

    assert run(client, [onset]) == [onset]
    assert len(client.calls) == 3


def test_ended_label_majority_does_not_add_a_new_end_proposed_by_only_one_vote():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [draft(doc, 0, active=False), draft(doc, 1, active=None)]
    new_end = response(draft_refs=("D2",), end_refs=["E3"])
    preserve = response(draft_refs=("D2",))
    preserve["target_reviews"][0]["end_state"] = "ENDED"
    client = Client(new_end, preserve, response(draft_refs=("D2",)), repeat_last=False)

    result = run(client, originals)

    assert len(result) == 2
    assert all(row is original for row, original in zip(result, originals, strict=True))
    assert len(client.calls) == 3


def test_new_end_majority_selects_first_nonempty_proposal_after_an_empty_ended_vote():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [draft(doc, 0, active=False), draft(doc, 1, active=None)]
    preserve = response(draft_refs=("D2",))
    preserve["target_reviews"][0]["end_state"] = "ENDED"
    first_new = response(draft_refs=("D2",), end_refs=["E3"])
    second_new = response(draft_refs=("D2",), end_refs=["E1", "E3"])
    second_new["target_reviews"][0]["end_observations"][0]["summary"] = "후행 표의 별도 요약"
    client = Client(preserve, first_new, second_new, repeat_last=False)

    result = run(client, originals)

    assert len(result) == 3 and result[:2] == originals
    assert result[-1].attribute_value == first_new["target_reviews"][0]["end_observations"][0]["summary"]
    assert [s.quote for s in result[-1].evidence_spans] == [doc.units[2].text]
    assert len(client.calls) == 3


def test_duplicate_only_end_proposal_does_not_count_toward_new_end_majority():
    original = draft(StatusEvidenceDocument(SOURCE), 1, active=False)
    client = Client(response(draft_count=0, end_refs=["E3"]), response(draft_count=0, end_refs=["E2"]), response(draft_count=0),
                    repeat_last=False)

    result = run(client, [original])

    assert len(result) == 1 and result[0] is original
    assert len(client.calls) == 3


def test_two_actual_new_end_votes_outvote_first_duplicate_only_ended_vote():
    original = draft(StatusEvidenceDocument(SOURCE), 1, active=False)
    client = Client(response(draft_count=0, end_refs=["E2"]), response(draft_count=0, end_refs=["E3"]),
                    response(draft_count=0, end_refs=["E1", "E3"]), repeat_last=False)

    result = run(client, [original])

    assert len(result) == 2 and result[0] is original
    assert [span.quote for span in result[1].evidence_spans] == [StatusEvidenceDocument(SOURCE).units[2].text]


def test_mixed_duplicate_and_new_proposal_counts_once_and_preserves_actual_representative():
    original = draft(StatusEvidenceDocument(SOURCE), 1, active=False)
    mixed = response(draft_count=0, end_refs=["E2"])
    new_end = {"summary": "실제 새 종료 관찰", "evidence_refs": ["E3"]}
    mixed["target_reviews"][0]["end_observations"].append(new_end)
    before = deepcopy(mixed)
    client = Client(mixed, response(draft_count=0, end_refs=["E1", "E3"]), response(draft_count=0), repeat_last=False)

    result = run(client, [original])

    assert mixed == before
    assert len(result) == 2 and result[0] is original
    assert result[1].attribute_value == new_end["summary"]
    assert [span.quote for span in result[1].evidence_spans] == [StatusEvidenceDocument(SOURCE).units[2].text]


@pytest.mark.parametrize("delete_votes,expected_count", [(1, 1), (2, 0)])
def test_process_deletion_requires_two_votes(delete_votes, expected_count):
    keep = response()
    remove = response()
    remove["draft_reviews"][0].update(classification="ONE_OFF_TREATMENT_PROCESS", evidence_refs=["E2"])
    votes = [keep] * (3 - delete_votes) + [remove] * delete_votes
    original = draft(StatusEvidenceDocument(SOURCE), 1, key="status.처치_효과", active=None)
    before = original.model_dump(mode="json")
    client = Client(*votes, repeat_last=False)

    assert len(run(client, [original])) == expected_count
    assert original.model_dump(mode="json") == before
    assert len(client.calls) == 3


def test_treatment_only_enum_contract_preserves_actual_environment_constraint_observation():
    source = "방이 어두워져 리안은 글자를 볼 수 없었다. 리안은 손으로 책상 가장자리를 더듬었다."
    original = draft(StatusEvidenceDocument(source), key="status.시야_제약")
    before = original.model_dump(mode="json")
    client = Client(response())

    result = run(client, [original], source=source)

    assert len(result) == 1 and result[0] is original
    assert result[0].model_dump(mode="json") == before
    classification_schema = client.calls[0]["response_schema"].schema["$defs"]["_DraftReview"]["properties"]["classification"]
    assert classification_schema["enum"] == ["STATE_OBSERVATION", "ONE_OFF_TREATMENT_PROCESS"]
    assert len(client.calls) == 3


def test_legacy_ambiguous_process_enum_cannot_complete_a_partial_deletion_majority():
    current = response()
    current["draft_reviews"][0].update(classification="ONE_OFF_TREATMENT_PROCESS", evidence_refs=["E2"])
    legacy = deepcopy(current)
    legacy["draft_reviews"][0]["classification"] = "ONE_OFF_PROCESS"
    client = Client(current, current, legacy, repeat_last=False)
    original = draft(StatusEvidenceDocument(SOURCE), 1, key="status.처치_효과", active=None)
    before = original.model_dump(mode="json")

    with pytest.raises(LlmExtractionError):
        run(client, [original])

    assert original.model_dump(mode="json") == before
    assert len(client.calls) == 3


def test_legacy_enum_retry_must_return_a_valid_current_contract_before_voting():
    legacy = response()
    legacy["draft_reviews"][0].update(classification="ONE_OFF_PROCESS", evidence_refs=["E2"])
    client = Client(legacy, response(), response(), response(), repeat_last=False)
    original = draft(StatusEvidenceDocument(SOURCE))

    assert run(client, [original], max_attempts=2) == [original]

    assert len(client.calls) == 4
    assert "literal_error" in json.loads(client.calls[1]["user_prompt"])["validation_feedback"]["reason_code"]
    assert client.calls[0]["user_prompt"] == client.calls[2]["user_prompt"] == client.calls[3]["user_prompt"]


def test_two_valid_end_votes_do_not_allow_partial_fallback_when_third_vote_is_invalid():
    invalid = response(end_refs=["E99"])
    client = Client(response(end_refs=["E3"]), response(end_refs=["E3"]), invalid, repeat_last=False)
    with pytest.raises(LlmExtractionError):
        run(client, [draft(StatusEvidenceDocument(SOURCE))])
    assert len(client.calls) == 3


def test_incomplete_vote_set_propagates_provider_failure_after_two_agreeing_votes():
    error = RuntimeError("third vote unavailable")
    client = Client(response(end_refs=["E3"]), response(end_refs=["E3"]), error, repeat_last=False)
    with pytest.raises(RuntimeError) as caught:
        run(client, [draft(StatusEvidenceDocument(SOURCE))])
    assert caught.value is error and len(client.calls) == 3


def test_validation_retry_is_local_to_its_vote_and_later_votes_start_from_original_input():
    client = Client(response(end_refs=["E99"]), response(end_refs=["E3"]),
                    response(end_refs=["E3"]), response(end_refs=["E3"]), repeat_last=False)
    assert len(run(client, [draft(StatusEvidenceDocument(SOURCE))], max_attempts=2)) == 2
    assert len(client.calls) == 4
    assert "validation_feedback" in json.loads(client.calls[1]["user_prompt"])
    assert client.calls[0]["user_prompt"] == client.calls[2]["user_prompt"] == client.calls[3]["user_prompt"]


def test_combined_majority_revalidates_state_existence_without_requesting_extra_votes():
    source = "리안의 상태가 발생했다. 변화가 관찰됐다. 다시 변화가 관찰됐다. 상태가 해소됐다."
    document = StatusEvidenceDocument(source)
    originals = [draft(document, i) for i in range(3)]
    votes = []
    for retained in range(3):
        vote = response(draft_count=3, end_refs=["E4"])
        for i, row in enumerate(vote["draft_reviews"]):
            if i != retained:
                row.update(classification="ONE_OFF_TREATMENT_PROCESS", evidence_refs=[f"E{i + 1}"])
        votes.append(vote)
    client = Client(*votes, repeat_last=False)
    before = [candidate.model_dump(mode="json") for candidate in originals]
    with pytest.raises(LlmExtractionError, match="CONSENSUS_INVALID.*END_WITHOUT_STATE"):
        run(client, originals, source=source)
    assert len(client.calls) == 3
    assert [candidate.model_dump(mode="json") for candidate in originals] == before


class _VoteLedger:
    def __init__(self, reject_attempt=None):
        self.reservations = []
        self.settlements = []
        self.reject_attempt = reject_attempt

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)
        if len(self.reservations) == self.reject_attempt:
            raise AiTokenQuotaExhaustedError()

    async def settle_ai_tokens(self, **kwargs):
        self.settlements.append(kwargs)

    async def release_ai_tokens(self, **kwargs):
        raise AssertionError("Successful provider responses must settle real usage.")


def test_protected_end_refs_keep_original_ids_targets_and_payload_outside_classification():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [draft(doc, 0, active=False), draft(doc, 1),
                 draft(doc, 2, active=False, key="status.통증"),
                 draft(doc, 2, active=None, key="status.통증")]
    before = [row.model_dump(mode="json") for row in originals]
    client = Client(response(draft_refs=("D2", "D4"), target_count=2))

    result = run(client, originals)

    assert [row.model_dump(mode="json") for row in result] == before
    assert all(actual is original for actual, original in zip(result, originals, strict=True))
    payload = json.loads(client.calls[0]["user_prompt"])
    assert [row["draft_ref"] for row in payload["draft_observations"]] == ["D2", "D4"]
    assert [row["draft_ref"] for row in payload["preserved_end_observations"]] == ["D1", "D3"]
    assert [row["draft_refs"] for row in payload["targets"]] == [["D1", "D2"], ["D3", "D4"]]
    for row, original in zip(payload["preserved_end_observations"], (originals[0], originals[2]), strict=True):
        assert row["value_json"] == original.value_json
        assert row["attribute_value"] == original.attribute_value
        assert row["raw_entity_mention"] == original.raw_entity_mention
    schema = client.calls[0]["response_schema"].schema
    assert schema["$defs"]["DraftRef"]["enum"] == ["D2", "D4"]
    assert schema["properties"]["draft_reviews"]["minItems"] == 2
    assert schema["properties"]["draft_reviews"]["maxItems"] == 2
    assert len(client.calls) == 3
    assert all(call == client.calls[0] for call in client.calls)


def test_all_protected_ends_still_collect_target_votes_without_any_classification():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [draft(doc, 1, active=False).model_copy(update={
        "attribute_value": "한 번의 처치가 진행되는 상태",
    }), draft(doc, 2, active=False, key="status.통증")]
    before = [row.model_dump(mode="json") for row in originals]
    vote = response(draft_count=0, target_count=2)
    for target in vote["target_reviews"]:
        target["end_state"] = "ENDED"
    client = Client(vote)

    result = run(client, originals)

    assert [row.model_dump(mode="json") for row in result] == before
    assert all(actual is original for actual, original in zip(result, originals, strict=True))
    assert len(client.calls) == 3
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["draft_observations"] == []
    assert [row["draft_ref"] for row in payload["preserved_end_observations"]] == ["D1", "D2"]
    schema = client.calls[0]["response_schema"].schema
    assert "DraftRef" not in schema["$defs"]
    assert schema["properties"]["draft_reviews"]["minItems"] == 0
    assert schema["properties"]["draft_reviews"]["maxItems"] == 0
    assert schema["properties"]["target_reviews"]["minItems"] == 2


@pytest.mark.parametrize("forbidden_ref,classification", [
    ("D2", "STATE_OBSERVATION"), ("D2", "ONE_OFF_TREATMENT_PROCESS"),
    ("D99", "STATE_OBSERVATION"),
])
def test_readonly_or_unknown_draft_classification_is_rejected_without_normalization(
    forbidden_ref, classification,
):
    doc = StatusEvidenceDocument(SOURCE)
    originals = [draft(doc), draft(doc, 2, active=False)]
    before = [row.model_dump(mode="json") for row in originals]
    invalid = response()
    invalid["draft_reviews"].append({"draft_ref": forbidden_ref, "classification": classification,
                                    "reason": "반환 대상 밖의 분류", "evidence_refs": ["E3"]})
    client = Client(invalid)

    with pytest.raises(LlmExtractionError):
        run(client, originals, max_attempts=3)

    assert len(client.calls) == 3  # The first vote exhausts retries; no valid votes were collected.
    assert [row.model_dump(mode="json") for row in originals] == before
    assert json.loads(client.calls[-1]["user_prompt"])["validation_feedback"]["reason_code"] == (
        "STATUS_REVIEW_DRAFT_COVERAGE"
    )


@pytest.mark.parametrize("refs", [("D1",), ("D1", "D2"), ("D1", "D1", "D3")])
def test_mixed_draft_coverage_requires_all_and_only_editable_original_refs(refs):
    doc = StatusEvidenceDocument(SOURCE)
    originals = [draft(doc), draft(doc, 1, active=False), draft(doc, 2)]
    client = Client(response(draft_refs=refs))
    with pytest.raises(LlmExtractionError):
        run(client, originals)


def test_protected_end_cannot_return_after_two_valid_votes_if_third_vote_is_invalid():
    original = draft(StatusEvidenceDocument(SOURCE), 1, active=False)
    before = original.model_dump(mode="json")
    valid = response(draft_count=0, end_refs=["E3"])
    client = Client(valid, valid, response(), repeat_last=False)

    with pytest.raises(LlmExtractionError):
        run(client, [original])

    assert len(client.calls) == 3
    assert original.model_dump(mode="json") == before


class _UsageClient(Client):
    async def create_text_response(self, **kwargs):
        item = await super().create_text_response(**kwargs)
        return LlmTextResponse(text=item.text, input_token_count=101,
                               cached_input_token_count=19, output_token_count=23)


@pytest.mark.parametrize("reject_third", [False, True])
def test_each_independent_vote_reserves_and_settles_with_actual_lease(reject_third):
    delegate = _UsageClient(response(end_refs=["E3"]))
    ledger = _VoteLedger(reject_attempt=3 if reject_third else None)
    lease_token = UUID(int=777)
    metered = MeteredTextGenerationClient(
        delegate=delegate, ledger=ledger, analysis_job_id=UUID(int=888),
        purpose="SETTING_EXTRACTION", default_model="gpt-5.6-terra", lease_token=lease_token,
        max_retries=0,
    )
    if reject_third:
        with pytest.raises(AiTokenQuotaExhaustedError):
            run(metered, [draft(StatusEvidenceDocument(SOURCE))])
    else:
        assert len(run(metered, [draft(StatusEvidenceDocument(SOURCE))])) == 2
    settled = 2 if reject_third else 3
    assert len(ledger.reservations) == 3
    assert len(ledger.settlements) == len(delegate.calls) == settled
    assert len({row["request_id"] for row in ledger.reservations}) == 3
    assert [row["attempt"] for row in ledger.reservations] == [1, 2, 3]
    assert all(row["lease_token"] == lease_token for row in ledger.reservations)
    assert all(row["purpose"] == "SETTING_EXTRACTION" for row in ledger.reservations)
    assert all((row["input_tokens"], row["cached_input_tokens"], row["output_tokens"])
               == (101, 19, 23) for row in ledger.settlements)
    assert metered.usage_snapshot().provider_request_count == settled


def with_kind(candidate, kind):
    candidate._status_observation_kind = kind
    return candidate


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_later_noncurrent_observation_does_not_block_actual_earlier_end(kind):
    source = "리안은 다쳤다. 치료를 마치고 정상적으로 달렸다. 예전의 상처 이야기를 했다."
    document = StatusEvidenceDocument(source)
    onset = with_kind(draft(document), "START")
    noncurrent = with_kind(draft(document, 2, active=None), kind)
    before = [row.model_dump(mode="json") for row in (onset, noncurrent)]
    client = Client(response(draft_refs=("D1",), end_refs=["E2"]))

    result = run(client, [onset, noncurrent], source=source)

    assert result[0] is onset and result[2] is noncurrent
    assert result[1]._status_observation_kind == "END"
    assert result[1].value_json["active"] is False
    assert result[1].evidence_spans[0].start_offset == document.units[1].start_offset
    assert [row.model_dump(mode="json") for row in (onset, noncurrent)] == before
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["targets"][0]["draft_refs"] == ["D1"]
    assert [row["draft_ref"] for row in payload["draft_observations"]] == ["D1"]
    assert payload["preserved_end_observations"] == []
    preserved = payload["preserved_noncurrent_observations"]
    assert len(preserved) == 1
    assert (preserved[0]["draft_ref"], preserved[0]["target_ref"],
            preserved[0]["observation_kind"], preserved[0]["evidence_refs"]) == ("D2", None, kind, ["E3"])
    assert preserved[0]["value_json"] == noncurrent.value_json
    assert preserved[0]["attribute_value"] == noncurrent.attribute_value
    assert len(client.calls) == 3


def test_all_noncurrent_observations_preserve_originals_without_model_call():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [with_kind(draft(doc, 0, active=None), "PAST"),
                 with_kind(draft(doc, 2, active=None), "HYPOTHETICAL")]
    before = [row.model_dump(mode="json") for row in originals]
    client = Client()

    result = run(client, originals, known=())

    assert client.calls == []
    assert all(actual is original for actual, original in zip(result, originals, strict=True))
    assert [row.model_dump(mode="json") for row in result] == before


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_only_noncurrent_draft_leaves_known_active_target_reviewable(kind):
    doc = StatusEvidenceDocument(SOURCE)
    original = with_kind(draft(doc, 2, active=None), kind)
    known = (KnownCharacter(CHARACTER_ID, "리안", (ActiveCharacterStatus("status.발화_불능", None),)),)
    client = Client(response(draft_count=0, end_refs=["E2"]))

    result = run(client, [original], known=known)

    assert result[-1] is original
    assert result[0]._status_observation_kind == "END"
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["targets"][0]["active_at_episode_start"] is True
    assert payload["targets"][0]["draft_refs"] == []
    assert payload["draft_observations"] == []
    assert client.calls[0]["response_schema"].schema["properties"]["draft_reviews"]["maxItems"] == 0
    assert len(client.calls) == 3


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_prior_noncurrent_observation_does_not_create_target_or_trust_new_subject(kind):
    doc = StatusEvidenceDocument(SOURCE)
    prior = with_kind(draft(doc, active=None).model_copy(update={"source_chunk_id": UUID(int=999)}), kind)
    before = prior.model_dump(mode="json")
    empty_client = Client()
    assert run(empty_client, [], known=(), prior_status_observations=(prior,)) == []
    assert empty_client.calls == []

    # An old mention cannot make two otherwise unverified current subjects share T1.
    originals = [with_kind(draft(doc), "START"), with_kind(draft(doc, 1), "START")]
    client = Client(response(draft_count=2, target_count=2))
    assert run(client, originals, known=(), prior_status_observations=(prior,)) == originals
    payload = json.loads(client.calls[0]["user_prompt"])
    assert [row["draft_refs"] for row in payload["targets"]] == [["D1"], ["D2"]]
    assert payload["prior_observations"] == []
    assert prior.model_dump(mode="json") == before


def test_mixed_temporal_kinds_keep_original_ids_and_readonly_end_subject_isolation():
    source = "그는 전에 다쳤다. 그는 지금 다쳤다. 다른 이는 나았다. 또 다칠 수도 있었다."
    doc = StatusEvidenceDocument(source)
    originals = [with_kind(draft(doc, 0, active=None, name="미상"), "PAST"),
                 with_kind(draft(doc, 1, name="미상"), "START"),
                 with_kind(draft(doc, 2, active=False, name="미상"), "END"),
                 with_kind(draft(doc, 3, active=None, name="미상"), "HYPOTHETICAL")]
    before = [row.model_dump(mode="json") for row in originals]
    client = Client(response(draft_refs=("D2",), target_count=2))

    result = run(client, originals, known=(), source=source)

    assert [row.model_dump(mode="json") for row in result] == before
    assert all(actual is original for actual, original in zip(result, originals, strict=True))
    payload = json.loads(client.calls[0]["user_prompt"])
    assert [row["draft_ref"] for row in payload["draft_observations"]] == ["D2"]
    assert [row["draft_ref"] for row in payload["preserved_end_observations"]] == ["D3"]
    assert [row["draft_ref"] for row in payload["preserved_noncurrent_observations"]] == ["D1", "D4"]
    assert [row["draft_refs"] for row in payload["targets"]] == [["D2"], ["D3"]]
    schema = client.calls[0]["response_schema"].schema
    assert schema["$defs"]["DraftRef"]["enum"] == ["D2"]
    assert schema["properties"]["draft_reviews"]["minItems"] == 1
    assert schema["properties"]["draft_reviews"]["maxItems"] == 1


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_noncurrent_classification_is_rejected_after_two_valid_votes_without_partial_result(kind):
    doc = StatusEvidenceDocument(SOURCE)
    originals = [with_kind(draft(doc), "START"), with_kind(draft(doc, 2, active=None), kind)]
    before = [row.model_dump(mode="json") for row in originals]
    valid = response(draft_refs=("D1",), end_refs=["E2"])
    invalid = deepcopy(valid)
    invalid["draft_reviews"].append({"draft_ref": "D2", "classification": "ONE_OFF_TREATMENT_PROCESS",
                                    "reason": "읽기 전용 원형을 바꾸려는 응답", "evidence_refs": ["E3"]})
    client = Client(valid, valid, invalid, repeat_last=False)

    with pytest.raises(LlmExtractionError):
        run(client, originals)

    assert len(client.calls) == 3
    assert [row.model_dump(mode="json") for row in originals] == before


@pytest.mark.parametrize("kind", ["START", "CONTINUE", "CHANGE", "END", "PAST", "HYPOTHETICAL"])
def test_observation_kind_is_private_and_survives_subject_and_offset_copies(kind):
    original = draft(StatusEvidenceDocument(SOURCE))
    assert original._status_observation_kind is None
    wire = original.model_dump(mode="json")
    original._status_observation_kind = kind

    copied = original.model_copy(update={"entity_name": "리안 미르"})
    deep_copied = copied.model_copy(deep=True, update={"evidence_spans": list(copied.evidence_spans)})

    assert original.model_dump(mode="json") == wire
    assert copied._status_observation_kind == deep_copied._status_observation_kind == kind
    assert "_status_observation_kind" not in original.model_json_schema()["properties"]
    assert "_status_observation_kind" not in copied.model_dump(mode="json")
    assert original.entity_name == "리안"


def with_group(candidate, index):
    candidate._status_observation_group = (CHUNK_ID, index)
    return candidate


def test_producer_group_keeps_start_and_protected_end_in_one_target_without_new_end():
    source = "그는 앞선 일을 떠올리지 못했다. 기억이 비어 있었다. 기억을 되찾았다. 앞선 일을 회상했다."
    doc = StatusEvidenceDocument(source)
    onset = with_group(with_kind(draft(doc, 0), "START"), 0)
    onset.evidence_spans.append(draft(doc, 1).evidence_spans[0])
    end = with_group(with_kind(draft(doc, 2, active=False), "END"), 0)
    end.evidence_spans.append(draft(doc, 3).evidence_spans[0])
    originals = [onset, end]
    before = [c.model_dump(mode="json") for c in originals]
    vote = response(draft_refs=())
    vote["target_reviews"][0]["end_state"] = "ENDED"
    client = Client(vote)

    result = run(client, originals, source=source, known=())

    assert result == originals and all(a is b for a, b in zip(result, originals, strict=True))
    assert [c.model_dump(mode="json") for c in originals] == before
    assert len(client.calls) == 3
    payload = json.loads(client.calls[0]["user_prompt"])
    assert [t["draft_refs"] for t in payload["targets"]] == [["D1", "D2"]]
    assert payload["draft_observations"] == []
    assert [d["draft_ref"] for d in payload["preserved_state_observations"]] == ["D1"]
    assert [d["draft_ref"] for d in payload["preserved_end_observations"]] == ["D2"]
    assert client.calls[0]["response_schema"].schema["$defs"]["TargetRef"]["enum"] == ["T1"]
    assert str(CHUNK_ID) not in client.calls[0]["user_prompt"]
    assert "producer_group" not in client.calls[0]["user_prompt"]


def test_separate_producer_groups_and_legacy_drafts_with_same_name_remain_isolated():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [with_group(draft(doc), 0), with_group(draft(doc, 1), 1), draft(doc, 2)]
    client = Client(response(draft_count=3, target_count=3))

    assert run(client, originals, known=()) == originals

    payload = json.loads(client.calls[0]["user_prompt"])
    assert [t["draft_refs"] for t in payload["targets"]] == [["D1"], ["D2"], ["D3"]]


def test_known_unique_subject_takes_precedence_over_different_producer_groups():
    doc = StatusEvidenceDocument(SOURCE)
    originals = [with_group(draft(doc), 0), with_group(draft(doc, 2, active=False), 1)]
    vote = response(draft_refs=("D1",))
    vote["target_reviews"][0]["end_state"] = "ENDED"
    client = Client(vote)

    assert run(client, originals) == originals

    payload = json.loads(client.calls[0]["user_prompt"])
    assert [t["draft_refs"] for t in payload["targets"]] == [["D1", "D2"]]


@pytest.mark.parametrize("group,code", [
    ((UUID(int=999), 0), "STATUS_REVIEW_PRODUCER_GROUP_SOURCE_MISMATCH"),
    ((str(CHUNK_ID), 0), "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
    ((CHUNK_ID, -1), "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
    ((CHUNK_ID, True), "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
    ((CHUNK_ID, 1.0), "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
    ((CHUNK_ID, "0"), "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
    ([CHUNK_ID, 0], "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
    ((CHUNK_ID,), "STATUS_REVIEW_INVALID_PRODUCER_GROUP"),
])
def test_invalid_private_producer_group_fails_before_any_provider_call(group, code):
    candidate = draft(StatusEvidenceDocument(SOURCE))
    candidate._status_observation_group = group
    before = candidate.model_dump(mode="json")
    client = Client()

    with pytest.raises(ValueError) as exc:
        run(client, [candidate], known=())

    assert str(exc.value) == code
    assert client.calls == []
    assert candidate.model_dump(mode="json") == before


@pytest.mark.parametrize("changed", [
    {"entity_name": "다른 사람"},
    {"raw_entity_mention": "다른 주체 지칭"},
    {"attribute_name": "status.다른_상태"},
    {"value_type": "NUMBER", "attribute_value": "1", "value_json": {"value": 1, "active": True}},
])
def test_same_producer_group_requires_identical_subject_raw_key_and_type(changed):
    doc = StatusEvidenceDocument(SOURCE)
    originals = [with_group(draft(doc), 0), with_group(draft(doc, 1).model_copy(update=changed), 0)]
    before = [c.model_dump(mode="json") for c in originals]
    client = Client()

    with pytest.raises(ValueError) as exc:
        run(client, originals)

    assert str(exc.value) == "STATUS_REVIEW_PRODUCER_GROUP_IDENTITY_CONFLICT"
    assert client.calls == []
    assert [c.model_dump(mode="json") for c in originals] == before


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_noncurrent_member_of_producer_group_stays_readonly_outside_current_target(kind):
    doc = StatusEvidenceDocument(SOURCE)
    onset = with_group(with_kind(draft(doc), "START"), 0)
    noncurrent = with_group(with_kind(draft(doc, 2, active=None), kind), 0)
    client = Client(response(draft_refs=("D1",), end_refs=["E2"]))

    result = run(client, [onset, noncurrent], known=())

    assert result[0] is onset and result[2] is noncurrent
    assert result[1]._status_observation_group == (CHUNK_ID, 0)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["targets"][0]["draft_refs"] == ["D1"]
    assert payload["preserved_noncurrent_observations"][0]["target_ref"] is None
    assert payload["preserved_noncurrent_observations"][0]["draft_ref"] == "D2"


def test_noncurrent_private_group_is_validated_even_without_any_current_target():
    candidate = with_kind(draft(StatusEvidenceDocument(SOURCE), active=None), "PAST")
    candidate._status_observation_group = (UUID(int=999), 0)
    client = Client()
    with pytest.raises(ValueError, match="STATUS_REVIEW_PRODUCER_GROUP_SOURCE_MISMATCH"):
        run(client, [candidate], known=())
    assert client.calls == []


def test_recurrence_in_same_producer_group_still_requires_end_after_latest_start():
    source = "그는 다쳤다. 나았다. 다시 다쳤다. 또 치료하고 나았다."
    doc = StatusEvidenceDocument(source)
    originals = [with_group(with_kind(draft(doc, 0), "START"), 0),
                 with_group(with_kind(draft(doc, 1, active=False), "END"), 0),
                 with_group(with_kind(draft(doc, 2), "START"), 0)]
    with pytest.raises(LlmExtractionError):
        run(Client(response(draft_refs=("D3",), end_refs=["E2"])), originals, source=source, known=())
    result = run(Client(response(draft_refs=("D3",), end_refs=["E4"])), originals, source=source, known=())
    assert result[:3] == originals
    assert result[-1]._status_observation_group == (CHUNK_ID, 0)
    assert result[-1].evidence_spans[0].start_offset == doc.units[3].start_offset


def test_producer_group_is_private_and_model_copy_preserves_it():
    original = draft(StatusEvidenceDocument(SOURCE))
    assert original._status_observation_group is None
    wire = original.model_dump(mode="json")
    original._status_observation_group = (CHUNK_ID, 17)
    renamed = original.model_copy(update={"entity_name": "리안 미르"})
    copied = renamed.model_copy(deep=True)

    assert original.model_dump(mode="json") == wire
    assert renamed._status_observation_group == copied._status_observation_group == (CHUNK_ID, 17)
    assert "_status_observation_group" not in original.model_json_schema()["properties"]
    assert "_status_observation_group" not in copied.model_dump(mode="json")


@pytest.mark.parametrize("kind", [None, "START", "CONTINUE", "CHANGE"])
def test_established_status_observation_is_readonly_without_changing_original(kind):
    doc = StatusEvidenceDocument(SOURCE)
    candidate = with_kind(draft(doc), kind)
    known = (KnownCharacter(CHARACTER_ID, "리안", (
        ActiveCharacterStatus(candidate.attribute_name, "현재 제약이 있음"),
    )),)
    original = candidate.model_dump(mode="json")
    client = Client(response(draft_refs=()))

    result = run(client, [candidate], known=known)

    assert result == [candidate] and result[0] is candidate
    assert candidate.model_dump(mode="json") == original
    assert candidate._status_observation_kind == kind
    assert len(client.calls) == 3 and all(call == client.calls[0] for call in client.calls)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["draft_observations"] == []
    assert payload["preserved_end_observations"] == []
    assert payload["preserved_noncurrent_observations"] == []
    assert [row["draft_ref"] for row in payload["preserved_state_observations"]] == ["D1"]
    assert payload["targets"][0]["draft_refs"] == ["D1"]
    schema = client.calls[0]["response_schema"].schema
    assert "DraftRef" not in schema["$defs"]
    assert schema["properties"]["draft_reviews"]["minItems"] == 0
    assert schema["properties"]["draft_reviews"]["maxItems"] == 0


@pytest.mark.parametrize("prior_active", [True, False, None])
def test_only_active_prior_protects_a_current_observation(prior_active):
    previous = draft(StatusEvidenceDocument("그는 앞서 상태를 확인했다."), active=prior_active)
    previous.source_chunk_id = UUID(int=800)
    original = with_kind(draft(StatusEvidenceDocument(SOURCE)), "CHANGE")
    client = Client(response(draft_refs=() if prior_active is True else ("D1",)))

    result = run(client, [original], known=(), prior_status_observations=(previous,))

    assert result == [original]
    payload = json.loads(client.calls[0]["user_prompt"])
    assert bool(payload["preserved_state_observations"]) is (prior_active is True)
    assert payload["targets"][0]["prior_observation_refs"] == ["H1"]
    assert payload["prior_observations"][0]["value_json"] == previous.value_json


def test_last_prior_end_does_not_grant_active_prior_protection():
    doc = StatusEvidenceDocument(SOURCE)
    prior_onset, prior_end = draft(doc), draft(doc, 2, active=False)
    prior_onset.source_chunk_id = prior_end.source_chunk_id = UUID(int=801)
    candidate = with_kind(draft(doc), "CHANGE")
    client = Client(response())

    assert run(client, [candidate], known=(),
               prior_status_observations=(prior_onset, prior_end)) == [candidate]

    assert json.loads(client.calls[0]["user_prompt"])["preserved_state_observations"] == []


def test_same_state_partial_recovery_and_end_survive_while_new_process_is_excluded():
    source = "어깨 상처의 통증이 조금 줄었다. 상처와 통증이 모두 사라졌다. 치료약의 효과가 나타났다. 전에 앓았던 열병을 회상했다."
    doc = StatusEvidenceDocument(source)
    improvement = with_group(with_kind(draft(doc, key="status.어깨_상처"), "CHANGE"), 0)
    improvement.attribute_value = "어깨 상처의 통증이 조금 줄어듦"
    end = with_group(with_kind(draft(doc, 1, key="status.어깨_상처", active=False), "END"), 0)
    process = with_group(with_kind(draft(doc, 2, key="status.약의_효과"), "START"), 1)
    past = with_group(with_kind(draft(doc, 3, key="status.열병", active=None), "PAST"), 2)
    originals = [improvement, end, process, past]
    wires = [candidate.model_dump(mode="json") for candidate in originals]
    vote = response(draft_refs=("D3",), target_count=2)
    vote["draft_reviews"][0].update(classification="ONE_OFF_TREATMENT_PROCESS", evidence_refs=["E3"])
    vote["target_reviews"][0]["end_state"] = "ENDED"
    client = Client(vote)

    result = run(client, originals, source=source, known=())

    assert result == [improvement, end, past]
    assert all(candidate is originals[index] for candidate, index in zip(result, (0, 1, 3), strict=True))
    assert [candidate.model_dump(mode="json") for candidate in originals] == wires
    assert [candidate._status_observation_kind for candidate in result] == ["CHANGE", "END", "PAST"]
    assert [candidate._status_observation_group for candidate in result] == [
        (CHUNK_ID, 0), (CHUNK_ID, 0), (CHUNK_ID, 2),
    ]
    payload = json.loads(client.calls[0]["user_prompt"])
    for name, refs in (("draft_observations", ["D3"]),
                       ("preserved_state_observations", ["D1"]),
                       ("preserved_end_observations", ["D2"]),
                       ("preserved_noncurrent_observations", ["D4"])):
        assert [row["draft_ref"] for row in payload[name]] == refs
    assert [target["draft_refs"] for target in payload["targets"]] == [["D1", "D2"], ["D3"]]
    schema = client.calls[0]["response_schema"].schema
    assert schema["$defs"]["DraftRef"]["enum"] == ["D3"]
    assert schema["properties"]["draft_reviews"]["minItems"] == 1
    assert schema["properties"]["draft_reviews"]["maxItems"] == 1
    assert len(client.calls) == 3


@pytest.mark.parametrize("classification", ["STATE_OBSERVATION", "ONE_OFF_TREATMENT_PROCESS"])
def test_readonly_state_cannot_be_reclassified_after_two_valid_votes(classification):
    candidate = draft(StatusEvidenceDocument(SOURCE))
    known = (KnownCharacter(CHARACTER_ID, "리안", (
        ActiveCharacterStatus(candidate.attribute_name, "현재 상태"),
    )),)
    valid = response(draft_refs=())
    invalid = response()
    invalid["draft_reviews"][0].update(classification=classification, evidence_refs=["E1"])
    client = Client(valid, valid, invalid, repeat_last=False)
    before = candidate.model_dump(mode="json")

    with pytest.raises(LlmExtractionError):
        run(client, [candidate], known=known)

    assert len(client.calls) == 3
    assert candidate.model_dump(mode="json") == before


def test_readonly_state_classification_retry_keeps_exact_readonly_payload():
    candidate = draft(StatusEvidenceDocument(SOURCE))
    known = (KnownCharacter(CHARACTER_ID, "리안", (
        ActiveCharacterStatus(candidate.attribute_name, "현재 상태"),
    )),)
    client = Client(response(), response(draft_refs=()))

    assert run(client, [candidate], known=known, max_attempts=2) == [candidate]

    assert len(client.calls) == 4
    original = json.loads(client.calls[0]["user_prompt"])
    retry = json.loads(client.calls[1]["user_prompt"])
    feedback = retry.pop("validation_feedback")
    assert feedback["reason_code"] == "STATUS_REVIEW_DRAFT_COVERAGE"
    assert retry == original
    assert client.calls[2] == client.calls[3] == client.calls[0]


def test_another_producer_group_end_does_not_protect_unresolved_same_name():
    doc = StatusEvidenceDocument(SOURCE)
    candidate = with_group(with_kind(draft(doc), "CHANGE"), 0)
    other_end = with_group(with_kind(draft(doc, 2, active=False), "END"), 1)
    client = Client(response(draft_refs=("D1",), target_count=2))

    assert run(client, [candidate, other_end], known=()) == [candidate, other_end]

    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["preserved_state_observations"] == []
    assert [target["draft_refs"] for target in payload["targets"]] == [["D1"], ["D2"]]


def test_ambiguous_known_name_does_not_inherit_either_active_target():
    candidate = with_group(with_kind(draft(StatusEvidenceDocument(SOURCE)), "CHANGE"), 0)
    statuses = (ActiveCharacterStatus(candidate.attribute_name, "현재 상태"),)
    known = (KnownCharacter(CHARACTER_ID, "리안", statuses),
             KnownCharacter(UUID(int=900), "리안", statuses))
    client = Client(response(draft_refs=("D1",), target_count=3))

    assert run(client, [candidate], known=known) == [candidate]

    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["preserved_state_observations"] == []
    assert [target["draft_refs"] for target in payload["targets"]] == [[], [], ["D1"]]


def test_readonly_active_state_still_sets_chronology_for_a_new_end():
    candidate = draft(StatusEvidenceDocument(SOURCE), 1)
    known = (KnownCharacter(CHARACTER_ID, "리안", (
        ActiveCharacterStatus(candidate.attribute_name, "현재 상태"),
    )),)
    with pytest.raises(LlmExtractionError):
        run(Client(response(draft_refs=(), end_refs=["E1"])), [candidate], known=known)
    result = run(Client(response(draft_refs=(), end_refs=["E3"])), [candidate], known=known)
    assert len(result) == 2 and result[0] is candidate
    assert result[1].value_json["active"] is False
