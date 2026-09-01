from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text

from app.schemas.worker import (
    WorkerEvidenceSpan,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
    WorkerWorldSettingSubject,
)

EPISODE_FROM = 1
EPISODE_TO = 4
RECONSTRUCTION_METHOD = "reverse-confirmed-candidate-history-v3"
SNAPSHOT_TRANSACTION_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class ReadSession(Protocol):
    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> Any: ...


@dataclass(frozen=True)
class ReplayTarget:
    world_setting_id: UUID
    category: str
    subject_name: str
    properties: tuple[WorkerWorldSettingProperty, ...]
    version: int
    created_at: datetime

    def as_worker_target(self) -> WorkerWorldSettingComparisonTarget:
        return WorkerWorldSettingComparisonTarget(
            world_setting_id=self.world_setting_id,
            subject_name=self.subject_name,
            properties=list(self.properties),
            version=self.version,
        )


@dataclass(frozen=True)
class ReplayCandidate:
    episode_no: int
    created_at: datetime
    compared_at: datetime | None
    payload: WorkerWorldSettingCandidatePayload


@dataclass(frozen=True)
class ReplayEpisode:
    episode_no: int
    candidates: tuple[ReplayCandidate, ...]
    targets: tuple[ReplayTarget, ...]
    reconstruction_fallback_count: int = 0

    def subjects(self, category: str) -> list[WorkerWorldSettingSubject]:
        return [
            WorkerWorldSettingSubject(
                world_setting_id=target.world_setting_id,
                subject_name=target.subject_name,
            )
            for target in self.targets
            if target.category == category
        ]

    def targets_by_id(self) -> dict[UUID, ReplayTarget]:
        return {target.world_setting_id: target for target in self.targets}


@dataclass(frozen=True)
class ReplayDataset:
    work_id: UUID
    episodes: tuple[ReplayEpisode, ...]
    dataset_hash: str

    @property
    def candidate_count(self) -> int:
        return sum(len(episode.candidates) for episode in self.episodes)

    @property
    def reconstruction_fallback_count(self) -> int:
        return sum(
            episode.reconstruction_fallback_count for episode in self.episodes
        )


@dataclass(frozen=True)
class _Mutation:
    mutation_key: UUID
    target_world_setting_id: UUID
    operation: str
    matched_scope_name: str | None
    matched_setting_name: str | None
    final_scope_name: str | None
    final_setting_name: str | None
    before_value: str | None
    base_version: int | None
    applied_version: int | None
    root_move_snapshots: tuple[_RootPropertyMoveSnapshot, ...] | None
    root_move_metadata_valid: bool
    root_moves_applied_version: int | None
    root_moves_disabled: bool | None
    reviewed_at: datetime


@dataclass(frozen=True)
class _RootPropertyMoveSnapshot:
    setting_name: str
    before_value: str


ELIGIBLE_WORKS_SQL = """
/* world-comparison-replay:eligible-works */
WITH candidate_jobs AS (
    SELECT DISTINCT
        episode.work_id,
        episode.id AS episode_id,
        episode.episode_no,
        candidate.analysis_job_id,
        analysis_job.created_at AS job_created_at
    FROM episodes AS episode
    JOIN world_setting_candidates AS candidate
      ON candidate.source_episode_id = episode.id
    JOIN analysis_jobs AS analysis_job
      ON analysis_job.id = candidate.analysis_job_id
    WHERE episode.episode_no BETWEEN :episode_from AND :episode_to
),
latest_jobs AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY episode_id
        ORDER BY job_created_at DESC, analysis_job_id DESC
    ) AS job_rank
    FROM candidate_jobs
)
SELECT work_id
FROM latest_jobs
WHERE job_rank = 1
GROUP BY work_id
HAVING COUNT(DISTINCT episode_no) = :episode_count
ORDER BY work_id
"""

CANDIDATES_SQL = """
/* world-comparison-replay:candidates */
WITH candidate_jobs AS (
    SELECT DISTINCT
        episode.id AS episode_id,
        episode.episode_no,
        candidate.analysis_job_id,
        analysis_job.created_at AS job_created_at
    FROM episodes AS episode
    JOIN world_setting_candidates AS candidate
      ON candidate.source_episode_id = episode.id
    JOIN analysis_jobs AS analysis_job
      ON analysis_job.id = candidate.analysis_job_id
    WHERE episode.work_id = :work_id
      AND episode.episode_no BETWEEN :episode_from AND :episode_to
),
latest_jobs AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY episode_id
        ORDER BY job_created_at DESC, analysis_job_id DESC
    ) AS job_rank
    FROM candidate_jobs
)
SELECT
    episode.episode_no,
    candidate.id AS candidate_id,
    candidate.work_id,
    candidate.source_episode_id,
    candidate.category,
    candidate.subject_name,
    candidate.scope_name,
    candidate.setting_name,
    candidate.extracted_value,
    candidate.evidence_spans,
    candidate.extraction_confidence,
    candidate.created_at,
    candidate.compared_at
FROM latest_jobs
JOIN episodes AS episode ON episode.id = latest_jobs.episode_id
JOIN world_setting_candidates AS candidate
  ON candidate.source_episode_id = latest_jobs.episode_id
 AND candidate.analysis_job_id = latest_jobs.analysis_job_id
WHERE latest_jobs.job_rank = 1
ORDER BY episode.episode_no, candidate.created_at, candidate.id
"""

TARGETS_SQL = """
/* world-comparison-replay:targets */
SELECT
    id,
    category,
    subject_name,
    properties_json,
    version,
    created_at
FROM world_settings
WHERE work_id = :work_id
ORDER BY category, normalized_subject_name, id
"""

MUTATION_CAPABILITIES_SQL = """
/* world-comparison-replay:mutation-capabilities */
SELECT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'world_setting_candidates'
      AND column_name = 'comparison_decision_id'
) AS has_comparison_decision_id,
EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'world_setting_comparison_decisions'
      AND column_name = 'existing_root_property_move_snapshots'
) AS has_root_move_snapshots,
EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'world_setting_comparison_decisions'
      AND column_name = 'root_property_moves_applied_world_setting_version'
) AS has_root_move_state
"""

MUTATIONS_SQL_TEMPLATE = """
/* world-comparison-replay:mutations */
SELECT
    {mutation_key_expression} AS mutation_key,
    candidate.target_world_setting_id,
    candidate.final_operation,
    candidate.matched_scope_name,
    candidate.matched_property_name,
    candidate.final_scope_name,
    candidate.final_setting_name,
    candidate.before_value,
    candidate.base_world_setting_version,
    candidate.reviewed_at,
    candidate.applied_world_setting_version,
    candidate.id,
    {root_move_columns}
FROM world_setting_candidates AS candidate
{decision_join}
WHERE candidate.work_id = :work_id
  AND candidate.review_status = 'CONFIRMED'
  AND candidate.final_operation IN ('ADD', 'UPDATE', 'MERGE')
  AND candidate.target_world_setting_id IS NOT NULL
  AND candidate.reviewed_at IS NOT NULL
ORDER BY candidate.reviewed_at DESC,
         candidate.applied_world_setting_version DESC NULLS LAST,
         candidate.id DESC
"""


def load_replay_dataset(
    session: ReadSession,
    work_id: UUID | None = None,
) -> ReplayDataset:
    """Load one immutable 1~4 episode snapshot without reading manuscript storage."""

    session.execute(text(SNAPSHOT_TRANSACTION_SQL))
    selected_work_id = work_id or _select_only_eligible_work(session)
    query_parameters = {
        "work_id": selected_work_id,
        "episode_from": EPISODE_FROM,
        "episode_to": EPISODE_TO,
    }
    candidate_rows = _mapping_rows(
        session.execute(text(CANDIDATES_SQL), query_parameters)
    )
    candidates = tuple(_candidate_from_row(row) for row in candidate_rows)
    _validate_episode_coverage(candidates)

    target_rows = _mapping_rows(
        session.execute(text(TARGETS_SQL), {"work_id": selected_work_id})
    )
    current_targets = tuple(_target_from_row(row) for row in target_rows)
    capability_rows = _mapping_rows(
        session.execute(text(MUTATION_CAPABILITIES_SQL))
    )
    if len(capability_rows) != 1:
        raise ValueError("Could not determine replay schema capabilities.")
    capabilities = capability_rows[0]
    has_comparison_decision_id = bool(capabilities["has_comparison_decision_id"])
    has_root_move_snapshots = bool(capabilities.get("has_root_move_snapshots"))
    has_root_move_state = bool(capabilities.get("has_root_move_state"))
    mutation_key_expression = (
        "COALESCE(candidate.comparison_decision_id, candidate.id)"
        if has_comparison_decision_id
        else "candidate.id"
    )
    decision_join = (
        "LEFT JOIN world_setting_comparison_decisions AS decision "
        "ON decision.id = candidate.comparison_decision_id"
        if has_comparison_decision_id
        else ""
    )
    root_move_columns = (
        "decision.existing_root_property_move_snapshots AS root_move_snapshots"
        if has_comparison_decision_id and has_root_move_snapshots
        else "NULL AS root_move_snapshots"
    )
    root_move_columns += (
        ", decision.root_property_moves_applied_world_setting_version "
        "AS root_moves_applied_version, decision.root_property_moves_disabled "
        "AS root_moves_disabled"
        if has_comparison_decision_id and has_root_move_state
        else ", NULL AS root_moves_applied_version, NULL AS root_moves_disabled"
    )
    mutation_rows = _mapping_rows(
        session.execute(
            text(
                MUTATIONS_SQL_TEMPLATE.format(
                    mutation_key_expression=mutation_key_expression,
                    decision_join=decision_join,
                    root_move_columns=root_move_columns,
                )
            ),
            {"work_id": selected_work_id},
        )
    )
    mutations = _deduplicated_mutations(mutation_rows)

    episodes = tuple(
        _episode_snapshot(episode_no, candidates, current_targets, mutations)
        for episode_no in range(EPISODE_FROM, EPISODE_TO + 1)
    )
    return ReplayDataset(
        work_id=selected_work_id,
        episodes=episodes,
        dataset_hash=_dataset_hash(selected_work_id, episodes),
    )


def _select_only_eligible_work(session: ReadSession) -> UUID:
    rows = _mapping_rows(
        session.execute(
            text(ELIGIBLE_WORKS_SQL),
            {
                "episode_from": EPISODE_FROM,
                "episode_to": EPISODE_TO,
                "episode_count": EPISODE_TO - EPISODE_FROM + 1,
            },
        )
    )
    if len(rows) != 1:
        raise ValueError(
            "Automatic work selection requires exactly one eligible work; "
            f"found {len(rows)}. Pass --work-id explicitly."
        )
    return _as_uuid(rows[0]["work_id"])


def _mapping_rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings().all()]


def _candidate_from_row(row: dict[str, Any]) -> ReplayCandidate:
    evidence_payload = _json_value(row["evidence_spans"])
    if not isinstance(evidence_payload, list) or not evidence_payload:
        raise ValueError("A replay candidate has no stored evidence snapshot.")
    confidence = row.get("extraction_confidence")
    return ReplayCandidate(
        episode_no=int(row["episode_no"]),
        created_at=_as_datetime(row["created_at"]),
        compared_at=_optional_datetime(row.get("compared_at")),
        payload=WorkerWorldSettingCandidatePayload(
            candidate_id=_as_uuid(row["candidate_id"]),
            work_id=_as_uuid(row["work_id"]),
            source_episode_id=_as_uuid(row["source_episode_id"]),
            category=str(row["category"]),
            subject_name=str(row["subject_name"]),
            scope_name=_optional_string(row.get("scope_name")),
            setting_name=str(row["setting_name"]),
            extracted_value=str(row["extracted_value"]),
            evidence_spans=[WorkerEvidenceSpan.model_validate(item) for item in evidence_payload],
            extraction_confidence=(
                float(confidence)
                if isinstance(confidence, (int, float, Decimal))
                else None
            ),
        ),
    )


def _target_from_row(row: dict[str, Any]) -> ReplayTarget:
    properties_json = _json_value(row["properties_json"])
    if not isinstance(properties_json, dict):
        raise TypeError("A replay target contains an invalid property snapshot.")
    return ReplayTarget(
        world_setting_id=_as_uuid(row["id"]),
        category=str(row["category"]),
        subject_name=str(row["subject_name"]),
        properties=tuple(_flatten_properties(properties_json)),
        version=int(row["version"]),
        created_at=_as_datetime(row["created_at"]),
    )


def _flatten_properties(properties: dict[str, Any]) -> list[WorkerWorldSettingProperty]:
    flattened: list[WorkerWorldSettingProperty] = []
    for name in sorted(properties):
        value = properties[name]
        if isinstance(value, str):
            flattened.append(
                WorkerWorldSettingProperty(scope_name=None, setting_name=name, value=value)
            )
            continue
        if not isinstance(value, dict):
            raise TypeError("A replay target contains a non-string property value.")
        for child_name in sorted(value):
            child_value = value[child_name]
            if not isinstance(child_value, str):
                raise TypeError(
                    "A replay target contains a nested non-string property value."
                )
            flattened.append(
                WorkerWorldSettingProperty(
                    scope_name=name,
                    setting_name=child_name,
                    value=child_value,
                )
            )
    return flattened


def _deduplicated_mutations(rows: list[dict[str, Any]]) -> tuple[_Mutation, ...]:
    mutations: list[_Mutation] = []
    seen_keys: set[UUID] = set()
    for row in rows:
        mutation_key = _as_uuid(row["mutation_key"])
        if mutation_key in seen_keys:
            continue
        seen_keys.add(mutation_key)
        root_move_snapshots, root_move_metadata_valid = _root_move_snapshots(row)
        mutations.append(
            _Mutation(
                mutation_key=mutation_key,
                target_world_setting_id=_as_uuid(row["target_world_setting_id"]),
                operation=str(row["final_operation"]),
                matched_scope_name=_optional_string(row.get("matched_scope_name")),
                matched_setting_name=_optional_string(row.get("matched_property_name")),
                final_scope_name=_optional_string(row.get("final_scope_name")),
                final_setting_name=_optional_string(row.get("final_setting_name")),
                before_value=_optional_string(row.get("before_value")),
                base_version=(
                    int(row["base_world_setting_version"])
                    if row.get("base_world_setting_version") is not None
                    else None
                ),
                applied_version=(
                    int(row["applied_world_setting_version"])
                    if row.get("applied_world_setting_version") is not None
                    else None
                ),
                root_move_snapshots=root_move_snapshots,
                root_move_metadata_valid=root_move_metadata_valid,
                root_moves_applied_version=(
                    int(row["root_moves_applied_version"])
                    if row.get("root_moves_applied_version") is not None
                    else None
                ),
                root_moves_disabled=(
                    bool(row["root_moves_disabled"])
                    if row.get("root_moves_disabled") is not None
                    else None
                ),
                reviewed_at=_as_datetime(row["reviewed_at"]),
            )
        )
    return tuple(mutations)


def _root_move_snapshots(
    row: dict[str, Any],
) -> tuple[tuple[_RootPropertyMoveSnapshot, ...] | None, bool]:
    raw_snapshots = row.get("root_move_snapshots")
    if raw_snapshots is None:
        # A pre-v39 schema could not have applied this feature, so absence is an
        # exact legacy no-op rather than a reconstruction fallback.
        return None, True
    try:
        payload = _json_value(raw_snapshots)
    except (TypeError, ValueError, json.JSONDecodeError):
        return (), False
    if not isinstance(payload, list):
        return (), False
    snapshots: list[_RootPropertyMoveSnapshot] = []
    seen_names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            return (), False
        setting_name = item.get("settingName")
        before_value = item.get("beforeValue")
        if (
            not isinstance(setting_name, str)
            or not setting_name.strip()
            or not isinstance(before_value, str)
            or not before_value.strip()
        ):
            return (), False
        normalized_name = backend_duplicate_key(setting_name)
        if normalized_name in seen_names:
            return (), False
        seen_names.add(normalized_name)
        snapshots.append(
            _RootPropertyMoveSnapshot(
                setting_name=setting_name,
                before_value=before_value,
            )
        )
    return tuple(snapshots), True


def _episode_snapshot(
    episode_no: int,
    candidates: tuple[ReplayCandidate, ...],
    current_targets: tuple[ReplayTarget, ...],
    mutations: tuple[_Mutation, ...],
) -> ReplayEpisode:
    episode_candidates = tuple(
        candidate for candidate in candidates if candidate.episode_no == episode_no
    )
    cutoff = min(
        candidate.compared_at or candidate.created_at for candidate in episode_candidates
    )
    targets, fallback_count = _reconstruct_targets(
        current_targets,
        mutations,
        cutoff,
    )
    return ReplayEpisode(
        episode_no=episode_no,
        candidates=episode_candidates,
        targets=targets,
        reconstruction_fallback_count=fallback_count,
    )


def _reconstruct_targets(
    current_targets: tuple[ReplayTarget, ...],
    mutations: tuple[_Mutation, ...],
    cutoff: datetime,
) -> tuple[tuple[ReplayTarget, ...], int]:
    target_state = {
        target.world_setting_id: {
            "target": target,
            "properties": {
                (property.scope_name, property.setting_name): property.value
                for property in target.properties
            },
            "version": target.version,
        }
        for target in current_targets
        if target.created_at < cutoff
    }
    fallback_count = 0
    for mutation in mutations:
        if mutation.reviewed_at < cutoff:
            continue
        state = target_state.get(mutation.target_world_setting_id)
        if state is None:
            current_target = next(
                (
                    target
                    for target in current_targets
                    if target.world_setting_id == mutation.target_world_setting_id
                ),
                None,
            )
            if current_target is None:
                raise ValueError("A replay mutation references a missing target snapshot.")
            state = {
                "target": current_target,
                "properties": {
                    (property.scope_name, property.setting_name): property.value
                    for property in current_target.properties
                },
                "version": current_target.version,
            }
            target_state[mutation.target_world_setting_id] = state
        if _reverse_mutation(state, mutation):
            fallback_count += 1
        if not state["properties"] and mutation.operation == "ADD":
            target_state.pop(mutation.target_world_setting_id, None)

    reconstructed: list[ReplayTarget] = []
    for state in target_state.values():
        target = state["target"]
        properties = tuple(
            WorkerWorldSettingProperty(
                scope_name=scope_name,
                setting_name=setting_name,
                value=value,
            )
            for (scope_name, setting_name), value in sorted(
                state["properties"].items(),
                key=lambda item: ((item[0][0] or ""), item[0][1]),
            )
        )
        reconstructed.append(
            ReplayTarget(
                world_setting_id=target.world_setting_id,
                category=target.category,
                subject_name=target.subject_name,
                properties=properties,
                version=max(0, int(state["version"])),
                created_at=target.created_at,
            )
        )
    return (
        tuple(
            sorted(
                reconstructed,
                key=lambda target: (
                    target.category,
                    target.subject_name.casefold(),
                    str(target.world_setting_id),
                ),
            )
        ),
        fallback_count,
    )


def _reverse_mutation(state: dict[str, Any], mutation: _Mutation) -> bool:
    properties: dict[tuple[str | None, str], str] = state["properties"]
    final_setting_name = mutation.final_setting_name or mutation.matched_setting_name
    final_path = (
        (mutation.final_scope_name, final_setting_name)
        if final_setting_name is not None
        else None
    )
    if final_path is None:
        raise ValueError("A replay mutation is missing its final property path.")
    _remove_equivalent_property(
        properties,
        final_path[0],
        final_path[1],
    )
    used_fallback = _reverse_root_property_moves(properties, mutation)
    if mutation.operation in {"UPDATE", "MERGE"}:
        if mutation.matched_setting_name is None or mutation.before_value is None:
            # Legacy or author-edited confirmations can lack the old value. Keeping the
            # current value would leak future state into the replay, so omit the path and
            # surface the conservative fallback in the aggregate report.
            used_fallback = True
        else:
            _remove_equivalent_property(
                properties,
                mutation.matched_scope_name,
                mutation.matched_setting_name,
            )
            properties[(mutation.matched_scope_name, mutation.matched_setting_name)] = (
                mutation.before_value
            )
    if mutation.base_version is not None:
        state["version"] = mutation.base_version
    else:
        state["version"] = max(0, int(state["version"]) - 1)
    return used_fallback


def _reverse_root_property_moves(
    properties: dict[tuple[str | None, str], str],
    mutation: _Mutation,
) -> bool:
    snapshots = mutation.root_move_snapshots
    if snapshots is None or not snapshots:
        return not mutation.root_move_metadata_valid
    if mutation.root_moves_disabled is True:
        # A disabled plan was author-edited before confirmation and was not applied.
        return mutation.root_moves_applied_version is not None
    if mutation.root_moves_applied_version is None:
        # Non-empty plans without an applied marker are incomplete metadata. Do not
        # guess that the destination properties came from a move.
        return True
    if mutation.operation != "ADD" or mutation.final_scope_name is None:
        return True

    used_fallback = (
        mutation.root_moves_disabled is None
        or mutation.applied_version is None
        or mutation.applied_version != mutation.root_moves_applied_version
    )
    for snapshot in snapshots:
        destination_found = _remove_equivalent_property(
            properties,
            mutation.final_scope_name,
            snapshot.setting_name,
        )
        if not destination_found:
            used_fallback = True
        _remove_equivalent_property(properties, None, snapshot.setting_name)
        properties[(None, snapshot.setting_name)] = snapshot.before_value
    return used_fallback


def backend_display_name(value: str) -> str:
    """Mirror Java WorldSettingNameNormalizer.displayName."""

    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return unicodedata.normalize("NFC", value[start:end])


def backend_duplicate_key(value: str) -> str:
    """Mirror Java WorldSettingNameNormalizer.duplicateKey."""

    return backend_display_name(value).lower()


def _remove_equivalent_property(
    properties: dict[tuple[str | None, str], str],
    scope_name: str | None,
    setting_name: str,
) -> bool:
    expected_scope_key = (
        None if scope_name is None else backend_duplicate_key(scope_name)
    )
    expected_setting_key = backend_duplicate_key(setting_name)
    equivalent_paths = [
        path
        for path in properties
        if (
            None if path[0] is None else backend_duplicate_key(path[0])
        )
        == expected_scope_key
        and backend_duplicate_key(path[1]) == expected_setting_key
    ]
    for path in equivalent_paths:
        properties.pop(path)
    return bool(equivalent_paths)


def _validate_episode_coverage(candidates: tuple[ReplayCandidate, ...]) -> None:
    episode_numbers = {candidate.episode_no for candidate in candidates}
    required = set(range(EPISODE_FROM, EPISODE_TO + 1))
    if episode_numbers != required:
        raise ValueError(
            "The selected work must have a latest stored candidate snapshot for every "
            "episode from 1 through 4."
        )


def _dataset_hash(work_id: UUID, episodes: tuple[ReplayEpisode, ...]) -> str:
    payload = {
        "work": str(work_id),
        "episodes": [
            {
                "episode": episode.episode_no,
                "candidates": [
                    candidate.payload.model_dump(mode="json", by_alias=True)
                    for candidate in episode.candidates
                ],
                "targets": [
                    {
                        "id": str(target.world_setting_id),
                        "category": target.category,
                        "subject": target.subject_name,
                        "version": target.version,
                        "properties": [
                            property.model_dump(mode="json", by_alias=True)
                            for property in target.properties
                        ],
                    }
                    for target in episode.targets
                ],
                "reconstructionFallbackCount": (
                    episode.reconstruction_fallback_count
                ),
            }
            for episode in episodes
        ],
    }
    return _salted_hash(work_id, payload, "dataset")


def privacy_safe_hash(work_id: UUID, payload: Any, namespace: str) -> str:
    return _salted_hash(work_id, payload, namespace)


def _salted_hash(work_id: UUID, payload: Any, namespace: str) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256()
    digest.update(b"catchhole-world-comparison-replay-v1\0")
    digest.update(work_id.bytes)
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return deepcopy(value)


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Replay snapshot timestamps must be database datetimes.")
    return value


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _as_datetime(value)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)
