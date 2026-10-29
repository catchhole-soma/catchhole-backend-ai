"""Bounded source-query planning; no story names or inferred facts enter scope."""

import re
from dataclasses import replace

from app.chatbot.qa_validation import ChatResponseContractError


def validate_part_episodes(plan, question):
    # A part may narrow to a single explicitly named chapter. Range expressions
    # retain the outer range; neither conversation nor generated prose is proof.
    observed = {int(n) for n in re.findall(r"(?<![\d~～–—-])(\d+)\s*화", question)}
    for part in plan.requested_parts:
        if part.source_episode_no is not None and part.source_episode_no not in observed:
            raise ChatResponseContractError("UNOBSERVED_PART_EPISODE")


def missing_scope(plan, draft, scope):
    """Only narrow when every missing requirement names the same explicit chapter."""
    if draft is None or not draft.missing_facets:
        return scope
    parts = [p for p in plan.requested_parts if p.ref in draft.missing_facets]
    episodes = {p.source_episode_no for p in parts}
    if len(parts) != len(set(draft.missing_facets)) or len(episodes) != 1:
        return scope
    episode = next(iter(episodes))
    if episode is None or not scope.episode_from <= episode <= scope.episode_to:
        return scope
    return replace(scope, episode_from=episode, episode_to=episode)


def scene_priority(seeds, neighbors, retained=()):
    """Read a small continuous scene before another mention of the same event.

    Adjacent hits can already be in seeds (and thus absent from neighbors). They
    still belong in the scene window. Only returned, authorized candidates are
    ordered here; source order never proves narrative chronology.
    """
    if not seeds:
        return []
    anchor = seeds[0]
    pool = list({c.chunk_id: c for c in [*seeds, *neighbors]}.values())

    def window(seed):
        same = {c.chunk_index: c for c in pool if c.source_fingerprint == seed.source_fingerprint}
        result = []
        for direction in (1, -1):
            for distance in (1, 2):
                candidate = same.get(seed.chunk_index + direction * distance)
                if candidate is None:
                    break  # Never bridge an unread chunk.
                result.append(candidate)
        return result

    adjacent = window(anchor)
    independent = next(
        (c for c in seeds[1:] if c.source_fingerprint != anchor.source_fingerprint), None
    )
    continuation = [
        c
        for c in [*seeds, *neighbors]
        if any(
            c.source_fingerprint == old.source_fingerprint
            and abs(c.chunk_index - old.chunk_index) == 1
            for old in retained
        )
    ]
    return list(
        {
            c.chunk_id: c
            for c in [
                *continuation,
                anchor,
                *([independent] if independent else []),
                *adjacent,
                *seeds,
            ]
        }.values()
    )
