from collections.abc import Iterable
from collections.abc import Set as AbstractSet


def world_setting_ref_mapping(
    expected_refs: AbstractSet[str],
    actual_refs: AbstractSet[str],
    approved_pairs: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Align scoring refs without renaming stored state or merging duplicate facts.

    Inputs use the ``fact:`` keys from evaluation state values. Each approved pair
    is (expected_ref, actual_ref); its category, subject and scope equivalence must
    already have been checked by the caller. Exact refs take precedence. Conflicting
    semantic correspondences remain unmatched rather than being chosen arbitrarily.
    """

    expected = {ref for ref in expected_refs if _is_world_fact_ref(ref)}
    actual = {ref for ref in actual_refs if _is_world_fact_ref(ref)}
    exact = expected & actual
    result = {ref: ref for ref in sorted(exact)}
    expected_by_actual: dict[str, set[str]] = {}
    actual_by_expected: dict[str, set[str]] = {}
    for expected_ref, actual_ref in approved_pairs:
        if (
            expected_ref not in expected
            or actual_ref not in actual
            or expected_ref in exact
            or actual_ref in exact
        ):
            continue
        expected_by_actual.setdefault(actual_ref, set()).add(expected_ref)
        actual_by_expected.setdefault(expected_ref, set()).add(actual_ref)
    for actual_ref in sorted(expected_by_actual):
        candidates = expected_by_actual[actual_ref]
        if len(candidates) != 1:
            continue
        expected_ref = next(iter(candidates))
        if len(actual_by_expected[expected_ref]) == 1:
            result[actual_ref] = expected_ref
    return result


def _is_world_fact_ref(ref: str) -> bool:
    parts = ref.split(":")
    return (
        len(parts) in {6, 7}
        and parts[:2] == ["fact", "gold"]
        and parts[2] in {"world", "world-by-subject-ref"}
        and all(parts[3:])
    )
