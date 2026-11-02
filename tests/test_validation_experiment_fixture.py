"""Experiment converter rules on a synthetic raw export, and the fixture data client's server parity."""

import asyncio
from copy import deepcopy

import pytest

from scripts.validation_experiment import synthetic
from scripts.validation_experiment.build_fixture import build, character_slots, convert
from scripts.validation_experiment.common import HarnessError, local_db_url, outside_repo
from scripts.validation_experiment.fixture_client import FixtureDataClient


def raw():
    texts = {no: text for no, (_, text) in synthetic.EPISODES.items()}
    return synthetic.raw_export(texts)


def test_character_slots_read_envelopes_snapshot_sources_and_legacy_values():
    row = {"currentAge": 24, "currentLevel": None,
           "profileJson": {"직업": {"value": "검사"}, "출신": {"name": "바렌"}, "목록": {"value": ["가", "나"]}},
           "itemsJson": {"의수": {"__catchhole_snapshot": {"format": "character-snapshot-entry", "version": 1,
                                                       "factValue": "강철 의수", "valueJson": {"value": "다른 값"}}}},
           "skillsJson": {"불 마법": {"value": "원래 값"}},
           "statusesJson": {"time.현재": {"value": "겨울"}, "왼팔": "상실", "참": {"value": True}}}
    sources = {("SKILL", "불 마법"): [{"sourceOrder": 1, "factValue": "옛 값", "sourceEpisodeNo": 1},
                                    {"sourceOrder": 2, "factValue": "최신 값", "sourceEpisodeNo": 5}]}
    slots, origins = character_slots(row, sources)
    assert list(slots) == sorted(slots)
    values = {key: slot["factValue"] for key, slot in slots.items()}
    assert values == {"AGE:age": "24", "ITEM:의수": "강철 의수", "PROFILE:목록": '["가","나"]', "PROFILE:직업": "검사",
                      "PROFILE:출신": "바렌", "SKILL:불 마법": "최신 값", "STATUS:왼팔": "상실", "STATUS:참": "true",
                      "TIME:time.현재": "겨울"}
    assert slots["TIME:time.현재"]["factType"] == "TIME" and origins["SKILL:불 마법"] == 5


def test_converter_orders_entries_and_builds_aliases_protected_paths_and_history():
    fixture, warnings = convert(raw(), target_no=3, target_file="003.txt", target_hash="a" * 64)
    domains = [entry["domain"] for entry in fixture["baselineEntries"]]
    assert domains == ["characters"] * 3 + ["worldSettings"] * 2 + ["worldHistory"]
    seoyun = fixture["baselineEntries"][0]
    assert seoyun["subjectRef"].startswith("character:") and seoyun["state"]["aliases"] == ["서윤"]
    assert seoyun["state"]["historyFacts"] == [] and seoyun["state"]["absences"] == {}
    river = fixture["baselineEntries"][4]
    assert river["state"]["protectedPaths"] == [{"scopeName": None, "settingName": "금기"}]
    history = fixture["baselineEntries"][5]["state"]
    assert history["sourceAvailability"] == "AVAILABLE" and history["settingName"] == "문"
    assert fixture["counts"] == {"characters": 3, "worldSettings": 2, "worldHistory": 1}
    assert [row["episodeNo"] for row in fixture["episodes"]] == [1, 2] and not warnings


def test_target_or_later_sources_are_mirrored_by_default_and_counted():
    export = raw()
    export["snapshotSources"].append({"characterId": export["characters"][1]["id"], "factType": "SKILL",
                                      "factKey": "불 마법", "sourceOrder": 2, "factValue": "미래 값", "sourceEpisodeNo": 3})
    export["worldSettings"][1]["earliestSourceEpisodeNo"] = 4
    mirrored, warnings = convert(deepcopy(export), target_no=3, target_file="t", target_hash="a" * 64)
    assert warnings == {"character_slots_from_target_or_later": 1, "world_settings_first_seen_target_or_later": 1}
    assert len(mirrored["baselineEntries"]) == 6
    excluded, _ = convert(deepcopy(export), target_no=3, target_file="t", target_hash="a" * 64,
                          exclude_target_or_later=True)
    names = [entry["state"].get("name") or entry["state"].get("subjectName") for entry in excluded["baselineEntries"]]
    assert "은빛 강" not in names
    dogyeom = next(entry for entry in excluded["baselineEntries"] if entry["state"].get("name") == "도겸")
    assert "SKILL:불 마법" not in dogyeom["state"]["slots"]


def test_fixture_client_serves_the_server_projection_from_the_fixture(tmp_path):
    manuscripts, raw_path = synthetic.write(tmp_path)
    fixture, _ = build(__import__("json").loads(raw_path.read_text()), manuscripts, target_no=3)
    assert fixture["localManuscriptHashMismatch"] == []
    client = FixtureDataClient(fixture, manuscripts)

    async def read():
        frozen = await client.read_input()
        subjects = await client.read_subjects()
        settings = await client.read_settings(["S1", "S4"])
        return frozen, subjects, settings

    frozen, subjects, settings = asyncio.run(read())
    assert [(source.ref, source.metadata["episodeNo"]) for source in frozen.sources] == [("R1", 2), ("R2", 1)]
    assert [row["name"] for row in subjects] == ["한서윤", "도겸", "바르탄", "회색탑", "은빛 강"]
    assert [row["entry_ref"] for row in settings] == ["B1", "B4"]  # the worldHistory row B6 of S4 is excluded
    (manuscripts / fixture["target"]["file"]).write_text("바뀐 원고", encoding="utf-8")
    with pytest.raises(HarnessError, match="changed"):
        FixtureDataClient(fixture, manuscripts)


def test_guards_refuse_repo_outputs_and_non_disposable_databases(tmp_path):
    from scripts.validation_experiment.common import REPO_ROOT

    with pytest.raises(HarnessError):
        outside_repo(REPO_ROOT / "docs")
    assert outside_repo(tmp_path) == tmp_path.resolve()
    for url in ("postgresql+psycopg://u:p@db.internal:5432/validation_experiment_x",
                "postgresql+psycopg://u:p@127.0.0.1:5432/catchhole",
                "sqlite:///validation_experiment_x"):
        with pytest.raises(HarnessError):
            local_db_url(url)
    assert local_db_url("postgresql+psycopg://u:p@127.0.0.1:15432/validation_experiment_31")


def test_manuscripts_are_found_by_episode_number_even_with_decomposed_korean_names(tmp_path):
    import unicodedata

    from scripts.validation_experiment.common import manuscript_path

    decomposed = unicodedata.normalize("NFD", "031화 합성 제목.txt")
    (tmp_path / decomposed).write_text("합성", encoding="utf-8")
    (tmp_path / "합본.txt").write_text("합성", encoding="utf-8")
    assert manuscript_path(tmp_path, 31).name == decomposed
    assert manuscript_path(tmp_path, 31, "031화 합성 제목.txt").name == decomposed
    with pytest.raises(HarnessError):
        manuscript_path(tmp_path, 30)
