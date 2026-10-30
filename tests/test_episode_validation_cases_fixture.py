"""GH314 V1 검수 기본 사례 fixture의 형식만 검사한다. 모델·네트워크·DB를 호출하지 않는다."""

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "episode_validation_cases.json"
MAX_EPISODE_CHARS = 3000


@pytest.fixture(scope="module")
def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def episodes(fixture):
    return {episode["episodeRef"]: episode for work in fixture["works"] for episode in work["episodes"]}


def test_fixture_is_synthetic_and_pins_validation_models(fixture):
    assert fixture["schemaVersion"] == 1
    assert fixture["synthetic"] is True
    assert fixture["models"]["setting"]["model"] == "gpt-6.1-terra"
    assert fixture["models"]["setting"]["reasoningEffort"] == {
        "PLANNER": "medium", "RELATED_SELECTION": "high", "COVERAGE": "high", "JUDGE": "medium",
    }
    assert fixture["models"]["typo"] == {"model": "gpt-6.1-terra", "reasoningEffort": {"TYPO": "low"}}


def test_episodes_are_short_unique_and_ordered(fixture, episodes):
    refs = [episode["episodeRef"] for work in fixture["works"] for episode in work["episodes"]]
    assert len(refs) == len(set(refs))
    for work in fixture["works"]:
        assert work["workRef"] and work["title"] and work["genre"]
        numbers = [episode["episodeNo"] for episode in work["episodes"] if episode["upload"] != "REPLACE_FILE"]
        assert numbers == list(range(1, len(numbers) + 1))
        for episode in work["episodes"]:
            assert episode["upload"] in {"SETTING_BUILD", "VALIDATION", "REPLACE_FILE"}
            assert 0 < len(episode["text"]) <= MAX_EPISODE_CHARS
            assert episode["text"].startswith(f"{episode['episodeNo']}화 ")
            if episode["upload"] == "REPLACE_FILE":
                replaced = episodes[episode["replaces"]]
                assert replaced["upload"] == "VALIDATION" and replaced["episodeNo"] == episode["episodeNo"]


def test_cases_reference_real_quotes_and_closed_enums(fixture, episodes):
    enums = fixture["enums"]
    ids = [case["caseId"] for case in fixture["cases"]]
    assert len(ids) == len(set(ids))
    for case in fixture["cases"]:
        target = episodes[case["target"]]
        assert target["upload"] in {"VALIDATION", "REPLACE_FILE"}, case["caseId"]
        assert case["branch"] in {"SETTING", "TYPO", "BOTH"}
        assert case["targetQuote"] in target["text"], case["caseId"]
        expected = case["expected"]
        assert isinstance(expected["finding"], bool)
        if case["branch"] == "SETTING" and expected["finding"]:
            assert expected["outcomes"] and set(expected["outcomes"]) <= set(enums["settingOutcome"])
            assert expected["impacts"] and set(expected["impacts"]) <= set(enums["impact"])
            assert set(expected["evidenceSides"]) == {"BASELINE", "TARGET"}
            assert expected["authorJudgment"] in {"NEEDS_FIX", "NOT_CONFLICT"}
            assert expected["baselineHint"]
        if case["branch"] == "TYPO" and expected["finding"]:
            assert expected["category"] in enums["typoCategory"]
            assert expected["replacement"] and expected["replacement"] != case["targetQuote"]
        if "authorJudgmentIfFlagged" in expected:
            assert expected["authorJudgmentIfFlagged"] == "KEEP_ORIGINAL" and expected["finding"] is False


def test_planted_typos_occur_once_and_are_fixed_in_the_replacement(fixture, episodes):
    for case in fixture["cases"]:
        if case["branch"] == "TYPO" and case["expected"]["finding"]:
            original = episodes[case["target"]]["text"]
            assert original.count(case["targetQuote"]) == 1, case["caseId"]
            fixed = [episode for episode in episodes.values() if episode.get("replaces") == case["target"]]
            for replacement in fixed:
                assert case["targetQuote"] not in replacement["text"], case["caseId"]


def test_every_validation_target_has_at_least_one_expectation(fixture, episodes):
    targets = {case["target"] for case in fixture["cases"]}
    validated = {ref for ref, episode in episodes.items() if episode["upload"] in {"VALIDATION", "REPLACE_FILE"}}
    assert validated == targets
    assert fixture["uncoveredTaskTableRows"], "실행하지 않은 계획 사례를 숨기지 않는다"
