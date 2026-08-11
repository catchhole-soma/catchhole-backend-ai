import argparse
import asyncio
import json
from pathlib import Path

from evals.setting_extraction.evaluator import evaluate_predictions
from evals.setting_extraction.loader import (
    load_gold_dataset,
    load_prediction_bundle,
    load_setting_schema_snapshot,
)
from evals.setting_extraction.semantic_judge import OpenAISemanticValueJudge


def main() -> None:
    args = _parse_args()
    gold = load_gold_dataset(args.gold, args.source_root)
    predictions = load_prediction_bundle(args.predictions)
    setting_schemas = (
        load_setting_schema_snapshot(args.setting_schemas)
        if args.setting_schemas is not None
        else None
    )
    semantic_judge = (
        OpenAISemanticValueJudge(model=args.judge_model)
        if args.semantic_judge == "openai"
        else None
    )
    report = asyncio.run(
        evaluate_predictions(
            gold,
            predictions,
            semantic_judge=semantic_judge,
            setting_schemas=setting_schemas,
        )
    )
    report["run"] = {
        "analysisModel": args.analysis_model,
        "semanticJudgeEnabled": semantic_judge is not None,
        "semanticJudgeModel": semantic_judge.model if semantic_judge is not None else None,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    if not args.quiet:
        print(serialized)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate stored character setting extraction predictions against gold data."
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument(
        "--predictions",
        type=Path,
        action="append",
        required=True,
        help="Repeat for each episode debug result or pass one episodes bundle.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="Base directory for private sourceFile paths in the gold dataset.",
    )
    parser.add_argument(
        "--semantic-judge",
        choices=("none", "openai"),
        default="none",
        help="Use OpenAI only when deterministic attributeValue comparison is inconclusive.",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Override the semantic judge model (default: gpt-5.6-luna).",
    )
    parser.add_argument(
        "--analysis-model",
        default=None,
        help="Record the model that produced the supplied predictions.",
    )
    parser.add_argument(
        "--setting-schemas",
        type=Path,
        default=None,
        help="Active character setting schema snapshot used to canonicalize prediction aliases.",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the detailed report, which may contain source-derived text.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
