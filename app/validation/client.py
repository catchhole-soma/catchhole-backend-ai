"""Dedicated Java validation HTTP contract. Scope tokens never enter model input."""
import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
from pydantic import Field

from app.validation.contracts import Contract, IncompleteInspection


class ExecutionScope(Contract):
    contractVersion: int = Field(ge=1, le=1, strict=True)
    reportId: UUID
    generation: int = Field(ge=1, strict=True)
    inputDigest: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class ValidationClaim:
    job_id: UUID
    lease_token: UUID = field(repr=False)
    scope: ExecutionScope
    wire: dict[str, Any] = field(repr=False)

    @classmethod
    def from_wire(cls, value):
        if any(type(value.get(key)) is not int or value[key] != 1
               for key in ("contractVersion", "projectionVersion", "clueVersion")):
            raise IncompleteInspection("UNSUPPORTED_EXECUTION_CONTRACT")
        for key in ("memberId", "baselineRevision", "reportRevision", "attempt"):
            if type(value.get(key)) is not int or value[key] < 1:
                raise IncompleteInspection("INVALID_EXECUTION_CONTRACT")
        for key in ("baselineHash", "targetHash", "manifestHash"):
            ExecutionScope(contractVersion=1, reportId=value["scope"]["reportId"],
                           generation=1, inputDigest=value[key])
        UUID(value["workId"])
        UUID(value["episodeId"])
        return cls(UUID(value["jobId"]), UUID(value["leaseToken"]),
                   ExecutionScope.model_validate(value["scope"]), value)


class ExecutionGate:
    def __init__(self):
        self.stopped = asyncio.Event()

    def require(self):
        if self.stopped.is_set():
            raise IncompleteInspection("EXECUTION_STOPPED")

    def stop(self):
        self.stopped.set()


class ValidationExecutionClient:
    def __init__(self, base_url, internal_api_key, http_client=None):
        self.base_url, self.key = base_url.rstrip("/"), internal_api_key
        self.http = http_client or httpx.AsyncClient(timeout=30)
        self.owns_http = http_client is None

    async def request(self, operation, *, claim=None, body=None, gate=None, late=False):
        if gate is not None and not late:
            gate.require()
        path = "/api/internal/v1/validation-jobs/"
        path += "claim" if claim is None else f"{claim.job_id}/{operation}"
        headers = {"X-Internal-Api-Key": self.key}
        if claim is not None:
            headers["X-Worker-Lease-Token"] = str(claim.lease_token)
        try:
            response = await self.http.request("GET" if body is None else "POST",
                                               self.base_url + path, headers=headers, json=body)
            response.raise_for_status()
            envelope = response.json()
            if not isinstance(envelope, dict) or "data" not in envelope:
                raise ValueError("Invalid common envelope")
            return envelope["data"]
        except httpx.HTTPStatusError as exc:
            code = None
            try:
                code = exc.response.json().get("error", {}).get("code")
            except (ValueError, AttributeError):
                pass
            if not isinstance(code, str):
                code = None
            independent_failure = code in {
                "VALIDATION_EXECUTION_LIMIT_EXCEEDED", "AI_TOKEN_QUOTA_EXHAUSTED",
                "VALIDATION_PUBLICATION_CONFLICT", "VALIDATION_IDEMPOTENCY_CONFLICT",
                "AI_TOKEN_RESERVATION_CONFLICT",
            }
            if (gate is not None and exc.response.status_code in {401, 403, 404, 409}
                    and not independent_failure and not late):
                gate.stop()
            raise IncompleteInspection(code if independent_failure else "EXECUTION_HTTP_REJECTED") from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise IncompleteInspection("EXECUTION_TRANSPORT_FAILED") from None

    async def claim_next(self, worker_id):
        value = await self.request("claim", body={"contractVersion": 1, "workerId": worker_id,
                                                "capabilities": ["VALIDATION_EXECUTION_V1"]})
        return ValidationClaim.from_wire(value) if value is not None else None

    async def leased(self, claim, operation, *, gate=None, late=False, **body):
        return await self.request(operation, claim=claim, gate=gate, late=late,
                                  body={"scope": claim.scope.model_dump(mode="json"), **body})

    async def load_checkpoint(self, claim, gate=None):
        return await self.request("checkpoint", claim=claim, gate=gate)

    async def aclose(self):
        if self.owns_http:
            await self.http.aclose()
