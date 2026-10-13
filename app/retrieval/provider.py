"""One bounded operational ledger execution per actual embedding attempt."""

import asyncio
import math
import time
from uuid import uuid4

import httpx

from app.chatbot.runtime import Execution, ProviderFailure, ProviderResult
from app.chatbot.providers import measured_usage
from app.embeddings.exceptions import RecoverableEmbeddingProviderError
from app.schemas.chat_execution import Finish, ChatResult
from app.usage.metering import _estimate_embedding_token_upper_bound, _usage_from_embedding_error


class IndexProviderFailure(ProviderFailure):
    def __init__(self, code, retryable, usage=None):
        super().__init__(usage)
        self.code, self.retryable = code, retryable


class OperationalEmbeddingProvider:
    def __init__(self, client, delegate, outbox, spec, actor):
        if not actor.strip() or len(actor) > 100:
            raise ValueError("A bounded operational actor is required")
        self.client, self.delegate, self.outbox = client, delegate, outbox
        self.spec, self.actor = spec, actor
        self.attempts = 0
        self.input_tokens = 0
        self.unknown_usage = 0

    async def embed(self, source, chunks, before_call):
        values = [c.chunk_text for c in chunks]
        maximum = _estimate_embedding_token_upper_bound(values)
        operation_id, runner = uuid4(), uuid4()
        dispatch = await self.client.operation(
            operation_id, source.work_id, self.actor, "RETRIEVAL_INDEX_SERVICE_PAID_V1", maximum
        )
        context = await self.client.start(dispatch, runner)
        if (
            not context.operational
            or context.work_id != source.work_id
            or context.question_id is not None
            or context.token_budget != maximum
        ):
            raise ValueError("Operational execution scope mismatch")
        execution = Execution(dispatch, runner, context, self.client, self.outbox)
        self.outbox.reserve_slot()  # finish report, separate from Execution's usage slot
        finish = Finish(failure="PROVIDER_FAILURE")

        async def preflight():
            await before_call(operation_id)

        async def invoke():
            self.attempts += 1
            try:
                result = await self.delegate.create_embeddings(values)
            except asyncio.CancelledError:
                self.unknown_usage += 1
                raise
            except Exception as exc:
                measured = _usage_from_embedding_error(exc)
                usage = measured_usage(measured, "FAILURE")
                self._usage(usage)
                recoverable = isinstance(exc, RecoverableEmbeddingProviderError)
                code = (
                    "PROVIDER_RETRYABLE"
                    if recoverable
                    else (
                        "PROVIDER_AUTH"
                        if isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code in {401, 403}
                        else "PROVIDER_CONTRACT"
                    )
                )
                raise IndexProviderFailure(code, recoverable, usage) from None
            usage = measured_usage((result.input_token_count, 0, 0), "SUCCESS")
            self._usage(usage)
            valid = (
                result.model == self.spec.model
                and len(result.embeddings) == len(chunks)
                and all(
                    len(v) == self.spec.dimensions
                    and all(math.isfinite(x) for x in v)
                    and any(x != 0 for x in v)
                    for v in result.embeddings
                )
            )
            if not valid:
                raise IndexProviderFailure("PROVIDER_CONTRACT", False, usage)
            return ProviderResult(result.embeddings, usage)

        interrupted = False
        async with asyncio.timeout(max(0, dispatch.deadline.timestamp() - time.time())):
            try:
                result = await execution.call(
                    "OPERATIONAL_BACKFILL", self.spec.model, maximum, invoke, preflight=preflight
                )
                finish = Finish(
                    result=ChatResult(
                        outcome="ANSWER",
                        text="Retrieval embedding attempt completed",
                        sources=[],
                        limitations=[],
                    )
                )
                return result
            except asyncio.CancelledError:
                interrupted = True
                raise
            finally:
                self.outbox.put(
                    f"finish:{operation_id}",
                    dispatch,
                    runner,
                    "finish",
                    finish.model_dump(by_alias=True, mode="json"),
                    dispatch.deadline.timestamp(),
                    reserved=True,
                )
                self.outbox.release_slot()
                # Cancellation cleanup persists reports without starting more network I/O.
                # Normal delivery shares both this deadline and the caller's time limit.
                if not interrupted:
                    await self.outbox.flush(self.client)

    def _usage(self, usage):
        if usage.kind == "SETTLE":
            self.input_tokens += usage.input_tokens
        else:
            self.unknown_usage += 1
