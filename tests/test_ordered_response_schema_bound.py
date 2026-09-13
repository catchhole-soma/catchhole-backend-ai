import asyncio

import pytest

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.ordered_context import BoundedOrderedClient, ensure_ordered_prompt_fits
from app.llm.protocols import LlmResponseSchema
from app.llm.responses import LlmTextResponse


def test_structured_schema_counts_toward_ordered_limit_before_calling_provider():
    requests = []

    class Client:
        async def create_text_response(self, **kwargs):
            requests.append(kwargs)
            return LlmTextResponse(text="{}")

    schema = LlmResponseSchema(name="resolution", schema={
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    })
    request = {"system_prompt": "Return JSON.", "user_prompt": "Input."}
    legacy_size = ensure_ordered_prompt_fits(**request)
    structured_size = ensure_ordered_prompt_fits(**request, response_schema=schema)
    assert structured_size > legacy_size

    with pytest.raises(OrderedInputContextError, match="exceeds_input_limit"):
        asyncio.run(BoundedOrderedClient(Client(), max_tokens=structured_size - 1)
                    .create_text_response(**request, response_schema=schema))
    assert requests == []

    asyncio.run(BoundedOrderedClient(Client(), max_tokens=structured_size)
                .create_text_response(**request, response_schema=schema))
    assert requests == [{**request, "response_schema": schema}]

    requests.clear()
    asyncio.run(BoundedOrderedClient(Client(), max_tokens=legacy_size)
                .create_text_response(**request))
    assert requests == [request]
