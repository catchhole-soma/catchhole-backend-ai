"""Small, deterministic boundaries around CHARACTER semantic scoring."""

import json
import re
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from evals.multi_stage_setting.contracts import infer_character_fact_type
from evals.setting_extraction.normalization import normalize_fact_key, normalize_text

if TYPE_CHECKING:
    from app.analysis.setting_extractor import CharacterSettingSchemaHint


def character_fact_key_spelling_matches(expected: str | None, actual: str | None) -> bool:
    """Ignore Korean word separators for scoring without changing stored keys or namespaces."""
    if not expected or not actual:
        return False

    def spelling(key: str) -> str:
        normalized = normalize_fact_key(normalize_text(key))
        namespace, separator, leaf = normalized.rpartition(".")
        return namespace + separator + re.sub(r"(?<=[가-힣])_+(?=[가-힣])", "", leaf)

    return spelling(expected) == spelling(actual)


def dynamic_status_key_pair(
    fact_type: str,
    expected_key: str | None,
    actual_key: str | None,
    *,
    schema_hints: Sequence["CharacterSettingSchemaHint"] = (),
) -> bool:
    """Whether differing keys may ask an LLM about the same dynamic STATUS.

    The caller must already require the same person and FactType. Schema exact
    and alias matches take precedence over patterns, as in the runtime resolver.
    Without a schema fixture, only the built-in ``status.*`` ORACLE fallback is
    available. This helper grants eligibility, never semantic equivalence.
    """

    if fact_type != "STATUS" or not expected_key or not actual_key:
        return False
    expected_key, actual_key = expected_key.strip(), actual_key.strip()
    if character_fact_key_spelling_matches(expected_key, actual_key):
        return False
    if not schema_hints:
        return all(_pattern_matches("status.*", key) for key in (expected_key, actual_key))
    expected_pattern = _resolved_status_pattern(expected_key, schema_hints)
    return expected_pattern is not None and expected_pattern == _resolved_status_pattern(
        actual_key, schema_hints
    )


def _resolved_status_pattern(
    key: str, schemas: Sequence["CharacterSettingSchemaHint"]
) -> int | None:
    if any(schema.schema_key.strip() == key for schema in schemas):
        return None
    for schema in schemas:
        schema_key = schema.schema_key.strip()
        namespace = schema_key.rsplit(".", 1)[0] + "." if "." in schema_key else ""
        if any(
            alias.strip()
            and "." not in alias.strip()
            and key in {alias.strip(), namespace + alias.strip()}
            for alias in schema.aliases
        ):
            return None
    matched = [
        index
        for index, schema in enumerate(schemas)
        if _pattern_matches(schema.attribute_pattern, key)
    ]
    if len(matched) != 1:
        return None
    schema = schemas[matched[0]]
    resolved_type = schema.canonical_fact_type or infer_character_fact_type(schema.schema_key)
    return matched[0] if resolved_type == "STATUS" else None


def _pattern_matches(pattern: str | None, key: str) -> bool:
    if pattern is None:
        return False
    pattern = pattern.strip()
    return (
        pattern.endswith(".*")
        and pattern.find("*") == len(pattern) - 1
        and key.startswith(pattern[:-1])
        and bool(key[len(pattern) - 1 :].strip())
        and "*" not in key
    )


def character_setting_ref_mapping(
    expected_refs: AbstractSet[str],
    actual_refs: AbstractSet[str],
    approved_pairs: Iterable[tuple[str, str]],
    *,
    schema_hints: Sequence["CharacterSettingSchemaHint"] = (),
    history_source_matches: AbstractSet[tuple[str, str, str]] = frozenset(),
) -> dict[str, str]:
    """Map actual scoring refs to expected refs, preserving identity and duplicates.

    Pairs are (expected, actual), already approved by the semantic judge. Facts
    and history (including their JSON variants) are supported, but each pair must
    have the same ref kind, person, FactType and history provenance/operation.
    Callers supply each approved ref pair explicitly; this never creates facts,
    changes reducer input, or rewrites target/removal references.
    """

    expected = {ref: _character_ref_parts(ref) for ref in expected_refs}
    actual = {ref: _character_ref_parts(ref) for ref in actual_refs}
    expected = {ref: parts for ref, parts in expected.items() if parts is not None}
    actual = {ref: parts for ref, parts in actual.items() if parts is not None}
    exact = expected.keys() & actual.keys()
    result = {ref: ref for ref in sorted(exact)}
    expected_by_actual: dict[str, set[str]] = {}
    actual_by_expected: dict[str, set[str]] = {}
    for expected_ref, actual_ref in approved_pairs:
        left, right = expected.get(expected_ref), actual.get(actual_ref)
        if (
            left is None
            or right is None
            or expected_ref in exact
            or actual_ref in exact
            or not _same_ref_identity(left[0], right[0], history_source_matches)
            or not (
                character_fact_key_spelling_matches(left[2], right[2])
                or dynamic_status_key_pair(left[1], left[2], right[2], schema_hints=schema_hints)
            )
        ):
            continue
        expected_by_actual.setdefault(actual_ref, set()).add(expected_ref)
        actual_by_expected.setdefault(expected_ref, set()).add(actual_ref)
    for actual_ref, candidates in sorted(expected_by_actual.items()):
        if len(candidates) == 1:
            expected_ref = next(iter(candidates))
            if len(actual_by_expected[expected_ref]) == 1:
                result[actual_ref] = expected_ref
    return result


def _same_ref_identity(
    left: tuple[str, ...], right: tuple[str, ...], source_matches: AbstractSet[tuple[str, str, str]]
) -> bool:
    if left == right:
        return True
    return (
        len(left) == len(right) == 6
        and left[:2] == right[:2]
        and left[3:] == right[3:]
        and (left[1], left[2], right[2]) in source_matches
    )


def _character_ref_parts(ref: str) -> tuple[tuple[str, ...], str, str] | None:
    kind, separator, tail = ref.partition(":")
    if not separator:
        return None
    if kind in {"fact", "fact-json"}:
        parts = tail.split(":")
        if len(parts) != 5 or parts[:2] != ["gold", "character"] or not all(parts):
            return None
        entity, fact_type, key = (unquote(part) for part in parts[2:])
        return (kind, entity, fact_type), fact_type, key
    if kind in {"history", "history-json"}:
        try:
            parts = json.loads(tail)
        except (ValueError, TypeError):
            return None
        if (
            not isinstance(parts, list)
            or len(parts) != 6
            or not all(isinstance(part, str) and part for part in parts)
        ):
            return None
        scenario, source, entity, fact_type, key, operation = parts
        return (kind, scenario, source, entity, fact_type, operation), fact_type, key
    return None


@dataclass(frozen=True)
class StructuredTextPair:
    path: str
    expected: str
    actual: str


@dataclass(frozen=True)
class StructuredSemanticComparison:
    structural_matched: bool
    text_pairs: tuple[StructuredTextPair, ...]


def compare_structured_semantics(
    expected: Any,
    actual: Any,
    *,
    strict_text_paths: AbstractSet[str] = frozenset(),
) -> StructuredSemanticComparison:
    """Validate a native JSON Gold subset and collect differing string leaves.

    Object extras are allowed; required fields, array length/order, scalar types,
    numbers, booleans and null remain strict. Named identity/enum fields remain
    exact strings; schemas with additional fixed string fields can supply their
    JSON Pointer paths in ``strict_text_paths``. Other string fields are eligible
    for semantic judgment, without guessing their meaning from lexical patterns.
    A caller may judge text only after ``structural_matched`` is true; an empty
    text-pair tuple alone does not mean the values match.
    """

    text_pairs: list[StructuredTextPair] = []

    def compare(left: Any, right: Any, path: str, strict_text: bool = False) -> bool:
        strict_text = strict_text or path in strict_text_paths
        if isinstance(left, dict):
            if not isinstance(right, dict):
                return False
            results = [
                key in right
                and compare(
                    value,
                    right[key],
                    path + "/" + _pointer_key(key),
                    strict_text or _is_structural_string_field(key),
                )
                for key, value in left.items()
            ]
            return all(results)
        if isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                return False
            results = [
                compare(value, right[index], path + f"/{index}", strict_text)
                for index, value in enumerate(left)
            ]
            return all(results)
        if isinstance(left, bool):
            return isinstance(right, bool) and left == right
        if isinstance(left, (int, float, Decimal)):
            return (
                isinstance(right, (int, float, Decimal))
                and not isinstance(right, bool)
                and Decimal(str(left)).is_finite()
                and Decimal(str(right)).is_finite()
                and Decimal(str(left)) == Decimal(str(right))
            )
        if left is None:
            return right is None
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        if strict_text:
            return left == right
        if normalize_text(left) != normalize_text(right):
            text_pairs.append(StructuredTextPair(path, left, right))
        return True

    matched = compare(expected, actual, "")
    return StructuredSemanticComparison(matched, tuple(text_pairs))


def _pointer_key(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _is_structural_string_field(key: str) -> bool:
    # Preserve schema identities and enums without treating arbitrary prose as
    # an enum because it happens to be short or written in uppercase.
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    parts = [part.casefold() for part in re.split(r"[_\-\s]+", separated) if part]
    return bool(parts) and (
        parts[-1] in {"id", "ids", "ref", "refs", "key", "keys", "type", "types"}
        or "".join(parts)
        in {
            "operation",
            "temporalscope",
            "status",
            "state",
            "mode",
            "kind",
            "code",
            "unit",
            "currency",
            "consolidation",
            "consolidationstatus",
        }
    )
