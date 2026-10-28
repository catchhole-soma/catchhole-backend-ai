"""Actual HTTP request construction, with an offline provider transport."""

import asyncio
from dataclasses import FrozenInstanceError
import json
from uuid import uuid4

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import LlmResponseSchema
from app.usage.metering import MeteredTextGenerationClient


def test_parallel_role_options_match_exact_count_and_preserve_default():
    from app.llm.protocols import LlmCallOptions

    bodies = []

    async def provider(request):
        body = json.loads(request.content)
        bodies.append((request.url.path, body))
        await asyncio.sleep(0)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 23})
        return httpx.Response(200, json={
            "status": "completed", "output_text": "{}",
            "usage": {"input_tokens": 23, "output_tokens": 2},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            client = OpenAIResponsesClient(
                "synthetic", "gpt-5.6-sol", "https://provider.invalid/responses",
                reasoning_effort="medium", http_client=http,
            )
            schema = LlmResponseSchema("inspection", {
                "type": "object", "properties": {}, "additionalProperties": False,
            })

            async def role(effort):
                options = LlmCallOptions(reasoning_effort=effort)
                assert await client.count_input_tokens(
                    "rules", effort, model="gpt-6.1-terra", response_schema=schema,
                    call_options=options,
                ) == 23
                await client.create_text_response(
                    "rules", effort, model="gpt-6.1-terra", response_schema=schema,
                    call_options=options,
                )

            await asyncio.gather(*(role(effort) for effort in ("medium", "high", "low")))
            await client.create_text_response("old extraction", "default")

    asyncio.run(run())
    for effort in ("medium", "high", "low"):
        matching = [(path, body) for path, body in bodies
                    if body["input"][1]["content"][0]["text"] == effort]
        assert len(matching) == 2
        count = next(body for path, body in matching if path.endswith("input_tokens"))
        generated = next(body for path, body in matching if not path.endswith("input_tokens"))
        assert count == {key: value for key, value in generated.items()
                         if key not in {"store", "max_output_tokens"}}
        assert generated["reasoning"] == {"effort": effort}
        assert generated["model"] == "gpt-6.1-terra"
    assert bodies[-1][1]["reasoning"] == {"effort": "medium"}
    assert bodies[-1][1]["model"] == "gpt-5.6-sol"
    options = LlmCallOptions(reasoning_effort="high")
    with pytest.raises(FrozenInstanceError):
        options.reasoning_effort = "low"


def test_metered_wrapper_forwards_role_options_to_count_and_generation():
    from app.llm.protocols import LlmCallOptions

    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 29})
        return httpx.Response(200, json={
            "status": "completed", "output_text": "{}",
            "usage": {"input_tokens": 29, "output_tokens": 2},
        })

    class Ledger:
        async def reserve_ai_tokens(self, **kwargs):
            pass

        async def settle_ai_tokens(self, **kwargs):
            pass

        async def release_ai_tokens(self, **kwargs):
            pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            raw = OpenAIResponsesClient(
                "synthetic", "gpt-5.6-sol", "https://provider.invalid/responses",
                reasoning_effort="medium", http_client=http,
            )
            client = MeteredTextGenerationClient(
                raw, Ledger(), uuid4(), "SETTING_EXTRACTION", "gpt-5.6-sol", uuid4(),
            )
            options = LlmCallOptions(reasoning_effort="high")
            assert await client.count_input_tokens(
                "rules", "source", model="gpt-6.1-terra", call_options=options,
            ) == 29
            response = await client.create_text_response(
                "rules", "source", model="gpt-6.1-terra", call_options=options,
            )
            assert response.text == "{}"

    asyncio.run(run())
    assert [body["reasoning"] for body in requests] == [{"effort": "high"}] * 2
