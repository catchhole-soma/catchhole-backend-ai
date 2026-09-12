"""Plain-language guidance must not discard valid decisions for cosmetic prose."""

import pytest

from app.analysis import character_fact_comparator as characters
from app.analysis.comparison_reason import USER_FACING_REASON_INSTRUCTIONS
from app.analysis.world_setting_comparator import _validate_user_facing_reason
from tests.test_ordered_character_slot_feedback import candidate, decision, run, offline  # noqa: F401
from tests.test_ordered_world_property_selection import (
    candidate as world_candidate, target, decision as world_decision, run as world_run,
    offline_dependencies,  # noqa: F401
)

# Imported fixtures intentionally install the same offline dependencies as the
# provider-boundary contract tests, without touching application settings files.
pytestmark = pytest.mark.usefixtures("offline", "offline_dependencies")


def test_input_owned_names_and_longer_story_words_are_not_technical_prose():
    _validate_user_facing_reason("루트비히가 등장하고 Rooted라는 주문을 쓴다.", [])
    _validate_user_facing_reason("Root의 성격을 확인한다.", [], display_names=("Root",))
    characters._validate_user_facing_reason_values(
        "Root의 성격을 확인한다.", "profile.attribute", [], display_names=("Root",),
    )


def test_character_cosmetic_terms_do_not_trigger_extra_calls_or_discard_a_valid_decision():
    source = candidate("C1", "profile.attribute")
    invalid = {**decision(source, "REVIEW_REQUIRED"), "comparison_reason": "기존 slot을 검토한다."}
    result, calls = run([[invalid]], [source])
    assert not isinstance(result, Exception), result
    assert len(calls) == 1
    assert result[0].decisions[0].comparison_reason == invalid["comparison_reason"]
    assert result[0].decisions[0].operation == "REVIEW_REQUIRED"
    assert USER_FACING_REASON_INSTRUCTIONS in calls[0]["system_prompt"]


def test_world_cosmetic_terms_do_not_trigger_extra_calls_and_source_stays_unchanged():
    source = world_candidate()
    before = source.model_dump()
    invalid = {**world_decision(source), "comparison_reason": "root slot에 추가한다."}
    result, calls = world_run([{"decisions": [invalid]}],
                              [source], [target()], ordered_context=True)
    assert not isinstance(result, Exception), result
    assert len(calls) == 1 and result[0].decisions[0].comparison_reason == invalid["comparison_reason"]
    assert result[1]["validation_diagnostics"] == []
    assert USER_FACING_REASON_INSTRUCTIONS in calls[0]["system_prompt"]
    assert source.model_dump() == before
