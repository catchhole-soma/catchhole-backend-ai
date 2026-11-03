"""Claim configuration and real validation ledger adapters. Defaults remain disabled."""
import asyncio
import math
from hashlib import sha256

from app.usage.metering import MeteredEmbeddingClient, MeteredTextGenerationClient
from app.validation.contracts import IncompleteInspection, InvocationBudget
from app.validation.models import GENERATION_ROLES, RoleRuntime

CONFIGURATION_VERSION = 2  # Job configuration v2: per-role model/effort/outputTokens, no catalogue page size
_BUDGET_FIELDS = {
    "model_calls": "modelCalls", "lookups": "lookups", "seconds": "seconds",
    "input_tokens": "inputTokens", "segment_chars": "segmentChars", "context_chars": "contextChars",
    "search_limit": "searchLimit",
}


def _positive(value):
    return type(value) is int and value >= 1


def _current_configuration(claim):
    if type(claim.wire.get("configurationVersion")) is not int or claim.wire["configurationVersion"] != CONFIGURATION_VERSION:
        raise ValueError("Legacy job configuration")


def claim_budgets(claim):
    """Per-branch InvocationBudget (per claim attempt); the server's per-run role caps stay authoritative."""
    try:
        _current_configuration(claim)
        claim_roles(claim)
        budgets = claim.wire["budgets"]
        return {branch: InvocationBudget(**{key: budgets[branch.lower()][wire] for key, wire in _BUDGET_FIELDS.items()})
                for branch in ("SETTING", "TYPO")}
    except IncompleteInspection:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IncompleteInspection("EXECUTION_BUDGET_CONFIGURATION_INVALID") from None


def claim_roles(claim):
    """`{PLANNER, JUDGE, TYPO: RoleRuntime}` frozen in the job configuration (v2); anything else is invalid."""
    try:
        _current_configuration(claim)
        models, roles = claim.wire["models"], claim.wire["budgets"]["roles"]
        result = {}
        for role in GENERATION_ROLES:
            model, cap = models[role], roles[role]
            if (not isinstance(model, dict) or not isinstance(cap, dict) or not _positive(cap.get("maxCalls"))
                    or not _positive(cap.get("maxReservedTokens"))):
                raise ValueError("Explicit frozen role settings required")
            result[role] = RoleRuntime.of(model["model"], model["reasoningEffort"], cap["outputTokens"])
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
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
        self.index_input = None

    async def reserve_ai_tokens(self, request_id, analysis_job_id, purpose, attempt,
                                model_name, reserved_tokens, lease_token):
        self.gate.require()
        if (analysis_job_id != self.claim.job_id or lease_token != self.claim.lease_token
                or not purpose.startswith("VALIDATION_")):
            raise IncompleteInspection("USAGE_OWNER_INVALID")
        await self.client.leased(self.claim, "usage/reserve", gate=self.gate,
            requestId=str(request_id), role=purpose.removeprefix("VALIDATION_"),
            model=model_name, reservedTokens=reserved_tokens,
            queryInput=self.query_input if purpose == "VALIDATION_QUERY_EMBEDDING" else None,
            # Only an index fill call carries indexInput; every other reservation keeps the v1 body unchanged.
            **({"indexInput": self.index_input} if purpose == "VALIDATION_INDEX_EMBEDDING" else {}))
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
    """One metered client per generation role, each defaulting to that role's frozen model."""

    def __init__(self, raw, ledger, claim, runtimes, *, request_semaphore=None):
        # request_semaphore is the runner's one process-wide provider cap; None keeps calls uncapped.
        self.runtimes = dict(runtimes)
        self.clients = {role: MeteredTextGenerationClient(
            raw, ledger, claim.job_id, "VALIDATION_" + role, runtime.model, claim.lease_token,
            request_semaphore=request_semaphore, max_retries=0, exact_input_reservation=True)
            for role, runtime in self.runtimes.items()}

    def for_validation_role(self, role):
        return self.clients[role]


def metered_embedding(raw, ledger, claim, spec, *, request_semaphore=None):
    """Optional QUERY_EMBEDDING client of one claim; None means VECTOR is not offered (GH344).

    A claim without any QUERY_EMBEDDING model or cap means the API did not configure the optional role, so
    the job runs keyword-only instead of failing. A role that is present but differs from the index, or has
    no positive cap, is still a configuration error.
    """
    if raw is None:
        return None
    model = claim.wire["models"].get("QUERY_EMBEDDING")
    cap = claim.wire["budgets"]["roles"].get("QUERY_EMBEDDING")
    if model is None and cap is None:
        return None
    if not isinstance(model, dict) or not isinstance(cap, dict):
        raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
    if (model.get("version") != spec.version or model.get("dimensions") != spec.dimensions
            or model.get("model") != spec.model or getattr(raw, "model", None) != spec.model
            or raw.version != spec.version or raw.dimensions != spec.dimensions
            or not cap.get("maxCalls") or not cap.get("maxReservedTokens")):
        raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
    return ValidationQueryEmbedding(raw, ledger, claim, spec, request_semaphore=request_semaphore)


class ValidationQueryEmbedding:
    def __init__(self, raw, ledger, claim, spec, *, request_semaphore=None):
        self.model, self.version, self.dimensions = spec.model, spec.version, spec.dimensions
        self.ledger, self.claim = ledger, claim
        self.last_receipt = None
        # The ledger's queryInput/requestId and last_receipt are shared per-job state; parallel judges must never
        # interleave one query's reservation identity with another's proof (the corpus also serializes searches).
        self._lock = asyncio.Lock()
        self.metered = MeteredEmbeddingClient(raw, ledger, claim.job_id, spec.model,
            claim.lease_token, request_semaphore=request_semaphore, max_retries=0,
            purpose="VALIDATION_QUERY_EMBEDDING")

    async def create_embedding(self, query):
        async with self._lock:
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


def metered_index_embedding(raw, ledger, claim, spec, *, request_semaphore=None):
    """Optional INDEX_EMBEDDING client that fills the frozen RAG gaps at intake; None means no fill.

    Like QUERY_EMBEDDING, a claim without any INDEX_EMBEDDING model or cap means the API did not configure the
    optional role (the run judges with whatever index exists). A role that is present but differs from the
    index, or has no positive cap, is a configuration error.
    """
    if raw is None:
        return None
    model = claim.wire["models"].get("INDEX_EMBEDDING")
    cap = claim.wire["budgets"]["roles"].get("INDEX_EMBEDDING")
    if model is None and cap is None:
        return None
    if not isinstance(model, dict) or not isinstance(cap, dict):
        raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
    if (model.get("version") != spec.version or model.get("dimensions") != spec.dimensions
            or model.get("model") != spec.model or getattr(raw, "model", None) != spec.model
            or raw.version != spec.version or raw.dimensions != spec.dimensions
            or not cap.get("maxCalls") or not cap.get("maxReservedTokens")):
        raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
    return ValidationIndexEmbedding(raw, ledger, claim, spec, request_semaphore=request_semaphore)


def index_input(claim, spec, document, chunks):
    """Hash-only identity fixed in the ledger before one index embedding call (no manuscript text)."""
    return {"contractVersion": 1, "inputDigest": claim.scope.inputDigest, "manifestHash": claim.wire["manifestHash"],
            "sourceRef": document.ref, "sourceHash": document.content_hash, "chunkCount": len(chunks),
            "chunkDigest": sha256("\n".join(chunk.chunk_hash for chunk in chunks).encode()).hexdigest(),
            "indexModel": spec.model, "indexVersion": spec.version, "indexDimensions": spec.dimensions}


class ValidationIndexEmbedding:
    """Embeds retrieval chunks of one frozen source per call through the run's INDEX_EMBEDDING role.

    One provider retry for a transient failure; each attempt is its own reservation under the per-run caps. The
    ledger's `index_input` is shared per-job state, so calls are serialized by one lock.
    """

    def __init__(self, raw, ledger, claim, spec, *, request_semaphore=None):
        self.model, self.version, self.dimensions = spec.model, spec.version, spec.dimensions
        self.ledger, self.claim, self.spec = ledger, claim, spec
        self._lock = asyncio.Lock()
        self.metered = MeteredEmbeddingClient(raw, ledger, claim.job_id, spec.model, claim.lease_token,
            request_semaphore=request_semaphore, max_retries=1, purpose="VALIDATION_INDEX_EMBEDDING")

    async def embed(self, document, chunks):
        async with self._lock:
            self.ledger.index_input = index_input(self.claim, self.spec, document, chunks)
            try:
                response = await self.metered.create_embeddings([chunk.chunk_text for chunk in chunks])
            finally:
                self.ledger.index_input = None
        vectors = response.embeddings
        if (response.model != self.model or len(vectors) != len(chunks)
                or any(len(vector) != self.dimensions
                       or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
                       or not any(value != 0 for value in vector) for vector in vectors)):
            raise IncompleteInspection("EMBEDDING_CONFIGURATION_INVALID")
        return vectors
