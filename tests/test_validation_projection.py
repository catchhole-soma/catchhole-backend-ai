"""GH349 amendment: compact model rows. Shapes follow the server projection of real baseline states (synthetic values)."""

import json
from copy import deepcopy

from app.validation.projection import compact_setting_row

SOURCE = {"sourceKind": "EPISODE", "sourceEpisodeNo": 3, "source_ref": "R1"}
EVIDENCE = {"availability": "VERIFIED", "quote": "민수는 왼손을 잃었다.", "start": 0, "end": 11, "source_ref": "R1"}


def character_row():
    """A server modelSettings character row after `toValidationModelState` (ids/hashes already stripped)."""
    slot = {"factType": "STATUS", "factKey": "손", "factValue": "왼손 상실", "valueJson": {"value": "왼손 상실"},
            "provenance": {"confirmationStatus": "CONFIRMED", "sourceEpisodeNo": 3, "reviewSource": "HUMAN",
                           "currentValueProtected": False, "currentSourceTimes": [SOURCE], **SOURCE},
            "supportingSources": [{"supportingSource": EVIDENCE, "narrativeTime": SOURCE}]}
    return {
        "entry_ref": "B1", "subject_ref": "S1", "domain": "characters", "name": "민수", "aliases": ["민수 씨"],
        "state": {
            "name": "민수", "aliases": ["민수 씨"], "protectedSlots": [{"factType": "STATUS", "factKey": "손"}],
            "snapshotVersion": 7, "currentNarrativeTime": {"referenceTimeKind": "EPISODE", "referenceEpisodeNo": 3,
                                                           "supportingSource": EVIDENCE},
            "historyFacts": [{"factType": "STATUS", "factKey": "손", "factValue": "양손 사용", "valueJson": None,
                              "operation": "ADD", "temporalScope": "PRESENT", "removedSnapshotEntries": [],
                              "narrativeTime": SOURCE, "supportingSource": EVIDENCE}],
            "slots": {"STATUS:손": slot,
                      "AGE:나이": {"factType": "AGE", "factKey": "나이", "factValue": "20", "temporalScope": "PRESENT",
                                 "effectiveFromEpisodeNo": 2, "valueJson": {"value": 20}}},
            "absences": {}, "provenance": {"confirmationStatus": "CONFIRMED", "reviewSource": "HUMAN"},
            "identityEvidence": [EVIDENCE], "currentSupportingEvidenceVersion": 1,
            "explicitAuthorCorrections": [{"kind": "AUTHOR_CORRECTION", "factKey": "손", "afterValue": "왼손 상실"}],
        },
        "author_correction": None,
    }


def test_character_row_keeps_identity_current_values_time_and_protection_only():
    row = character_row()
    original = deepcopy(row)
    assert compact_setting_row(row) == {
        "entry_ref": "B1", "subject_ref": "S1", "domain": "characters", "name": "민수", "aliases": ["민수 씨"],
        "state": {
            "name": "민수", "aliases": ["민수 씨"], "protectedSlots": [{"factType": "STATUS", "factKey": "손"}],
            "currentNarrativeTime": {"referenceTimeKind": "EPISODE", "referenceEpisodeNo": 3},
            "slots": {"STATUS:손": {"factType": "STATUS", "factKey": "손", "factValue": "왼손 상실"},
                      "AGE:나이": {"factType": "AGE", "factKey": "나이", "factValue": "20", "temporalScope": "PRESENT",
                                 "effectiveFromEpisodeNo": 2}},
            "absences": {},
        },
        "author_correction": None,
    }
    assert row == original  # pure: the canonical row (bundle, lookups) is never modified


def test_world_setting_and_world_history_rows_keep_values_and_drop_evidence():
    world = {"entry_ref": "B2", "subject_ref": "S2", "domain": "worldSettings", "name": "북부 왕국", "aliases": [],
             "state": {"category": "NATION", "subjectName": "북부 왕국", "normalizedSubjectName": "북부왕국", "version": 4,
                       # Author setting names are data: a property literally called "version" survives.
                       "propertiesJson": {"기후": "한랭", "version": "구판", "수도권": {"수도": "설원성"}},
                       "protectedPaths": [{"scopeName": None, "settingName": "기후"}],
                       "provenanceByPath": {'[null,"기후"]': {"confirmationStatus": "CONFIRMED",
                                                              "currentSourceTimes": [SOURCE], **SOURCE}},
                       "provenance": {"confirmationStatus": "CONFIRMED", **SOURCE}, "latestSourceEpisodeNo": 3},
             "author_correction": None}
    history = {"entry_ref": "B3", "subject_ref": "S2", "domain": "worldHistory", "name": "북부 왕국", "aliases": [],
               "state": {"category": "NATION", "subjectName": "북부 왕국", "scopeName": None, "settingName": "기후",
                         "value": "온화", "operation": "UPDATE", "historyOnly": False, "effectiveFromEpisodeNo": 1,
                         "temporalScope": "PRESENT", "sourceAvailability": "AVAILABLE", "evidenceSpans": [EVIDENCE],
                         "comparison": {"decision": "UPDATE", "reason": "긴 비교 기록"}, "narrativeTime": SOURCE,
                         **SOURCE}}
    assert compact_setting_row(world)["state"] == {
        "category": "NATION", "subjectName": "북부 왕국", "normalizedSubjectName": "북부왕국",
        "propertiesJson": {"기후": "한랭", "version": "구판", "수도권": {"수도": "설원성"}},
        "protectedPaths": [{"scopeName": None, "settingName": "기후"}], "latestSourceEpisodeNo": 3}
    compact = compact_setting_row(history)
    assert "author_correction" not in compact  # only rows that carry it keep it
    assert compact["state"] == {
        "category": "NATION", "subjectName": "북부 왕국", "scopeName": None, "settingName": "기후", "value": "온화",
        "operation": "UPDATE", "historyOnly": False, "effectiveFromEpisodeNo": 1, "temporalScope": "PRESENT",
        "sourceAvailability": "AVAILABLE", "narrativeTime": SOURCE, **SOURCE}


def test_production_shaped_large_character_entry_shrinks_to_its_slot_values():
    row = character_row()
    slot = row["state"]["slots"].pop("STATUS:손")
    for index in range(78):  # the largest production entry: 78 slots, a long history
        row["state"]["slots"][f"PROFILE:항목{index}"] = {**deepcopy(slot), "factKey": f"항목{index}"}
    row["state"]["historyFacts"] *= 200
    full = json.dumps(row, ensure_ascii=False)
    compact = json.dumps(compact_setting_row(row), ensure_ascii=False)
    assert len(compact.encode()) * 10 < len(full.encode())
    for key in ("historyFacts", "supportingSources", "supportingSource", "provenance", "identityEvidence",
                "valueJson", "snapshotVersion", "currentSupportingEvidenceVersion", "quote"):
        assert f'"{key}"' in full and f'"{key}"' not in compact
    assert compact.count('"factValue"') == 79
