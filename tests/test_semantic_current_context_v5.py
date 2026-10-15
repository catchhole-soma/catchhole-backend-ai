"""Frozen claim carrier validation and advisory prompt serialization, offline only."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.semantic_current_context import (
    SemanticCurrentContext,
    prepare_semantic_current_context,
)

RUN = UUID("10000000-0000-0000-0000-000000000001")
C1 = UUID("10000000-0000-0000-0000-000000000002")
C2 = UUID("10000000-0000-0000-0000-000000000003")
S1 = UUID("10000000-0000-0000-0000-000000000004")
INPUT_HASH = "a" * 64
SOURCE_HASH = "b" * 64


def carrier_data():
    return {
        "formatVersion": 1, "runId": str(RUN), "generation": 4,
        "inputStateHash": INPUT_HASH, "sourceHash": SOURCE_HASH, "complete": True,
        "characters": [
            {"characterId": str(C1), "provisionalSubjectKey": None,
             "currentSlots": [
                 {"factType": "PROFILE", "factKey": "profile.직책",
                  "factValue": "동문 경비대장", "valueJson": {"value": "동문 경비대장"}},
                 {"factType": "AGE", "factKey": "age", "factValue": "28", "valueJson": 28},
                 {"factType": "LEVEL", "factKey": "level", "factValue": None, "valueJson": None},
             ]},
            {"characterId": str(C2), "provisionalSubjectKey": None,
             "currentSlots": [{"factType": "PROFILE", "factKey": "profile.직책",
                               "factValue": "선원", "valueJson": {"value": "선원"}}]},
        ],
        "worldSubjects": [{"subjectId": str(S1), "provisionalSubjectKey": None,
                           "category": "LOCATION", "subjectName": "은항",
                           "properties": [{"scopeName": "동문", "settingName": "통행세", "value": "두 닢"}]}],
    }


def identities():
    return [
        SimpleNamespace(character_id=C1, name="백솔", aliases=["기록관"],
                        identity_evidence=[SimpleNamespace(quote="기록관 백솔")]),
        SimpleNamespace(character_id=C2, name="백솔", aliases=["선원"],
                        identity_evidence=[SimpleNamespace(quote="선원 백솔")]),
    ]


def prepare(data=None, *, known=None, provisional=(), context=None, source_hash=SOURCE_HASH):
    return prepare_semantic_current_context(
        SemanticCurrentContext.model_validate(carrier_data() if data is None else data),
        analysis_context=context or SimpleNamespace(
            run_id=RUN, generation=4, input_state_hash=INPUT_HASH, source_hash=SOURCE_HASH,
        ),
        known_characters=identities() if known is None else known,
        provisional_characters=provisional,
        source_hash=source_hash,
    )


def test_current_display_json_and_same_name_owners_survive_without_internal_ids():
    input_context = prepare()
    chars = json.loads(input_context.character_prompt_json())["characters"]
    assert len(chars) == 2
    assert chars[0]["ref"] != chars[1]["ref"]
    assert [x["name"] for x in chars] == ["백솔", "백솔"]
    assert chars[0]["current_slots"][0]["fact_value"] == "동문 경비대장"
    assert chars[0]["current_slots"][0]["value_json"] == {"value": "동문 경비대장"}
    assert chars[0]["current_slots"][2]["fact_value"] is None
    assert chars[0]["current_slots"][2]["value_json"] is None
    text = input_context.character_prompt_json() + input_context.world_prompt_json()
    assert all(str(x) not in text for x in (RUN, C1, C2, S1))
    assert INPUT_HASH not in text and SOURCE_HASH not in text
    assert "provenance" not in text and "sourceCandidates" not in text


def test_valid_empty_current_is_not_missing_context():
    data = carrier_data(); data["characters"] = []; data["worldSubjects"] = []
    assert json.loads(prepare(data, known=[]).character_prompt_json())["characters"] == []
    with pytest.raises(OrderedInputContextError, match="missing"):
        prepare_semantic_current_context(None, analysis_context=SimpleNamespace(),
                                         known_characters=(), provisional_characters=(),
                                         source_hash=SOURCE_HASH)


@pytest.mark.parametrize("field,value", [
    ("generation", 3), ("inputStateHash", "c" * 64), ("sourceHash", "c" * 64),
    ("runId", "10000000-0000-0000-0000-000000000099"),
])
def test_stale_carrier_rejected_before_use(field, value):
    data = carrier_data(); data[field] = value
    with pytest.raises(OrderedInputContextError): prepare(data)


def test_claimed_source_is_separately_fenced():
    with pytest.raises(OrderedInputContextError): prepare(source_hash="c" * 64)


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("characters"),
    lambda d: d.update(complete=False),
    lambda d: d.update(complete=1),
    lambda d: d.update(formatVersion=2),
    lambda d: d.update(generation=True),
    lambda d: d.update(extra="ignored?"),
    lambda d: d["characters"][0].pop("provisionalSubjectKey"),
    lambda d: d["characters"][0].update(provisionalSubjectKey="other"),
    lambda d: d["characters"][0].update(characterId=None),
    lambda d: d["characters"][0]["currentSlots"][0].pop("factValue"),
    lambda d: d["characters"][0]["currentSlots"][0].update(factValue=2),
    lambda d: d["characters"][0]["currentSlots"][0].update(factType="UNKNOWN"),
    lambda d: d["characters"].append(deepcopy(d["characters"][0])),
    lambda d: d["characters"][0]["currentSlots"].append(deepcopy(d["characters"][0]["currentSlots"][0])),
    lambda d: d["worldSubjects"].append(deepcopy(d["worldSubjects"][0])),
    lambda d: d["worldSubjects"][0]["properties"].append(deepcopy(d["worldSubjects"][0]["properties"][0])),
    lambda d: d["worldSubjects"][0].update(category="UNKNOWN"),
    lambda d: d["worldSubjects"][0]["properties"][0].update(value=None),
    lambda d: d["worldSubjects"][0]["properties"][0].update(value={"value": "two"}),
    lambda d: d["worldSubjects"][0]["properties"][0].update(value=2),
])
def test_strict_shape_owner_xor_and_duplicate_rejection(mutate):
    data = carrier_data(); mutate(data)
    with pytest.raises((ValidationError, OrderedInputContextError)): prepare(data)


def test_missing_and_unknown_character_identity_rejected():
    with pytest.raises(OrderedInputContextError): prepare(known=identities()[:1])
    data = carrier_data(); data["characters"] = data["characters"][:1]
    with pytest.raises(OrderedInputContextError): prepare(data)


def test_provisional_current_uses_same_claim_identity_without_exposing_key():
    key = "provisional-character:10000000-0000-0000-0000-000000000005"
    data = carrier_data(); data["characters"] = [
        {"characterId": None, "provisionalSubjectKey": key,
         "currentSlots": []},
    ]
    provisional = [SimpleNamespace(provisional_subject_key=key,
                                   name="청류", aliases=[], identity_evidence=[])]
    result = prepare(data, known=[], provisional=provisional)
    assert "청류" in result.character_prompt_json()
    assert key not in result.character_prompt_json()


@pytest.mark.parametrize("domain,key", [
    ("character", "provisional-world:10000000-0000-0000-0000-000000000005"),
    ("character", "provisional-character:invented"),
    ("world", "provisional-character:10000000-0000-0000-0000-000000000005"),
    ("world", "provisional-world:invented"),
])
def test_provisional_ref_requires_domain_prefix_and_uuid(domain, key):
    data = carrier_data()
    if domain == "character":
        data["characters"][0].update(characterId=None, provisionalSubjectKey=key)
    else:
        data["worldSubjects"][0].update(subjectId=None, provisionalSubjectKey=key)
    with pytest.raises(ValidationError):
        SemanticCurrentContext.model_validate(data)


def test_existing_time_enum_remains_read_only_current_not_new_extraction_domain():
    data = carrier_data()
    data["characters"][0]["currentSlots"].append(
        {"factType": "TIME", "factKey": "legacy_time", "factValue": "어제", "valueJson": None},
    )
    assert '"fact_type":"TIME"' in prepare(data).character_prompt_json()


def test_explicit_inactive_status_is_preserved_without_inferring_current_policy():
    data = carrier_data()
    status = {"active": False, "severity": None}
    data["characters"][0]["currentSlots"].append(
        {"factType": "STATUS", "factKey": "status.손상", "factValue": "종료", "valueJson": status},
    )
    slots = json.loads(prepare(data).character_prompt_json())["characters"][0]["current_slots"]
    assert slots[-1] == {"fact_type": "STATUS", "fact_key": "status.손상",
                         "fact_value": "종료", "value_json": status}


def test_all_age_level_slots_count_toward_4096_limit():
    data = carrier_data(); data["worldSubjects"] = []
    data["characters"] = data["characters"][:1]
    data["characters"][0]["currentSlots"] = [
        {"factType": "PROFILE", "factKey": f"profile.축{i}", "factValue": "값", "valueJson": None}
        for i in range(4095)
    ] + [
        {"factType": "AGE", "factKey": "age", "factValue": "1", "valueJson": 1},
        {"factType": "LEVEL", "factKey": "level", "factValue": "1", "valueJson": 1},
    ]
    with pytest.raises((ValidationError, OrderedInputContextError)): prepare(data, known=identities()[:1])


def test_total_4096_includes_character_age_level_and_world_property_at_inclusive_boundary():
    data = carrier_data(); data["characters"] = data["characters"][:1]
    data["characters"][0]["currentSlots"] = [
        {"factType": "PROFILE", "factKey": f"profile.축{i}", "factValue": "값", "valueJson": None}
        for i in range(4093)
    ] + [
        {"factType": "AGE", "factKey": "age", "factValue": "1", "valueJson": 1},
        {"factType": "LEVEL", "factKey": "level", "factValue": "1", "valueJson": 1},
    ]
    valid = SemanticCurrentContext.model_validate(data)
    assert len(valid.characters[0].current_slots) + len(valid.world_subjects[0].properties) == 4096
    data["worldSubjects"][0]["properties"].append({"scopeName": None, "settingName": "독립 항목", "value": "값"})
    with pytest.raises(ValidationError, match="item limit"):
        SemanticCurrentContext.model_validate(data)


def test_entire_serialized_carrier_1mib_is_inclusive_and_next_byte_fails():
    data = carrier_data(); data["worldSubjects"][0]["properties"][0]["value"] = ""
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    data["worldSubjects"][0]["properties"][0]["value"] = "x" * (1_048_576 - len(encoded))
    valid = SemanticCurrentContext.model_validate(data)
    assert len(json.dumps(valid.model_dump(mode="json", by_alias=True), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode()) == 1_048_576
    data["worldSubjects"][0]["properties"][0]["value"] += "x"
    with pytest.raises(ValidationError, match="byte limit"):
        SemanticCurrentContext.model_validate(data)


@pytest.mark.parametrize("field,value", [("scopeName", " 동문 "), ("settingName", " 통행세 ")])
def test_normalized_duplicate_world_path_is_rejected(field, value):
    data = carrier_data()
    duplicate = deepcopy(data["worldSubjects"][0]["properties"][0]); duplicate[field] = value
    data["worldSubjects"][0]["properties"].append(duplicate)
    with pytest.raises(ValidationError, match="duplicate property path"):
        SemanticCurrentContext.model_validate(data)


def test_entire_utf8_carrier_is_bounded_including_world_owner_name():
    data = carrier_data(); data["worldSubjects"][0]["properties"][0]["value"] = "한" * 350000
    with pytest.raises((ValidationError, OrderedInputContextError)): prepare(data)


def test_provider_snapshot_does_not_change_if_raw_transport_is_mutated_later():
    data = carrier_data()
    result = prepare(data)
    before = result.character_prompt_json()
    data["characters"][0]["currentSlots"][0]["valueJson"]["value"] = "다른 직책"
    assert result.character_prompt_json() == before
