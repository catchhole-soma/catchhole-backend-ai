import argparse
import json
from pathlib import Path
import re

from evals.multi_stage_setting.contracts import (
    GoldSnapshotV3,
    Stage2Gold,
    StateGenerationStatus,
    known_characters_for_runtime,
)
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.state_effects import (
    ScenarioStateTransition,
    build_gold_state_chain,
)
from evals.multi_stage_setting.state_preview import render_notion_before_state


SAFE_FILE_PART = re.compile(r"[^A-Za-z0-9._-]+")
PREVIEW_MODE = "preview"
VERIFIED_MODE = "verified"


def main() -> None:
    args = _parse_args()
    gold = load_gold_snapshot_v3(args.gold, state_root=args.state_root)
    is_verified = args.mode == VERIFIED_MODE
    if is_verified:
        _require_final_gold(gold)
    transitions = build_gold_state_chain(gold)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    updated_scenarios = []
    used_names: set[str] = set()
    for scenario in sorted(gold.scenarios, key=lambda item: (item.episode_no, item.scenario_id)):
        transition = transitions[scenario.scenario_id]
        file_part = SAFE_FILE_PART.sub("-", scenario.scenario_id).strip("-.") or "scenario"
        if file_part in used_names:
            raise ValueError("Scenario IDs collide after safe state filename normalization.")
        used_names.add(file_part)
        before_name = f"{scenario.episode_no:04d}-{file_part}.before.json"
        after_name = f"{scenario.episode_no:04d}-{file_part}.after.json"
        preview_name = f"{scenario.episode_no:04d}-{file_part}.before.notion.md"
        if is_verified:
            _write_state(args.output_dir / before_name, transition.before_state)
            _write_state(args.output_dir / after_name, transition.after_state)
        (args.output_dir / preview_name).write_text(
            render_notion_before_state(scenario, transition.before_state) + "\n",
            encoding="utf-8",
        )
        before_hash = transition.before_state.content_hash()
        after_hash = transition.after_state.content_hash()
        manifest_row = {
            "scenarioId": scenario.scenario_id,
            "episodeNo": scenario.episode_no,
            "stateGenerationStatus": (
                StateGenerationStatus.VERIFIED
                if is_verified
                else StateGenerationStatus.GENERATED
            ),
            "beforeStatePreviewUri": preview_name,
            "appliedDecisionIds": list(transition.applied_decision_ids),
            "heldDecisionIds": list(transition.held_decision_ids),
        }
        if is_verified:
            manifest_row.update(
                {
                    "beforeStateUri": before_name,
                    "beforeStateHash": f"sha256:{before_hash}",
                    "afterStateUri": after_name,
                    "afterStateHash": f"sha256:{after_hash}",
                }
            )
        else:
            manifest_row.update(
                {
                    "previewBeforeStateHash": f"sha256:{before_hash}",
                    "previewAfterStateHash": f"sha256:{after_hash}",
                }
            )
        manifest_rows.append(manifest_row)
        scenario_updates = {
            "state_generation_status": (
                StateGenerationStatus.VERIFIED
                if is_verified
                else StateGenerationStatus.GENERATED
            ),
            "before_state_uri": before_name if is_verified else None,
            "before_state_hash": (
                f"sha256:{before_hash}" if is_verified else None
            ),
            "after_state_uri": after_name if is_verified else None,
            "after_state_hash": (
                f"sha256:{after_hash}" if is_verified else None
            ),
            "known_character_names": [
                item.name for item in known_characters_for_runtime(transition.before_state)
            ],
            "provided_context": _known_character_context(transition.before_state),
        }
        updated_scenarios.append(scenario.model_copy(update=scenario_updates))

    updated_stage2 = _materialize_stage2_before_values(gold, transitions)
    updated = gold.model_copy(
        update={
            "scenarios": updated_scenarios,
            "stage2": updated_stage2,
            "fixture_hash": None,
        }
    ).with_fixture_hash()
    manifest = {
        "schemaVersion": gold.schema_version,
        "mode": args.mode,
        "sourceFixtureHash": gold.fixture_hash,
        "inputFixtureHash": gold.fixture_hash,
        "updatedFixtureHash": updated.fixture_hash,
        "states": manifest_rows,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.updated_gold is not None:
        args.updated_gold.parent.mkdir(parents=True, exist_ok=True)
        args.updated_gold.write_text(
            json.dumps(updated.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(
        f"Evaluation states {args.mode} generated "
        f"scenarios={len(manifest_rows)} output_dir={args.output_dir}"
    )


def _require_final_gold(gold: GoldSnapshotV3) -> None:
    non_final = [
        *(
            f"scenario:{item.scenario_id}"
            for item in gold.scenarios
            if str(item.review_status) != "FINAL"
        ),
        *(
            f"stage1:{item.gold_id}"
            for item in gold.stage1
            if str(item.review_status) != "FINAL"
        ),
        *(
            f"stage2:{item.decision_id}"
            for item in gold.stage2
            if str(item.review_status) != "FINAL"
        ),
    ]
    if non_final:
        raise ValueError(
            "Verified state export requires every included Scenario and Gold row to be FINAL: "
            + ", ".join(non_final)
        )


def _write_state(path: Path, state) -> None:
    # EvaluationState.content_hash()와 같은 canonical byte 표현을 사용한다. 따라서
    # before/afterStateHash는 논리 내용 hash이면서 실제 fixture 파일의 SHA-256이기도 하다.
    path.write_text(
        json.dumps(
            state.canonical().model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _known_character_context(state) -> str:
    names = [item.name for item in known_characters_for_runtime(state)]
    return f"knownCharacters=[{', '.join(names)}]"


def _materialize_stage2_before_values(
    gold: GoldSnapshotV3,
    transitions: dict[str, ScenarioStateTransition],
) -> list[Stage2Gold]:
    resolved = {
        item.decision_id: item
        for transition in transitions.values()
        for item in transition.resolved_decision_befores
    }
    updated: list[Stage2Gold] = []
    for decision in gold.stage2:
        before = resolved[decision.decision_id]
        changes = {}
        if decision.before_value is None and before.value is not None:
            changes["before_value"] = before.value
        if decision.before_value_json is None and before.value_json is not None:
            changes["before_value_json"] = before.value_json
        updated.append(decision.model_copy(update=changes) if changes else decision)
    return updated


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render a DRAFT state preview or apply FINAL Stage2 Gold and export "
            "verified state fixtures."
        )
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--updated-gold", type=Path, default=None)
    parser.add_argument(
        "--mode",
        choices=(PREVIEW_MODE, VERIFIED_MODE),
        default=PREVIEW_MODE,
        help=(
            "preview allows non-FINAL rows and emits review artifacts only; "
            "verified requires FINAL rows and emits official state fixtures."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
