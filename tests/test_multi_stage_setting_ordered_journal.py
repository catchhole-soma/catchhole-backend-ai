from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from evals.multi_stage_setting.ordered_journal import (
    JournalChange,
    SealedJournal,
    canonical_journal_json,
    journal_state_hash,
    load_journal_json,
    replay_sealed_journals,
)


RUN = UUID(int=1)
WORK = UUID(int=2)
EMPTY = {"characters": {}, "worldSettings": {}, "references": {}}


def _journal(changes, before, after, *, sequence=0, **overrides):
    return SealedJournal(
        **({
            "format_version": 1, "status": "SEALED", "run_id": RUN,
            "work_id": WORK, "generation": 1, "sequence": sequence,
            "episode_no": sequence + 1, "job_id": UUID(int=10 + sequence),
            "input_state_hash": journal_state_hash(before),
            "output_state_hash": journal_state_hash(after), "changes": changes,
        } | overrides)
    )


def _replay(journals, initial=EMPTY):
    return replay_sealed_journals(
        initial, journals, run_id=RUN, work_id=WORK, generation=1,
        before_sequence=len(journals), before_episode_no=len(journals) + 1,
    )


@pytest.mark.parametrize("version", [1, 2])
def test_shared_java_journal_fixture_replays_to_the_same_state_canonical_json_and_hash(version):
    fixture_path = Path(__file__).parent / "fixtures" / "multi_stage_setting_saved_predictions"
    fixture = load_journal_json((fixture_path / f"ordered-journal-v{version}.json").read_text())
    initial = fixture["baseState"]
    expected = fixture["expectedState"]
    changes = [JournalChange.model_validate(item) for item in fixture["changes"]]
    journal = _journal(changes, initial, expected, format_version=version)

    actual = _replay([journal], initial)

    assert actual == expected
    assert canonical_journal_json(actual) == fixture["expectedCanonical"]
    assert journal_state_hash(actual) == fixture["expectedHash"]
    if version == 1:
        assert fixture["expectedHash"] == (
            "df2482cd25db7efbf1f410e58698868e734620cab8fade8f88d877e3e18b8424"
        )


def test_java_compatible_hash_preserves_unicode_arrays_and_decimal_precision():
    parsed = load_journal_json('{"나":[1.00,1e-9,-0.00],"a":123456789012345678901234567890.100}')
    assert isinstance(parsed["a"], Decimal)
    assert canonical_journal_json(parsed) == (
        '{"a":123456789012345678901234567890.1,"나":[1,0.000000001,0]}'
    )
    assert canonical_journal_json({"\ue000": 1, "😀": 2}) == '{"😀":2,"\ue000":1}'
    assert journal_state_hash({"b": 1.0, "a": [2, 1]}) == journal_state_hash(
        {"a": [2, 1], "b": 1}
    )
    assert journal_state_hash({"a": [1, 2]}) != journal_state_hash({"a": [2, 1]})


def test_replay_keeps_order_provenance_and_source_values_without_mutating_inputs():
    target = "provisional:character:run:source"
    first = JournalChange(
        event_id="discovery", path=["characters", target], operation="DISCOVERY",
        value={"name": "가람", "confirmed": False, "sourceEpisodeNo": 1, "status": "injury"},
        source_candidate_ids=[UUID(int=7)],
    )
    s1 = deepcopy(EMPTY)
    s1["characters"][target] = first.value
    second = JournalChange(
        event_id="remove", path=["characters", target, "status"], operation="REMOVE", remove=True,
    )
    s2 = deepcopy(s1)
    del s2["characters"][target]["status"]
    journals = [_journal([first], EMPTY, s1), _journal([second], s1, s2, sequence=1)]
    frozen = deepcopy(journals)

    assert _replay(journals) == s2
    assert _replay(journals) == s2
    assert EMPTY == {"characters": {}, "worldSettings": {}, "references": {}}
    assert journals == frozen
    assert _replay(journals)["characters"][target]["confirmed"] is False


def test_identical_event_is_idempotent_but_reused_event_content_fails():
    add = JournalChange(event_id="e", path=["references", "r"], value={"a": 1}, operation="EXCLUDE")
    after = deepcopy(EMPTY) | {"references": {"r": {"a": 1}}}
    assert _replay([_journal([add, add], EMPTY, after)]) == after
    assert _replay([
        _journal([add], EMPTY, after), _journal([add], after, after, sequence=1)
    ]) == after
    changed = add.model_copy(update={"value": {"a": 2}})
    with pytest.raises(ValueError, match="eventId was reused"):
        _replay([_journal([add, changed], EMPTY, after)])


@pytest.mark.parametrize("field,value", [
    ("run_id", UUID(int=99)), ("work_id", UUID(int=99)), ("generation", 2),
    ("sequence", 1), ("episode_no", 2), ("input_state_hash", "stale"),
    ("output_state_hash", "stale"),
])
def test_replay_rejects_foreign_future_and_stale_journals(field, value):
    journal = _journal([], EMPTY, EMPTY, **{field: value})
    with pytest.raises(ValueError):
        _replay([journal])


@pytest.mark.parametrize("status", ["PENDING", "FAILED", "INVALIDATED"])
def test_only_sealed_journals_are_eligible(status):
    with pytest.raises(ValidationError):
        _journal([], EMPTY, EMPTY, status=status)


def test_missing_predecessor_and_missing_patch_parent_or_remove_target_fail():
    with pytest.raises(ValueError, match="Every predecessor"):
        replay_sealed_journals(
            EMPTY, [], run_id=RUN, work_id=WORK, generation=1,
            before_sequence=1, before_episode_no=2,
        )
    for change, error in [
        (JournalChange(event_id="e", path=["characters", "missing", "status"], value="x", operation="ADD"), "parent"),
        (JournalChange(event_id="e", path=["characters", "missing"], remove=True, operation="REMOVE"), "does not exist"),
    ]:
        with pytest.raises(ValueError, match=error):
            _replay([_journal([change], EMPTY, EMPTY)])


def test_set_null_or_invalid_root_cannot_be_replayed():
    for path, value in [(["characters", "c"], None), (["gold", "c"], {})]:
        with pytest.raises(ValidationError):
            JournalChange(event_id="e", path=path, value=value, operation="ADD")


def test_mixed_v1_v2_chain_preserves_history_order_duplicate_facts_evidence_and_inputs():
    initial = deepcopy(EMPTY)
    initial["characters"]["hero"] = {"historyFacts": [{"factValue": "첫 이력"}]}
    old_value = initial["characters"]["hero"]["historyFacts"] + [{"factValue": "구형 추가"}]
    legacy = JournalChange(event_id="legacy", path=["characters", "hero", "historyFacts"],
                           value=old_value, operation="UPDATE")
    after_legacy = deepcopy(initial)
    after_legacy["characters"]["hero"]["historyFacts"] = deepcopy(old_value)
    new_fact = {"factValue": "회복", "supportingSource": {
        "sourceEpisodeNo": 2, "evidenceSpans": [{"quote": "원문 근거 😀"}],
    }}
    append = JournalChange(event_id="append", path=legacy.path, value=[new_fact, new_fact],
                           operation="REMOVE", value_mode="APPEND_HISTORY")
    expected = deepcopy(after_legacy)
    expected["characters"]["hero"]["historyFacts"].extend(deepcopy(append.value))
    journals = [_journal([legacy], initial, after_legacy),
                _journal([append, append], after_legacy, expected, sequence=1, format_version=2)]
    frozen = deepcopy(journals)

    assert _replay(journals, initial) == expected
    assert _replay(journals, initial) == expected
    assert journals == frozen
    assert len(initial["characters"]["hero"]["historyFacts"]) == 1
    assert len(expected["characters"]["hero"]["historyFacts"]) == 4
    assert "valueMode" not in legacy.model_dump(by_alias=True)
    assert append.model_dump(by_alias=True)["valueMode"] == "APPEND_HISTORY"


def test_append_cannot_be_mislabelled_as_v1_or_reuse_replacement_event_id():
    initial = deepcopy(EMPTY) | {"characters": {"hero": {"historyFacts": []}}}
    append = JournalChange(event_id="e", path=["characters", "hero", "historyFacts"],
                           value=[], operation="UPDATE", value_mode="APPEND_HISTORY")
    with pytest.raises(ValidationError, match="storage version 2"):
        _journal([append], initial, initial)
    replace = JournalChange(event_id="e", path=append.path, value=[], operation="UPDATE")
    with pytest.raises(ValueError, match="eventId was reused"):
        _replay([_journal([replace, append], initial, initial, format_version=2)], initial)
    valid = _journal([append], initial, initial, format_version=2)
    with pytest.raises(ValidationError, match="storage version 2"):
        _replay([valid.model_copy(update={"format_version": 1})], initial)


@pytest.mark.parametrize("version", [0, 3, True, False, 1.0, "2", None, 4294967297])
def test_unknown_or_noninteger_journal_storage_versions_are_rejected(version):
    with pytest.raises(ValidationError):
        _journal([], EMPTY, EMPTY, format_version=version)


@pytest.mark.parametrize("overrides", [
    {"value_mode": "UNKNOWN"}, {"value_mode": None}, {"value": {}}, {"value": None},
    {"remove": True}, {"path": ["worldSettings", "hero", "historyFacts"]},
    {"path": ["characters", "hero", "slots"]},
    {"path": ["characters", "hero", "historyFacts", "nested"]},
])
def test_history_append_requires_the_character_history_array(overrides):
    with pytest.raises(ValidationError):
        JournalChange.model_validate({"event_id": "e", "path": ["characters", "hero", "historyFacts"],
                                      "value": [], "operation": "UPDATE", "value_mode": "APPEND_HISTORY"} | overrides)


def test_append_creates_missing_history_but_rejects_corrupt_existing_history_atomically():
    initial = deepcopy(EMPTY) | {"characters": {"hero": {}}}
    append = JournalChange(event_id="e", path=["characters", "hero", "historyFacts"],
                           value=[{"factValue": "첫 관측"}], operation="UPDATE", value_mode="APPEND_HISTORY")
    expected = deepcopy(EMPTY) | {"characters": {"hero": {"historyFacts": deepcopy(append.value)}}}
    assert _replay([_journal([append], initial, expected, format_version=2)], initial) == expected
    assert initial["characters"]["hero"] == {}
    for invalid_history in [None, {}, "invalid"]:
        corrupt = deepcopy(EMPTY) | {"characters": {"hero": {"historyFacts": invalid_history}}}
        saved = deepcopy(corrupt)
        with pytest.raises(ValueError, match="target must be an array"):
            _replay([_journal([append], corrupt, expected, format_version=2)], corrupt)
        assert corrupt == saved
