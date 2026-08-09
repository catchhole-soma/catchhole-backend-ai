import argparse
import json
import os
from pathlib import Path

from evals.setting_extraction.notion_exporter import (
    NotionDataSourceClient,
    build_gold_dataset,
)


def main() -> None:
    args = _parse_args()
    token = os.getenv("NOTION_API_TOKEN", "")
    data_source_id = args.data_source_id or os.getenv("NOTION_GOLD_DATA_SOURCE_ID", "")
    episode_numbers = _parse_episode_numbers(args.episodes)
    client = NotionDataSourceClient(token)
    try:
        pages = client.query_all_pages(
            data_source_id,
            episode_numbers=episode_numbers,
        )
    finally:
        client.close()

    dataset = build_gold_dataset(
        pages,
        dataset_name=args.dataset_name,
        dataset_version=args.dataset_version,
        episode_numbers=episode_numbers,
        source_file_pattern=args.source_file_pattern,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dataset.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    candidate_count = sum(len(episode.candidates) for episode in dataset.episodes)
    print(
        "Notion gold snapshot written "
        f"version={dataset.dataset_version} "
        f"episodes={len(dataset.episodes)} candidates={candidate_count} "
        f"output={args.output}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the structured Notion annotation data source to GoldDataset JSON."
    )
    parser.add_argument(
        "--data-source-id",
        default=None,
        help="Defaults to NOTION_GOLD_DATA_SOURCE_ID.",
    )
    parser.add_argument("--dataset-name", default="설정 추출 답안지")
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument(
        "--episodes",
        default=None,
        help="Optional comma-separated episode numbers, for example 2,3.",
    )
    parser.add_argument("--source-file-pattern", default="{episode_no:02d}화.txt")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _parse_episode_numbers(value: str | None) -> set[int] | None:
    if value is None or not value.strip():
        return None
    numbers = {int(item.strip()) for item in value.split(",") if item.strip()}
    if not numbers or any(number < 1 for number in numbers):
        raise ValueError("--episodes must contain positive episode numbers.")
    return numbers


if __name__ == "__main__":
    main()
