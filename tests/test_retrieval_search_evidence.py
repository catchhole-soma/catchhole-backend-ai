from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from app.retrieval.chunking import POLICY
from app.retrieval.evidence import compose_evidence
from app.retrieval.search_contracts import ChunkEvidence


def chunk(value="가나다😀라마", *, episode=None, start=0, method="KEYWORD", score=1.0, **kwargs):
    return ChunkEvidence(
        chunk_id=kwargs.pop("chunk_id", uuid4()),
        episode_id=episode or uuid4(),
        episode_no=1,
        chunk_index=0,
        text=value,
        start_offset=start,
        end_offset=start + len(value),
        chunk_hash=sha256(value.encode()).hexdigest(),
        source_key="private-source",
        source_version="v1",
        source_hash="a" * 64,
        policy=POLICY,
        methods=(method,),
        score=score,
        **kwargs,
    )


def test_overlaps_bridged_in_any_rank_order_preserve_unicode_and_original_refs():
    episode = uuid4()
    original = "앞😀부분\r\n가나다뒷부분"
    first = chunk(original[:6], episode=episode, score=1.0)
    last = chunk(original[9:], episode=episode, start=9, score=0.9)
    middle = chunk(original[4:11], episode=episode, start=4, score=0.8)
    bundle = compose_evidence(
        [last, first, middle], max_items=3, per_episode=3, max_tokens=100, count_tokens=len
    )
    assert len(bundle.passages) == 1
    passage = bundle.passages[0]
    assert passage.text == original and passage.start_offset == 0
    assert passage.end_offset == len(original)
    assert {c.chunk_id for c in passage.chunks} == {first.chunk_id, middle.chunk_id, last.chunk_id}
    assert bundle.token_count == len(original) and not bundle.limited


def test_same_chunk_from_multiple_methods_is_deduplicated_but_provenance_is_preserved():
    original = chunk()
    bundle = compose_evidence(
        [original, replace(original, methods=("VECTOR",), score=0.8)], count_tokens=len
    )
    assert len(bundle.chunks) == 1
    assert bundle.chunks[0].methods == ("KEYWORD", "VECTOR")
    assert not bundle.limited


def test_identical_quote_at_same_position_is_not_repeated_under_different_chunk_ids():
    first = chunk("same quote")
    duplicate = replace(first, chunk_id=uuid4(), methods=("VECTOR",))
    bundle = compose_evidence([first, duplicate], count_tokens=len)
    assert len(bundle.chunks) == 1
    assert set(bundle.chunks[0].methods) == {"KEYWORD", "VECTOR"}
    assert bundle.passages[0].text == first.text and not bundle.limited


def test_quote_above_receipt_character_limit_is_omitted_with_explicit_limitation():
    oversized = chunk("x" * 4001)
    normal = chunk("normal")
    bundle = compose_evidence([oversized, normal], max_tokens=10000, count_tokens=len)
    assert bundle.chunks == (normal,) and bundle.limited


def test_direct_priority_and_episode_diversity_prevent_single_chapter_monopoly():
    dominant, other, direct_episode = uuid4(), uuid4(), uuid4()
    candidates = [chunk(f"dense {n}", episode=dominant, start=n * 20, score=0.99) for n in range(5)]
    direct = chunk("direct", episode=direct_episode, method="DIRECT", score=0)
    alternative = chunk("other", episode=other, score=0.5)
    bundle = compose_evidence(
        [*candidates, alternative, direct],
        max_items=3,
        per_episode=3,
        max_tokens=100,
        count_tokens=len,
    )
    assert bundle.chunks[0] == direct
    assert {c.episode_id for c in bundle.chunks} == {dominant, other, direct_episode}
    assert bundle.limited


def test_context_budget_and_per_episode_budget_are_enforced_without_silent_slicing():
    episode = uuid4()
    first = chunk("first", episode=episode)
    second = chunk("second", episode=episode, start=30)
    other = chunk("other")
    bundle = compose_evidence([first, second, other], per_episode=1, max_tokens=8, count_tokens=len)
    assert bundle.limited and bundle.token_count <= 8
    assert all(c.text in {"first", "second", "other"} for c in bundle.chunks)
    assert len(bundle.chunks) == 1


def test_contained_duplicate_ranges_merge_without_duplicating_text():
    first = chunk("0123456789")
    nested = chunk("234", episode=first.episode_id, start=2)
    result = compose_evidence([first, nested], count_tokens=len)
    assert result.passages[0].text == first.text
    assert result.token_count == 10


def test_different_versions_or_quote_positions_are_not_deduplicated():
    first = chunk("same")
    other_position = chunk("same", episode=first.episode_id, start=20)
    other_version = replace(chunk("same", episode=first.episode_id), source_version="v2")
    bundle = compose_evidence([first, other_position, other_version], count_tokens=len)
    assert len(bundle.passages) == len(bundle.chunks) == 3


def test_invalid_hash_offsets_and_conflicting_references_are_rejected():
    first = chunk("012345")
    bad_hash = replace(chunk(), chunk_hash="b" * 64)
    bad_offset = replace(chunk(), end_offset=999)
    conflict = replace(first, source_hash="c" * 64)
    bundle = compose_evidence([first, bad_hash, bad_offset, conflict], count_tokens=len)
    assert not bundle.chunks and bundle.limited and bundle.rejected_count == 4


def test_conflicting_overlap_never_creates_a_fabricated_passage():
    first = chunk("abcdef")
    conflicting = chunk("ZZZZ", episode=first.episode_id, start=3, score=0.5)
    bundle = compose_evidence([first, conflicting], count_tokens=len)
    assert bundle.chunks == (first,)
    assert bundle.passages[0].text == "abcdef"
    assert bundle.rejected_count == 1 and bundle.limited


@pytest.mark.parametrize("settings", [dict(max_items=0), dict(max_tokens=0), dict(per_episode=13)])
def test_invalid_budgets_are_rejected(settings):
    with pytest.raises(ValueError):
        compose_evidence([], **settings)
