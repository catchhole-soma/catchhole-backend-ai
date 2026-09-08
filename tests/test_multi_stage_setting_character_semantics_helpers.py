import json
from copy import deepcopy
from decimal import Decimal

import pytest

from app.analysis.setting_extractor import CharacterSettingSchemaHint
from evals.multi_stage_setting.character_semantics import (
    StructuredTextPair,
    character_setting_ref_mapping,
    compare_structured_semantics,
    dynamic_status_key_pair,
)
from evals.multi_stage_setting.contracts import character_state_ref


def _schema(key="statuses.dynamic", pattern="status.*", *, aliases=(), fact_type="STATUS"):
    return CharacterSettingSchemaHint(
        schema_key=key,
        display_name="상태",
        attribute_pattern=pattern,
        aliases=aliases,
        value_type="JSON",
        canonical_fact_type=fact_type,
    )


def test_dynamic_status_keys_use_the_same_unique_schema_pattern() -> None:
    assert dynamic_status_key_pair(
        "STATUS", "status.오른발_부상", "status.오른쪽_발_상처", schema_hints=[_schema()]
    )
    assert dynamic_status_key_pair("STATUS", "status.부상", "status.상처")


@pytest.mark.parametrize(
    ("fact_type", "expected", "actual"),
    [
        ("PROFILE", "profile.species", "profile.race"),
        ("SKILL", "skill.검술", "skill.검_다루기"),
        ("STATUS", "status.부상", "profile.부상"),
        ("STATUS", "status.부상", "status."),
        ("STATUS", "status.부상", "status.*"),
        ("STATUS", "status.부상", None),
        ("STATUS", "status.머리 부상", "status.머리_부상"),
    ],
)
def test_only_differing_dynamic_status_keys_are_semantic_candidates(
    fact_type, expected, actual
) -> None:
    assert not dynamic_status_key_pair(fact_type, expected, actual)


@pytest.mark.parametrize(
    "fixed",
    [
        _schema("status.부상", None),
        _schema("status.injury", None, aliases=("부상",)),
    ],
)
def test_fixed_schema_and_alias_precede_dynamic_patterns(fixed) -> None:
    assert not dynamic_status_key_pair(
        "STATUS", "status.부상", "status.상처", schema_hints=[_schema(), fixed]
    )


def test_missing_ambiguous_and_different_schema_patterns_are_not_eligible() -> None:
    assert not dynamic_status_key_pair(
        "STATUS", "status.부상", "status.상처", schema_hints=[_schema(pattern="skill.*")]
    )
    assert not dynamic_status_key_pair(
        "STATUS", "status.부상", "status.상처", schema_hints=[_schema(), _schema()]
    )
    assert not dynamic_status_key_pair(
        "STATUS", "status.부상", "status.상처", schema_hints=[_schema(fact_type="SKILL")]
    )
    assert not dynamic_status_key_pair(
        "STATUS",
        "condition.부상",
        "status.상처",
        schema_hints=[_schema(pattern="condition.*"), _schema()],
    )


def test_explicit_custom_status_pattern_is_supported_without_guessing() -> None:
    assert dynamic_status_key_pair(
        "STATUS",
        "condition.부상",
        "condition.상처",
        schema_hints=[_schema("custom.condition", "condition.*")],
    )
    assert not dynamic_status_key_pair("STATUS", "condition.부상", "condition.상처")


def _ref(key, *, kind="fact", entity="character:bjorn", fact_type="STATUS"):
    if kind in {"history", "history-json"}:
        return (
            kind + ":" + json.dumps(["S1", "C1", entity, fact_type, key, "ADD"], ensure_ascii=False)
        )
    return kind + ":" + character_state_ref(entity, fact_type, key)


@pytest.mark.parametrize("kind", ["fact", "fact-json", "history", "history-json"])
def test_ref_mapping_aligns_approved_dynamic_names_without_mutating_inputs(kind) -> None:
    expected = {_ref("status.오른발:부상", kind=kind)}
    actual = {_ref("status.오른발:상처", kind=kind)}
    pairs = [(next(iter(expected)), next(iter(actual)))]
    original = deepcopy((expected, actual, pairs))

    assert character_setting_ref_mapping(expected, actual, pairs) == {
        next(iter(actual)): next(iter(expected))
    }
    assert (expected, actual, pairs) == original


def test_exact_ref_wins_and_duplicate_semantic_state_remains_extra() -> None:
    canonical, alias = _ref("status.부상"), _ref("status.상처")
    values = {canonical: "부상", alias: "중복"}
    mapping = character_setting_ref_mapping({canonical}, set(values), [(canonical, alias)])

    assert mapping == {canonical: canonical}
    assert {mapping.get(ref, ref): value for ref, value in values.items()} == values


def test_ref_mapping_rejects_both_ambiguity_directions_and_keeps_independent_match() -> None:
    a, b, c = [_ref("status." + name) for name in ["부상", "독", "피로"]]
    x, y, z = [_ref("status." + name) for name in ["상처", "손상", "탈진"]]
    assert character_setting_ref_mapping({a, c}, {x, y, z}, [(a, x), (a, y), (c, z)]) == {z: c}
    assert character_setting_ref_mapping({a, b}, {x}, [(a, x), (b, x)]) == {}
    assert character_setting_ref_mapping({a}, {x}, [(a, x), (a, x)]) == {x: a}


@pytest.mark.parametrize(
    "actual",
    [
        _ref("status.상처", entity="character:thor"),
        _ref("status.상처", fact_type="PROFILE"),
        _ref("status.상처", kind="fact-json"),
        _ref("status.상처", kind="history"),
        "known-character:character:bjorn",
        "fact:gold:world:WORLD_RULE_HISTORY:게임:규칙",
        "fact:gold:character:character%3Abjorn:STATUS:",
    ],
)
def test_approved_pair_cannot_cross_structural_identity(actual) -> None:
    expected = _ref("status.부상")
    assert character_setting_ref_mapping({expected}, {actual}, [(expected, actual)]) == {}


def test_history_mapping_preserves_scenario_source_and_operation() -> None:
    expected, actual = _ref("status.부상", kind="history"), _ref("status.상처", kind="history")
    for old, new in [("S1", "S2"), ("C1", "C2"), ("ADD", "REMOVE")]:
        changed = actual.replace('"' + old + '"', '"' + new + '"')
        assert character_setting_ref_mapping({expected}, {changed}, [(expected, changed)]) == {}


def test_absent_refs_and_fixed_key_renames_are_not_created_or_approved() -> None:
    expected, actual, absent = [_ref("status." + name) for name in ["부상", "상처", "피로"]]
    assert character_setting_ref_mapping({expected}, {actual}, [(expected, absent)]) == {}
    fixed, alternate = (
        _ref("profile.species", fact_type="PROFILE"),
        _ref("profile.race", fact_type="PROFILE"),
    )
    assert character_setting_ref_mapping({fixed}, {alternate}, [(fixed, alternate)]) == {}


def test_structured_comparison_collects_narrative_leaves_with_json_pointer_paths() -> None:
    expected = {"value": "다리에 상처가 있다", "a/b~c": [{"description": "이동이 힘들다"}]}
    actual = {"value": "다리가 다쳤다", "a/b~c": [{"description": "걷기 어렵다"}], "extra": "허용"}
    original = deepcopy((expected, actual))

    comparison = compare_structured_semantics(expected, actual)

    assert comparison.structural_matched is True
    assert comparison.text_pairs == (
        StructuredTextPair("/value", "다리에 상처가 있다", "다리가 다쳤다"),
        StructuredTextPair("/a~1b~0c/0/description", "이동이 힘들다", "걷기 어렵다"),
    )
    assert (expected, actual) == original


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ({"value": 3}, {"value": "3"}),
        ({"value": 3}, {"value": 4}),
        ({"value": 1}, {"value": True}),
        ({"active": True}, {"active": 1}),
        ({"active": False}, {"active": "false"}),
        ({"active": False}, {"active": True}),
        ({"value": "3"}, {"value": 3}),
        ({"value": None}, {"value": "null"}),
        ({"value": "서술"}, {}),
        ({"items": [1, 2]}, {"items": [2, 1]}),
        ({"items": [1]}, {"items": [1, 2]}),
        ({"items": []}, {"items": {}}),
    ],
)
def test_structured_types_scalar_values_required_fields_and_lists_stay_strict(expected, actual):
    assert compare_structured_semantics(expected, actual).structural_matched is False


def test_equivalent_scalars_normalized_text_and_extra_fields_need_no_semantic_call() -> None:
    result = compare_structured_semantics(
        {"name": "  바바리안 ", "score": Decimal("3.0"), "active": False, "empty": None},
        {"name": "바바리안", "score": 3, "active": False, "empty": None, "extra": "허용"},
    )
    assert result.structural_matched is True
    assert result.text_pairs == ()


def test_structural_failure_cannot_be_rescued_by_matching_narrative_leaves() -> None:
    result = compare_structured_semantics(
        {"active": True, "description": "머리가 아프다"},
        {"active": False, "description": "두통이 있다"},
    )
    assert result.structural_matched is False
    assert result.text_pairs == (
        StructuredTextPair("/description", "머리가 아프다", "두통이 있다"),
    )


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "ref",
        "factKey",
        "fact_type",
        "valueType",
        "entity_id",
        "operation",
        "temporalScope",
        "status",
        "unit",
        "currency",
    ],
)
def test_named_identity_and_enum_string_fields_stay_exact(field) -> None:
    result = compare_structured_semantics({field: "PRESENT"}, {field: "present"})
    assert result.structural_matched is False
    assert result.text_pairs == ()


def test_structural_string_lists_and_custom_schema_paths_remain_strict() -> None:
    result = compare_structured_semantics(
        {"removedRefs": ["abc"], "metadata": {"custom": "fixed"}},
        {"removedRefs": ["ABC"], "metadata": {"custom": "different"}},
        strict_text_paths={"/metadata/custom"},
    )
    assert result.structural_matched is False
    assert result.text_pairs == ()
