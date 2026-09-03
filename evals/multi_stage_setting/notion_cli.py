import argparse
import json
import os
from pathlib import Path

from evals.multi_stage_setting.contracts import ReviewStatus
from evals.multi_stage_setting.notion_exporter import (
    build_gold_snapshot_v3,
    validate_notion_v3_schemas,
)
from evals.setting_extraction.notion_exporter import NotionDataSourceClient


def main() -> None:
    args = _parse_args()
    token = os.getenv("NOTION_API_TOKEN", "")
    source_ids = {
        "scenario": args.scenario_data_source_id
        or os.getenv("NOTION_SCENARIO_DATA_SOURCE_ID", ""),
        "stage1": args.stage1_data_source_id
        or os.getenv("NOTION_STAGE1_GOLD_DATA_SOURCE_ID", ""),
        "stage2": args.stage2_data_source_id
        or os.getenv("NOTION_STAGE2_GOLD_DATA_SOURCE_ID", ""),
    }
    missing = [name for name, value in source_ids.items() if not value.strip()]
    if missing:
        raise ValueError("Missing v3 Notion data source IDs: " + ", ".join(missing))

    episode_numbers = _parse_episode_numbers(args.episodes)
    statuses = _parse_review_statuses(args.review_status)
    client = NotionDataSourceClient(token)
    try:
        claim_schema_mode = validate_notion_v3_schemas(
            scenario_schema=client.retrieve_property_schema(source_ids["scenario"]),
            stage1_schema=client.retrieve_property_schema(source_ids["stage1"]),
            stage2_schema=client.retrieve_property_schema(source_ids["stage2"]),
        )
        if claim_schema_mode == "LEGACY":
            print(
                "Warning: Stage2 is using legacy 유지/추가/제거/금지 Claim columns; "
                "migrate to 반영 결과 필수/금지 사실."
            )
        scenario_pages = client.query_all_pages(
            source_ids["scenario"],
            sorts=[{"property": "회차", "direction": "ascending"}],
        )
        stage1_pages = client.query_all_pages(
            source_ids["stage1"],
        )
        stage2_pages = client.query_all_pages(
            source_ids["stage2"],
        )
    finally:
        client.close()

    snapshot = build_gold_snapshot_v3(
        scenario_pages,
        stage1_pages,
        stage2_pages,
        dataset_name=args.dataset_name,
        dataset_version=args.dataset_version,
        episode_numbers=episode_numbers,
        review_statuses=statuses,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            snapshot.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "Notion multi-stage Gold snapshot written "
        f"version={snapshot.dataset_version} fixture_hash={snapshot.fixture_hash} "
        f"scenarios={len(snapshot.scenarios)} stage1={len(snapshot.stage1)} "
        f"stage2={len(snapshot.stage2)} output={args.output}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Scenario + Stage1 + Stage2 Notion databases to setting-eval/v3."
    )
    parser.add_argument("--scenario-data-source-id", default=None)
    parser.add_argument("--stage1-data-source-id", default=None)
    parser.add_argument("--stage2-data-source-id", default=None)
    parser.add_argument("--dataset-name", default="다단계 설정 평가 답안지")
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument("--episodes", default=None)
    parser.add_argument(
        "--review-status",
        default="FINAL",
        help="Comma-separated statuses. Automation should keep the default FINAL.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _parse_episode_numbers(value: str | None) -> set[int] | None:
    if value is None or not value.strip():
        return None
    result = {int(item.strip()) for item in value.split(",") if item.strip()}
    if not result or any(item < 1 for item in result):
        raise ValueError("--episodes must contain positive episode numbers.")
    return result


def _parse_review_statuses(value: str) -> set[ReviewStatus]:
    try:
        statuses = {
            ReviewStatus(item.strip()) for item in value.split(",") if item.strip()
        }
    except ValueError:
        raise ValueError("--review-status accepts DRAFT, IN_REVIEW, FINAL.") from None
    if not statuses:
        raise ValueError("--review-status must not be empty.")
    return statuses


if __name__ == "__main__":
    main()
