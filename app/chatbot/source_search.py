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


def scene_priority(seeds, neighbors):
    """Keep two independent hits and a small coherent window around the best hit.

    Neighbors were authorized/revalidated by retrieval. Reserve at most two slots
    for the leading scene, leaving the remaining rank order and all caps intact.
    """
    if not seeds:
        return []
    anchor = seeds[0]
    adjacent = [
        c
        for c in neighbors
        if c.source_fingerprint == anchor.source_fingerprint
        and abs(c.chunk_index - anchor.chunk_index) == 1
    ]
    adjacent.sort(key=lambda c: c.chunk_index)
    return [*seeds[:2], *adjacent[:2], *seeds[2:]]
