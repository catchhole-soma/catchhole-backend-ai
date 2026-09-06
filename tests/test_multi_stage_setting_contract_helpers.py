from app.mappers.world_setting_candidate_mapper import (
    world_setting_path_key as production_world_setting_path_key,
)
from evals.multi_stage_setting.contracts import (
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioGold,
    character_state_ref,
    world_path_key,
    world_state_ref,
    world_subject_ref,
)


def test_fixture_domain_sets_serialize_in_canonical_order() -> None:
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"WORLD", "CHARACTER"},
        gold_version="v3",
        candidate_free=True,
        start_state_mode="EMPTY",
        cumulative_through_episode=0,
        review_status="FINAL",
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="canonical domain order",
        scenarios=[scenario],
    ).with_fixture_hash()
    prediction = PredictionBundleV3(
        fixture_hash=gold.fixture_hash or "missing",
        mode="ORACLE",
        evaluation_domains={"WORLD", "CHARACTER"},
    )

    assert gold.model_dump(mode="json", by_alias=True)["scenarios"][0][
        "targetDomains"
    ] == ["CHARACTER", "WORLD"]
    assert prediction.model_dump(mode="json", by_alias=True)["evaluationDomains"] == [
        "CHARACTER",
        "WORLD",
    ]


def test_character_state_ref_escapes_segments_to_prevent_delimiter_collision() -> None:
    embedded_entity_delimiter = character_state_ref("a:B", "C", "d")
    shifted_delimiter = character_state_ref("a", "B", "C:d")

    assert embedded_entity_delimiter == "gold:character:a%3AB:C:d"
    assert shifted_delimiter == "gold:character:a:B:C%3Ad"
    assert embedded_entity_delimiter != shifted_delimiter


def test_world_refs_distinguish_path_shape_and_literal_escape_text() -> None:
    setting_delimiter = world_state_ref("RACE", "a", None, "b:c")
    explicit_scope = world_state_ref("RACE", "a", "b", "c")

    assert setting_delimiter == "gold:world:RACE:a:b%3Ac"
    assert explicit_scope == "gold:world:RACE:a:b:c"
    assert setting_delimiter != explicit_scope
    assert world_subject_ref("RACE", "a:b") != world_subject_ref("RACE", "a%3Ab")


def test_evaluator_reuses_production_world_path_normalization() -> None:
    path = ("RACE", "  Cafe\u0301  ", "  1층  ", "  체격  ")

    assert world_path_key(*path) == production_world_setting_path_key(*path)
    assert world_path_key(*path) == ("RACE", "café", "1층", "체격")


def test_evaluator_preserves_production_internal_whitespace_identity() -> None:
    double_space = world_path_key("RACE", "고블린  족", None, "체격")
    single_space = world_path_key("RACE", "고블린 족", None, "체격")

    assert double_space != single_space
    assert double_space == production_world_setting_path_key(
        "RACE", "고블린  족", None, "체격"
    )


def test_world_normalization_matches_java_lowercase_not_unicode_casefold() -> None:
    german_sharp_s = world_path_key("RACE", "Straße", None, "체격")
    expanded_ss = world_path_key("RACE", "STRASSE", None, "체격")

    assert german_sharp_s[1] == "straße"
    assert expanded_ss[1] == "strasse"
    assert german_sharp_s != expanded_ss
