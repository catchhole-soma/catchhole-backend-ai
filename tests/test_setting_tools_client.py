import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.clients.setting_tools_client import (
    SettingToolClientError,
    SettingToolContext,
    SettingToolsClient,
)
from app.schemas.setting_tools import SettingTool, SettingToolQuery, SettingToolResponse


FIXTURE = Path(__file__).parent / "fixtures" / "setting-tools-v1.json"


def test_java_http_contract_fixture_round_trips_without_losing_metadata():
    payload = json.loads(FIXTURE.read_text())
    result = SettingToolResponse.model_validate(payload)
    assert result.model_dump(mode="json", by_alias=True) == payload
    assert result.items.content[0].evidence.quotes[0].start_offset == 2
    assert result.items.content[0].evidence.source_chunk_id is None


def test_server_context_is_in_headers_and_not_model_arguments():
    context = SettingToolContext(member_id=42, work_id=uuid4())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": json.loads(FIXTURE.read_text())})

    client = SettingToolsClient(
        "https://spring.example",
        "private-test-key",
        context,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = asyncio.run(
        client.query(
            SettingToolQuery(
                tool=SettingTool.CHARACTER_EVIDENCE,
                target_id=UUID("00000000-0000-0000-0000-000000000001"),
            )
        )
    )
    request = requests[0]
    assert request.url.path == f"/api/internal/v1/works/{context.work_id}/setting-tools/queries"
    assert request.headers["X-Setting-Tool-Member-Id"] == "42"
    assert request.headers["X-Internal-Api-Key"] == "private-test-key"
    assert "memberId" not in json.loads(request.content)
    assert "workId" not in json.loads(request.content)
    assert "lease" not in " ".join(request.headers).lower()
    assert response.items.total_elements == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"tool": "EXECUTE_SQL", "query": "select *"},
        {"tool": "FIND_CHARACTERS", "memberId": 1},
        {"tool": "FIND_CHARACTERS", "workId": str(uuid4())},
        {"tool": "FIND_CHARACTERS", "size": 51},
        {"tool": "FIND_CHARACTERS", "size": True},
        {"tool": "CHARACTER_CURRENT"},
        {"tool": "CHARACTER_CURRENT", "targetId": str(uuid4()), "episodeTo": 10},
        {"tool": "WORLD_CURRENT", "targetId": str(uuid4()), "scopeName": "고급"},
        {"tool": "WORLD_HISTORY", "targetId": str(uuid4()), "episodeFrom": 3, "episodeTo": 2},
        {"tool": "CHARACTER_EVIDENCE", "targetId": str(uuid4()), "page": 1},
    ],
)
def test_rejects_unbounded_or_unauthorized_model_arguments(arguments):
    with pytest.raises(ValidationError):
        SettingToolQuery.model_validate(arguments)


@pytest.mark.parametrize("status", [302, 400, 401, 404, 409, 500, 503])
def test_http_failures_do_not_expose_response_body_or_follow_redirects(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="private-manuscript-and-key",
            headers={"location": "https://untrusted.example"},
        )

    client = SettingToolsClient(
        "https://spring.example",
        "secret",
        SettingToolContext(1, uuid4()),
        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    with pytest.raises(SettingToolClientError) as raised:
        asyncio.run(client.query(SettingToolQuery(tool=SettingTool.FIND_CHARACTERS)))
    assert raised.value.status_code == status
    assert "private" not in str(raised.value)
    assert "secret" not in str(raised.value)
    assert len(calls) == 1


def test_transport_error_is_typed_without_raw_request_details():
    def handler(request):
        raise httpx.ReadTimeout("private-detail", request=request)

    client = SettingToolsClient(
        "https://spring.example",
        "secret",
        SettingToolContext(1, uuid4()),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SettingToolClientError, match="TRANSPORT_ERROR") as raised:
        asyncio.run(client.query(SettingToolQuery(tool=SettingTool.FIND_CHARACTERS)))
    assert "private-detail" not in str(raised.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schemaVersion=2),
        lambda payload: payload.update(tool="WORLD_EVIDENCE"),
        lambda payload: payload["items"].update(page=1),
        lambda payload: payload["items"]["content"][0]["evidence"].update(
            status="NEW_UNHANDLED_STATUS"
        ),
    ],
)
def test_rejects_changed_contract_instead_of_treating_it_as_empty_results(mutate):
    payload = json.loads(FIXTURE.read_text())
    mutate(payload)
    client = SettingToolsClient(
        "https://spring.example",
        "secret",
        SettingToolContext(1, uuid4()),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"success": True, "data": payload})
            )
        ),
    )
    with pytest.raises(SettingToolClientError, match="CONTRACT_ERROR"):
        asyncio.run(
            client.query(SettingToolQuery(tool=SettingTool.CHARACTER_EVIDENCE, target_id=uuid4()))
        )


def test_partial_status_and_empty_results_remain_distinct():
    payload = json.loads(FIXTURE.read_text())
    payload["partial"] = True
    evidence = payload["items"]["content"][0]["evidence"]
    evidence.update(status="SOURCE_PURGED", quotes=[], matchesCurrentSource=False)
    result = SettingToolResponse.model_validate(payload)
    assert result.partial
    assert result.items.content[0].evidence.status == "SOURCE_PURGED"
    payload["items"].update(content=[], totalElements=0, totalPages=0)
    payload["partial"] = False
    assert SettingToolResponse.model_validate(payload).items.content == []
