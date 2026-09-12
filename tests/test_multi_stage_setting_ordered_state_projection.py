from copy import deepcopy
from decimal import Decimal

import pytest

from evals.multi_stage_setting.ordered_state_projection import project_backend_state


ACTUAL = "00000000-0000-0000-0000-000000000001"
TEMPORARY = "provisional-character:00000000-0000-0000-0000-000000000002"


def _state():
    return {
        "characters": {
            f"character:{ACTUAL}": {
                "name": "가람", "actualCharacterId": ACTUAL, "provisionalSubjectKey": None,
                "slots": {"STATUS:status.arm": {
                    "factType": "STATUS", "factKey": "status.arm", "factValue": "왼팔 부상",
                    "valueJson": {"active": True, "value": "왼팔 부상"},
                    "provenance": {"confirmationStatus": "CONFIRMED", "sourceEpisodeNo": 1},
                }},
            },
            TEMPORARY: {
                "name": "가람", "actualCharacterId": None, "provisionalSubjectKey": TEMPORARY,
                "slots": {}, "provenance": {"confirmationStatus": "PROVISIONAL"},
            },
        },
        "worldSettings": {
            f"world:{ACTUAL}": {
                "actualWorldSettingId": ACTUAL, "provisionalSubjectKey": None,
                "category": "LOCATION", "subjectName": "등대",
                "propertiesJson": {"위치": "바닷가", "불빛": {"색": "파랑", "밝기": "강함"}},
            },
        },
        "references": {
            "history": {
                "domain": "characters", "operation": "HISTORY_ONLY", "targetRef": TEMPORARY,
                "sourceEpisodeNo": 2, "candidateId": "candidate-2", "factType": "ITEM",
                "factKey": "item.potion", "factValue": "포션 사용", "valueJson": {"value": "포션 사용"},
                "temporalScope": "PRESENT",
            },
            "review": {"domain": "characters", "operation": "REVIEW_REQUIRED"},
            "exclude": {"domain": "worldSettings", "operation": "EXCLUDE"},
        },
    }


def test_domain_projection_preserves_separate_same_name_targets_full_paths_and_history():
    state = _state()
    original = deepcopy(state)

    projected = project_backend_state(state, scenario_id_by_episode={1: "S1", 2: "S2"})

    assert state == original
    assert len(projected.known_characters) == 2
    assert {item.entity_ref for item in projected.known_characters} == {f"character:{ACTUAL}", TEMPORARY}
    assert len(projected.character_facts) == 1
    assert projected.character_history[0].source_gold_id == "prediction:candidate-2"
    assert projected.character_history[0].value == "포션 사용"
    assert {(item.scope_name, item.setting_name) for item in projected.world_facts} == {
        (None, "위치"), ("불빛", "색"), ("불빛", "밝기"),
    }
    assert all(item.subject_ref == f"world:{ACTUAL}" for item in projected.world_facts)


def test_projection_never_impersonates_provisional_ref_as_actual_id():
    state = _state()
    target = state["characters"].pop(TEMPORARY)
    target["provisionalSubjectKey"] = "character:forged"
    state["characters"]["character:forged"] = target

    with pytest.raises(ValueError, match="distinct namespace"):
        project_backend_state(state, scenario_id_by_episode={2: "S2"})


def test_history_snapshot_incomplete_data_fails_instead_of_borrowing_gold():
    state = _state()
    del state["references"]["history"]["factKey"]
    with pytest.raises(KeyError):
        project_backend_state(state, scenario_id_by_episode={2: "S2"})


def test_private_human_rejection_policy_is_never_projected_as_future_history_or_fact():
    state = _state()
    expected = project_backend_state(state, scenario_id_by_episode={2: "S2"})
    state["references"]["human-rejection:future"] = {
        "kind": "HUMAN_REJECTION_POLICY", "policy": "EXACT_SOURCE_CLAIM_V1",
        "domain": "characters", "fingerprint": "a" * 64,
        "rejectedEpisodeNo": 10, "rejectedCandidateId": "old-candidate",
        # Even accidental history-like metadata cannot make a private constraint an effect.
        "operation": "HISTORY_ONLY",
    }
    assert project_backend_state(state, scenario_id_by_episode={2: "S2"}) == expected


def test_decimal_projection_keeps_json_number_types_and_rejects_precision_loss():
    state = _state()
    slot = state["characters"][f"character:{ACTUAL}"]["slots"]["STATUS:status.arm"]
    slot["valueJson"] = {"value": Decimal("1.2300"), "large": Decimal("123456789012345678901234567890")}
    projected = project_backend_state(state, scenario_id_by_episode={2: "S2"})
    value = projected.character_facts[0].value_json
    assert value == {"value": 1.23, "large": 123456789012345678901234567890}
    assert isinstance(value["value"], float)
    projected.content_hash()
    slot["valueJson"] = {"value": Decimal("12345678901234567890.123456789")}
    with pytest.raises(ValueError, match="precision loss"):
        project_backend_state(state, scenario_id_by_episode={2: "S2"})
