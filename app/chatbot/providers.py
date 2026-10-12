"""Execution-scoped adapters for the existing text/embedding provider contracts.

Delegates must make exactly one transport call (existing raw clients do); do not
pass the analysis Metered* wrappers, which own their own retry/lease lifecycle.
"""

from app.chatbot.runtime import Execution, ProviderFailure, ProviderResult
from app.llm.protocols import LlmResponseSchema, TextGenerationClient
from app.schemas.chat_execution import Usage
from app.usage.metering import (
    EmbeddingApi,
    _estimate_text_token_upper_bound,
    _estimate_embedding_token_upper_bound,
    _usage_from_text_error,
    _usage_from_embedding_error,
)


def measured_usage(values: tuple[int, int, int] | None, outcome: str) -> Usage:
    if values is None:
        return Usage(kind="UNKNOWN")
    try:
        return Usage(
            kind="SETTLE",
            input_tokens=values[0],
            cached_input_tokens=values[1],
            output_tokens=values[2],
            outcome=outcome,
        )
    except ValueError:
        return Usage(kind="UNKNOWN")


class ChatTextClient:
    def __init__(
        self,
        delegate: TextGenerationClient,
        execution: Execution,
        *,
        purpose: str,
        default_model: str,
    ):
        if purpose not in {"CHAT_INTERPRETATION", "CHAT_ANSWER"}:
            raise ValueError("Invalid chat text purpose")
        self.delegate, self.execution = delegate, execution
        self.purpose, self.default_model = purpose, default_model

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
        response_schema: LlmResponseSchema | None = None,
    ):
        if max_output_tokens < 1:
            raise ValueError("Positive output cap is required")
        effective_model = model or self.default_model
        maximum = _estimate_text_token_upper_bound(
            system_prompt, user_prompt, effective_model, max_output_tokens, response_schema
        )

        async def invoke():
            try:
                result = await self.delegate.create_text_response(
                    system_prompt,
                    user_prompt,
                    model=effective_model,
                    max_output_tokens=max_output_tokens,
                    prompt_cache_key=prompt_cache_key,
                    response_schema=response_schema,
                )
            except Exception as exc:
                raise ProviderFailure(
                    measured_usage(_usage_from_text_error(exc), "FAILURE")
                ) from None
            values = (
                result.input_token_count,
                result.cached_input_token_count or 0,
                result.output_token_count,
            )
            return ProviderResult(result, measured_usage(values, "SUCCESS"))

        return await self.execution.call(self.purpose, effective_model, maximum, invoke)


class ChatEmbeddingClient:
    def __init__(self, delegate: EmbeddingApi, execution: Execution, *, model_name: str):
        self.delegate, self.execution, self.model_name = delegate, execution, model_name
        self.version = delegate.version

    async def create_embeddings(self, inputs: list[str]):
        if not inputs:
            raise ValueError("Embedding input is required")
        maximum = _estimate_embedding_token_upper_bound(inputs)
        purpose = (
            "OPERATIONAL_BACKFILL" if self.execution.context.operational else "CHAT_QUERY_EMBEDDING"
        )

        async def invoke():
            try:
                result = await self.delegate.create_embeddings(inputs)
            except Exception as exc:
                raise ProviderFailure(
                    measured_usage(_usage_from_embedding_error(exc), "FAILURE")
                ) from None
            return ProviderResult(
                result, measured_usage((result.input_token_count, 0, 0), "SUCCESS")
            )

        return await self.execution.call(purpose, self.model_name, maximum, invoke)
