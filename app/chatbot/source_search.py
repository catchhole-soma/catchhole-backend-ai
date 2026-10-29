"""Bounded source-query planning; no story names or inferred facts enter scope."""

import re
from dataclasses import replace
from itertools import zip_longest

from app.chatbot.qa_validation import ChatResponseContractError


def validate_part_episodes(plan, question):
    # A part may narrow to a single explicitly named chapter. Range expressions
    # retain the outer range; neither conversation nor generated prose is proof.
    observed = {int(n) for n in re.findall(r"(?<![\d~～–—-])(\d+)\s*화", question)}
    for part in plan.requested_parts:
        if part.source_episode_no is not None and part.source_episode_no not in observed:
            raise ChatResponseContractError("UNOBSERVED_PART_EPISODE")
    episodes = {p.source_episode_no for p in plan.requested_parts}
    if (
        plan.episode_from is not None
        and plan.episode_from == plan.episode_to
        and plan.episode_from in episodes
        and None in episodes
    ):
        # A single chapter cannot simultaneously be a whole-question filter and
        # only one part's bound. Ask the planner to resolve its own contradiction;
        # never widen a user scope or assign the missing part a chapter ourselves.
        raise ChatResponseContractError("MIXED_PART_SOURCE_RANGE")


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


def explicit_episode_seeds(plan, seeds):
    """Priority hint only: never filter away other parts or widen authorized scope."""
    episodes = {
        p.source_episode_no for p in plan.requested_parts if p.source_episode_no is not None
    }
    return sorted(seeds, key=lambda c: c.episode_no not in episodes)


def independent_scene_seeds(seeds):
    """Give independent scenes a neighbor turn before repeated hits in one source."""
    seen, anchors = set(), []
    for seed in seeds:
        if seed.source_fingerprint not in seen:
            anchors.append(seed)
            seen.add(seed.source_fingerprint)
        if len(anchors) == 3:
            break
    selected = {c.chunk_id for c in anchors}
    return [*anchors, *(c for c in seeds if c.chunk_id not in selected)]


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
        # Cross-source neighbors are only returned at actual chapter boundaries
        # by the opt-in repository path. Keep their separate source identities.
        result.extend(
            c
            for c in neighbors
            if "NEIGHBOR" in c.methods and abs(c.episode_no - seed.episode_no) == 1
        )
        return result

    # Share the bounded context among a few independent scenes. One leading
    # scene must not consume every neighbor slot before another ranked hit gets
    # its surrounding sentence. Extra bare hits still keep their original rank.
    anchors = [anchor]
    for seed in independent_scene_seeds(seeds)[1:3]:
        if any(c.source_fingerprint == seed.source_fingerprint for c in anchors):
            continue
        if len(anchors) == 1 or window(seed):
            anchors.append(seed)
        if len(anchors) == 3:
            break
    adjacent = [c for group in zip_longest(*(window(s) for s in anchors)) for c in group if c]
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
                *anchors,
                *adjacent,
                *seeds,
            ]
        }.values()
    )
