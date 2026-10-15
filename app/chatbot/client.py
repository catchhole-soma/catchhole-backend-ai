"""Safe internal HTTP boundary; transport retries never invoke a model."""

from uuid import UUID
import httpx
from pydantic import ValidationError
from app.schemas.chat_execution import Dispatch, ExecutionContext, CallView
from app.schemas.chat_retrieval import RetrievalReceipt


class ChatTransportError(RuntimeError):
    def __init__(self, status: int | None = None, code: str | None = None):
        self.status = status
        self.code = code
        super().__init__("Chat execution transport failed")

    @property
    def terminal(self) -> bool:
        return self.status in {400, 403, 404, 409, 410, 422}


class ChatExecutionClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient):
        self.base_url, self.api_key, self.http = base_url.rstrip("/"), api_key, http

    async def request(
        self, dispatch: Dispatch, runner: UUID, action: str, body=None, *, method="POST"
    ):
        try:
            response = await self.http.request(
                method,
                f"{self.base_url}/api/internal/v1/chat-runs/{dispatch.attempt_id}/{action}",
                headers={
                    "X-Internal-Api-Key": self.api_key,
                    "X-Chat-Capability": dispatch.capability,
                    "X-Chat-Runner": str(runner),
                },
                json=body,
                timeout=10,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise ChatTransportError() from None
        if not 200 <= response.status_code < 300:
            # Only allow-listed codes leave this boundary, never error messages or request data.
            code = None
            try:
                envelope = response.json()
                error = envelope.get("error") if isinstance(envelope, dict) else None
                raw_code = error.get("code") if isinstance(error, dict) else None
                if isinstance(raw_code, str):
                    code = {
                        "AI_TOKEN_QUOTA_EXHAUSTED": "QUOTA_EXHAUSTED",
                        "CHAT_LIMIT_EXCEEDED": "EXECUTION_LIMIT_EXCEEDED",
                        "CHAT_SCOPE_INVALID": "SOURCE_CHANGED",
                        "CHAT_SOURCE_UNAVAILABLE": "STORAGE_UNAVAILABLE",
                    }.get(raw_code)
            except ValueError:
                pass
            raise ChatTransportError(response.status_code, code)
        try:
            envelope = response.json()
            if envelope["success"] is not True:
                raise ValueError()
            return envelope["data"]
        except (ValueError, KeyError, TypeError):
            raise ChatTransportError() from None

    async def start(self, dispatch: Dispatch, runner: UUID) -> ExecutionContext:
        try:
            context = ExecutionContext.model_validate(await self.request(dispatch, runner, "start"))
            if context.attempt_id != dispatch.attempt_id or context.deadline != dispatch.deadline:
                raise ValueError()
            return context
        except (ValueError, ValidationError):
            raise ChatTransportError(409) from None

    async def reserve(self, dispatch, runner, call_id, purpose, model, tokens) -> CallView:
        raw = await self.request(
            dispatch,
            runner,
            "calls",
            {"callId": str(call_id), "purpose": purpose, "model": model, "reservedTokens": tokens},
        )
        try:
            result = CallView.model_validate(raw)
            if (
                result.call_id != call_id
                or result.reserved_tokens != tokens
                or result.status != "RESERVED"
            ):
                raise ValueError()
            return result
        except ValueError:
            raise ChatTransportError(409) from None

    async def retrieval_sources(self, dispatch, runner, sources):
        raw = await self.request(
            dispatch,
            runner,
            "retrieval-sources",
            {"sources": [source.model_dump(by_alias=True, mode="json") for source in sources]},
        )
        try:
            receipt = RetrievalReceipt.model_validate(raw)
            if receipt.sources != list(sources):
                raise ValueError("Receipt does not match the exact requested slices")
            return receipt.sources
        except ValueError:
            raise ChatTransportError(409, "SOURCE_CHANGED") from None

    async def operation(
        self, operation_id: UUID, work_id: UUID, actor: str, policy: str, tokens: int
    ) -> Dispatch:
        try:
            response = await self.http.post(
                f"{self.base_url}/api/internal/v1/chat-runs/operations/{operation_id}",
                headers={"X-Internal-Api-Key": self.api_key},
                json={
                    "workId": str(work_id),
                    "actor": actor,
                    "policy": policy,
                    "tokenBudget": tokens,
                },
                timeout=10,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise ChatTransportError() from None
        if not 200 <= response.status_code < 300:
            raise ChatTransportError(response.status_code)
        try:
            body = response.json()
            if body["success"] is not True:
                raise ValueError()
            result = Dispatch.model_validate(body["data"])
            if result.attempt_id != operation_id:
                raise ValueError()
            return result
        except (ValueError, KeyError, TypeError):
            raise ChatTransportError() from None
