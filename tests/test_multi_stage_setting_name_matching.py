import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    WorldStage1Gold,
    WorldStage1Prediction,
)
from evals.multi_stage_setting.matching import (
    match_stage1,
    world_setting_context_matches,
    world_setting_name_matches,
    world_setting_name_pairs,
)


def test_semantic_setting_name_match_is_used_for_identity_and_reach() -> None:
    result = _match([_gold()], [_prediction()], {("W1", "P1"): True})

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.identity_matched
    assert match.setting_name_match_method == "SEMANTIC"
    assert match.value_status == "MATCH"
    assert match.upstream_outcome == "REACHED"


@pytest.mark.parametrize(
    ("gold_values", "prediction_values", "value_status", "upstream"),
    [
        (
            ["체력이 가득해도 사망한다.", "부활할 수 없다."],
            ["체력이 가득해도 사망한다."],
            "SEMANTIC_JUDGE_REQUIRED",
            "REACHED",
        ),
        (
            ["체력이 가득해도 사망한다."],
            ["체력이 가득하면 사망하지 않는다."],
            "SEMANTIC_JUDGE_REQUIRED",
            "REACHED",
        ),
    ],
)
def test_semantic_name_match_does_not_decide_value_correctness(
    gold_values: list[str],
    prediction_values: list[str],
    value_status: str,
    upstream: str,
) -> None:
    result = _match(
        [_gold(values=gold_values)],
        [_prediction(values=prediction_values)],
        {("W1", "P1"): True},
    )

    match = result.matches[0]
    assert match.identity_matched
    assert match.value_status == value_status
    assert match.upstream_outcome == upstream


@pytest.mark.parametrize("name_decision", [False, None])
def test_equal_values_do_not_override_negative_or_pending_name_judgment(
    name_decision: bool | None,
) -> None:
    result = _match([_gold()], [_prediction()], {("W1", "P1"): name_decision})

    match = result.matches[0]
    assert match.value_status == "MATCH"
    assert match.identity_matched is name_decision
    assert match.setting_name_match_method is None
    assert match.upstream_outcome == (
        "REACHED" if name_decision is None else "UPSTREAM_VALUE_ERROR"
    )


@pytest.mark.parametrize("name_decision", [False, None])
@pytest.mark.parametrize(
    ("setting", "method"), [(" 전투 위험성 ", "EXACT"), ("전투 규칙", "ALIAS")]
)
def test_reviewed_names_keep_precedence_over_semantic_judgment(
    name_decision: bool | None,
    setting: str,
    method: str,
) -> None:
    gold = _gold(aliases=["전투 규칙"])
    prediction = _prediction(setting=setting)

    result = _match([gold], [prediction], {("W1", "P1"): name_decision})

    assert result.matches[0].identity_matched
    assert result.matches[0].setting_name_match_method == method
    assert world_setting_name_matches(gold, setting)
    assert world_setting_name_pairs([gold], [prediction]) == []


@pytest.mark.parametrize(
    "changed_context",
    [
        {"category": "POWER_SYSTEM"},
        {"subject_name": "다른 게임"},
    ],
)
def test_semantic_name_match_cannot_cross_category_or_subject(
    changed_context: dict[str, str],
) -> None:
    gold = _gold()
    prediction = _prediction().model_copy(update=changed_context)

    result = _match([gold], [prediction], {("W1", "P1"): True})

    assert not world_setting_context_matches(gold, prediction)
    assert world_setting_name_pairs([gold], [prediction]) == []
    assert not any(match.identity_matched for match in result.matches)
    assert not any(match.setting_name_match_method for match in result.matches)


@pytest.mark.parametrize("scope_match", [True, False, None])
def test_scope_judgment_is_independent_of_approved_setting_name(scope_match: bool | None) -> None:
    gold = _gold()
    prediction = _prediction().model_copy(update={"scope_name": "게임 규칙"})

    result = match_stage1(
        [gold], [prediction], domain="WORLD", source_text=None,
        world_setting_name_matches={("W1", "P1"): True},
        world_scope_matches={("W1", "P1"): scope_match},
    )

    assert world_setting_context_matches(gold, prediction)
    assert world_setting_name_pairs([gold], [prediction]) == [(gold, prediction)]
    assert result.matches[0].setting_name_match_method == "SEMANTIC"
    assert result.matches[0].identity_matched is scope_match
    assert result.matches[0].value_status == "MATCH"
    assert result.matches[0].upstream_outcome == (
        "UPSTREAM_VALUE_ERROR" if scope_match is False else "REACHED"
    )


def test_name_pair_context_uses_production_normalization() -> None:
    gold = _gold().model_copy(update={"subject_name": "Dungeon", "scope_name": "Combat"})
    prediction = _prediction().model_copy(
        update={"subject_name": " dungeon ", "scope_name": " combat "}
    )

    assert world_setting_name_pairs([gold], [prediction]) == [(gold, prediction)]
    assert _match([gold], [prediction], {("W1", "P1"): True}).matches[0].identity_matched

    spaced_gold = gold.model_copy(update={"subject_name": "Dungeon  Stone"})
    spaced_prediction = prediction.model_copy(update={"subject_name": "Dungeon Stone"})
    assert world_setting_name_pairs([spaced_gold], [spaced_prediction]) == []


def test_semantic_names_choose_one_to_one_pairs_before_value_similarity() -> None:
    gold = [
        _gold(gold_id="W1", setting="전투 위험성", values=["전투 설명"]),
        _gold(gold_id="W2", setting="동료 요구 조건", values=["동료 설명"]),
    ]
    predictions = [
        _prediction(candidate_id="P2", setting="진행 조건", values=["전투 설명"]),
        _prediction(candidate_id="P1", setting="전투 규칙", values=["동료 설명"]),
    ]
    decisions = {
        ("W1", "P1"): True,
        ("W1", "P2"): False,
        ("W2", "P1"): False,
        ("W2", "P2"): True,
    }

    result = _match(gold, predictions, decisions)

    assert result.prediction_id_by_gold_id == {"W1": "P1", "W2": "P2"}
    assert all(match.identity_matched for match in result.matches)
    assert all(match.value_status == "SEMANTIC_JUDGE_REQUIRED" for match in result.matches)
    assert result.missed_gold == ()
    assert result.extra_predictions == ()


def test_one_prediction_cannot_satisfy_multiple_semantically_matching_gold_rows() -> None:
    gold = [_gold(gold_id="W1"), _gold(gold_id="W2", setting="사망 가능성")]

    result = _match(gold, [_prediction()], {("W1", "P1"): True, ("W2", "P1"): True})

    assert len(result.matches) == 1
    assert len(result.missed_gold) == 1


def test_name_pairs_and_matching_share_grouped_gold_and_prediction_ids() -> None:
    gold = [
        _gold(gold_id="W1", values=["설명 하나"]),
        _gold(gold_id="W2", values=["설명 둘"]),
    ]
    predictions = [
        _prediction(candidate_id="P1", values=["설명 하나"]),
        _prediction(candidate_id="P2", values=["설명 둘"]),
    ]

    pairs = world_setting_name_pairs(gold, predictions)
    result = _match(gold, predictions, {("W1", "P1"): True})

    assert len(pairs) == 1
    grouped_gold, grouped_prediction = pairs[0]
    assert (grouped_gold.gold_id, grouped_prediction.candidate_id) == ("W1", "P1")
    assert grouped_gold.source_values == ["설명 하나", "설명 둘"]
    assert grouped_prediction.source_values == ["설명 하나", "설명 둘"]
    assert result.matches[0].source_gold_ids == ("W1", "W2")
    assert result.matches[0].identity_matched
    assert result.matches[0].value_status == "MATCH"


def test_grouped_reviewed_alias_is_not_sent_for_semantic_judgment() -> None:
    gold = [_gold(gold_id="W1"), _gold(gold_id="W2", aliases=["전투 규칙"])]

    assert world_setting_name_pairs(gold, [_prediction()]) == []
    assert _match(gold, [_prediction()], {}).matches[0].setting_name_match_method == "ALIAS"


def test_name_pair_collection_excludes_non_extract_and_character_rows() -> None:
    gold = _gold().model_copy(update={"decision": "DO_NOT_EXTRACT"})
    character = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        review_status="FINAL",
        evidence_quotes=["아이가 나타났다."],
        domain="CHARACTER",
        candidate_kind="CHARACTER_DISCOVERY",
        entity_ref="character:child",
        entity_name="아이",
    )
    prediction = CharacterStage1Prediction(
        candidate_id="PC1",
        domain="CHARACTER",
        candidate_kind="CHARACTER_DISCOVERY",
        entity_name="아이",
    )

    assert world_setting_name_pairs([gold, character], [_prediction(), prediction]) == []


def test_omitted_name_judgments_preserve_existing_matching_behavior() -> None:
    gold = [_gold()]
    predictions = [_prediction()]

    default_result = match_stage1(gold, predictions, domain="WORLD", source_text=None)
    empty_result = _match(gold, predictions, {})

    assert default_result == empty_result
    assert default_result.matches[0].entity_or_subject_matched
    assert not default_result.matches[0].path_or_fact_matched


def _match(gold, predictions, judgments):
    return match_stage1(
        gold,
        predictions,
        domain="WORLD",
        source_text=None,
        world_setting_name_matches=judgments,
    )


def _gold(
    *,
    gold_id: str = "W1",
    setting: str = "전투 위험성",
    values: list[str] | None = None,
    aliases: list[str] | None = None,
) -> WorldStage1Gold:
    return WorldStage1Gold(
        gold_id=gold_id,
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["체력이 가득해도 사망한다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="WORLD_RULE_HISTORY",
        subject_name="던전 앤 스톤",
        setting_name=setting,
        accepted_setting_name_aliases=aliases or [],
        source_values=values or ["체력이 가득해도 사망한다."],
    )


def _prediction(
    *,
    candidate_id: str = "P1",
    setting: str = "전투 규칙",
    values: list[str] | None = None,
) -> WorldStage1Prediction:
    return WorldStage1Prediction(
        candidate_id=candidate_id,
        domain="WORLD",
        category="WORLD_RULE_HISTORY",
        subject_name="던전 앤 스톤",
        setting_name=setting,
        source_values=values or ["체력이 가득해도 사망한다."],
    )
