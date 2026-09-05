from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evals.multi_stage_setting import SCHEMA_VERSION
from evals.multi_stage_setting.contracts import (
    EvaluationState,
    GoldSnapshotV3,
    PredictionBundleV3,
    StartStateMode,
)


def load_gold_snapshot_v3(
    path: Path,
    *,
    source_root: Path | None = None,
    source_file_pattern: str = "{episode_no:02d}화.txt",
    state_root: Path | None = None,
) -> GoldSnapshotV3:
    payload = _read_versioned_json(path, "Gold snapshot")
    snapshot = GoldSnapshotV3.model_validate(payload)
    computed_hash = snapshot.computed_fixture_hash()
    if snapshot.fixture_hash is None:
        snapshot = snapshot.model_copy(update={"fixture_hash": computed_hash})
    elif snapshot.fixture_hash != computed_hash:
        raise ValueError(
            "Gold snapshot fixtureHash does not match its canonical content: "
            f"declared={snapshot.fixture_hash} computed={computed_hash}."
        )

    resolved = snapshot
    if source_root is not None:
        resolved = _attach_sources(resolved, source_root, source_file_pattern)
    if state_root is not None:
        resolved = _attach_seed_states(resolved, state_root)
    return resolved


def load_prediction_bundle_v3(path: Path, *, fixture_hash: str) -> PredictionBundleV3:
    payload = _read_versioned_json(path, "Prediction bundle")
    bundle = PredictionBundleV3.model_validate(payload)
    if bundle.fixture_hash != fixture_hash:
        raise ValueError(
            "Prediction fixtureHash differs from Gold; refusing to compare different fixtures."
        )
    return bundle


def _attach_sources(
    snapshot: GoldSnapshotV3,
    source_root: Path,
    source_file_pattern: str,
) -> GoldSnapshotV3:
    root = source_root.resolve()
    scenarios = []
    for scenario in snapshot.scenarios:
        source_name = _source_file_name(scenario.source_identifier, source_file_pattern, scenario.episode_no)
        source_path = (root / source_name).resolve()
        try:
            source_path.relative_to(root)
        except ValueError:
            raise ValueError(
                f"Source file for scenario {scenario.scenario_id} escapes the source root."
            ) from None
        if not source_path.is_file():
            raise ValueError(
                f"Source file for scenario {scenario.scenario_id} was not found: {source_path}"
            )
        source_bytes = source_path.read_bytes()
        if scenario.source_hash is not None:
            actual_hash = hashlib.sha256(source_bytes).hexdigest()
            expected_hash = scenario.source_hash.removeprefix("sha256:")
            if actual_hash != expected_hash:
                raise ValueError(
                    f"Source hash mismatch for scenario {scenario.scenario_id}."
                )
        scenarios.append(
            scenario.model_copy(update={"source_text": source_bytes.decode("utf-8")})
        )
    return snapshot.model_copy(update={"scenarios": scenarios})


def _attach_seed_states(snapshot: GoldSnapshotV3, state_root: Path) -> GoldSnapshotV3:
    root = state_root.resolve()
    scenarios = []
    for scenario in snapshot.scenarios:
        if scenario.start_state_mode != StartStateMode.SEED or scenario.seed_state is not None:
            scenarios.append(scenario)
            continue
        if scenario.before_state_uri is None:
            raise ValueError(f"SEED scenario {scenario.scenario_id} has no beforeState URI.")
        if scenario.before_state_hash is None:
            raise ValueError(
                f"External SEED scenario {scenario.scenario_id} has no beforeState hash."
            )
        state_path = (root / scenario.before_state_uri).resolve()
        try:
            state_path.relative_to(root)
        except ValueError:
            raise ValueError(
                f"beforeState URI for scenario {scenario.scenario_id} escapes state root."
            ) from None
        if not state_path.is_file():
            raise ValueError(f"Seed state was not found for scenario {scenario.scenario_id}.")
        state = EvaluationState.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
        expected = scenario.before_state_hash.removeprefix("sha256:")
        if state.content_hash() != expected:
            raise ValueError(f"Seed state hash mismatch for scenario {scenario.scenario_id}.")
        scenarios.append(scenario.model_copy(update={"seed_state": state}))
    return snapshot.model_copy(update={"scenarios": scenarios})


def _source_file_name(identifier: str, pattern: str, episode_no: int) -> str:
    # URL/Notion ID와 작성자 로컬의 절대경로는 실행 환경의 고정 pattern으로 변환한다.
    # 절대경로를 그대로 신뢰하면 CI의 private source root를 벗어나며, 작성자 PC 경로가
    # fixture의 실행 계약으로 굳어지므로 회차 기반 파일명을 사용한다.
    candidate = Path(identifier)
    if "://" not in identifier and not candidate.is_absolute() and candidate.suffix:
        return identifier
    return pattern.format(episode_no=episode_no, source_identifier=identifier)


def _read_versioned_json(path: Path, label: str) -> Any:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON must be an object: {path}")
    version = payload.get("schemaVersion")
    if version != SCHEMA_VERSION:
        if version is None:
            raise ValueError(
                f"{label} has no schemaVersion. Use the legacy setting_extraction CLI for v1/v2."
            )
        raise ValueError(f"Unsupported {label} schemaVersion={version!r}.")
    return payload
