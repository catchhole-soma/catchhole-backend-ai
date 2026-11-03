"""SETTING grid v4 segmentation: shared Java/Python vectors and the invariants that make it safe."""

import json
import random
from itertools import pairwise
from pathlib import Path

import pytest

from app.validation.contracts import (
    segment_wire,
    setting_cuts,
    split_segments,
    split_setting_segments,
)


def grid_cases():
    path = Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json"
    return [case for case in json.loads(path.read_text())["execution"]["gridCases"] if "expectedSetting" in case]


def case_text(case):
    repeat = case.get("targetRepeat")
    return case["target"] if repeat is None else repeat["unit"] * repeat["count"]


@pytest.mark.parametrize("case", grid_cases(), ids=lambda case: case["name"])
def test_shared_grid_v4_vectors_match_the_python_split(case):
    text, budgets = case_text(case), case["configuration"]["budgets"]
    setting, typo = budgets["setting"], budgets["typo"]
    segments = split_setting_segments(text, unit_chars=setting["segmentChars"], context_chars=setting["contextChars"])
    expected = case["expectedSetting"]
    assert expected["gridVersion"] == 4
    assert [segment_wire(segment) for segment in segments] == expected["segments"]
    assert expected["units"] == ([{"stage": "PLANNER", "ref": segment.ref} for segment in segments]
                                 + [{"stage": "MERGED_PLAN", "ref": "PLAN"}])
    assert set(expected) == {"gridVersion", "segments", "units"}
    # TYPO keeps the old fixed split (grid v1).
    typo_segments = split_segments(text, core_chars=typo["segmentChars"], context_chars=typo["contextChars"])
    assert case["expectedTypo"]["gridVersion"] == 1
    assert [segment_wire(segment) for segment in typo_segments] == case["expectedTypo"]["segments"]


def test_shared_vectors_cover_every_required_shape():
    names = {case["name"] for case in grid_cases()}
    assert {"v4-length-at-most-unit", "v4-exact-multiple", "v4-no-newline-fallback", "v4-newline-tie", "v4-crlf",
            "v4-non-bmp-code-points", "v4-context-clipped-both-ends", "v4-unit-one", "v4-unit-two",
            "v4-25000-three-segments"} <= names
    big = next(case for case in grid_cases() if case["name"] == "v4-25000-three-segments")
    assert len(case_text(big)) == 25000 and len(big["expectedSetting"]["segments"]) == 3


def test_newline_rules_tie_crlf_and_fallback():
    # Two newlines one code point from t = 5: the smaller cut wins.
    assert setting_cuts("가나다\n마\n사아자차", 5) == [4]
    # Only "\n" counts; "\r\n" stays together and the cut falls right after "\n".
    text = "가나\r\n다라\r\n마바\r\n사아"
    cut = setting_cuts(text, 7)[0]
    assert text[cut - 1] == "\n" and text[cut - 2] == "\r"
    # No newline inside the window: max(t, lo + 1).
    assert setting_cuts("가나다라마바사아자차", 4) == [3, 6]
    # Non-BMP characters are single code points.
    assert setting_cuts("😀😀\n😀😀", 3) == [3]


def test_short_and_empty_targets():
    assert split_setting_segments("", unit_chars=10, context_chars=1) == ()
    (only,) = split_setting_segments("가나다", unit_chars=10, context_chars=5)
    assert (only.ref, only.start, only.end, only.context_start, only.context_end) == ("TSEG1", 0, 3, 0, 3)
    for unit, context in ((0, 1), (1, -1), (True, 1)):
        with pytest.raises(ValueError):
            split_setting_segments("가", unit_chars=unit, context_chars=context)


def test_random_targets_keep_the_safety_invariants():
    generator = random.Random(349)
    alphabet = "가나다라마 \n\r😀a"
    for _ in range(400):
        length = generator.randint(1, 120)
        text = "".join(generator.choice(alphabet) for _ in range(length))
        unit = generator.randint(1, 40)
        context = generator.randint(0, 15)
        segments = split_setting_segments(text, unit_chars=unit, context_chars=context)
        n = (length + unit - 1) // unit
        assert len(segments) == n
        assert segments[0].start == 0 and segments[-1].end == length
        for index, segment in enumerate(segments, 1):
            assert segment.ref == f"TSEG{index}" and segment.start < segment.end
            assert segment.context_start == max(0, segment.start - context)
            assert segment.context_end == min(length, segment.end + context)
        for k, (left, right) in enumerate(pairwise(segments), 1):
            cut = left.end
            assert cut == right.start
            t, lo, hi = (k * length) // n, ((2 * k - 1) * length) // (2 * n), ((2 * k + 1) * length) // (2 * n)
            assert lo < cut <= hi
            newlines = [p for p in range(lo + 1, hi + 1) if text[p - 1] == "\n"]
            if newlines:
                assert text[cut - 1] == "\n"
                assert (abs(cut - t), cut) == min((abs(p - t), p) for p in newlines)
            else:
                assert cut == max(t, lo + 1)
