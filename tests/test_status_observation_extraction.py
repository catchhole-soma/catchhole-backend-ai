import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.schemas import (
    CharacterSettingProviderResponse,
    character_setting_provider_json_schema,
)
from app.analysis.status_evidence import StatusEvidenceDocument, StatusEvidenceReferenceError
from app.analysis.status_observation_extraction import (
    ResolvedStatusObservations,
    StatusObservationConflictError,
    build_status_observation_schema,
    resolve_status_observations,
)


def _observation(kind="START", refs=None, value=None):
    return {"kind": kind, "attribute_value": "원형 표시 값", "confidence": 0.83,
            "value_json": {"name": "발 부상"} if value is None else value,
            "evidence_refs": ["E1"] if refs is None else refs}


def _state(observations=None, origin="STARTS_IN_CURRENT_CHUNK", value_type="JSON"):
    return {"entity_name": "리안", "raw_entity_mention": "그", "attribute_name": "status.발_부상",
            "value_type": value_type, "origin": origin,
            "observations": [_observation()] if observations is None else observations}


def _resolve(states, source="다쳤다. 나았다. 다시 다쳤다. 다시 나았다."):
    return resolve_status_observations({"states": states}, StatusEvidenceDocument(source))


def _domain(result):
    return CharacterSettingProviderResponse.model_validate(result.provider_payload).to_extraction_result(UUID(int=1))


def test_separate_start_end_round_trip_into_existing_provider_and_domain_contract():
    result = _resolve([_state([_observation(), _observation("END", ["E2"])])])
    assert isinstance(result, ResolvedStatusObservations)
    assert result.kinds == ("START", "END")
    assert result.state_indexes == (0, 0)
    candidates = _domain(result).candidates
    assert [c.value_json["active"] for c in candidates] == [True, False]
    assert [c.evidence_spans[0].quote for c in candidates] == ["다쳤다.", "나았다."]
    assert all(c.attribute_value == "원형 표시 값" and c.confidence == 0.83 for c in candidates)
    assert all(c.value_json["name"] == "발 부상" for c in candidates)
    assert all(c.candidate_kind == "SETTING" and c.entity_type == "CHARACTER" for c in candidates)
    assert all("kind" not in c and "origin" not in c for c in result.provider_payload["candidates"])


@pytest.mark.parametrize("first", ["END", "CONTINUE", "CHANGE"])
def test_current_origin_requires_first_current_observation_to_be_start(first):
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_CURRENT_START_REQUIRED"):
        _resolve([_state([_observation("PAST"), _observation(first, ["E2"])])])


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_noncurrent_does_not_establish_current_start(kind):
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_CURRENT_START_REQUIRED"):
        _resolve([_state([_observation(kind)])])
    result = _resolve([_state([_observation(kind), _observation("START", ["E2"]), _observation("END", ["E3"])])])
    assert result.kinds == (kind, "START", "END")
    assert "active" not in _domain(result).candidates[0].value_json


def test_preexisting_end_requires_no_snapshot_or_start_and_can_precede_recurrence():
    result = _resolve([_state([_observation("END"), _observation("START", ["E2"]), _observation("END", ["E3"])],
                             origin="PREEXISTING_OR_UNSPECIFIED")])
    assert result.kinds == ("END", "START", "END")
    assert [c.value_json["active"] for c in _domain(result).candidates] == [False, True, False]


def test_repeated_start_end_cycles_keep_all_occurrences_and_literal_offsets():
    source = "\r\n  다쳤다. 나았다.\r\n\r\n  다쳤다. 나았다.\r\n"
    observations = [_observation(kind, [f"E{i}"]) for i, kind in enumerate(("START", "END", "START", "END"), 1)]
    result = _resolve([_state(observations)], source)
    candidates = _domain(result).candidates
    assert result.kinds == ("START", "END", "START", "END")
    assert len(candidates) == 4
    starts = [c.evidence_spans[0].start_offset for c in candidates]
    assert starts == sorted(set(starts))
    for candidate in candidates:
        span = candidate.evidence_spans[0]
        assert source[span.start_offset:span.end_offset] == span.quote


@pytest.mark.parametrize("origin", ["STARTS_IN_CURRENT_CHUNK", "PREEXISTING_OR_UNSPECIFIED"])
def test_end_must_be_strictly_after_latest_start_even_when_units_overlap(origin):
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT"):
        _resolve([_state([_observation("START", ["E1", "E3"]), _observation("END", ["E3"])], origin)])
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT"):
        _resolve([_state([_observation("START"), _observation("END", ["E2"]),
                         _observation("START", ["E3"]), _observation("END", ["E3"])], origin)])


def test_observation_source_order_is_checked_and_refs_order_is_not_rewritten():
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT"):
        _resolve([_state([_observation("START", ["E3"]), _observation("END", ["E2"])])])
    result = _resolve([_state([_observation("START", ["E2", "E1"]), _observation("END", ["E3"])])])
    assert [s["quote"] for s in result.provider_payload["candidates"][0]["evidence_spans"]] == ["나았다.", "다쳤다."]


def test_distinct_states_interleave_in_source_order_with_aligned_kinds_and_stable_ties():
    first = _state([_observation("START"), _observation("END", ["E3"])])
    second = _state([_observation("CONTINUE", ["E2"]), _observation("PAST", ["E3"])], "PREEXISTING_OR_UNSPECIFIED")
    second["attribute_name"] = "status.독"
    result = _resolve([first, second])
    assert result.kinds == ("START", "CONTINUE", "END", "PAST")
    assert result.state_indexes == (0, 1, 0, 1)
    assert [c["attribute_name"] for c in result.provider_payload["candidates"]] == [
        "status.발_부상", "status.독", "status.발_부상", "status.독",
    ]
    for candidate, kind, state_index in zip(result.provider_payload["candidates"], result.kinds,
                                             result.state_indexes, strict=True):
        state = [first, second][state_index]
        observation = next(o for o in state["observations"] if o["kind"] == kind)
        assert candidate["entity_name"] == state["entity_name"]
        assert candidate["raw_entity_mention"] == state["raw_entity_mention"]
        assert candidate["attribute_value"] == observation["attribute_value"]
        assert candidate["confidence"] == observation["confidence"]
        assert "state_index" not in candidate and "group" not in candidate


def test_duplicate_state_group_cannot_bypass_origin_or_chronology():
    first = _state()
    second = _state([_observation("END", ["E2"])], "PREEXISTING_OR_UNSPECIFIED")
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_DUPLICATE_STATE"):
        _resolve([first, second])


@pytest.mark.parametrize("value_type, value", [
    ("STRING", {"value": " 원형 문자열 ", "extra_json": '{"memo":"정보"}'}),
    ("NUMBER", {"value": 3.25, "extra_json": None}),
    ("BOOLEAN", {"value": False, "extra_json": '{"memo":"정보"}'}),
    ("JSON", {"name": " 원형 부상 명칭 "}),
])
def test_all_four_existing_typed_values_preserved_with_only_active_added(value_type, value):
    observation = _observation(value=value)
    state = _state([observation], value_type=value_type)
    state["entity_name"] = " 리안 "
    result = _resolve([state])
    wire = result.provider_payload["candidates"][0]
    assert wire["entity_name"] == state["entity_name"]
    assert wire["raw_entity_mention"] == state["raw_entity_mention"]
    assert wire["attribute_value"] == observation["attribute_value"]
    assert wire["value_type"] == value_type
    expected = value if value_type == "JSON" else json.loads(value["extra_json"]) if value["extra_json"] is not None else {}
    assert json.loads(wire["value_json"]["extra_json"]) == {**expected, "active": True}
    assert {k: v for k, v in wire["value_json"].items() if k != "extra_json"} == {
        k: v for k, v in value.items() if k != "extra_json" and value_type != "JSON"
    }
    assert _domain(result).candidates[0].value_json["active"] is True


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_noncurrent_value_wire_and_text_stay_exact_and_kind_survives(kind):
    value = {"name": " 과거 또는 가정 "}
    result = _resolve([_state([_observation(kind, value=value)], "PREEXISTING_OR_UNSPECIFIED")])
    assert json.loads(result.provider_payload["candidates"][0]["value_json"]["extra_json"]) == value
    assert result.kinds == (kind,)
    assert "active" not in _domain(result).candidates[0].value_json


@pytest.mark.parametrize("active", [True, False, None, "false", 1])
def test_model_cannot_inject_active_in_extra_json(active):
    obs = _observation(value={"value": "원형", "extra_json": json.dumps({"active": active})})
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_ACTIVE_FORBIDDEN"):
        _resolve([_state([obs], value_type="STRING")])


@pytest.mark.parametrize("value_type, value", [
    ("NUMBER", {"value": "3", "extra_json": None}),
    ("NUMBER", {"value": True, "extra_json": None}),
    ("NUMBER", {"value": float("inf"), "extra_json": None}),
    ("BOOLEAN", {"value": 1, "extra_json": None}),
    ("STRING", {"value": 3, "extra_json": None}),
    ("JSON", {"extra_json": "[]"}),
    ("JSON", {"extra_json": "{}", "active": False}),
    ("UNKNOWN", {"value": None, "extra_json": None}),
])
def test_existing_provider_type_constraints_and_active_field_forbidden(value_type, value):
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_INVALID_PAYLOAD"):
        _resolve([_state([_observation(value=value)], value_type=value_type)])


@pytest.mark.parametrize("refs, code", [(["E999"], "UNKNOWN_REF"), (["E1", "E1"], "DUPLICATE_REF"),
                                        (["previous:E1"], "UNKNOWN_REF"), ([], "INVALID_PAYLOAD")])
def test_refs_use_existing_codec_with_safe_observation_locations(refs, code):
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        _resolve([_state([_observation(refs=refs)])])
    assert caught.value.reason_code.endswith(code)
    assert caught.value.field_locs[0].startswith("states.0.observations.0.evidence_refs")


def test_input_is_immutable_on_success_and_atomic_later_failure():
    state = _state([_observation("START"), _observation("END", ["E2"])])
    payload = {"states": [state]}
    before = deepcopy(payload)
    result = resolve_status_observations(payload, StatusEvidenceDocument("다쳤다. 나았다."))
    result.provider_payload["candidates"][0]["value_json"]["extra_json"] = "{}"
    assert payload == before
    state["observations"][1]["evidence_refs"] = ["PRIVATE_NOVEL_TEXT"]
    before = deepcopy(payload)
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        resolve_status_observations(payload, StatusEvidenceDocument("다쳤다. 나았다."))
    assert payload == before
    assert "PRIVATE_NOVEL_TEXT" not in repr(caught.value)
    assert caught.value.field_locs == ("states.0.observations.1.evidence_refs.0",)


@pytest.mark.parametrize("field,value", [("confidence", -0.1), ("confidence", 1.1), ("confidence", True),
                                         ("PRIVATE_NOVEL_TEXT", "secret")])
def test_confidence_and_extra_fields_are_strict_with_source_free_errors(field, value):
    observation = _observation()
    observation[field] = value
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        _resolve([_state([observation])])
    assert "PRIVATE_NOVEL_TEXT" not in repr(caught.value)
    assert all("PRIVATE_NOVEL_TEXT" not in loc for loc in caught.value.field_locs)


def test_empty_output_and_nullable_confidence():
    empty = _resolve([])
    assert empty.provider_payload == {"candidates": []}
    assert empty.kinds == empty.state_indexes == ()
    observation = _observation()
    observation["confidence"] = None
    assert _domain(_resolve([_state([observation])])).candidates[0].confidence is None


def test_schema_has_only_minimal_strict_wire_and_one_literal_reference_definition():
    original = deepcopy(character_setting_provider_json_schema())
    schema = build_status_observation_schema(StatusEvidenceDocument("다쳤다. 나았다."))
    assert schema.strict and schema.name == "character_status_observations"
    assert schema.schema["$defs"]["_StatusObservationEvidenceReference"]["enum"] == ["E1", "E2"]
    def nodes(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from nodes(child)
    for node in nodes(schema.schema):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        assert "oneOf" not in node and "discriminator" not in node
    text = json.dumps(schema.schema)
    for forbidden in ('"active"', '"quote"', '"start_offset"', '"source_chunk_id"', '"anchor"', '"state_ref"', '"observation_ref"'):
        assert forbidden not in text
    assert text.count('"E1"') == 1
    schema.schema["$defs"]["_StatusObservationEvidenceReference"]["enum"].append("FORGED")
    assert "FORGED" not in json.dumps(build_status_observation_schema(StatusEvidenceDocument("다쳤다.")).schema)
    assert character_setting_provider_json_schema() == original
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_EVIDENCE_EMPTY_DOCUMENT"):
        build_status_observation_schema(StatusEvidenceDocument(" "))


@pytest.mark.parametrize("kind", ["CONTINUE", "CHANGE"])
@pytest.mark.parametrize("origin", ["STARTS_IN_CURRENT_CHUNK", "PREEXISTING_OR_UNSPECIFIED"])
def test_end_must_follow_latest_active_observation_not_only_start(kind, origin):
    observations = [_observation("START"), _observation(kind, ["E2"]), _observation("END", ["E2"])]
    if origin == "PREEXISTING_OR_UNSPECIFIED":
        observations.pop(0)
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT"):
        _resolve([_state(observations, origin)])
    observations[-1]["evidence_refs"] = ["E3"]
    assert _domain(_resolve([_state(observations, origin)])).candidates[-1].value_json["active"] is False


@pytest.mark.parametrize("kind", ["PAST", "HYPOTHETICAL"])
def test_noncurrent_observation_does_not_advance_last_active_anchor(kind):
    observations = [_observation("CONTINUE"), _observation(kind, ["E2"]), _observation("END", ["E2"])]
    result = _resolve([_state(observations, "PREEXISTING_OR_UNSPECIFIED")])
    assert result.kinds == ("CONTINUE", kind, "END")
    assert _domain(result).candidates[-1].value_json["active"] is False


@pytest.mark.parametrize("field", ["detail", "cause", "missing_memory", "summary", "active", "extra_json"])
def test_json_status_rejects_unknown_fields_atomically_instead_of_dropping_them(field):
    payload = {"states": [_state([_observation(value={"name": "발 부상", field: "PRIVATE_VALUE"})])]}
    before = deepcopy(payload)
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        resolve_status_observations(payload, StatusEvidenceDocument("다쳤다."))
    assert caught.value.reason_code == "STATUS_OBSERVATION_INVALID_PAYLOAD"
    assert payload == before
    assert "PRIVATE_VALUE" not in repr(caught.value)


@pytest.mark.parametrize("name", ["", "  ", None, True, 1])
def test_json_status_name_must_be_a_nonblank_string(name):
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        _resolve([_state([_observation(value={"name": name})])])
    assert caught.value.reason_code == "STATUS_OBSERVATION_INVALID_PAYLOAD"
    assert caught.value.field_locs == ("states.0.observations.0.value_json.name",)


def test_json_status_schema_is_name_only_and_legacy_provider_json_is_unchanged():
    schema = build_status_observation_schema(StatusEvidenceDocument("다쳤다.")).schema
    definition = schema["$defs"]["_StatusJsonValue"]
    assert set(definition["properties"]) == {"name"}
    assert definition["required"] == ["name"]
    assert definition["additionalProperties"] is False
    result = _resolve([_state()])
    assert _domain(result).candidates[0].value_json == {"name": "발 부상", "active": True}
    result.provider_payload["candidates"][0]["value_json"]["extra_json"] = json.dumps({
        "name": "예전 상태", "legacy_detail": {"preserved": True},
    })
    assert _domain(result).candidates[0].value_json["legacy_detail"] == {"preserved": True}


@pytest.mark.parametrize("kind", ["START", "CONTINUE", "CHANGE"])
@pytest.mark.parametrize("end_refs", [["E2", "E3"], ["E3", "E2"], ["E1", "E3"]])
def test_transition_overlap_reports_both_observations_even_with_later_end_evidence(kind, end_refs):
    observations = [_observation(kind, ["E1", "E2"]), _observation("END", end_refs)]
    if kind != "START":
        observations.insert(0, _observation("START"))
    payload = {"states": [_state(observations)]}
    before = deepcopy(payload)
    with pytest.raises(StatusObservationConflictError) as caught:
        resolve_status_observations(payload, StatusEvidenceDocument("다쳤다. 계속 아팠다. 나았다."))
    assert caught.value.reason_code == "STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT"
    end_index = len(observations) - 1
    assert caught.value.field_locs == (
        f"states.0.observations.{end_index - 1}.evidence_refs",
        f"states.0.observations.{end_index}.evidence_refs",
    )
    assert caught.value.observations == ((kind, ("E1", "E2")), ("END", tuple(end_refs)))
    assert payload == before


def test_conflict_hints_are_readonly_detached_and_contain_no_source_values():
    observations = [_observation("START", ["E1", "E2"]), _observation("END", ["E2"])]
    state = _state(observations)
    state.update(entity_name="PRIVATE_NAME", raw_entity_mention="PRIVATE_MENTION",
                 attribute_name="status.PRIVATE_KEY")
    for observation in observations:
        observation.update(attribute_value="PRIVATE_VALUE", value_json={"name": "PRIVATE_STATUS"})
    payload = {"states": [state]}
    before = deepcopy(payload)
    with pytest.raises(StatusObservationConflictError) as caught:
        resolve_status_observations(payload, StatusEvidenceDocument("PRIVATE_QUOTE. OTHER_QUOTE."))
    error = caught.value
    assert payload == before
    assert error.observations == (("START", ("E1", "E2")), ("END", ("E2",)))
    assert set(vars(error)) == {"reason_code", "field_locs", "_observations"}
    assert error.args == ("STATUS_OBSERVATION_TRANSITION_EVIDENCE_CONFLICT",)
    assert str(error) == error.reason_code
    assert "PRIVATE" not in repr(error) + json.dumps(vars(error))
    with pytest.raises(AttributeError):
        error.observations = ()
    with pytest.raises(TypeError):
        error.observations[0][1][0] = "E999"
    observations[0]["kind"] = "CHANGE"
    observations[0]["evidence_refs"][0] = "PRIVATE_REPLACEMENT"
    assert error.observations == (("START", ("E1", "E2")), ("END", ("E2",)))


@pytest.mark.parametrize("invalid", ["schema", "kind", "unknown_ref", "duplicate_ref"])
def test_conflict_hints_are_never_created_before_full_schema_and_literal_ref_validation(invalid):
    conflict = _state([_observation("START"), _observation("END")])
    later = _state()
    later["attribute_name"] = "status.other"
    observation = later["observations"][0]
    if invalid == "schema":
        observation["value_json"]["PRIVATE_FIELD"] = "PRIVATE_VALUE"
    elif invalid == "kind":
        observation["kind"] = "PRIVATE_KIND"
    elif invalid == "unknown_ref":
        observation["evidence_refs"] = ["PRIVATE_REF"]
    else:
        observation["evidence_refs"] = ["E1", "E1"]
    payload = {"states": [conflict, later]}
    before = deepcopy(payload)
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        resolve_status_observations(payload, StatusEvidenceDocument("현재 문장."))
    assert not isinstance(caught.value, StatusObservationConflictError)
    assert not hasattr(caught.value, "observations")
    assert "PRIVATE" not in repr(caught.value) + json.dumps(vars(caught.value))
    assert payload == before


def test_disjoint_transition_keeps_each_observation_value_and_exact_provenance():
    source = "  다쳤다. 계속 아팠다.\r\n  나았다. 다시 걸었다."
    start = _observation("START", ["E2", "E1"], {"name": " 발 부상 "})
    end = _observation("END", ["E4", "E3"], {"name": " 발 부상 "})
    end["attribute_value"] = "원형 종료 표시"
    payload = {"states": [_state([start, end])]}
    before = deepcopy(payload)
    result = resolve_status_observations(payload, StatusEvidenceDocument(source))
    assert payload == before
    assert result.kinds == ("START", "END") and result.state_indexes == (0, 0)
    candidates = _domain(result).candidates
    assert [c.attribute_value for c in candidates] == [start["attribute_value"], end["attribute_value"]]
    assert [c.value_json for c in candidates] == [
        {"name": " 발 부상 ", "active": True}, {"name": " 발 부상 ", "active": False},
    ]
    for candidate in candidates:
        for span in candidate.evidence_spans:
            assert source[span.start_offset:span.end_offset] == span.quote
