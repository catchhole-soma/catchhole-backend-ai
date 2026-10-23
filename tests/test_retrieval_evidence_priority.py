"""Evidence priorities preserve source identity and obey the normal composition limits."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.retrieval.evidence import compose_evidence
from tests.test_retrieval_search_evidence import chunk, omission_counts


@pytest.mark.parametrize("round_robin", [False, True])
def test_explicit_priority_order_precedes_scores_retained_and_direct(round_robin):
    first = chunk("first", method="NEIGHBOR", score=0.01)
    second = chunk("second", score=0.1)
    retained = chunk("retained", score=0.9)
    direct = chunk("direct", method="DIRECT", score=1.0)
    result = compose_evidence(
        [direct, retained, second, first],
        priority_chunk_ids=[first.chunk_id, second.chunk_id, first.chunk_id, uuid4()],
        retained_chunk_ids=[second.chunk_id, retained.chunk_id],
        max_items=4,
        count_tokens=len,
        episode_round_robin=round_robin,
    )
    assert result.chunks == (first, second, retained, direct)
    assert omission_counts(result) == {}
    assert result.input_count == result.candidate_count == 4


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"max_items": 1}, "ITEM_LIMIT"),
        ({"per_episode": 1}, "EPISODE_LIMIT"),
        ({"max_tokens": 5}, "TOKEN_LIMIT"),
    ],
)
def test_priority_never_bypasses_item_episode_or_token_budgets(settings, reason):
    first = chunk("first", score=0.1)
    second = chunk("second", episode=first.episode_id, start=20, score=1.0)
    result = compose_evidence(
        [second, first],
        priority_chunk_ids=[first.chunk_id, second.chunk_id],
        count_tokens=len,
        **settings,
    )
    assert result.chunks == (first,)
    assert omission_counts(result) == {reason: 1}


def test_priority_selects_exact_quote_representative_before_retained_or_score():
    original = chunk("same source", method="DIRECT", score=1.0)
    later_priority = replace(original, chunk_id=uuid4(), methods=("KEYWORD",))
    first_priority = replace(original, chunk_id=uuid4(), methods=("VECTOR",), score=0.1)
    result = compose_evidence(
        [original, later_priority, first_priority],
        priority_chunk_ids=[first_priority.chunk_id, later_priority.chunk_id],
        retained_chunk_ids=[original.chunk_id],
        max_items=1,
        per_episode=1,
        max_tokens=len(original.text),
        count_tokens=len,
    )
    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_id == first_priority.chunk_id
    assert set(result.chunks[0].methods) == {"KEYWORD", "VECTOR", "DIRECT"}
    assert result.passages[0].text == original.text
    assert result.duplicate_count == 2
    assert omission_counts(result) == {}


def test_invalid_or_conflicting_priority_reference_is_still_rejected():
    original = chunk("first")
    conflicting = replace(original, source_hash="c" * 64)
    invalid = replace(chunk("invalid"), chunk_hash="b" * 64)
    fallback = chunk("fallback")
    result = compose_evidence(
        [fallback, invalid, original, conflicting],
        priority_chunk_ids=[original.chunk_id, invalid.chunk_id],
        count_tokens=len,
    )
    assert result.chunks == (fallback,)
    assert result.rejected_count == 3
    assert omission_counts(result) == {"CONFLICTING_REFERENCE": 2, "INVALID_CHUNK": 1}


def test_conflicting_priority_overlap_is_rejected_without_fabricating_passage():
    first = chunk("abcdef", score=0.1)
    conflicting = chunk("ZZZZ", episode=first.episode_id, start=3, score=1.0)
    result = compose_evidence(
        [conflicting, first],
        priority_chunk_ids=[first.chunk_id, conflicting.chunk_id],
        count_tokens=len,
    )
    assert result.chunks == (first,)
    assert result.passages[0].text == first.text
    assert result.rejected_count == 1
    assert omission_counts(result) == {"CONFLICTING_OVERLAP": 1}
