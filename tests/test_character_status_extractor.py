import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.exceptions import LlmExtractionError
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.usage.metering import MeteredTextGenerationClient

CHUNK_ID = UUID(int=1)
CHARACTER_ID = UUID(int=2)
LEASE_TOKEN = UUID(int=3)
STATUS_SCHEMA = CharacterSettingSchemaHint("status", "상태", "status.*", (), "JSON")
LEVEL_SCHEMA = CharacterSettingSchemaHint("level", "레벨", None, (), "NUMBER")


def _status(active=True, *, quote="리안의 발이 다쳤다.", value="발 부상") -> dict:
    return {
        "candidate_kind": "SETTING", "entity_type": "CHARACTER", "entity_name": "리안",
        "raw_entity_mention": "리안", "attribute_name": "status.발_부상",
        "attribute_value": value, "value_type": "JSON",
        "value_json": {
            "extra_json": json.dumps({"name": "발 부상", "active": active}, ensure_ascii=False),
        },
        "evidence_spans": [{"quote": quote, "start_offset": None, "end_offset": None}],
        "confidence": 0.9,
    }


def _status_refs(active=True, *, refs=("E1",), value="발 부상") -> dict:
    extra = {"name": "발 부상"}
    if isinstance(active, str):
        extra["active"] = active  # Deliberately invalid provider-controlled active.
    return {
        "entity_name": "리안", "raw_entity_mention": "리안",
        "attribute_name": "status.발_부상", "value_type": "JSON",
        "origin": "STARTS_IN_CURRENT_CHUNK" if active is True else "PREEXISTING_OR_UNSPECIFIED",
        "observations": [{
            "kind": "START" if active is True else "END" if active is False or isinstance(active, str) else "CONTINUE",
            "attribute_value": value,
            "value_json": extra,
            "evidence_refs": list(refs), "confidence": 0.9,
        }],
    }


def _timeline(*states):
    return {**states[0], "observations": [observation for state in states for observation in state["observations"]]}


def _level() -> dict:
    return {
        **_status(), "attribute_name": "level", "attribute_value": "12", "value_type": "NUMBER",
        "value_json": {"value": 12, "extra_json": None},
    }


def _discovery() -> dict:
    return {
        **_status(), "candidate_kind": "CHARACTER_DISCOVERY", "attribute_name": None,
        "attribute_value": None, "value_type": None, "value_json": None,
    }


class SequenceClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        if self.responses:
            response = self.responses.pop(0)
        else:
            assert kwargs["prompt_cache_key"].startswith("status-observation-review:v9")
            payload = json.loads(kwargs["user_prompt"])
            response = {
                "draft_reviews": [
                    {"draft_ref": item["draft_ref"], "classification": "STATE_OBSERVATION",
                     "reason": "관찰을 그대로 보존한다.", "evidence_refs": []}
                    for item in payload["draft_observations"]
                ],
                "target_reviews": [
                    {"target_ref": item["target_ref"], "end_state": "UNKNOWN",
                     "reason": "추가 종료를 판단하지 못한다.", "end_observations": []}
                    for item in payload["targets"]
                ],
            }
        if isinstance(response, Exception):
            raise response
        root_key = "states" if kwargs["prompt_cache_key"].startswith("status-extraction:") else "candidates"
        text = response if isinstance(response, str) else json.dumps(
            response if isinstance(response, dict) else {root_key: response}
        )
        return LlmTextResponse(text=text, input_token_count=100, output_token_count=20)


def _extract(client, *, schema_hints=(LEVEL_SCHEMA, STATUS_SCHEMA), max_attempts=1,
             chunk_text="리안의 발이 다쳤다. 약을 먹었다. 한참 뒤 리안은 통증 없이 걸었다.", **kwargs):
    extractor = CharacterSettingExtractor(llm_client=client, max_attempts=max_attempts)
    return asyncio.run(extractor.extract_from_chunk(
        source_chunk_id=CHUNK_ID,
        chunk_text=chunk_text,
        schema_hints=schema_hints,
        **kwargs,
    ))


def test_status_review_replaces_drafts_and_preserves_nonstatus_and_discovery() -> None:
    recovered = _status_refs(False, refs=("E1", "E3"), value="발 기능 회복")
    client = SequenceClient([[_level(), _status(), _discovery()], [recovered]])
    context = {"previous_episode": "전에 발이 다쳤다.", "next_chunk": None}
    result = _extract(
        client,
        known_characters=(KnownCharacter(
            character_id=CHARACTER_ID, name="리안",
            active_statuses=(ActiveCharacterStatus("status.발_부상", "발 부상"),),
        ),),
        narrative_context=context,
    )

    assert len(client.requests) == 5
    assert [candidate.attribute_name for candidate in result.candidates] == [
        "level", None, "status.발_부상",
    ]
    assert result.candidates[0].value_json == {"value": 12}
    assert result.candidates[1].candidate_kind == "CHARACTER_DISCOVERY"
    assert result.candidates[2].value_json["active"] is False
    assert result.candidates[2].evidence_spans[-1].quote == "한참 뒤 리안은 통증 없이 걸었다."
    assert all(candidate.source_chunk_id == CHUNK_ID for candidate in result.candidates)
    for request in client.requests[:2]:
        assert "리안은 통증 없이 걸었다." in request["user_prompt"]
        assert "narrative_context (read-only):" in request["user_prompt"]
        assert "전에 발이 다쳤다." in request["user_prompt"]
        assert str(CHARACTER_ID) not in request["user_prompt"]
        assert str(CHUNK_ID) not in request["user_prompt"]
    review = client.requests[1]
    assert "first_pass_draft" not in review["user_prompt"]
    assert '"attribute_name": "level"' not in review["user_prompt"]
    assert review["prompt_cache_key"].startswith("status-extraction:v11:")
    assert review["response_schema"] is not client.requests[0]["response_schema"]
    assert "evidence_refs" in json.dumps(review["response_schema"].schema)
    assert '"evidence_spans"' not in json.dumps(review["response_schema"].schema)
    assert context == {"previous_episode": "전에 발이 다쳤다.", "next_chunk": None}


def test_reference_episode_evidence_is_retried_without_copying_it_into_current_episode() -> None:
    foreign = _status(quote="이전 회차에서 리안은 다쳤다.")
    client = SequenceClient([[foreign], [], [_status_refs(refs=("PREVIOUS:E1",))], [_status_refs(False)]])

    result = _extract(
        client, max_attempts=2,
        narrative_context={"previous_episode": "이전 회차에서 리안은 다쳤다."},
    )

    assert len(client.requests) == 7
    assert "EVIDENCE_OUTSIDE_CURRENT_CHUNK" in client.requests[1]["user_prompt"]
    assert "STATUS_EVIDENCE_UNKNOWN_REF" in client.requests[3]["user_prompt"]
    assert result.candidates[0].evidence_spans[0].quote == "리안의 발이 다쳤다."


def test_status_review_reads_the_chunk_even_when_general_pass_is_empty() -> None:
    client = SequenceClient([[], [_status_refs(False)]])
    result = _extract(client)
    assert len(client.requests) == 5
    assert len(result.candidates) == 1
    assert "first_pass_draft" not in client.requests[1]["user_prompt"]


def test_status_unknown_reference_retries_with_safe_field_location() -> None:
    foreign = _status_refs(refs=("previous-private-sentence",))
    client = SequenceClient([[], [foreign], [_status_refs()]])
    result = _extract(client, max_attempts=2)
    correction = client.requests[2]["user_prompt"]
    assert "STATUS_EVIDENCE_UNKNOWN_REF" in correction
    assert "evidence_refs" in correction
    assert "previous-private-sentence" not in correction
    assert result.candidates[0].evidence_spans[0].quote == "리안의 발이 다쳤다."


def test_status_unknown_reference_fails_without_saving_partial_results() -> None:
    foreign = _status_refs(refs=("PREVIOUS:E1",))
    client = SequenceClient([[], [foreign], [foreign]])
    with pytest.raises(LlmExtractionError, match="STATUS_EVIDENCE_UNKNOWN_REF"):
        _extract(client, max_attempts=2)
    assert len(client.requests) == 3


def test_status_references_bind_original_crlf_and_offsets() -> None:
    text = "리안의 발이 다쳤다.\r\n\r\n약을 먹었다.\r\n한참 뒤 리안은 통증 없이 걸었다."
    client = SequenceClient([[], [_status_refs(False, refs=("E1", "E3"))]])
    result = _extract(client, chunk_text=text)
    spans = result.candidates[0].evidence_spans
    assert [span.quote for span in spans] == ["리안의 발이 다쳤다.", "한참 뒤 리안은 통증 없이 걸었다."]
    assert all(text[span.start_offset:span.end_offset] == span.quote for span in spans)
    assert spans[1].start_offset == text.index("한참 뒤")


def test_status_free_text_quotes_cannot_bypass_reference_contract() -> None:
    client = SequenceClient([[], [_status(quote="이전 원문에서 옮긴 인용")], [_status_refs()]])
    result = _extract(client, max_attempts=2)
    assert "STATUS_OBSERVATION_INVALID_PAYLOAD" in client.requests[2]["user_prompt"]
    assert "이전 원문에서 옮긴 인용" not in client.requests[2]["user_prompt"]
    assert result.candidates[0].evidence_spans[0].quote == "리안의 발이 다쳤다."


def test_status_same_sentence_recurrence_keeps_distinct_source_positions() -> None:
    from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets

    text = "리안의 발이 다쳤다. 리안의 발이 나았다. 리안의 발이 다쳤다."
    client = SequenceClient([[], [_timeline(
        _status_refs(refs=("E1",)),
        _status_refs(False, refs=("E2",), value="발 기능 회복"),
        _status_refs(refs=("E3",)),
    )]])
    result = _extract(client, chunk_text=text)
    resolved = resolve_candidate_evidence_offsets(
        result.candidates, text, 300, preserve_status_offsets=True,
    )
    starts = [candidate.evidence_spans[0].start_offset for candidate in resolved]
    assert starts == [300, 300 + text.index("리안의 발이 나았다."), 300 + text.rindex("리안의 발이 다쳤다.")]
    assert resolved[0].evidence_spans[0].quote == resolved[2].evidence_spans[0].quote
    assert starts[0] < starts[1] < starts[2]


def test_no_status_schema_keeps_single_call_even_with_active_status_context() -> None:
    client = SequenceClient([[_level()]])
    result = _extract(client, schema_hints=(LEVEL_SCHEMA,))
    assert len(client.requests) == 1
    assert result.candidates[0].attribute_name == "level"


def test_empty_review_discards_unsupported_status_drafts() -> None:
    client = SequenceClient([[_status(), _level()], []])
    result = _extract(client)
    assert [candidate.attribute_name for candidate in result.candidates] == ["level"]


def test_review_validation_failure_fails_the_whole_chunk_without_draft_fallback() -> None:
    client = SequenceClient([[_status(), _level()], "invalid", "still invalid"])
    with pytest.raises(LlmExtractionError, match="RESPONSE_JSON_INVALID"):
        _extract(client, max_attempts=2)
    assert len(client.requests) == 3
    assert "status-extraction" in client.requests[1]["prompt_cache_key"]
    assert client.requests[1]["prompt_cache_key"] == client.requests[2]["prompt_cache_key"]


def test_review_retries_nonstatus_outputs_instead_of_modifying_general_results() -> None:
    discovery = _discovery()
    discovery.pop("evidence_spans")
    discovery["evidence_refs"] = ["E1"]
    client = SequenceClient([[_level()], [discovery], [_status_refs(False)]])
    result = _extract(client, max_attempts=2)
    assert [candidate.attribute_name for candidate in result.candidates] == ["level", "status.발_부상"]
    assert "STATUS_OBSERVATION_INVALID_PAYLOAD" in client.requests[2]["user_prompt"]


def test_status_provider_cannot_override_derived_active_and_retries_safely() -> None:
    client = SequenceClient([[], [_status_refs("false")], [_status_refs(False)]])
    result = _extract(client, max_attempts=2)
    assert result.candidates[0].value_json["active"] is False
    assert "STATUS_OBSERVATION_INVALID_PAYLOAD" in client.requests[2]["user_prompt"]


@pytest.mark.parametrize("field", ["detail", "cause", "missing_memory", "extra_json"])
def test_status_json_extra_fields_are_rejected_before_review_and_never_reinjected(field):
    invalid = _status_refs()
    invalid["observations"][0]["value_json"][field] = "unsupported-private-value-marker"
    client = SequenceClient([[], [invalid], [_status_refs()]])

    result = _extract(client, max_attempts=2)

    assert len(client.requests) == 6
    assert result.candidates[0].value_json == {"name": "발 부상", "active": True}
    schema = client.requests[1]["response_schema"].schema
    assert schema["$defs"]["_StatusJsonValue"]["additionalProperties"] is False
    assert set(schema["$defs"]["_StatusJsonValue"]["properties"]) == {"name"}
    retry = client.requests[2]["user_prompt"]
    assert "STATUS_OBSERVATION_INVALID_PAYLOAD" in retry
    assert "unsupported-private-value-marker" not in retry
    for request in client.requests[3:]:
        payload = json.loads(request["user_prompt"])
        assert payload["draft_observations"][0]["value_json"] == {"name": "발 부상", "active": True}


@pytest.mark.parametrize("value_type,value,display", [
    ("STRING", "관찰값", "관찰값"), ("NUMBER", 3, "3"), ("BOOLEAN", True, "true"),
])
def test_scalar_status_keeps_existing_typed_value_wire(value_type, value, display):
    state = _status_refs(value=display)
    state["value_type"] = value_type
    state["observations"][0]["value_json"] = {"value": value, "extra_json": None}
    schema = CharacterSettingSchemaHint("status", "상태", "status.*", (), value_type)
    client = SequenceClient([[], [state]])

    result = _extract(client, schema_hints=(schema,))

    assert result.candidates[0].value_json == {"value": value, "active": True}
    assert result.candidates[0].value_type == value_type
    assert result.candidates[0].attribute_value == display
    assert result.candidates[0]._status_observation_kind == "START"
    assert result.candidates[0]._status_observation_group == (CHUNK_ID, 0)


def test_transition_conflict_requires_repairing_both_observations_not_appending_later_evidence():
    text = "리안의 발이 다쳤다. 리안은 약을 먹었다. 리안은 통증 없이 걸었다. 리안은 집에 도착했다."
    mixed = _timeline(_status_refs(refs=("E1", "E2")), _status_refs(False, refs=("E2", "E3")))
    padded = _timeline(_status_refs(refs=("E1", "E2")), _status_refs(False, refs=("E2", "E3", "E4")))
    corrected = _timeline(_status_refs(refs=("E1",)), _status_refs(False, refs=("E3",), value="발 기능 회복"))
    client = SequenceClient([[], [mixed], [padded], [corrected]])
    ledger = Ledger()

    result = _extract(_metered(client, ledger), max_attempts=3, chunk_text=text)

    assert len(client.requests) == len(ledger.reservations) == len(ledger.settlements) == 7
    for request, end_refs in zip(client.requests[2:4], [("E2", "E3"), ("E2", "E3", "E4")], strict=True):
        retry = request["user_prompt"]
        assert "STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT" in retry
        assert "states.0.observations.0.evidence_refs" in retry
        assert "states.0.observations.1.evidence_refs" in retry
        assert "양쪽 evidence_refs" in retry
        assert "뒤의 E를 억지로 추가" in retry
        conflict = json.JSONDecoder().raw_decode(retry.split("previous_status_observation_conflict:\n")[1])[0]
        assert conflict == {"observations": [
            {"fieldLoc": "states.0.observations.0.evidence_refs", "kind": "START",
             "evidence_refs": ["E1", "E2"]},
            {"fieldLoc": "states.0.observations.1.evidence_refs", "kind": "END",
             "evidence_refs": list(end_refs)},
        ]}
        assert retry.count("previous_status_observation_conflict:") == 1
    assert [c._status_observation_kind for c in result.candidates] == ["START", "END"]
    assert [c.evidence_spans[0].quote for c in result.candidates] == [
        "리안의 발이 다쳤다.", "리안은 통증 없이 걸었다.",
    ]
    assert all(c._status_observation_group == (CHUNK_ID, 0) for c in result.candidates)
    assert len(client.requests[4:]) == 3
    assert all(request["prompt_cache_key"].startswith("status-observation-review:v9")
               for request in client.requests[4:])


def test_persistent_transition_conflict_fails_before_review_without_partial_results():
    mixed = _timeline(_status_refs(refs=("E1", "E2")), _status_refs(False, refs=("E2", "E3")))
    client = SequenceClient([[_level()], [mixed], [mixed]])
    with pytest.raises(LlmExtractionError, match="STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT"):
        _extract(client, max_attempts=2)
    assert len(client.requests) == 3
    assert all(not request["prompt_cache_key"].startswith("status-observation-review:")
               for request in client.requests)


def test_conflict_retry_carries_only_validated_kinds_and_refs_without_response_values(caplog):
    mixed = _timeline(_status_refs(refs=("E1", "E2")), _status_refs(False, refs=("E2", "E3")))
    mixed["entity_name"] = "PRIVATE_PROVIDER_ENTITY"
    mixed["raw_entity_mention"] = "PRIVATE_PROVIDER_MENTION"
    mixed["attribute_name"] = "status.PRIVATE_PROVIDER_KEY"
    mixed["observations"][0]["attribute_value"] = "PRIVATE_PROVIDER_VALUE"
    mixed["observations"][0]["value_json"]["name"] = "PRIVATE_PROVIDER_JSON_NAME"
    corrected = _timeline(_status_refs(), _status_refs(False, refs=("E3",), value="발 기능 회복"))
    client = SequenceClient([[], [mixed], [corrected]])

    result = _extract(client, max_attempts=2)

    retry = client.requests[2]["user_prompt"]
    assert "previous_status_observation_conflict:" in retry
    assert "PRIVATE_PROVIDER" not in retry
    assert "PRIVATE_PROVIDER" not in caplog.text
    assert "previous_status_observation_conflict:" not in caplog.text
    assert "previous_status_observation_conflict:" not in client.requests[1]["user_prompt"]
    assert all("previous_status_observation_conflict:" not in r["user_prompt"] for r in client.requests[3:])
    assert [c._status_observation_kind for c in result.candidates] == ["START", "END"]


def test_conflict_retry_keeps_same_bounded_context_when_output_cap_expands():
    mixed = _timeline(_status_refs(refs=("E1", "E2")), _status_refs(False, refs=("E2", "E3")))
    truncated = LlmOutputTruncatedError(
        "output truncated", incomplete_reason="max_output_tokens", max_output_tokens=6000,
        input_token_count=100, output_token_count=6000,
    )
    corrected = _timeline(_status_refs(), _status_refs(False, refs=("E3",), value="발 기능 회복"))
    client = SequenceClient([[], [mixed], truncated, [corrected]])

    result = _extract(client, max_attempts=2)

    assert client.requests[2]["user_prompt"] == client.requests[3]["user_prompt"]
    assert "previous_status_observation_conflict:" in client.requests[3]["user_prompt"]
    assert [r["max_output_tokens"] for r in client.requests[:4]] == [6000, 6000, 6000, 12000]
    assert len(client.requests) == 7
    assert len(result.candidates) == 2


def test_current_chunk_end_requires_separate_onset_before_review() -> None:
    collapsed = _status_refs(False, refs=("E1", "E3"), value="발을 다쳤다가 회복함")
    collapsed["origin"] = "STARTS_IN_CURRENT_CHUNK"
    corrected = _timeline(_status_refs(), _status_refs(False, refs=("E3",), value="발 기능 회복"))
    client = SequenceClient([[], [collapsed], [corrected]])

    result = _extract(client, max_attempts=2)

    assert len(client.requests) == 6
    assert "START" in client.requests[2]["user_prompt"]
    assert [candidate.value_json["active"] for candidate in result.candidates] == [True, False]
    assert [candidate.evidence_spans[0].quote for candidate in result.candidates] == [
        "리안의 발이 다쳤다.", "한참 뒤 리안은 통증 없이 걸었다.",
    ]


def test_later_recollection_does_not_block_earlier_current_recovery() -> None:
    text = "리안의 발이 다쳤다. 리안은 통증 없이 걸었다. 리안은 지난겨울 다쳤던 일을 떠올렸다."
    past = _status_refs(None, refs=("E3",), value="지난겨울 발을 다쳤던 과거 이력")
    past["observations"][0]["kind"] = "PAST"
    review = _observation_review(end_refs=("E2",))
    client = SequenceClient([[], [_timeline(_status_refs(), past)], review, review, review])

    result = _extract(
        client, chunk_text=text,
        known_characters=(KnownCharacter(character_id=CHARACTER_ID, name="리안"),),
    )

    assert len(client.requests) == 5
    assert len(result.candidates) == 3
    ended = [c for c in result.candidates if c.value_json.get("active") is False]
    assert len(ended) == 1
    assert ended[0].evidence_spans[0].quote == "리안은 통증 없이 걸었다."
    historical = [c for c in result.candidates if c._status_observation_kind == "PAST"]
    assert len(historical) == 1
    assert "active" not in historical[0].value_json
    assert "_status_observation_kind" not in historical[0].model_dump()


def test_review_truncation_expands_only_its_own_attempt() -> None:
    truncated = LlmOutputTruncatedError(
        "output truncated", incomplete_reason="max_output_tokens", max_output_tokens=6000,
        input_token_count=100, output_token_count=6000,
    )
    client = SequenceClient([[_level()], truncated, [_status_refs(False)]])
    result = _extract(client)
    assert len(result.candidates) == 2
    assert [request["max_output_tokens"] for request in client.requests] == [6000, 6000, 12000, 6000, 6000, 6000]
    assert client.requests[1]["user_prompt"] == client.requests[2]["user_prompt"]


class Ledger:
    def __init__(self, *, reject_second=False) -> None:
        self.reservations = []
        self.settlements = []
        self.reject_second = reject_second

    async def reserve_ai_tokens(self, **kwargs) -> None:
        self.reservations.append(kwargs)
        if self.reject_second and len(self.reservations) == 2:
            raise AiTokenQuotaExhaustedError()

    async def settle_ai_tokens(self, **kwargs) -> None:
        self.settlements.append(kwargs)

    async def release_ai_tokens(self, **kwargs) -> None:
        raise AssertionError("These successful responses must settle their real usage.")


def _metered(client, ledger):
    return MeteredTextGenerationClient(
        delegate=client, ledger=ledger, analysis_job_id=UUID(int=4),
        purpose="SETTING_EXTRACTION", default_model="gpt-5.6-terra",
        lease_token=LEASE_TOKEN, max_retries=0,
    )


def test_general_status_and_observation_review_each_settle_with_the_actual_lease() -> None:
    client = SequenceClient([[], [_status_refs(False)]])
    ledger = Ledger()
    _extract(_metered(client, ledger))
    assert len(ledger.reservations) == len(ledger.settlements) == 5
    assert len({item["request_id"] for item in ledger.reservations}) == 5
    assert [item["attempt"] for item in ledger.reservations] == [1, 2, 3, 4, 5]
    assert all(item["lease_token"] == LEASE_TOKEN for item in ledger.reservations)
    assert all(item["purpose"] == "SETTING_EXTRACTION" for item in ledger.reservations)
    assert all(item["reserved_tokens"] > 6000 for item in ledger.reservations)
    assert all(item["outcome"] == "SUCCESS" for item in ledger.settlements)


def test_status_quota_rejection_never_calls_provider_or_returns_first_draft() -> None:
    client = SequenceClient([[_status()]])
    ledger = Ledger(reject_second=True)
    with pytest.raises(AiTokenQuotaExhaustedError):
        _extract(_metered(client, ledger))
    assert len(client.requests) == 1
    assert len(ledger.reservations) == 2
    assert len(ledger.settlements) == 1


def _observation_review(*, end_refs=(), classification="STATE_OBSERVATION", draft_refs=()):
    return {
        "draft_reviews": [{
            "draft_ref": "D1", "classification": classification,
            "reason": "원문 관찰의 성격을 확인했다.", "evidence_refs": list(draft_refs),
        }],
        "target_reviews": [{
            "target_ref": "T1", "end_state": "ENDED" if end_refs else "UNKNOWN",
            "reason": "후속 행동에서 기능 회복을 확인했다." if end_refs else "추가 종료 근거는 없다.",
            "end_observations": [{"summary": "발 기능이 회복되어 통증 없이 걸음",
                                  "evidence_refs": list(end_refs)}] if end_refs else [],
        }],
    }


def test_observation_review_adds_missing_end_without_rewriting_onset_or_source() -> None:
    text = "리안의 발이 다쳤다. 약을 먹었다. 한참 뒤 리안은 통증 없이 걸었다."
    review = _observation_review(end_refs=("E3",))
    client = SequenceClient([[], [_status_refs()], review, review, review])
    result = _extract(client, chunk_text=text)
    assert len(client.requests) == 5
    assert len(result.candidates) == 2
    assert all(req["user_prompt"] == client.requests[2]["user_prompt"]
               for req in client.requests[2:])
    onset, end = result.candidates
    assert onset.value_json == {"name": "발 부상", "active": True}
    assert onset.attribute_value == "발 부상"
    assert onset.evidence_spans[0].quote == "리안의 발이 다쳤다."
    assert end.entity_name == onset.entity_name
    assert end.attribute_name == onset.attribute_name
    assert end.value_json["active"] is False
    assert end.source_chunk_id == CHUNK_ID
    assert end.evidence_spans[0].quote == "한참 뒤 리안은 통증 없이 걸었다."
    assert text[end.evidence_spans[0].start_offset:end.evidence_spans[0].end_offset] == end.evidence_spans[0].quote
    assert all(str(CHUNK_ID) not in req["user_prompt"] for req in client.requests)


def test_observation_review_foreign_evidence_fails_whole_chunk_before_storage() -> None:
    client = SequenceClient([[_level()], [_status_refs()],
                             _observation_review(end_refs=("PREVIOUS:E1",))])
    with pytest.raises(LlmExtractionError):
        _extract(client)
    assert len(client.requests) == 3


def test_observation_review_excludes_one_off_process_and_preserves_general_fact() -> None:
    process = _status_refs(None)
    process["attribute_name"] = "status.일회_처치"
    process["observations"][0]["attribute_value"] = "한 번의 처치로 새살이 차오름"
    process["observations"][0]["value_json"] = {"name": "일회 처치"}
    review = _observation_review(classification="ONE_OFF_TREATMENT_PROCESS", draft_refs=("E1",))
    client = SequenceClient([[_level()], [process], review, review, review])
    result = _extract(client, chunk_text="연고를 한 번 바르자 새살이 차올랐다.")
    assert [item.attribute_name for item in result.candidates] == ["level"]


def test_extracted_end_with_treatment_wording_is_preserved_outside_classification() -> None:
    text = "리안의 발이 다쳤다. 연고를 바르자 조직이 재생됐다. 리안은 통증 없이 걸었다."
    end = _status_refs(False, refs=("E2",), value="조직이 재생되는 처치 중 상태")
    client = SequenceClient([[], [_timeline(_status_refs(), end)]])
    result = _extract(
        client, chunk_text=text,
        known_characters=(KnownCharacter(character_id=CHARACTER_ID, name="리안"),),
    )

    assert len(client.requests) == 5
    assert len(result.candidates) == 2
    assert result.candidates[1].value_json["active"] is False
    assert result.candidates[1].attribute_value == "조직이 재생되는 처치 중 상태"
    assert result.candidates[1].evidence_spans[0].quote == "연고를 바르자 조직이 재생됐다."
    for request in client.requests[2:]:
        payload = json.loads(request["user_prompt"])
        assert payload["draft_observations"] == []
        assert [item["draft_ref"] for item in payload["preserved_state_observations"]] == ["D1"]
        assert [item["draft_ref"] for item in payload["preserved_end_observations"]] == ["D2"]
        assert payload["preserved_end_observations"][0]["value_json"]["active"] is False
        assert request["response_schema"].schema["properties"]["draft_reviews"]["maxItems"] == 0


def test_same_producer_state_links_onset_and_existing_end_without_duplicate() -> None:
    end = _status_refs(False, refs=("E2", "E3"), value="발 기능 회복")
    review = _observation_review()
    review["draft_reviews"] = []
    review["target_reviews"][0]["end_state"] = "ENDED"
    client = SequenceClient([[], [_timeline(_status_refs(), end)], review, review, review])

    result = _extract(client)

    assert len(client.requests) == 5
    assert len(result.candidates) == 2
    assert [c.value_json["active"] for c in result.candidates] == [True, False]
    assert [c._status_observation_group for c in result.candidates] == [(CHUNK_ID, 0)] * 2
    assert all("_status_observation_group" not in c.model_dump() for c in result.candidates)
    for request in client.requests[2:]:
        payload = json.loads(request["user_prompt"])
        assert len(payload["targets"]) == 1
        assert payload["targets"][0]["draft_refs"] == ["D1", "D2"]
        assert str(CHUNK_ID) not in request["user_prompt"]


def test_existing_injury_improvement_and_end_survive_review_with_exact_evidence() -> None:
    text = "연고를 바르자 상처가 차오르고 통증이 줄었다. 이후 리안은 통증 없이 걸었다."
    change = _status_refs(None, refs=("E1",), value="상처가 차오르고 통증이 줄어듦")
    change["observations"][0]["kind"] = "CHANGE"
    end = _status_refs(False, refs=("E2",), value="발 기능 회복")
    review = _observation_review()
    review["draft_reviews"] = []
    review["target_reviews"][0]["end_state"] = "ENDED"
    client = SequenceClient([[], [_timeline(change, end)], review, review, review])

    result = _extract(client, chunk_text=text, known_characters=(KnownCharacter(
        character_id=CHARACTER_ID, name="리안",
        active_statuses=(ActiveCharacterStatus("status.발_부상", "발 부상"),),
    ),))

    assert [candidate._status_observation_kind for candidate in result.candidates] == ["CHANGE", "END"]
    assert [candidate.value_json["active"] for candidate in result.candidates] == [True, False]
    assert [candidate.evidence_spans[0].quote for candidate in result.candidates] == [
        "연고를 바르자 상처가 차오르고 통증이 줄었다.", "이후 리안은 통증 없이 걸었다.",
    ]
    assert result.candidates[0].attribute_value == "상처가 차오르고 통증이 줄어듦"
    assert len(client.requests) == 5
    for request in client.requests[2:]:
        payload = json.loads(request["user_prompt"])
        assert payload["draft_observations"] == []
        assert [row["draft_ref"] for row in payload["preserved_state_observations"]] == ["D1"]
        assert [row["draft_ref"] for row in payload["preserved_end_observations"]] == ["D2"]
        assert payload["targets"][0]["draft_refs"] == ["D1", "D2"]


def test_observation_review_quota_failure_does_not_return_unreviewed_drafts() -> None:
    class RejectThirdLedger(Ledger):
        async def reserve_ai_tokens(self, **kwargs):
            await super().reserve_ai_tokens(**kwargs)
            if len(self.reservations) == 3:
                raise AiTokenQuotaExhaustedError()

    client = SequenceClient([[_level()], [_status_refs()]])
    ledger = RejectThirdLedger()
    with pytest.raises(AiTokenQuotaExhaustedError):
        _extract(_metered(client, ledger))
    assert len(client.requests) == 2
    assert len(ledger.reservations) == 3
    assert len(ledger.settlements) == 2


def test_observation_review_has_its_own_bounded_output_expansion() -> None:
    truncated = LlmOutputTruncatedError(
        "output truncated", incomplete_reason="max_output_tokens", max_output_tokens=6000,
        input_token_count=100, output_token_count=6000,
    )
    client = SequenceClient([[], [_status_refs()], truncated])
    result = _extract(client)
    assert len(result.candidates) == 1
    assert [req["max_output_tokens"] for req in client.requests] == [6000, 6000, 6000, 12000, 6000, 6000]
    assert client.requests[2]["user_prompt"] == client.requests[3]["user_prompt"]


def test_consensus_third_vote_quota_failure_does_not_return_two_vote_end() -> None:
    class RejectFifthLedger(Ledger):
        async def reserve_ai_tokens(self, **kwargs):
            await super().reserve_ai_tokens(**kwargs)
            if len(self.reservations) == 5:
                raise AiTokenQuotaExhaustedError()

    review = _observation_review(end_refs=("E3",))
    client = SequenceClient([[_level()], [_status_refs()], review, review])
    ledger = RejectFifthLedger()
    with pytest.raises(AiTokenQuotaExhaustedError):
        _extract(_metered(client, ledger))
    assert len(client.requests) == 4
    assert len(ledger.reservations) == 5
    assert len(ledger.settlements) == 4
    assert len({item["request_id"] for item in ledger.reservations}) == 5
    assert all(item["lease_token"] == LEASE_TOKEN for item in ledger.reservations)
    assert sum(item["input_tokens"] + item["output_tokens"]
               for item in ledger.settlements) == 480
