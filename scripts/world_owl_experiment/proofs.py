"""Enumerate all subset-minimal supports using a monotone OWL decision oracle.

These are justifications (sets of necessary premises), not engine derivation traces.
Every other minimal support must omit an element of the support just found. Explore
those omissions until the finite search is exhausted, or fail explicitly on budget.
"""

from __future__ import annotations


class ProofLimitError(RuntimeError):
    pass


def minimal_supports(ids, predicate, *, max_nodes=8192):
    todo = [frozenset(ids)]
    visited = set()
    answers = set()
    while todo:
        allowed = todo.pop()
        if allowed in visited:
            continue
        if len(visited) >= max_nodes:
            raise ProofLimitError("minimal-support enumeration incomplete: node limit")
        visited.add(allowed)
        if not predicate(allowed):
            continue
        minimal = allowed
        for key in sorted(allowed):
            candidate = minimal - {key}
            if predicate(candidate):
                minimal = candidate
        answers.add(minimal)
        # Reverse insertion makes the search order deterministic.
        for key in sorted(minimal, reverse=True):
            todo.append(allowed - {key})
    return sorted([sorted(answer) for answer in answers])
