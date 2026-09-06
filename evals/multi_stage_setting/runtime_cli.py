import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path

from evals.multi_stage_setting.contracts import EvaluationDomain, EvaluationMode
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.runtime_adapter import (
    RuntimePricing,
    create_default_runtime_components,
    run_multi_stage_predictions,
)
from scripts.run_episode_text_analysis_debug import load_character_setting_schema_hints


def main() -> None:
    args = _parse_args()
    mode = EvaluationMode(args.mode)
    domains = _parse_domains(args.domains)
    episodes = _parse_episode_numbers(args.episodes)
    if mode != EvaluationMode.ORACLE and args.source_root is None:
        raise ValueError("FIXED/ROLLING require --source-root.")
    schema_hints = (
        load_character_setting_schema_hints(args.character_setting_schemas)
        if args.character_setting_schemas is not None
        else ()
    )
    gold = load_gold_snapshot_v3(
        args.gold,
        source_root=args.source_root,
        source_file_pattern=args.source_file_pattern,
        state_root=args.state_root,
    )
    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode=mode,
            components=create_default_runtime_components(
                analysis_model=args.analysis_model,
                subject_resolution_model=args.subject_resolution_model,
                comparison_model=args.comparison_model,
            ),
            character_schema_hints=schema_hints,
            max_chunks=args.max_chunks,
            analysis_model=args.analysis_model,
            subject_resolution_model=args.subject_resolution_model,
            comparison_model=args.comparison_model,
            domains=domains,
            episode_numbers=episodes,
            pricing=_pricing_from_args(args),
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(bundle.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    failure_count = sum(len(scenario.failures) for scenario in bundle.scenarios)
    print(
        "Multi-stage predictions written "
        f"mode={bundle.mode} domains={','.join(sorted(item.value for item in domains))} "
        f"scenarios={len(bundle.scenarios)} failures={failure_count} output={args.output}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run production setting extractors/comparators against a v3 evaluation fixture."
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--mode", choices=[item.value for item in EvaluationMode], required=True)
    parser.add_argument("--domains", default="CHARACTER,WORLD")
    parser.add_argument("--episodes", default=None)
    parser.add_argument("--source-root", type=Path, default=None)
    parser.add_argument("--source-file-pattern", default="{episode_no:02d}화.txt")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--character-setting-schemas", type=Path, default=None)
    parser.add_argument("--analysis-model", default=None)
    parser.add_argument("--subject-resolution-model", default=None)
    parser.add_argument("--comparison-model", default=None)
    parser.add_argument("--max-chunks", type=int, default=None)
    parser.add_argument("--input-usd-per-million", type=Decimal, default=None)
    parser.add_argument("--cached-input-usd-per-million", type=Decimal, default=None)
    parser.add_argument("--output-usd-per-million", type=Decimal, default=None)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _parse_domains(value: str) -> set[EvaluationDomain]:
    try:
        domains = {
            EvaluationDomain(item.strip()) for item in value.split(",") if item.strip()
        }
    except ValueError:
        raise ValueError("--domains accepts CHARACTER and WORLD.") from None
    if not domains:
        raise ValueError("--domains must not be empty.")
    return domains


def _parse_episode_numbers(value: str | None) -> set[int] | None:
    if value is None or not value.strip():
        return None
    try:
        episodes = {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError:
        raise ValueError("--episodes must be a comma-separated integer list.") from None
    if not episodes or any(item < 1 for item in episodes):
        raise ValueError("--episodes must contain positive episode numbers.")
    return episodes


def _pricing_from_args(args: argparse.Namespace) -> RuntimePricing | None:
    values = (
        args.input_usd_per_million,
        args.cached_input_usd_per_million,
        args.output_usd_per_million,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("All three per-million pricing arguments must be supplied together.")
    if any(value < 0 for value in values):
        raise ValueError("Per-million pricing arguments must be non-negative.")
    return RuntimePricing(
        input_usd_per_million=args.input_usd_per_million,
        cached_input_usd_per_million=args.cached_input_usd_per_million,
        output_usd_per_million=args.output_usd_per_million,
    )


if __name__ == "__main__":
    main()
