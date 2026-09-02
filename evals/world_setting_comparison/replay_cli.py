import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.llm.openai_client import OpenAIResponsesClient
from evals.world_setting_comparison.replay_runner import (
    WorldSettingComparisonReplayRunner,
)
from evals.world_setting_comparison.replay_snapshot import load_replay_dataset


def main() -> None:
    args = _parse_args()
    report = asyncio.run(_run(args))
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


async def _run(args: argparse.Namespace) -> dict:
    with Session(get_engine()) as session:
        try:
            dataset = load_replay_dataset(session, args.work_id)
        finally:
            session.rollback()

    provider = OpenAIResponsesClient.from_settings()
    try:
        runner = WorldSettingComparisonReplayRunner(
            delegate=provider,
            model=args.model,
        )
        return await runner.run(dataset)
    finally:
        await provider.aclose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay stored world-setting candidates from episodes 1-4 through "
            "single and batch comparison without mutating Spring or PostgreSQL."
        )
    )
    parser.add_argument(
        "--work-id",
        type=UUID,
        default=None,
        help="Work UUID. Omit only when the database has exactly one eligible work.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="One model used by subject resolution and comparison in both arms.",
    )
    parser.add_argument(
        "--confirm-external-provider-data-transfer",
        action="store_true",
        required=True,
        help=(
            "Confirm that stored private candidates, values, and evidence will be sent "
            "to the configured external model provider."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    main()
