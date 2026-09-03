import asyncio
import json
import logging
from pathlib import Path
import sys

import httpx
import pytest

from evals.setting_extraction.notion_exporter import (
    NotionDataSourceClient,
    build_gold_dataset,
)
from evals.setting_extraction.report_cli import (
    build_machine_summary,
    render_markdown_summary,
)
from evals.setting_extraction.run_gold_analysis import (
    resolve_episode_source_file,
    run_analysis_without_source_logs,
)


def test_build_gold_dataset_filters_episodes_and_preserves_annotation_contract() -> None:
    pages = [
        _annotation_page(
            page_id="page-3",
            row_id="3-1",
            episode_no=3,
            fact_key="profile.species",
            attribute_value="인간",
        ),
        _annotation_page(
            page_id="page-2",
            row_id="2-1",
            episode_no=2,
            fact_key="profile.species",
            attribute_value="바바리안",
        ),
    ]

    dataset = build_gold_dataset(
        pages,
        dataset_name="설정 추출 답안지",
        episode_numbers={2},
    )

    assert dataset.dataset_version.startswith("notion-")
    assert [episode.episode_no for episode in dataset.episodes] == [2]
    assert dataset.episodes[0].title is None
    assert dataset.episodes[0].source_file == "02화.txt"
    candidate = dataset.episodes[0].candidates[0]
    assert candidate.fact_key == "profile.species"
    assert candidate.accepted_fact_key_aliases == ["profile.종족"]
    assert candidate.attribute_value == "바바리안"
    assert candidate.value_json == {"value": "바바리안"}
    assert candidate.evidence_quotes == ["그는 바바리안이다."]


def test_unselected_incomplete_episode_does_not_block_selected_export() -> None:
    selected = _annotation_page(page_id="page-2", row_id="2-1", episode_no=2)
    incomplete = _annotation_page(page_id="page-3", row_id="3-1", episode_no=3)
    incomplete["properties"]["정답 attributeValue"] = _rich_text("")

    dataset = build_gold_dataset(
        [incomplete, selected],
        dataset_name="설정 추출 답안지",
        episode_numbers={2},
    )

    assert [episode.episode_no for episode in dataset.episodes] == [2]


def test_requested_episode_without_annotations_fails_export() -> None:
    page = _annotation_page(page_id="page-2", row_id="2-1", episode_no=2)

    with pytest.raises(ValueError, match=r"episodes: 3\."):
        build_gold_dataset(
            [page],
            dataset_name="설정 추출 답안지",
            episode_numbers={2, 3},
        )


def test_snapshot_version_is_stable_when_notion_page_order_changes() -> None:
    first = _annotation_page(page_id="page-a", row_id="2-1", episode_no=2)
    second = _annotation_page(
        page_id="page-b",
        row_id="2-2",
        episode_no=2,
        sort_order=2,
        fact_key="profile.gender",
        attribute_value="남성",
    )

    forward = build_gold_dataset([first, second], dataset_name="gold")
    reverse = build_gold_dataset([second, first], dataset_name="gold")

    assert forward.dataset_version == reverse.dataset_version


def test_invalid_extract_row_reports_only_safe_row_and_field_details() -> None:
    page = _annotation_page(page_id="page-a", row_id="2-17", episode_no=2)
    page["properties"]["정답 attributeValue"] = _rich_text("")
    private_quote = "외부에 노출하면 안 되는 원문"
    page["properties"]["원문 근거"] = _rich_text(private_quote)

    with pytest.raises(ValueError, match="2-17") as exc_info:
        build_gold_dataset([page], dataset_name="gold")

    assert "정답 attributeValue" in str(exc_info.value)
    assert private_quote not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_notion_client_retries_and_reads_all_cursor_pages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        body = json.loads(request.content)
        if "start_cursor" not in body:
            return httpx.Response(
                200,
                json={"results": [{"id": "first"}], "has_more": True, "next_cursor": "next"},
            )
        return httpx.Response(
            200,
            json={"results": [{"id": "second"}], "has_more": False, "next_cursor": None},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = NotionDataSourceClient("secret", client=http_client, sleep=lambda _: None)

    assert [page["id"] for page in client.query_all_pages("source-id")] == [
        "first",
        "second",
    ]
    assert len(requests) == 3


def test_notion_client_filters_selected_episodes_before_row_validation() -> None:
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [], "has_more": False})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = NotionDataSourceClient("secret", client=http_client)

    client.query_all_pages("source-id", episode_numbers={3, 2})

    assert request_bodies[0]["filter"] == {
        "or": [
            {"property": "회차", "number": {"equals": 2}},
            {"property": "회차", "number": {"equals": 3}},
        ]
    }


def test_notion_client_retrieves_property_name_and_type_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/data_sources/source-id"
        return httpx.Response(
            200,
            json={
                "properties": {
                    "회차": {"id": "episode", "type": "number"},
                    "검수 상태": {"id": "review", "type": "select"},
                }
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = NotionDataSourceClient("secret", client=http_client)

    assert client.retrieve_property_schema("source-id") == {
        "회차": "number",
        "검수 상태": "select",
    }


def test_source_file_cannot_escape_private_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    dataset = build_gold_dataset(
        [_annotation_page(page_id="page-a", row_id="2-1", episode_no=2)],
        dataset_name="gold",
        source_file_pattern="../outside.txt",
    )

    with pytest.raises(ValueError, match="escapes the source root"):
        resolve_episode_source_file(dataset.episodes[0], source_root)


def test_live_analysis_failure_does_not_expose_source_details(capsys) -> None:
    private_text = "private manuscript evidence quote"

    async def fail_with_source_details() -> dict:
        print(private_text)
        print(private_text, file=sys.stderr)
        logging.getLogger("eval-source-test").error(private_text)
        raise ValueError(private_text)

    with pytest.raises(
        RuntimeError,
        match=r"Episode 2 analysis failed \(ValueError\)\.",
    ) as exc_info:
        asyncio.run(
            run_analysis_without_source_logs(
                episode_no=2,
                operation=fail_with_source_details,
            )
        )

    captured = capsys.readouterr()
    assert private_text not in captured.out
    assert private_text not in captured.err
    assert private_text not in str(exc_info.value)


def test_markdown_summary_marks_scores_as_informational() -> None:
    markdown = render_markdown_summary(
        {
            "run": {
                "analysisModel": "gpt-5.6-terra",
                "semanticJudgeEnabled": True,
                "semanticJudgeModel": "gpt-5.6-luna",
            },
            "dataset": {"name": "gold", "version": "v1", "episodeCount": 1},
            "metrics": {"detectionPrecision": 0.75, "factF1": None},
            "counts": {"goldExtract": 4, "predictions": 5, "detectionMatches": 3},
        }
    )

    assert "75.00%" in markdown
    assert "gpt-5.6-terra" in markdown
    assert "gpt-5.6-luna" in markdown
    assert "판정 대기/대상 없음" in markdown
    assert "낮은 점수만으로 워크플로를 실패시키지 않음" in markdown


def test_machine_summary_excludes_source_derived_details() -> None:
    summary = build_machine_summary(
        {
            "run": {
                "analysisModel": "gpt-5.6-terra",
                "semanticJudgeEnabled": True,
                "semanticJudgeModel": "gpt-5.6-luna",
            },
            "dataset": {"name": "gold", "version": "v1", "episodeCount": 1},
            "metrics": {"detectionPrecision": 0.75},
            "counts": {"predictions": 5},
            "episodes": [{"evidence": "private source quote"}],
            "semanticJudge": {"reason": "source-derived explanation"},
        }
    )

    assert set(summary) == {"run", "dataset", "metrics", "counts"}
    assert summary["run"]["analysisModel"] == "gpt-5.6-terra"
    assert "private source quote" not in json.dumps(summary)


def _annotation_page(
    *,
    page_id: str,
    row_id: str,
    episode_no: int,
    sort_order: int = 1,
    fact_key: str = "profile.species",
    attribute_value: str = "바바리안",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "정답 ID": _title(row_id),
            "회차": _number(episode_no),
            "정렬 순서": _number(sort_order),
            "판정": _select("EXTRACT"),
            "소속 캐릭터": _rich_text("비요른 얀델"),
            "canonical factKey": _rich_text(fact_key),
            "추가 허용 factKey 별칭": _rich_text('["profile.종족"]'),
            "valueType": _select("STRING"),
            "정답 attributeValue": _rich_text(attribute_value),
            "정답 valueJson": _rich_text(
                json.dumps({"value": attribute_value}, ensure_ascii=False)
            ),
            "원문 근거": _rich_text("그는 바바리안이다."),
            "중요도": _select("MUST"),
            "비고(판정 사유·검수 메모)": _rich_text("핵심 프로필"),
        },
    }


def _title(value: str) -> dict:
    return {"type": "title", "title": [{"plain_text": value}]}


def _rich_text(value: str) -> dict:
    items = [{"plain_text": value}] if value else []
    return {"type": "rich_text", "rich_text": items}


def _number(value: int) -> dict:
    return {"type": "number", "number": value}


def _select(value: str) -> dict:
    return {"type": "select", "select": {"name": value}}
