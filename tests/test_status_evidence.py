import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.schemas import (
    CharacterSettingProviderResponse,
    character_setting_provider_json_schema,
)
from app.analysis.status_evidence import StatusEvidenceDocument, StatusEvidenceReferenceError


def _candidate(refs=None):
    return {
        "candidate_kind": "SETTING",
        "entity_type": "CHARACTER",
        "entity_name": "리안",
        "raw_entity_mention": "그",
        "attribute_name": "status.발_부상",
        "attribute_value": "발 부상 종료",
        "value_type": "JSON",
        "value_json": {"extra_json": '{"name":"발 부상","active":false}'},
        "confidence": 0.9,
        "evidence_refs": ["E1"] if refs is None else refs,
    }


def test_sentence_units_keep_repeated_observations_at_distinct_offsets():
    source = "리안은 다쳤다. 리안은 나았다. 리안은 다쳤다."
    document = StatusEvidenceDocument(source)

    assert document.references == ("E1", "E2", "E3")
    assert [unit.text for unit in document.units] == [
        "리안은 다쳤다.", "리안은 나았다.", "리안은 다쳤다.",
    ]
    payload = {"candidates": [_candidate(["E1"]), _candidate(["E2"]), _candidate(["E3"])]}
    spans = [candidate["evidence_spans"][0]
             for candidate in document.resolve_payload(payload)["candidates"]]
    assert spans[0]["quote"] == spans[2]["quote"]
    assert [span["start_offset"] for span in spans] == [0, 9, 18]
    assert spans[2]["start_offset"] != source.find(spans[2]["quote"])
    for span in spans:
        assert source[span["start_offset"]:span["end_offset"]] == span["quote"]


def test_crlf_blank_lines_indentation_and_internal_whitespace_preserve_source_offsets():
    source = "\r\n  처음  다쳤다.\r\n\r\n\t약을 먹었다!  통증이 줄었다.\r\n  다시 걸었다.  \r\n"
    document = StatusEvidenceDocument(source)

    assert [unit.text for unit in document.units] == [
        "처음  다쳤다.", "약을 먹었다!", "통증이 줄었다.", "다시 걸었다.",
    ]
    for unit in document.units:
        assert unit.start_offset == source.index(unit.text)
        assert source[unit.start_offset:unit.end_offset] == unit.text
    assert document.units[1].start_offset > len("\n  처음  다쳤다.\n\n\t")
    assert json.loads(document.render()) == [
        {"ref": unit.ref, "text": unit.text} for unit in document.units
    ]


@pytest.mark.parametrize("source, expected", [
    ('“아프다!” 그는 멈췄다. (다시 걸었다.) 넘어졌다.',
     ['“아프다!”', '그는 멈췄다.', '(다시 걸었다.)', '넘어졌다.']),
    ('「痛い！」 まだ痛い。 治った？ はい。', ['「痛い！」', 'まだ痛い。', '治った？', 'はい。']),
    ('체온은 38.5도였다. 이제 37.0도다.', ['체온은 38.5도였다.', '이제 37.0도다.']),
    ('멈추었다\n다시 움직였다', ['멈추었다', '다시 움직였다']),
])
def test_explicit_sentence_boundaries(source, expected):
    document = StatusEvidenceDocument(source)
    assert [unit.text for unit in document.units] == expected
    for unit in document.units:
        assert source[unit.start_offset:unit.end_offset] == unit.text


def test_decode_preserves_other_fields_and_input_and_reuses_existing_provider_contract():
    document = StatusEvidenceDocument("약을 먹었다. 계속 아팠다. 나중에 걸었다.")
    payload = {"candidates": [_candidate(["E3", "E1"])]}
    before = deepcopy(payload)

    decoded = document.resolve_payload(payload)
    assert payload == before
    assert {key: value for key, value in decoded["candidates"][0].items()
            if key != "evidence_spans"} == {
                key: value for key, value in before["candidates"][0].items()
                if key != "evidence_refs"
            }
    assert [span["quote"] for span in decoded["candidates"][0]["evidence_spans"]] == [
        "나중에 걸었다.", "약을 먹었다.",
    ]
    result = CharacterSettingProviderResponse.model_validate(decoded).to_extraction_result(UUID(int=1))
    assert result.candidates[0].value_json == {"name": "발 부상", "active": False}
    assert result.candidates[0].evidence_spans[0].start_offset == document.units[2].start_offset
    decoded["candidates"][0]["value_json"]["extra_json"] = "{}"
    assert payload == before


def test_repeated_text_references_in_one_candidate_are_distinct_occurrences():
    document = StatusEvidenceDocument("아팠다. 나았다. 아팠다.")
    decoded = document.resolve_payload({"candidates": [_candidate(["E1", "E3"])]})
    spans = decoded["candidates"][0]["evidence_spans"]
    assert len(spans) == 2
    assert spans[0]["quote"] == spans[1]["quote"]
    assert spans[0]["start_offset"] < spans[1]["start_offset"]


def test_schema_is_strict_and_request_local_without_mutating_cached_general_schema():
    original = deepcopy(character_setting_provider_json_schema())
    document = StatusEvidenceDocument("다쳤다. 나았다.")
    schema = document.response_schema

    assert schema.strict is True
    assert schema.name == "character_status_extraction"
    nodes = list(_schema_nodes(schema.schema))
    candidates = [node for node in nodes if "evidence_refs" in node.get("properties", {})]
    assert len(candidates) == 6
    reference_definition = "_StatusEvidenceReference"
    assert schema.schema["$defs"][reference_definition] == {
        "type": "string", "enum": ["E1", "E2"],
    }
    assert len([node for node in nodes if node.get("enum") == ["E1", "E2"]]) == 1
    for candidate in candidates:
        refs = candidate["properties"]["evidence_refs"]
        assert refs == {"type": "array", "items": {"$ref": f"#/$defs/{reference_definition}"},
                        "minItems": 1}
        assert "evidence_spans" not in candidate["properties"]
    for node in nodes:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        assert "oneOf" not in node
        assert "discriminator" not in node
    serialized = json.dumps(schema.schema)
    assert '"source_chunk_id"' not in serialized
    assert '"quote"' not in serialized
    assert '"start_offset"' not in serialized
    schema.schema["$defs"][reference_definition]["enum"].append("FORGED")
    assert "FORGED" not in json.dumps(document.response_schema.schema)
    assert character_setting_provider_json_schema() == original
    assert '"E2"' not in json.dumps(StatusEvidenceDocument("다쳤다.").response_schema.schema)


@pytest.mark.parametrize("refs, code, loc", [
    ([], "INVALID_REFS", "candidates.0.evidence_refs"),
    (None, "INVALID_REFS", "candidates.0.evidence_refs"),
    ("E1", "INVALID_REFS", "candidates.0.evidence_refs"),
    ([True], "INVALID_REFS", "candidates.0.evidence_refs.0"),
    ([1], "INVALID_REFS", "candidates.0.evidence_refs.0"),
    ([{}], "INVALID_REFS", "candidates.0.evidence_refs.0"),
    (["E999"], "UNKNOWN_REF", "candidates.0.evidence_refs.0"),
    (["previous_episode:E1"], "UNKNOWN_REF", "candidates.0.evidence_refs.0"),
    (["E1 "], "UNKNOWN_REF", "candidates.0.evidence_refs.0"),
    (["PRIVATE_NOVEL_TEXT"], "UNKNOWN_REF", "candidates.0.evidence_refs.0"),
    (["E1", "E1"], "DUPLICATE_REF", "candidates.0.evidence_refs.1"),
])
def test_invalid_references_raise_only_safe_code_and_index(refs, code, loc):
    document = StatusEvidenceDocument("PRIVATE_NOVEL_TEXT")
    candidate = _candidate()
    candidate["evidence_refs"] = refs
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        document.resolve_payload({"candidates": [candidate]})
    assert caught.value.reason_code == f"STATUS_EVIDENCE_{code}"
    assert caught.value.field_locs == (loc,)
    assert str(caught.value) == f"STATUS_EVIDENCE_{code}"
    assert "PRIVATE_NOVEL_TEXT" not in repr(caught.value)


@pytest.mark.parametrize("payload, loc", [
    (None, "candidates"),
    ([], "candidates"),
    ({}, "candidates"),
    ({"candidates": None}, "candidates"),
    ({"candidates": [], "PRIVATE_NOVEL_TEXT": "secret"}, "candidates"),
    ({"candidates": ["PRIVATE_NOVEL_TEXT"]}, "candidates.0"),
    ({"candidates": [{**_candidate(), "evidence_spans": []}]}, "candidates.0.evidence_spans"),
])
def test_invalid_payload_shape_is_rejected_without_source_values(payload, loc):
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        StatusEvidenceDocument("PRIVATE_NOVEL_TEXT").resolve_payload(payload)
    assert caught.value.reason_code == "STATUS_EVIDENCE_INVALID_PAYLOAD"
    assert caught.value.field_locs == (loc,)
    assert "PRIVATE_NOVEL_TEXT" not in repr(caught.value)


def test_missing_references_are_rejected_and_later_failure_does_not_mutate_payload():
    candidate = _candidate()
    del candidate["evidence_refs"]
    payload = {"candidates": [_candidate(), candidate]}
    before = deepcopy(payload)
    with pytest.raises(StatusEvidenceReferenceError) as caught:
        StatusEvidenceDocument("다쳤다.").resolve_payload(payload)
    assert caught.value.field_locs == ("candidates.1.evidence_refs",)
    assert payload == before


@pytest.mark.parametrize("source", ["", " \r\n\t\n"])
def test_empty_document_can_resolve_empty_output_but_cannot_build_provider_schema(source):
    document = StatusEvidenceDocument(source)
    assert document.references == ()
    assert document.units == ()
    assert document.render() == "[]"
    assert document.resolve_payload({"candidates": []}) == {"candidates": []}
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_EVIDENCE_EMPTY_DOCUMENT"):
        _ = document.response_schema
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_EVIDENCE_UNKNOWN_REF"):
        document.resolve_payload({"candidates": [_candidate()]})


def test_none_document_is_not_silently_converted_to_source_text():
    with pytest.raises(StatusEvidenceReferenceError, match="STATUS_EVIDENCE_INVALID_DOCUMENT"):
        StatusEvidenceDocument(None)


def test_document_with_source_allows_no_status_observations():
    assert StatusEvidenceDocument("그는 문을 보았다.").resolve_payload({"candidates": []}) == {
        "candidates": [],
    }


def _schema_nodes(node):
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from _schema_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _schema_nodes(child)
