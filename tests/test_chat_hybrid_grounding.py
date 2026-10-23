"""Synthetic regressions for locating support in merged manuscript passages."""

from dataclasses import dataclass

import pytest

from app.chatbot.hybrid_grounding import resolve_support_quotes, resolve_support_references


@dataclass(frozen=True)
class Support:
    evidence_ref: str
    quote: str


def passage(text, *references):
    return {
        "text": text,
        "references": [
            {"evidenceRef": ref, "start": start, "end": end} for ref, start, end in references
        ],
    }


def test_quote_in_later_chunk_replaces_incorrect_earlier_reference():
    text = "문 앞에서 머뭇거렸다. 문을 열고 처음으로 밖으로 나갔다."
    later_start = text.index("문을")
    earlier_end = text.index("처음으로")
    data = passage(text, ("E1", 0, earlier_end), ("E2", later_start, len(text)))

    assert resolve_support_references([Support("E1", "처음으로 밖으로 나갔다.")], [data]) == ["E2"]


def test_quote_crossing_chunk_boundary_preserves_both_original_references():
    data = passage("abcdefghi", ("E1", 0, 5), ("E2", 3, 9))

    assert resolve_support_references([Support("E1", "bcdefgh")], [data]) == ["E1", "E2"]


def test_overlap_quote_keeps_one_valid_anchor_not_both_chunks():
    data = passage("abcdefghi", ("E1", 0, 6), ("E2", 3, 9))

    assert resolve_support_references([Support("E2", "de")], [data]) == ["E2"]


def test_quote_fully_in_later_chunk_does_not_add_partially_overlapping_chunk():
    data = passage("abcdefghi", ("E1", 0, 5), ("E2", 3, 9))

    assert resolve_support_references([Support("E1", "defgh")], [data]) == ["E2"]


def test_multiple_overlapping_chunks_use_minimum_complete_cover():
    data = passage("abcdefghijkl", ("E1", 0, 5), ("E2", 2, 7), ("E3", 4, 12))

    assert resolve_support_references([Support("E2", "bcdefghijk")], [data]) == ["E1", "E3"]


def test_touching_chunks_can_cover_one_quote_without_a_gap():
    data = passage("abcdef", ("E1", 0, 3), ("E2", 3, 6))

    assert resolve_support_references([Support("E1", "bcde")], [data]) == ["E1", "E2"]


def test_uncovered_whitespace_between_reference_ranges_is_not_ignored():
    data = passage("abc def", ("E1", 0, 3), ("E2", 4, 7))

    with pytest.raises(ValueError, match="^GROUNDING_UNCOVERED_QUOTE$"):
        resolve_support_references([Support("E1", "bc de")], [data])


def test_quote_cannot_be_rescued_from_a_different_passage_or_source():
    first = passage("그는 문을 닫았다.", ("E1", 0, 10))
    second_text = "그는 문을 열었다."
    second = passage(second_text, ("E2", 0, len(second_text)))

    with pytest.raises(ValueError, match="^GROUNDING_QUOTE_NOT_FOUND$"):
        resolve_support_references([Support("E1", second_text)], [first, second])


def test_equal_quotes_in_distinct_sources_do_not_override_the_explicit_anchor():
    text = "그는 문을 닫았다."
    first = passage(text, ("E1", 0, len(text)))
    second = passage(text, ("E2", 0, len(text)))

    # Text matching cannot decide which event the claim means. It must never
    # silently choose a different episode just because the wording also matches.
    assert resolve_support_references([Support("E2", text)], [first, second]) == ["E2"]


@pytest.mark.parametrize("text,quote", [("반복. 반복.", "반복."), ("aaaaa", "aaa")])
def test_repeated_or_overlapping_quote_occurrences_are_ambiguous(text, quote):
    data = passage(text, ("E1", 0, len(text)))

    with pytest.raises(ValueError, match="^GROUNDING_AMBIGUOUS_QUOTE$"):
        resolve_support_references([Support("E1", quote)], [data])


@pytest.mark.parametrize("quote", ["", " \n", "문을  닫았다.", "문을 닫았다..."])
def test_blank_or_altered_quotes_are_not_normalized_into_matches(quote):
    text = "그는 문을 닫았다."
    data = passage(text, ("E1", 0, len(text)))

    with pytest.raises(ValueError, match="^GROUNDING_QUOTE_NOT_FOUND$"):
        resolve_support_references([Support("E1", quote)], [data])


def test_unknown_anchor_is_not_replaced_by_matching_quote_elsewhere():
    data = passage("abcdef", ("E1", 0, 6))

    with pytest.raises(ValueError, match="^GROUNDING_UNKNOWN_REFERENCE$"):
        resolve_support_references([Support("E9", "abc")], [data])


def test_unicode_offsets_are_code_points_including_astral_characters():
    text = "🙂문을 열었다. 🐉처음 만났다."
    split = text.index("🐉")
    data = passage(text, ("E1", 0, split), ("E2", split, len(text)))

    assert resolve_support_references([Support("E1", "🐉처음 만났다.")], [data]) == ["E2"]


def test_support_order_is_preserved_and_duplicate_citations_removed():
    data = passage("abcdefghi", ("E1", 0, 5), ("E2", 3, 9))

    assert resolve_support_references(
        [Support("E1", "ghi"), Support("E2", "ab"), Support("E1", "defgh")], [data]
    ) == ["E2", "E1"]


@pytest.mark.parametrize(
    "references",
    [
        [],
        [("E1", -1, 3)],
        [("E1", 0, 7)],
        [("E1", 3, 3)],
        [("E1", False, 3)],
        [("E1", 0, "3")],
        [("E1", 0, 3), ("E1", 3, 6)],
    ],
)
def test_invalid_or_ambiguous_reference_metadata_is_rejected(references):
    data = passage("abcdef", *references)

    with pytest.raises(ValueError, match="^GROUNDING_INVALID_PASSAGE$"):
        resolve_support_references([Support("E1", "abc")], [data])


def test_one_reference_cannot_identify_two_distinct_sources():
    first = passage("abc", ("E1", 0, 3))
    second = passage("def", ("E1", 0, 3))

    with pytest.raises(ValueError, match="^GROUNDING_INVALID_PASSAGE$"):
        resolve_support_references([Support("E1", "def")], [first, second])


def test_empty_supports_do_not_silently_succeed():
    with pytest.raises(ValueError, match="^GROUNDING_SUPPORT_REQUIRED$"):
        resolve_support_references([], [])


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r", " \n\t", "\n\n"])
def test_line_break_rendering_is_recovered_as_exact_original_quote(line_break):
    text = f"🙂문이 닫혔다.{line_break}그래서 그는 돌아갔다."
    quote = "문이 닫혔다. 그래서 그는 돌아갔다."
    data = passage(text, ("E1", 0, len(text)))

    resolved = resolve_support_quotes([Support("E1", quote)], [data])

    assert resolved[0].quote == text[1:]
    assert resolved[0].evidence_refs == ("E1",)
    assert resolved[0].quote in text


def test_line_break_rendering_keeps_all_original_chunk_references():
    text = "문이 닫혔다.\n그래서 돌아갔다."
    split = text.index("그래서")
    data = passage(text, ("E1", 0, split), ("E2", split, len(text)))

    resolved = resolve_support_quotes([Support("E1", text.replace("\n", " "))], [data])

    assert resolved[0].quote == text
    assert resolved[0].evidence_refs == ("E1", "E2")


def test_line_break_rendering_does_not_fill_uncovered_source_spans():
    text = "문이 닫혔다.\n그래서 돌아갔다."
    split = text.index("그래서")
    data = passage(text, ("E1", 0, split - 1), ("E2", split, len(text)))

    with pytest.raises(ValueError, match="^GROUNDING_UNCOVERED_QUOTE$"):
        resolve_support_quotes([Support("E1", text.replace("\n", " "))], [data])


def test_line_break_rendering_does_not_reanchor_to_another_passage():
    first = passage("다른 사건이다.", ("E1", 0, 8))
    text = "문이 닫혔다.\n그래서 돌아갔다."
    second = passage(text, ("E2", 0, len(text)))

    with pytest.raises(ValueError, match="^GROUNDING_QUOTE_NOT_FOUND$"):
        resolve_support_quotes([Support("E1", text.replace("\n", " "))], [first, second])


def test_line_break_rendering_ambiguity_is_rejected():
    text = "문이\n닫혔다. 문이\r\n닫혔다."
    data = passage(text, ("E1", 0, len(text)))

    with pytest.raises(ValueError, match="^GROUNDING_AMBIGUOUS_QUOTE$"):
        resolve_support_quotes([Support("E1", "문이 닫혔다.")], [data])


@pytest.mark.parametrize(
    "quote",
    [
        "문이닫혔다. 그래서 돌아갔다.",
        "문이 닫혔다. 그래서  돌아갔다.",
        "문이 닫혔다! 그래서 돌아갔다.",
    ],
)
def test_line_break_rendering_does_not_correct_words_spacing_or_punctuation(quote):
    text = "문이 닫혔다.\n그래서 돌아갔다."
    data = passage(text, ("E1", 0, len(text)))

    with pytest.raises(ValueError, match="^GROUNDING_QUOTE_NOT_FOUND$"):
        resolve_support_quotes([Support("E1", quote)], [data])
