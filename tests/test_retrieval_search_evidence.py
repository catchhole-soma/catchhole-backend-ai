from dataclasses import asdict, replace
from hashlib import sha256
from uuid import uuid4

import pytest

from app.retrieval.chunking import POLICY
from app.retrieval.evidence import EvidenceBundle, compose_evidence
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


def omission_counts(bundle):
    counts = {omission.reason: omission.count for omission in bundle.omissions}
    assert len(counts) == len(bundle.omissions)
    assert all(count > 0 for count in counts.values())
    assert bundle.limited == bool(counts)
    assert bundle.input_count == bundle.duplicate_count + len(bundle.chunks) + sum(counts.values())
    return counts


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
    assert omission_counts(bundle) == {}
    assert bundle.input_count == bundle.candidate_count == 3
    assert bundle.duplicate_count == 0


def test_same_chunk_from_multiple_methods_is_deduplicated_but_provenance_is_preserved():
    original = chunk()
    bundle = compose_evidence(
        [original, replace(original, methods=("VECTOR",), score=0.8)], count_tokens=len
    )
    assert len(bundle.chunks) == 1
    assert bundle.chunks[0].methods == ("KEYWORD", "VECTOR")
    assert not bundle.limited
    assert omission_counts(bundle) == {}
    assert bundle.input_count == 2
    assert bundle.duplicate_count == bundle.candidate_count == 1


def test_identical_quote_at_same_position_is_not_repeated_under_different_chunk_ids():
    first = chunk("same quote")
    duplicate = replace(first, chunk_id=uuid4(), methods=("VECTOR",))
    bundle = compose_evidence([first, duplicate], count_tokens=len)
    assert len(bundle.chunks) == 1
    assert set(bundle.chunks[0].methods) == {"KEYWORD", "VECTOR"}
    assert bundle.passages[0].text == first.text and not bundle.limited
    assert omission_counts(bundle) == {}
    assert bundle.input_count == 2
    assert bundle.duplicate_count == bundle.candidate_count == 1


def test_quote_above_receipt_character_limit_is_omitted_with_explicit_limitation():
    oversized = chunk("x" * 4001)
    normal = chunk("normal")
    bundle = compose_evidence([oversized, normal], max_tokens=10000, count_tokens=len)
    assert bundle.chunks == (normal,) and bundle.limited
    assert omission_counts(bundle) == {"QUOTE_CHARACTER_LIMIT": 1}
    assert bundle.rejected_count == 0  # Preserve the existing rejection-count contract.


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
    assert omission_counts(bundle) == {"ITEM_LIMIT": 4}
    assert bundle.input_count == bundle.candidate_count == 7


def test_ranked_composition_keeps_second_strong_hit_before_weaker_episode_diversity():
    episodes = {number: uuid4() for number in (3, 8, 12, 18, 25, 40, 60, 70)}
    ranked = []
    for number, index, score in (
        (40, 0, 1.0),
        (60, 0, 1.0),
        (70, 0, 1.0),
        (3, 0, 0.7),
        (3, 4, 0.7),
        (8, 0, 0.7),
        (12, 0, 0.7),
        (18, 0, 0.7),
        (25, 0, 0.7),
    ):
        ranked.append(
            replace(
                chunk(
                    f"passage {number}/{index}",
                    episode=episodes[number],
                    start=index * 100,
                    score=score,
                ),
                episode_no=number,
                chunk_index=index,
            )
        )
    options = dict(max_items=8, per_episode=3, max_tokens=6000, count_tokens=len)
    default = compose_evidence(list(reversed(ranked)), **options)
    selected = compose_evidence(list(reversed(ranked)), episode_round_robin=False, **options)
    assert ranked[4] not in default.chunks
    assert default.chunks[-1] == ranked[-1]
    assert selected.chunks == tuple(ranked[:8])
    assert omission_counts(selected) == {"ITEM_LIMIT": 1}


def test_ranked_composition_keeps_direct_retained_and_all_capacity_guards():
    episode = uuid4()
    candidates = [
        replace(chunk(f"hit{n}", episode=episode, start=n * 10, score=1 - n / 10), chunk_index=n)
        for n in range(4)
    ]
    direct = chunk("direct", method="DIRECT", score=0)
    retained = chunk("retained", method="VECTOR", score=0)
    neighbor = chunk("neighbor", method="NEIGHBOR", score=1)
    result = compose_evidence(
        [neighbor, *reversed(candidates), direct, retained],
        episode_round_robin=False,
        retained_chunk_ids=[retained.chunk_id],
        max_items=8,
        per_episode=3,
        max_tokens=26,
        count_tokens=len,
    )
    assert result.chunks == (retained, direct, *candidates[:3])
    assert result.token_count == 26
    assert omission_counts(result) == {"EPISODE_LIMIT": 1, "TOKEN_LIMIT": 1}


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
    assert omission_counts(result) == {}
    assert len(result.chunks) == 2  # Merging passages never erases original references.
    assert result.duplicate_count == 0


def test_different_versions_or_quote_positions_are_not_deduplicated():
    first = chunk("same")
    other_position = chunk("same", episode=first.episode_id, start=20)
    other_version = replace(chunk("same", episode=first.episode_id), source_version="v2")
    bundle = compose_evidence([first, other_position, other_version], count_tokens=len)
    assert len(bundle.passages) == len(bundle.chunks) == 3
    assert omission_counts(bundle) == {}
    assert bundle.duplicate_count == 0


def test_invalid_hash_offsets_and_conflicting_references_are_rejected():
    first = chunk("012345")
    bad_hash = replace(chunk(), chunk_hash="b" * 64)
    bad_offset = replace(chunk(), end_offset=999)
    conflict = replace(first, source_hash="c" * 64)
    bundle = compose_evidence([first, bad_hash, bad_offset, conflict], count_tokens=len)
    assert not bundle.chunks and bundle.limited and bundle.rejected_count == 4
    assert omission_counts(bundle) == {"INVALID_CHUNK": 2, "CONFLICTING_REFERENCE": 2}
    assert bundle.candidate_count == 0


def test_conflicting_overlap_never_creates_a_fabricated_passage():
    first = chunk("abcdef")
    conflicting = chunk("ZZZZ", episode=first.episode_id, start=3, score=0.5)
    bundle = compose_evidence([first, conflicting], count_tokens=len)
    assert bundle.chunks == (first,)
    assert bundle.passages[0].text == "abcdef"
    assert bundle.rejected_count == 1 and bundle.limited
    assert omission_counts(bundle) == {"CONFLICTING_OVERLAP": 1}
    assert bundle.candidate_count == 2


@pytest.mark.parametrize(
    ("maximum", "per_episode", "reason"),
    [(1, 1, "ITEM_LIMIT"), (2, 1, "EPISODE_LIMIT")],
)
def test_only_first_blocking_limit_is_counted_per_candidate(maximum, per_episode, reason):
    episode = uuid4()
    first = chunk("first", episode=episode, score=1)
    second = chunk("second", episode=episode, start=10, score=0.5)
    bundle = compose_evidence(
        [first, second], max_items=maximum, per_episode=per_episode, count_tokens=len
    )
    assert bundle.chunks == (first,)
    assert omission_counts(bundle) == {reason: 1}
    assert bundle.rejected_count == 0


def test_token_limit_omits_whole_candidate_but_keeps_later_fitting_candidate():
    episode = uuid4()
    first = chunk("first", episode=episode, score=1)
    too_long = chunk("too long", episode=episode, start=20, score=0.9)
    fitting = chunk("!", episode=episode, start=40, score=0.8)
    bundle = compose_evidence(
        [first, too_long, fitting], max_tokens=6, per_episode=3, count_tokens=len
    )
    assert bundle.chunks == (first, fitting)
    assert bundle.token_count == 6
    assert [passage.text for passage in bundle.passages] == ["first", "!"]
    assert omission_counts(bundle) == {"TOKEN_LIMIT": 1}
    assert bundle.rejected_count == 0


def test_exact_duplicates_do_not_consume_reference_or_episode_budget():
    original = chunk("same source")
    same_id = replace(original, methods=("VECTOR",))
    same_position = replace(original, chunk_id=uuid4(), methods=("NEIGHBOR",))
    bundle = compose_evidence(
        [original, same_id, same_position],
        max_items=1,
        per_episode=1,
        max_tokens=len(original.text),
        count_tokens=len,
    )
    assert omission_counts(bundle) == {}
    assert bundle.input_count == 3 and bundle.duplicate_count == 2
    assert bundle.candidate_count == len(bundle.chunks) == len(bundle.passages) == 1
    assert set(bundle.chunks[0].methods) == {"KEYWORD", "VECTOR", "NEIGHBOR"}


def test_conflicting_reference_diagnostics_include_later_reappearances():
    original = chunk("first")
    duplicate = replace(original, methods=("VECTOR",))
    conflicting = replace(original, source_hash="c" * 64)
    bundle = compose_evidence([original, duplicate, conflicting, original], count_tokens=len)
    assert not bundle.chunks
    assert bundle.input_count == 4 and bundle.duplicate_count == 1
    assert bundle.rejected_count == 3
    assert omission_counts(bundle) == {"CONFLICTING_REFERENCE": 3}


def test_omission_diagnostics_contain_only_fixed_codes_and_aggregate_counts():
    secret = "private-source-text"
    invalid = replace(chunk(secret), chunk_hash="b" * 64)
    bundle = compose_evidence([invalid], count_tokens=len)
    assert [asdict(omission) for omission in bundle.omissions] == [
        {"reason": "INVALID_CHUNK", "count": 1}
    ]
    assert secret not in repr(bundle.omissions)
    assert str(invalid.chunk_id) not in repr(bundle.omissions)
    assert invalid.source_key not in repr(bundle.omissions)
    assert omission_counts(bundle) == {"INVALID_CHUNK": 1}


def test_existing_positional_bundle_constructor_remains_compatible():
    bundle = EvidenceBundle((), (), 0, False, 0)
    assert bundle.omissions == ()
    assert bundle.input_count == bundle.duplicate_count == bundle.candidate_count == 0


@pytest.mark.parametrize("settings", [dict(max_items=0), dict(max_tokens=0), dict(per_episode=101)])
def test_invalid_budgets_are_rejected(settings):
    with pytest.raises(ValueError):
        compose_evidence([], **settings)
