import pytest

from evals.multi_stage_setting.contracts import world_state_ref
from evals.multi_stage_setting.world_name_state import world_setting_ref_mapping


def test_approved_setting_name_ref_is_aligned_without_mutating_inputs() -> None:
    expected = {_ref("전투 위험성")}
    actual = {_ref("전투 규칙")}
    pairs = [(_ref("전투 위험성"), _ref("전투 규칙"))]

    mapping = world_setting_ref_mapping(expected, actual, pairs)

    assert mapping == {_ref("전투 규칙"): _ref("전투 위험성")}
    assert expected == {_ref("전투 위험성")}
    assert actual == {_ref("전투 규칙")}
    assert pairs == [(_ref("전투 위험성"), _ref("전투 규칙"))]


def test_exact_ref_has_priority_and_duplicate_semantic_fact_stays_extra() -> None:
    canonical = _ref("전투 위험성")
    alias = _ref("전투 규칙")
    actual_values = {canonical: "내용", alias: "중복 추출"}

    mapping = world_setting_ref_mapping({canonical}, set(actual_values), [(canonical, alias)])
    scored_values = {mapping.get(ref, ref): value for ref, value in actual_values.items()}

    assert mapping == {canonical: canonical}
    assert scored_values == actual_values
    assert len(scored_values) == 2


def test_exact_actual_ref_cannot_be_stolen_by_another_expected_fact() -> None:
    first = _ref("전투 위험성")
    second = _ref("동료 요구 조건")

    mapping = world_setting_ref_mapping({first, second}, {first}, [(second, first)])

    assert mapping == {first: first}


def test_multiple_semantic_predictions_for_one_expected_ref_remain_unmapped() -> None:
    expected = _ref("전투 위험성")
    first = _ref("전투 규칙")
    second = _ref("전투 판정")

    mapping = world_setting_ref_mapping(
        {expected}, {first, second}, [(expected, first), (expected, second)]
    )

    assert mapping == {}
    assert len({mapping.get(ref, ref) for ref in {first, second}}) == 2


def test_one_semantic_prediction_cannot_satisfy_multiple_expected_refs() -> None:
    first = _ref("전투 위험성")
    second = _ref("사망 규칙")
    actual = _ref("전투 규칙")

    assert (
        world_setting_ref_mapping({first, second}, {actual}, [(first, actual), (second, actual)])
        == {}
    )


def test_duplicate_identical_approvals_are_not_ambiguous() -> None:
    expected = _ref("전투 위험성")
    actual = _ref("전투 규칙")

    assert world_setting_ref_mapping(
        {expected}, {actual}, [(expected, actual), (expected, actual)]
    ) == {actual: expected}


def test_ambiguous_edges_do_not_block_an_independent_unique_match() -> None:
    combat = _ref("전투 위험성")
    companion = _ref("동료 요구 조건")
    combat_a = _ref("전투 규칙")
    combat_b = _ref("전투 판정")
    companion_alias = _ref("진행 조건")
    pairs = [(combat, combat_a), (combat, combat_b), (companion, companion_alias)]

    forward = world_setting_ref_mapping(
        {combat, companion}, {combat_a, combat_b, companion_alias}, pairs
    )
    reverse = world_setting_ref_mapping(
        {combat, companion}, {combat_a, combat_b, companion_alias}, reversed(pairs)
    )

    assert forward == reverse == {companion_alias: companion}


def test_approvals_for_absent_facts_do_not_create_state_entries() -> None:
    expected = _ref("전투 위험성")
    actual = _ref("전투 규칙")
    absent = _ref("없는 항목")

    assert (
        world_setting_ref_mapping({expected}, {actual}, [(expected, absent), (absent, actual)])
        == {}
    )


@pytest.mark.parametrize("subject_ref", [None, "backend:world:42"])
@pytest.mark.parametrize("scope", [None, "게임:규칙"])
def test_world_fact_ref_supports_subject_identity_and_escaped_scoped_names(
    subject_ref: str | None,
    scope: str | None,
) -> None:
    expected = _ref("전투:위험성", subject_ref=subject_ref, scope=scope)
    actual = _ref("전투:규칙", subject_ref=subject_ref, scope=scope)

    assert world_setting_ref_mapping({expected}, {actual}, [(expected, actual)]) == {
        actual: expected
    }


@pytest.mark.parametrize(
    "other_ref",
    [
        "fact:gold:character:character%3Abjorn:PROFILE:profile.species",
        "gold:world:WORLD_RULE_HISTORY:게임:전투 규칙",
        "fact:gold:world-subject:WORLD_RULE_HISTORY:게임",
        "fact-json:gold:world:WORLD_RULE_HISTORY:게임:전투 규칙",
        "fact:gold:world:WORLD_RULE_HISTORY:게임:",
        'held-conflict:["S1","D1"]',
        "known-character:bjorn",
    ],
)
def test_only_full_world_fact_scoring_refs_are_mapped(other_ref: str) -> None:
    world = _ref("전투 위험성")

    assert world_setting_ref_mapping(
        {world, other_ref}, {world, other_ref}, [(world, other_ref), (other_ref, world)]
    ) == {world: world}


def _ref(
    setting: str,
    *,
    subject_ref: str | None = None,
    scope: str | None = None,
) -> str:
    return "fact:" + world_state_ref(
        "WORLD_RULE_HISTORY", "던전 앤 스톤", scope, setting, subject_ref=subject_ref
    )
