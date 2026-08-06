from collections.abc import Callable, Iterable
import hashlib
import json
import time
from typing import Any

import httpx

from evals.setting_extraction.models import GoldDataset


NOTION_API_BASE_URL = "https://api.notion.com/v1"
NOTION_API_VERSION = "2026-03-11"
MAX_QUERY_ATTEMPTS = 4


class NotionDataSourceClient:
    """Notion의 구조화 Annotation 행을 페이지 단위로 모두 조회한다."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token.strip():
            raise ValueError("Notion API token must not be blank.")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_API_VERSION,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def query_all_pages(
        self,
        data_source_id: str,
        *,
        episode_numbers: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        if not data_source_id.strip():
            raise ValueError("Notion data source ID must not be blank.")

        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "page_size": 100,
                "result_type": "page",
                "sorts": [
                    {"property": "회차", "direction": "ascending"},
                    {"property": "정렬 순서", "direction": "ascending"},
                ],
            }
            if episode_numbers is not None:
                body["filter"] = {
                    "or": [
                        {
                            "property": "회차",
                            "number": {"equals": episode_no},
                        }
                        for episode_no in sorted(episode_numbers)
                    ]
                }
            if cursor is not None:
                body["start_cursor"] = cursor

            payload = self._post_with_retry(
                f"{NOTION_API_BASE_URL}/data_sources/{data_source_id}/query",
                body,
            )
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("Notion data source response has no results array.")
            pages.extend(item for item in results if isinstance(item, dict))

            if not payload.get("has_more"):
                return pages
            cursor = payload.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                raise ValueError("Notion response has_more=true but no next_cursor.")

    def _post_with_retry(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, MAX_QUERY_ATTEMPTS + 1):
            response = self._client.post(url, headers=self._headers, json=body)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < MAX_QUERY_ATTEMPTS:
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    delay_seconds = max(float(retry_after), 0.0)
                except ValueError:
                    delay_seconds = 1.0
                self._sleep(delay_seconds)
                continue

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Notion data source response must be a JSON object.")
            return payload

        raise AssertionError("Notion retry loop exited unexpectedly.")


def build_gold_dataset(
    pages: Iterable[dict[str, Any]],
    *,
    dataset_name: str,
    dataset_version: str | None = None,
    episode_numbers: set[int] | None = None,
    source_file_pattern: str = "{episode_no:02d}화.txt",
) -> GoldDataset:
    """Notion 행을 평가기가 소비하는 고정 GoldDataset 스냅샷으로 변환한다."""

    rows = []
    for page in pages:
        if (
            episode_numbers is not None
            and _annotation_episode_no(page) not in episode_numbers
        ):
            continue
        rows.append(_parse_annotation_page(page))
    if not rows:
        raise ValueError("No Notion annotation rows matched the selected episodes.")

    rows.sort(key=lambda row: (row["episode_no"], row["sort_order"], row["page_id"]))
    candidates_by_episode: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        candidates_by_episode.setdefault(row["episode_no"], []).append(row["candidate"])

    episodes = [
        {
            "episodeNo": episode_no,
            "title": f"{episode_no}화",
            "sourceFile": source_file_pattern.format(episode_no=episode_no),
            "candidates": candidates,
        }
        for episode_no, candidates in sorted(candidates_by_episode.items())
    ]
    snapshot_body = {
        "name": dataset_name,
        "episodes": episodes,
    }
    resolved_version = dataset_version or _snapshot_version(snapshot_body)
    return GoldDataset.model_validate(
        {
            "datasetVersion": resolved_version,
            **snapshot_body,
        }
    )


def _annotation_episode_no(page: dict[str, Any]) -> int:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Notion annotation page has no properties object.")
    return int(_read_number(properties, "회차"))


def _parse_annotation_page(page: dict[str, Any]) -> dict[str, Any]:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Notion annotation page has no properties object.")

    page_id = str(page.get("id") or "unknown")
    row_id = _read_text(properties, "정답 ID") or page_id
    episode_no = _annotation_episode_no(page)
    sort_order = _read_number(properties, "정렬 순서")
    decision = _read_select(properties, "판정")
    aliases = _parse_aliases(_read_text(properties, "추가 허용 factKey 별칭"), row_id)
    value_json = _parse_value_json(_read_text(properties, "정답 valueJson"), row_id)
    evidence_quotes = [
        line.strip()
        for line in _read_text(properties, "원문 근거").splitlines()
        if line.strip()
    ]

    candidate = {
        "decision": decision,
        "importance": _read_select(properties, "중요도") or None,
        "entityName": _required_text(properties, "소속 캐릭터", row_id),
        "factKey": _read_text(properties, "canonical factKey") or None,
        "factKeyAliases": aliases,
        "valueType": _read_select(properties, "valueType") or None,
        "attributeValue": _read_text(properties, "정답 attributeValue") or None,
        "valueJson": value_json,
        "evidenceQuotes": evidence_quotes,
        "note": _read_text(properties, "비고(판정 사유·검수 메모)") or None,
    }
    try:
        # 행 식별자를 붙여 검증 오류가 어느 Notion 행인지 바로 드러나게 한다.
        validated = GoldDataset.model_validate(
            {
                "datasetVersion": "row-validation",
                "name": "row-validation",
                "episodes": [
                    {
                        "episodeNo": int(episode_no),
                        "candidates": [candidate],
                    }
                ],
            }
        )
    except ValueError as exc:
        raise ValueError(f"Invalid Notion annotation row {row_id}: {exc}") from exc

    return {
        "page_id": page_id,
        "episode_no": int(episode_no),
        "sort_order": int(sort_order),
        "candidate": validated.episodes[0].candidates[0].model_dump(
            mode="json",
            by_alias=True,
            exclude={"gold_id"},
            exclude_none=True,
        ),
    }


def _property(properties: dict[str, Any], name: str) -> dict[str, Any]:
    value = properties.get(name)
    if not isinstance(value, dict):
        return {}
    return value


def _read_text(properties: dict[str, Any], name: str) -> str:
    prop = _property(properties, name)
    prop_type = prop.get("type")
    if prop_type not in {"title", "rich_text"}:
        return ""
    items = prop.get(prop_type)
    if not isinstance(items, list):
        return ""
    return "".join(
        str(item.get("plain_text") or "")
        for item in items
        if isinstance(item, dict)
    ).strip()


def _required_text(properties: dict[str, Any], name: str, row_id: str) -> str:
    value = _read_text(properties, name)
    if not value:
        raise ValueError(f"Notion annotation row {row_id} requires {name}.")
    return value


def _read_select(properties: dict[str, Any], name: str) -> str:
    selected = _property(properties, name).get("select")
    return str(selected.get("name") or "").strip() if isinstance(selected, dict) else ""


def _read_number(properties: dict[str, Any], name: str) -> float:
    value = _property(properties, name).get("number")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Notion annotation row requires numeric {name}.")
    if int(value) != value:
        raise ValueError(f"Notion annotation row requires integral {name}.")
    return value


def _parse_aliases(value: str, row_id: str) -> list[str]:
    if not value:
        return []
    try:
        aliases = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid factKey alias JSON in Notion row {row_id}: {exc}") from exc
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias.strip() for alias in aliases
    ):
        raise ValueError(f"factKey aliases must be a JSON string array in Notion row {row_id}.")
    return [alias.strip() for alias in aliases]


def _parse_value_json(value: str, row_id: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid valueJson in Notion row {row_id}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"valueJson must be a JSON object in Notion row {row_id}.")
    return parsed


def _snapshot_version(snapshot_body: dict[str, Any]) -> str:
    canonical = json.dumps(
        snapshot_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"notion-{hashlib.sha256(canonical).hexdigest()[:12]}"
