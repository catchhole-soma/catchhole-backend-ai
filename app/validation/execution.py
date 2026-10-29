"""Claim configuration and real validation ledger adapters. Defaults remain disabled."""
import asyncio
from hashlib import sha256

from app.usage.metering import MeteredEmbeddingClient, MeteredTextGenerationClient
from app.validation.contracts import IncompleteInspection, InvocationBudget
from app.validation.models import MODEL, ROLE_OPTIONS

_BUDGET_FIELDS = {
    "model_calls": "modelCalls", "lookups": "lookups", "seconds": "seconds",
    "input_tokens": "inputTokens", "output_tokens": "outputTokens",
    "segment_chars": "segmentChars", "context_chars": "contextChars",
    "catalogue_page_size": "cataloguePageSize", "search_limit": "searchLimit",
}


def claim_budgets(claim):
    try:
        budgets = claim.wire["budgets"]
        for role, options in ROLE_OPTIONS.items():
            model, cap = claim.wire["models"][role], budgets["roles"][role]
            if (model != {"model": MODEL, "reasoningEffort": options.reasoning_effort}
                    or type(cap["maxCalls"]) is not int or cap["maxCalls"] < 1
                    or type(cap["maxReservedTokens"]) is not int or cap["maxReservedTokens"] < 1):
                raise ValueError("Explicit frozen role settings required")
        return {branch: InvocationBudget(**{key: budgets[branch.lower()][wire]
                                           for key, wire in _BUDGET_FIELDS.items()})
                for branch in ("SETTING", "TYPO")}
    except (KeyError, TypeError, ValueError):
        raise IncompleteInspection("EXECUTION_BUDGET_CONFIGURATION_INVALID") from None


class ValidationLedger:
    """The metering Protocol's legacy parameter name never creates an AnalysisJob owner."""
    def __init__(self, client, claim, gate):
        self.client, self.claim, self.gate = client, claim, gate
        self.requests = set()
        self._locks, self._settled = {}, set()
        self._metering_tasks = set()
        self._drain_task = None
        self.query_input = None
        self.query_request_id = None

    async def reserve_ai_tokens(self, request_id, analysis_job_id, purpose, attempt,
                                model_name, reserved_tokens, lease_token):
        self.gate.require()
        if (analysis_job_id != self.claim.job_id or lease_token != self.claim.lease_token
                or not purpose.startswith("VALIDATION_")):
            raise IncompleteInspection("USAGE_OWNER_INVALID")
        await self.client.leased(self.claim, "usage/reserve", gate=self.gate,
            requestId=str(request_id), role=purpose.removeprefix("VALIDATION_"),
            model=model_name, reservedTokens=reserved_tokens,
            queryInput=self.query_input if purpose == "VALIDATION_QUERY_EMBEDDING" else None)
        if purpose == "VALIDATION_QUERY_EMBEDDING":
            self.query_request_id = str(request_id)
        self.requests.add(request_id)

    async def settle_ai_tokens(self, request_id, input_tokens, cached_input_tokens, output_tokens, outcome):
        async with self._locks.setdefault(request_id, asyncio.Lock()):
            await self.client.leased(self.claim, f"usage/{request_id}", late=True,
                kind="SETTLE", inputTokens=input_tokens, cachedInputTokens=cached_input_tokens,
                outputTokens=output_tokens, outcome=outcome)
            self._settled.add(request_id)
            self.requests.discard(request_id)

    async def release_ai_tokens(self, request_id, outcome):
        # Metering's missing/ambiguous usage is UNKNOWN, never proof of no provider call.
        async with self._locks.setdefault(request_id, asyncio.Lock()):
            if request_id in self._settled:
                return
            await self.client.leased(self.claim, f"usage/{request_id}", late=True,
                kind="UNKNOWN", inputTokens=None, cachedInputTokens=None, outputTokens=None, outcome=None)
            self.requests.discard(request_id)

    def track_metering_task(self, task):
        # Includes reservation-response -> UNKNOWN chains, not just the current HTTP coroutine.
        self._metering_tasks.add(task)

    async def drain(self):
        self.gate.stop()
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain())
        await asyncio.shield(self._drain_task)

    async def _drain(self):
        failures = []
        while self._metering_tasks:
            pending = tuple(self._metering_tasks)
            results = await asyncio.gather(*pending, return_exceptions=True)
            self._metering_tasks.difference_update(pending)
            failures.extend(result for result in results if isinstance(result, BaseException))
        if failures:
            raise failures[0]
        await self.mark_unknown()

    async def mark_unknown(self):
        for request_id in tuple(self.requests):
            await self.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")


class ValidationRoleClients:
    def __init__(self, raw, ledger, claim):
        self.clients = {role: MeteredTextGenerationClient(
            raw, ledger, claim.job_id, "VALIDATION_" + role, MODEL, claim.lease_token,
            max_retries=0, exact_input_reservation=True) for role in ROLE_OPTIONS}

    def for_validation_role(self, role):
        return self.clients[role]


def metered_embedding(raw, ledger, claim, spec):
    if raw is None:
        return None
    model = claim.wire["models"].get("QUERY_EMBEDDING", {})
    cap = claim.wire["budgets"]["roles"].get("QUERY_EMBEDDING", {})
    if (model.get("version") != spec.version or model.get("dimensions") != spec.dimensions
            or model.get("model") != spec.model or getattr(raw, "model", None) != spec.model
            or raw.version != spec.version or raw.dimensions != spec.dimensions
            or not cap.get("maxCalls") or not cap.get("maxReservedTokens")):
        raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
    return ValidationQueryEmbedding(raw, ledger, claim, spec)


class ValidationQueryEmbedding:
    def __init__(self, raw, ledger, claim, spec):
        self.model, self.version, self.dimensions = spec.model, spec.version, spec.dimensions
        self.ledger, self.claim = ledger, claim
        self.last_receipt = None
        self.metered = MeteredEmbeddingClient(raw, ledger, claim.job_id, spec.model,
            claim.lease_token, max_retries=0, purpose="VALIDATION_QUERY_EMBEDDING")

    async def create_embedding(self, query):
        self.last_receipt = None
        proof = {"contractVersion": 1, "inputDigest": self.claim.scope.inputDigest,
                 "queryHash": sha256(query.encode()).hexdigest(), "indexModel": self.model,
                 "indexVersion": self.version, "indexDimensions": self.dimensions,
                 "manifestHash": self.claim.wire["manifestHash"]}
        self.ledger.query_input = proof
        response = await self.metered.create_embeddings([query])
        if response.model != self.model or len(response.embeddings[0]) != self.dimensions:
            raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
        self.last_receipt = {**proof, "requestId": self.ledger.query_request_id}
        return response.embeddings[0]
