import argparse
import asyncio
import json
from pathlib import Path

from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.loaders import (
    load_gold_snapshot_v3,
    load_prediction_bundle_v3,
)
from evals.multi_stage_setting.semantic_outcome import OpenAISemanticOutcomeJudge


def main() -> None:
    args = _parse_args()
    gold = load_gold_snapshot_v3(
        args.gold,
        source_root=args.source_root,
        source_file_pattern=args.source_file_pattern,
        state_root=args.state_root,
    )
    predictions = load_prediction_bundle_v3(
        args.predictions,
        fixture_hash=gold.fixture_hash or "",
    )
    semantic_judge = None
    if args.semantic_judge == "openai":
        semantic_judge = (
            OpenAISemanticOutcomeJudge(model=args.judge_model)
            if args.judge_model is not None
            else OpenAISemanticOutcomeJudge()
        )
    report = asyncio.run(
        evaluate_multi_stage(gold, predictions, semantic_judge=semantic_judge)
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized, encoding="utf-8")
    if not args.quiet:
        print(serialized)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score setting-eval/v3 Stage1, Stage2, and cumulative state predictions."
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=None)
    parser.add_argument("--source-file-pattern", default="{episode_no:02d}화.txt")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument(
        "--semantic-judge",
        choices=("none", "openai"),
        default="none",
    )
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
