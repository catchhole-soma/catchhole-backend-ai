from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonDecision,
)
from app.domain.enums import (
    CharacterFactComparisonOperation,
    CharacterFactTemporalScope,
    SettingValueType,
)
from app.domain.setting_values import normalize_setting_display_value


@dataclass(frozen=True)
class CharacterProjectionEntry:
    """A request-local active snapshot entry.

    ``reference`` is either a persisted ``P*`` ref supplied by Spring or the
    ``Q*`` projected ref of an earlier decision in this batch. Backend identifiers never
    enter this model or the provider prompt.
    """

    reference: str
    fact_type: str
    fact_key: str
    fact_value: str | None
    value_json: Any | None
    origin: str = "PERSISTED"
    source_candidate_ref: str | None = None
    dependency_candidate_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CharacterProjectionApplication:
    decision: CharacterFactComparisonDecision
    projected_entry: CharacterProjectionEntry | None
    dependency_candidate_refs: tuple[str, ...]


class CharacterProjectionState:
    """Sequential, in-memory character snapshot used during one batch call."""

    def __init__(self, entries: list[CharacterProjectionEntry]) -> None:
        self._entries_by_ref: dict[str, CharacterProjectionEntry] = {}
        self._ref_by_slot: dict[tuple[str, str], str] = {}
        for entry in entries:
            if entry.reference in self._entries_by_ref:
                raise ValueError(f"Duplicate snapshot ref: {entry.reference}")
            slot = (entry.fact_type, entry.fact_key)
            if slot in self._ref_by_slot:
                raise ValueError("Canonical snapshot slot must be unique.")
            self._entries_by_ref[entry.reference] = entry
            self._ref_by_slot[slot] = entry.reference

    @property
    def entries(self) -> list[CharacterProjectionEntry]:
        return list(self._entries_by_ref.values())

    @property
    def entries_by_ref(self) -> dict[str, CharacterProjectionEntry]:
        return dict(self._entries_by_ref)

    def exact_target_ref(self, fact_type: str, fact_key: str) -> str | None:
        return self._ref_by_slot.get((fact_type, fact_key))

    def apply(
        self,
        *,
        candidate_ref: str,
        projected_snapshot_ref: str,
        fact_type: str,
        resolved_fact_key: str,
        value_type: SettingValueType | None,
        candidate_value_json: Any | None,
        decision: CharacterFactComparisonDecision,
    ) -> CharacterProjectionApplication:
        validate_character_fact_decision(
            decision,
            candidate_fact_type=fact_type,
            resolved_fact_key=resolved_fact_key,
            candidate_value_type=value_type,
            candidate_value_json=candidate_value_json,
            entries_by_ref=self._entries_by_ref,
        )

        dependency_candidate_refs = self._dependencies_for(
            [
                *decision.removed_snapshot_refs,
                *([] if decision.target_ref is None else [decision.target_ref]),
            ]
        )
        for removed_ref in decision.removed_snapshot_refs:
            self._remove(removed_ref)

        projected_entry: CharacterProjectionEntry | None = None
        if decision.operation in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
        }:
            if decision.target_ref is not None:
                self._remove(decision.target_ref)
            if projected_snapshot_ref in self._entries_by_ref:
                raise ValueError(
                    f"Projected snapshot ref is already active: {projected_snapshot_ref}"
                )
            projected_entry = CharacterProjectionEntry(
                reference=projected_snapshot_ref,
                fact_type=fact_type,
                fact_key=resolved_fact_key,
                fact_value=decision.proposed_fact_value,
                value_json=decision.proposed_value_json,
                origin="PRIOR_DECISION",
                source_candidate_ref=candidate_ref,
                dependency_candidate_refs=tuple(
                    [*dependency_candidate_refs, candidate_ref]
                ),
            )
            self._insert(projected_entry)

        return CharacterProjectionApplication(
            decision=decision,
            projected_entry=projected_entry,
            dependency_candidate_refs=dependency_candidate_refs,
        )

    def _insert(self, entry: CharacterProjectionEntry) -> None:
        slot = (entry.fact_type, entry.fact_key)
        if slot in self._ref_by_slot:
            raise ValueError("Projected canonical snapshot slot must be unique.")
        self._entries_by_ref[entry.reference] = entry
        self._ref_by_slot[slot] = entry.reference

    def _remove(self, reference: str) -> None:
        entry = self._entries_by_ref.pop(reference)
        self._ref_by_slot.pop((entry.fact_type, entry.fact_key), None)

    def _dependencies_for(self, references: list[str]) -> tuple[str, ...]:
        dependencies: set[str] = set()
        for reference in references:
            entry = self._entries_by_ref[reference]
            dependencies.update(entry.dependency_candidate_refs)
            if entry.source_candidate_ref is not None:
                dependencies.add(entry.source_candidate_ref)
        # removed refs의 반환 순서가 달라도 complete/audit/hash는 같은 chronology를
        # 가져야 한다. C10을 C2보다 앞세우는 문자열 정렬 대신 numeric ref 순서로 고정한다.
        return tuple(sorted(dependencies, key=_candidate_ref_sort_key))


def _candidate_ref_sort_key(reference: str) -> tuple[int, int | str]:
    match = re.fullmatch(r"C([1-9][0-9]*)", reference)
    if match is None:
        return (1, reference)
    return (0, int(match.group(1)))


def validate_resolved_canonical_fact_key(
    *,
    initial_fact_key: str,
    resolved_fact_key: str,
    canonical_key_resolution: str,
    fact_type: str,
) -> None:
    """Keep exact/alias keys fixed; only a pattern STATUS key may normalize."""

    mutable_pattern_status = (
        canonical_key_resolution == "PATTERN" and fact_type == "STATUS"
    )
    if canonical_key_resolution in {"EXACT", "ALIAS", "PATTERN"} and not (
        mutable_pattern_status
    ):
        if resolved_fact_key != initial_fact_key:
            raise ValueError("A fixed canonical Fact key must not be changed.")
        return
    if not mutable_pattern_status:
        raise ValueError("Only a pattern STATUS key may be semantically normalized.")
    suffix = (
        resolved_fact_key.removeprefix("status.")
        if resolved_fact_key.startswith("status.")
        else ""
    )
    if (
        not suffix
        or len(resolved_fact_key) > 150
        or any(not (character.isalnum() or character in "_-") for character in suffix)
    ):
        raise ValueError("Resolved STATUS key must use the status.* canonical form.")


def validate_character_fact_decision(
    decision: CharacterFactComparisonDecision,
    *,
    candidate_fact_type: str,
    resolved_fact_key: str,
    candidate_value_type: SettingValueType | None,
    candidate_value_json: Any | None,
    entries_by_ref: dict[str, CharacterProjectionEntry],
) -> None:
    """Shared #126 operation validator for legacy and projected batch flows."""

    validate_status_active_value(
        candidate_fact_type,
        candidate_value_json,
        field_name="candidate.value_json",
    )
    requested_refs = set(decision.removed_snapshot_refs)
    if decision.target_ref is not None:
        requested_refs.add(decision.target_ref)
    unknown_refs = requested_refs - entries_by_ref.keys()
    if unknown_refs:
        raise ValueError(f"Unknown snapshot refs: {sorted(unknown_refs)}")

    exact_target_ref = next(
        (
            reference
            for reference, entry in entries_by_ref.items()
            if entry.fact_type == candidate_fact_type and entry.fact_key == resolved_fact_key
        ),
        None,
    )

    if decision.target_ref is not None:
        target = entries_by_ref[decision.target_ref]
        if target.fact_type != candidate_fact_type or target.fact_key != resolved_fact_key:
            raise ValueError("UPDATE and MERGE must target the resolved canonical Fact key.")
        if decision.target_ref in decision.removed_snapshot_refs:
            raise ValueError("The comparison target must not also be removed.")

    if decision.operation == CharacterFactComparisonOperation.ADD and exact_target_ref is not None:
        raise ValueError("ADD is invalid when the canonical Fact slot already exists.")

    applies_to_snapshot = decision.operation in {
        CharacterFactComparisonOperation.ADD,
        CharacterFactComparisonOperation.UPDATE,
        CharacterFactComparisonOperation.MERGE,
    }
    if applies_to_snapshot:
        validate_status_active_value(
            candidate_fact_type,
            decision.proposed_value_json,
            field_name="proposed_value_json",
        )
        if has_explicit_inactive_status_value(
            candidate_fact_type,
            candidate_value_json,
        ) or has_explicit_inactive_status_value(
            candidate_fact_type,
            decision.proposed_value_json,
        ):
            raise ValueError("An explicitly inactive STATUS value must not enter the snapshot.")

    if (
        decision.operation == CharacterFactComparisonOperation.REMOVE
        and candidate_fact_type != "STATUS"
    ):
        raise ValueError("REMOVE is only allowed for a canonical STATUS slot.")

    for removed_ref in decision.removed_snapshot_refs:
        if entries_by_ref[removed_ref].fact_type != "STATUS":
            raise ValueError("Only STATUS snapshot entries may be removed in the MVP.")

    if decision.removed_snapshot_refs and (
        candidate_fact_type != "STATUS"
        or decision.temporal_scope != CharacterFactTemporalScope.PRESENT
        or decision.operation
        not in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
            CharacterFactComparisonOperation.REMOVE,
        }
    ):
        raise ValueError("Snapshot removal requires a PRESENT STATUS transition.")

    if applies_to_snapshot:
        normalize_setting_display_value(
            candidate_value_type,
            decision.proposed_value_json,
            decision.proposed_fact_value,
        )


def is_explicit_inactive_status(fact_type: str, value_json: Any | None) -> bool:
    return has_explicit_inactive_status_value(fact_type, value_json)


def has_explicit_inactive_status_value(
    fact_type: str,
    value_json: Any | None,
) -> bool:
    return (
        fact_type == "STATUS"
        and isinstance(value_json, dict)
        and value_json.get("active") is False
    )


def validate_status_active_value(
    fact_type: str,
    value_json: Any | None,
    *,
    field_name: str,
) -> None:
    if (
        fact_type == "STATUS"
        and isinstance(value_json, dict)
        and "active" in value_json
        and type(value_json["active"]) is not bool
    ):
        raise ValueError(f"{field_name}.active must be a JSON boolean for STATUS.")
