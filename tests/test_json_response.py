import asyncio
import logging

import pytest
from pydantic import BaseModel

from app.analysis.json_response import request_validated_model
from app.llm.exceptions import LlmIncompleteResponseError


class ResponseModel(BaseModel):
    value: str


class IncompleteResponseClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(self, **kwargs):
        self.call_count += 1
        raise LlmIncompleteResponseError("provider incomplete")


def test_non_truncation_incomplete_response_is_not_retried() -> None:
    client = IncompleteResponseClient()

    with pytest.raises(LlmIncompleteResponseError):
        asyncio.run(
            request_validated_model(
                client=client,
                response_model=ResponseModel,
                system_prompt="Return JSON.",
                user_prompt="input",
                model="test-model",
                max_output_tokens=100,
                max_attempts=3,
                prompt_cache_key="test-key",
                operation_name="test extraction",
                logger=logging.getLogger(__name__),
            )
        )

    assert client.call_count == 1
