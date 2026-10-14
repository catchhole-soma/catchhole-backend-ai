"""Offline, synthetic QA replay. No .env, provider client, database or network access.

python -m app.chatbot.replay --fixture tests/fixtures/chat-qa-v1.json --case current_ability
Scripted model decisions verify plumbing, not production model quality.
"""

import argparse
import asyncio
import copy
import json
import tempfile
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx

from app.chatbot.client import ChatExecutionClient
from app.chatbot.outbox import Outbox
from app.chatbot.qa import ChatQaHandler
from app.chatbot.qa_config import ChatQaSettings
from app.chatbot.runtime import ChatRuntime
from app.llm.responses import LlmTextResponse
from app.schemas.chat_execution import Dispatch, ExecutionContext
from app.schemas.setting_tools import SettingToolQuery, SettingToolResponse


def offline_settings(**overrides):
    # Explicit values take precedence over every environment value, including enabled.
    values = {name: field.default for name, field in ChatQaSettings.model_fields.items()}
    return ChatQaSettings(**{**values, "enabled": False, **overrides})


def load_fixture(path: Path):
    fixture = json.loads(path.read_text())
    # Variants keep only deliberate differences from the base synthetic catalog.
    catalogs = {}
    for name, definition in fixture["catalogs"].items():
        if isinstance(definition, list):
            catalogs[name] = definition
            continue
        catalog = copy.deepcopy(catalogs[definition["base"]])
        for patch in definition.get("patches", []):
            value = catalog
            for segment in patch["path"]:
                value = value[segment]
            value.update(patch["changes"])
        catalog.extend(definition.get("append", []))
        catalogs[name] = catalog
    fixture["catalogs"] = catalogs
    return fixture


class ScriptedTextClient:
    def __init__(self, outputs):
        self.outputs = copy.deepcopy(outputs)
        self.requests = []

    async def create_text_response(self, system_prompt, user_prompt, **kwargs):
        self.requests.append({"system": system_prompt, "input": json.loads(user_prompt), **kwargs})
        if not self.outputs:
            raise AssertionError("Unexpected model call in offline replay")
        value = self.outputs.pop(0)
        return LlmTextResponse(
            text=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            input_token_count=10,
            cached_input_token_count=0,
            output_token_count=5,
        )


class ReplaySpring:
    def __init__(self, context, catalog):
        self.context = context
        self.catalog = copy.deepcopy(catalog)
        self.queries, self.reservations, self.reports, self.finishes = [], [], [], []
        self.query_status = None
        self.reserve_status = None
        self.reserve_code = None

    def respond(self, request):
        if request.url.host != "spring.test":
            raise AssertionError("Offline replay cannot access another host")
        action = request.url.path.split(f"/chat-runs/{self.context.attempt_id}/")[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "start":
            data = self.context.model_dump(by_alias=True, mode="json")
        elif action == "calls":
            if self.reserve_status:
                return httpx.Response(
                    self.reserve_status,
                    json={"success": False, "error": {"code": self.reserve_code}},
                )
            self.reservations.append(body)
            data = {
                "callId": body["callId"],
                "status": "RESERVED",
                "reservedTokens": body["reservedTokens"],
                "inputTokens": None,
                "cachedInputTokens": None,
                "outputTokens": None,
            }
        elif action == "tools":
            query = SettingToolQuery.model_validate(body)
            self.queries.append(body)
            if self.query_status:
                return httpx.Response(self.query_status, json={"success": False})
            data = self.lookup(query)
        elif action == "finish":
            self.finishes.append(body)
            data = {}
        elif action.startswith("calls/"):
            self.reports.append(body)
            data = {}
        else:
            raise AssertionError("Unknown offline action")
        return httpx.Response(200, json={"success": True, "data": data})

    def lookup(self, query):
        tool = query.tool.value

        def matching(item):
            return (query.fact_type is None or item["category"] == query.fact_type) and (
                query.setting_key is None
                or (item["settingKey"], item["scopeName"]) == (query.setting_key, query.scope_name)
            )

        def in_range(item):
            episode = item["sourceEpisodeNo"]
            return (
                query.episode_from is None or episode is not None and episode >= query.episode_from
            ) and (query.episode_to is None or episode is not None and episode <= query.episode_to)

        def matches_current_scalar(item, expected):
            # Mirror Java SettingToolScalarValue: display text and descriptive
            # fields cannot establish an exact current-value condition.
            raw = item.get("structuredValueJson")
            if raw is None:
                return False
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                return False
            if isinstance(value, dict):
                primary = value.get("value")
                value = primary if primary is not None else value.get("name")
            if value is None or isinstance(value, (dict, list)):
                return False
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

            def normalize(text):
                return unicodedata.normalize("NFC", text.strip()).lower()

            return normalize(text) == normalize(expected)

        if tool.startswith("FIND_"):
            kind = "CHARACTER" if tool == "FIND_CHARACTERS" else "WORLD_SETTING"
            items = [
                s["subject"]
                for s in self.catalog
                if s["subject"]["kind"] == kind
                and (query.query or "").casefold() in s["subject"]["subjectName"].casefold()
                and (
                    query.value_equals is None
                    or any(
                        matching(i) and matches_current_scalar(i, query.value_equals)
                        for i in s.get("current", [])
                    )
                )
            ]
        elif tool.endswith("_FIELDS"):
            domain_kind = "CHARACTER" if tool == "CHARACTER_FIELDS" else "WORLD_SETTING"
            fields = {}
            for subject in self.catalog:
                if subject["subject"]["kind"] != domain_kind or (
                    query.target_id is not None and subject["subject"]["id"] != str(query.target_id)
                ):
                    continue
                scoped = query.episode_from is not None or query.episode_to is not None
                values = ([] if scoped else subject.get("current", [])) + (
                    [i for i in subject.get("history", []) if in_range(i)]
                    if query.target_id
                    else []
                )
                for item in values:
                    if not matching(item):
                        continue
                    identity = (item["category"], item["scopeName"], item["settingKey"])
                    field = copy.deepcopy(item)
                    field.update(
                        kind="FIELD",
                        id=None,
                        value=None,
                        structuredValueJson=None,
                        sources=[],
                        evidence=None,
                        valueTruncated=False,
                        contributesToCurrent=None,
                        historyOnly=None,
                        operation=None,
                        sourceEpisodeNo=None,
                        sourceEpisodeId=None,
                        effectiveFromEpisodeNo=None,
                    )
                    if query.target_id is None:
                        field.update(
                            subjectId=str(self.context.work_id),
                            subjectName="작품의 인물 설정",
                            settingVersion=None,
                        )
                    fields[identity] = field
            items = [
                fields[key] for key in sorted(fields, key=lambda key: tuple(v or "" for v in key))
            ]
        elif tool.endswith("_EVIDENCE"):
            items = [
                e
                for subject in self.catalog
                for e in subject.get("evidence", [])
                if e["id"] == str(query.target_id)
            ]
            for item in items:
                episode = item["evidence"]["sourceEpisodeNo"]
                if (
                    query.episode_from is not None
                    and (episode is None or episode < query.episode_from)
                    or query.episode_to is not None
                    and (episode is None or episode > query.episode_to)
                ):
                    item = copy.deepcopy(item)
                    item["value"] = item["structuredValueJson"] = None
                    item["evidence"].update(status="OUT_OF_RANGE", quotes=[])
                    items = [item]
        else:
            key = "history" if tool.endswith("_HISTORY") else "current"
            items = [
                item
                for subject in self.catalog
                if subject["subject"]["id"] == str(query.target_id)
                for item in subject.get(key, [])
                if matching(item)
            ]
            if key == "history":
                items = [
                    item
                    for item in items
                    if (
                        query.episode_from is None
                        or item["sourceEpisodeNo"] is not None
                        and item["sourceEpisodeNo"] >= query.episode_from
                    )
                    and (
                        query.episode_to is None
                        or item["sourceEpisodeNo"] is not None
                        and item["sourceEpisodeNo"] <= query.episode_to
                    )
                ]
        total = len(items)
        start = query.page * query.size
        content = items[start : start + query.size]
        return SettingToolResponse.model_validate(
            {
                "schemaVersion": 1,
                "tool": tool,
                "partial": False,
                "limitations": ["SETTING_BOOK_UNSUPPORTED"],
                "items": {
                    "content": content,
                    "page": query.page,
                    "size": query.size,
                    "totalElements": total,
                    "totalPages": (total + query.size - 1) // query.size,
                    "hasNext": start + query.size < total,
                },
            }
        ).model_dump(by_alias=True, mode="json")


async def replay_case(case, catalog, *, outputs=None, config=None, spring_setup=None):
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    dispatch = Dispatch(
        schema_version=1, attempt_id=UUID(int=9001), capability="a" * 64, deadline=deadline
    )
    context = ExecutionContext(
        schema_version=1,
        question_id=UUID(int=9002),
        attempt_id=dispatch.attempt_id,
        work_id=UUID(int=9003),
        member_id=1,
        question=case["question"],
        episode_from=case.get("episodeFrom"),
        episode_to=case.get("episodeTo"),
        deadline=deadline,
        token_budget=32000,
        max_calls=case.get("maxCalls", 8),
        max_tool_calls=case.get("maxToolCalls", 20),
        operational=False,
    )
    spring = ReplaySpring(context, catalog)
    if spring_setup:
        spring_setup(spring)
    raw = ScriptedTextClient(outputs if outputs is not None else case["modelOutputs"])
    cfg = offline_settings(**(config or {}))
    with tempfile.TemporaryDirectory(prefix="catchhole-qa-replay-") as directory:
        async with httpx.AsyncClient(transport=httpx.MockTransport(spring.respond)) as http:
            box = Outbox(str(Path(directory) / "outbox.sqlite3"))
            runtime = ChatRuntime(
                ChatExecutionClient("http://spring.test", "synthetic-internal-key", http),
                box,
                handler=ChatQaHandler(raw, cfg),
            )
            try:
                runtime.submit(dispatch)
                await asyncio.gather(*list(runtime.tasks.values()))
                await runtime.deliver()
                if len(spring.finishes) != 1:
                    raise AssertionError("Offline runtime did not report exactly one finish")
                return {
                    "id": case["id"],
                    "question": case["question"],
                    "queries": spring.queries,
                    "finish": spring.finishes[0],
                    "reservations": spring.reservations,
                    "usageReports": spring.reports,
                    "modelRequests": raw.requests,
                }
            finally:
                box.close()


def check_expected(case, result):
    expected = case["expected"]
    actual = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert actual["outcome"] == expected["outcome"]
    assert len(actual["sources"]) == expected["sourceCount"]
    for text in expected.get("contains", []):
        assert text in actual["text"], (case["id"], text)
    for text in expected.get("excludes", []):
        assert text not in actual["text"], (case["id"], text)
    assert [q["tool"] for q in result["queries"]] == expected["tools"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--case")
    args = parser.parse_args()
    fixture = load_fixture(args.fixture)
    cases = [c for c in fixture["cases"] if args.case is None or c["id"] == args.case]
    if not cases:
        parser.error("Unknown case")
    for case in cases:
        result = asyncio.run(replay_case(case, fixture["catalogs"][case["catalog"]]))
        check_expected(case, result)
        if args.case:
            result.pop("modelRequests")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"PASS {case['id']}: {result['finish']['result']['outcome']} / {len(result['queries'])} queries"
            )
    print(
        f"{len(cases)} synthetic replays passed; provider API calls: 0; model quality: not measured"
    )


if __name__ == "__main__":
    main()
