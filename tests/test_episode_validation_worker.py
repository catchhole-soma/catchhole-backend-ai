"""Dedicated execution/ledger tests use only synthetic HTTP providers."""
import asyncio
from uuid import uuid4

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.usage.metering import MeteredTextGenerationClient
from tests.test_ai_token_metering import FakeLedger


def test_validation_exact_reservation_does_not_change_ordinary_defaults():
    async def run():
        paths = []
        async def provider(request):
            paths.append(request.url.path)
            if request.url.path.endswith("input_tokens"):
                return httpx.Response(200, json={"input_tokens": 23})
            return httpx.Response(200, json={"status": "completed", "output_text": "{}",
                                             "usage": {"input_tokens": 23, "output_tokens": 4}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            raw = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses", http_client=http)
            ledger = FakeLedger()
            client = MeteredTextGenerationClient(raw, ledger, uuid4(), "VALIDATION_TYPO",
                "gpt-6.1-terra", uuid4(), max_retries=0, exact_input_reservation=True)
            await client.count_input_tokens(system_prompt="synthetic", user_prompt="fixture")
            await client.create_text_response(system_prompt="synthetic", user_prompt="fixture", max_output_tokens=100)
            assert ledger.reservations[0]["reserved_tokens"] == 123
            assert paths.count("/responses/input_tokens") == 1
    asyncio.run(run())


@pytest.mark.parametrize("code", ["VALIDATION_EXECUTION_LIMIT_EXCEEDED", "AI_TOKEN_QUOTA_EXHAUSTED", "VALIDATION_PUBLICATION_CONFLICT"])
def test_one_role_cap_rejection_leaves_the_other_branch_publishable(code):
    from app.validation.client import (
        ExecutionGate,
        ExecutionScope,
        ValidationClaim,
        ValidationExecutionClient,
    )
    from app.validation.contracts import IncompleteInspection
    async def run():
        gate = ExecutionGate()
        claim = ValidationClaim(uuid4(), uuid4(), ExecutionScope(contractVersion=1, reportId=uuid4(),
                                                                generation=1, inputDigest="a" * 64), {})
        async def server(request):
            if request.url.path.endswith("reserve"):
                return httpx.Response(409, json={"error": {"code": code}})
            return httpx.Response(200, json={"data": {"unitRef": "TSEG1"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
            client = ValidationExecutionClient("https://spring.invalid", "synthetic", http)
            with pytest.raises(IncompleteInspection):
                await client.leased(claim, "usage/reserve", gate=gate)
            assert not gate.stopped.is_set()
            assert await client.leased(claim, "units", gate=gate) == {"unitRef": "TSEG1"}
    asyncio.run(run())
