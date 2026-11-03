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


class ShutdownTransport(httpx.AsyncBaseTransport):
    """Only the backend/provider HTTP boundaries are synthetic; runtime and metering are real."""
    def __init__(self, phase):
        import json
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        self.phase = phase
        self.fixture = json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json").read_text())["execution"]
        self.wire = self.fixture["responses"]["claim"]
        self.wire["leaseExpiresAt"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        self.entered, self.release, self.typo_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.provider_started, self.finalizing = asyncio.Event(), asyncio.Event()
        self.closed, self.close_count, self.stopping = False, 0, False
        self.events, self.reservations, self.states, self.finalizations = [], {}, {}, {}
        self.provider_calls, self.publications_after_stop, self.reservations_after_stop = [], [], []
        self.provider_after_stop = []
        self.inline_close = None

    async def handle_async_request(self, request):
        import json
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path.endswith("/claim"):
            if self.phase == "claim":
                self.entered.set()
                await self.release.wait()
            return httpx.Response(200, json={"data": self.wire})
        if path.endswith("/checkpoint") and request.method == "GET":
            if self.inline_close is not None:
                await self.inline_close()
                raise AssertionError("inline close must cancel its own operation")
            if self.phase == "bootstrap":
                self.entered.set()
                await self.release.wait()
            return httpx.Response(200, json={"data": self.wire["checkpoint"]})
        if path.endswith("/data"):
            operation = body["operation"]
            name = {"INPUT": "dataInput", "SUBJECTS": "dataSubjects", "SETTINGS": "dataSettings",
                    "READ_SOURCE": "dataReadTarget", "VALIDATE": "dataInput"}[operation]
            return httpx.Response(200, json={"data": self.fixture["responses"][name]})
        if path.endswith("usage/reserve"):
            role, ref = body["role"], body["requestId"]
            if self.stopping:
                self.reservations_after_stop.append(role)
            self.reservations[ref], self.states[ref] = role, "RESERVED"
            self.events.append(("reserve", role))
            if role == "PLANNER" and self.phase == "reserve":
                self.entered.set()
                await self.release.wait()
            assert not self.closed, "reservation response lost to premature transport closure"
            return httpx.Response(200, json={"data": {}})
        if "/usage/" in path:
            ref, kind = path.rsplit("/", 1)[-1], body["kind"]
            role = self.reservations[ref]
            self.events.append((kind + "_started", role))
            if role == "PLANNER":
                self.finalizing.set()
                if self.phase in {"provider", "unknown", "settle"}:
                    self.entered.set()
                    await self.release.wait()
            assert not self.closed, "usage finalization attempted after transport closure"
            self.states[ref] = "SETTLED" if kind == "SETTLE" else "UNKNOWN"
            self.finalizations[ref] = {key: body[key] for key in ("kind", "inputTokens", "cachedInputTokens", "outputTokens", "outcome")}
            self.events.append((kind + "_finished", role))
            return httpx.Response(200, json={"data": {}})
        if path.endswith(("/plans", "/units", "/complete", "/revisions")):
            if self.stopping:
                self.publications_after_stop.append(path)
            raise AssertionError("paused synthetic calls must not publish")
        raise AssertionError("unexpected synthetic backend route")

    async def provider(self, request):
        import json
        body = json.loads(request.content)
        payload = json.loads(body["input"][1]["content"][0]["text"])
        role = payload.get("role", "TYPO")
        if self.stopping:
            self.provider_after_stop.append(role)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 23})
        self.provider_calls.append(role)
        if role == "TYPO":
            self.typo_started.set()
            await asyncio.Event().wait()
        assert role == "PLANNER"
        if self.phase == "provider":
            self.provider_started.set()
            await asyncio.Event().wait()
        if self.phase == "unknown":
            return httpx.Response(503)
        assert self.phase == "settle"
        result = {"questions": [], "segments": [{"segment_ref": "TSEG1", "status": "NO_CHECK", "reason": "합성"}]}
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(result), "usage": {"input_tokens": 23, "output_tokens": 7}})

    async def aclose(self):
        self.close_count += 1
        self.closed = True
        self.events.append(("closed", None))


async def shutdown_runtime(phase, **worker_options):
    from app.validation.client import ValidationClaim, ValidationExecutionClient
    from app.worker.episode_validation_worker import EpisodeValidationWorker

    backend = ShutdownTransport(phase)
    http = httpx.AsyncClient(transport=backend)
    provider = httpx.AsyncClient(transport=httpx.MockTransport(backend.provider))
    client = ValidationExecutionClient("https://spring.invalid", "synthetic", http)
    client.owns_http = True  # This fixture owns the injected HTTP pool, as the default runtime does.
    raw = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses", http_client=provider)
    worker = EpisodeValidationWorker(client, raw, enabled=True, **worker_options)
    claim = ValidationClaim.from_wire(backend.wire)
    return backend, provider, worker, claim


@pytest.mark.parametrize("phase", ["provider", "reserve", "unknown", "settle"])
def test_active_close_drains_real_meter_and_owned_usage_before_http_close(phase, caplog):
    async def run():
        backend, provider, worker, claim = await shutdown_runtime(phase)
        execution = asyncio.create_task(worker.process_claimed(claim))
        closers = []
        try:
            await asyncio.wait_for(backend.typo_started.wait(), 2)
            await asyncio.wait_for((backend.provider_started if phase == "provider" else backend.entered).wait(), 2)
            backend.stopping = True
            closers = [asyncio.create_task(worker.aclose()), asyncio.create_task(worker.aclose())]
            if phase in {"provider", "unknown"}:
                await asyncio.wait_for(backend.finalizing.wait(), 2)
            # Synthetic deadline only: close cannot finish while the owned backend response is withheld.
            done, _ = await asyncio.wait(closers, timeout=0.02)
            assert not done, backend.events
            assert not backend.closed
            backend.release.set()
            await asyncio.wait_for(asyncio.gather(*closers), 2)
            result = (await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), 2))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert backend.close_count == 1
            assert not backend.provider_after_stop and not backend.publications_after_stop
            assert not backend.reservations_after_stop
            assert not any(state == "RESERVED" for state in backend.states.values())
            planner = [ref for ref, role in backend.reservations.items() if role == "PLANNER"]
            assert len(planner) == 1
            assert backend.states[planner[0]] == ("SETTLED" if phase == "settle" else "UNKNOWN")
            if phase == "settle":
                assert backend.finalizations[planner[0]] == {"kind": "SETTLE", "inputTokens": 23,
                    "cachedInputTokens": 0, "outputTokens": 7, "outcome": "SUCCESS"}
            else:
                assert backend.finalizations[planner[0]] == {"kind": "UNKNOWN", "inputTokens": None,
                    "cachedInputTokens": None, "outputTokens": None, "outcome": None}
            if phase == "reserve":
                assert "PLANNER" not in backend.provider_calls
            assert backend.events[-1] == ("closed", None)
            await worker.aclose()
            assert backend.close_count == 1
        finally:
            backend.release.set()
            execution.cancel()
            await asyncio.gather(execution, *closers, return_exceptions=True)
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())
    assert "Failed to finalize AI token" not in caplog.text
    assert "Detached AI token ledger call failed" not in caplog.text


@pytest.mark.parametrize("phase", ["claim", "bootstrap"])
def test_close_drains_claim_or_prebranch_bootstrap_and_rejects_new_work(phase):
    from app.validation.contracts import IncompleteInspection
    async def run():
        backend, provider, worker, claim = await shutdown_runtime(phase)
        operation = asyncio.create_task(worker.claim_next() if phase == "claim" else worker.process_claimed(claim))
        try:
            await asyncio.wait_for(backend.entered.wait(), 2)
            await asyncio.wait_for(worker.aclose(), 2)
            assert operation.done()
            assert isinstance((await asyncio.gather(operation, return_exceptions=True))[0], asyncio.CancelledError)
            assert not backend.reservations and not backend.provider_calls
            with pytest.raises(IncompleteInspection, match="EXECUTION_STOPPED"):
                await worker.claim_next()
            with pytest.raises(IncompleteInspection, match="EXECUTION_STOPPED"):
                await worker.process_claimed(claim)
            assert backend.close_count == 1
        finally:
            backend.release.set()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())


def test_canceled_close_caller_does_not_interrupt_shared_usage_drain():
    async def run():
        backend, provider, worker, claim = await shutdown_runtime("provider")
        operation = asyncio.create_task(worker.process_claimed(claim))
        closers = []
        try:
            await asyncio.wait_for(backend.typo_started.wait(), 2)
            await asyncio.wait_for(backend.provider_started.wait(), 2)
            backend.stopping = True
            closers = [asyncio.create_task(worker.aclose()), asyncio.create_task(worker.aclose())]
            await asyncio.wait_for(backend.finalizing.wait(), 2)
            closers[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await closers[0]
            assert not backend.closed
            backend.release.set()
            await asyncio.wait_for(closers[1], 2)
            result = (await asyncio.gather(operation, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert backend.close_count == 1
            assert not any(state == "RESERVED" for state in backend.states.values())
            assert backend.events[-1] == ("closed", None)
        finally:
            backend.release.set()
            operation.cancel()
            await asyncio.gather(operation, *closers, return_exceptions=True)
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())


def test_inline_close_does_not_await_its_own_bootstrap_operation():
    async def run():
        backend, provider, worker, claim = await shutdown_runtime("bootstrap")
        backend.inline_close = worker.aclose
        try:
            result = (await asyncio.wait_for(asyncio.gather(worker.process_claimed(claim), return_exceptions=True), 2))[0]
            assert isinstance(result, asyncio.CancelledError)
            await asyncio.wait_for(worker.aclose(), 2)
            assert backend.close_count == 1
            assert not backend.reservations and not backend.provider_calls
        finally:
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())


def test_close_before_owned_claim_dispatch_does_not_start_backend_io():
    async def run():
        backend, provider, worker, _ = await shutdown_runtime("claim")
        operation = asyncio.create_task(worker.claim_next())
        try:
            await asyncio.sleep(0)  # Public call has registered its owned task; its HTTP body has not run.
            async with asyncio.timeout(2):
                await worker.aclose()
            result = (await asyncio.gather(operation, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert not backend.entered.is_set()
            assert backend.close_count == 1
        finally:
            backend.release.set()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())


async def _until(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("capped", [True, False])
def test_llm_request_semaphore_reaches_every_role_and_query_embedding_client(monkeypatch, capped):
    """GH343: the runner's one process-wide cap is the same object on every metered client."""
    import app.worker.episode_validation_worker as worker_module
    from app.embeddings.client import OpenAIEmbeddingsClient
    from app.validation.client import ValidationClaim, ValidationExecutionClient
    from app.validation.contracts import IncompleteInspection
    from app.validation.models import GENERATION_ROLES

    built = {}
    real_roles, real_embedding = worker_module.ValidationRoleClients, worker_module.metered_embedding

    def roles_spy(*args, **kwargs):
        built["roles"] = real_roles(*args, **kwargs)
        return built["roles"]

    def embedding_spy(*args, **kwargs):
        built["embedding"] = real_embedding(*args, **kwargs)
        return built["embedding"]

    monkeypatch.setattr(worker_module, "ValidationRoleClients", roles_spy)
    monkeypatch.setattr(worker_module, "metered_embedding", embedding_spy)

    async def never_called(request):
        raise AssertionError("no backend or provider request is expected")

    async def run():
        backend = ShutdownTransport("settle")
        wire = backend.wire
        wire["models"]["QUERY_EMBEDDING"] = {"model": "text-embedding-3-small", "version": "v1", "dimensions": 1536}
        wire["budgets"]["roles"]["QUERY_EMBEDDING"] = {"maxCalls": 2, "maxReservedTokens": 1000}
        semaphore = asyncio.Semaphore(2) if capped else None
        async with httpx.AsyncClient(transport=httpx.MockTransport(never_called)) as http:
            raw = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses",
                                        http_client=http)
            embedding = OpenAIEmbeddingsClient("synthetic", "text-embedding-3-small", 1536, "v1",
                                               "https://provider.invalid/embeddings", http)
            client = ValidationExecutionClient("https://spring.invalid", "synthetic", http)

            async def rejected_checkpoint(claim, gate=None):
                raise IncompleteInspection("EXECUTION_HTTP_REJECTED")

            client.load_checkpoint = rejected_checkpoint
            worker = worker_module.EpisodeValidationWorker(client, raw, enabled=True, embedding_client=embedding,
                                                           llm_request_semaphore=semaphore)
            try:
                with pytest.raises(IncompleteInspection, match="EXECUTION_HTTP_REJECTED"):
                    await worker.process_claimed(ValidationClaim.from_wire(wire))
            finally:
                await worker.aclose()
        return semaphore

    semaphore = asyncio.run(run())
    clients = [built["roles"].for_validation_role(role) for role in GENERATION_ROLES]
    assert len(clients) == 3 and set(built["roles"].clients) == set(GENERATION_ROLES)
    # The shared fixture freezes the production pairing: PLANNER/TYPO Terra, JUDGE Luna (configuration v2).
    assert [client.default_model for client in clients] == [
        {"PLANNER": "gpt-6.1-terra", "JUDGE": "gpt-6-luna", "TYPO": "gpt-6.1-terra"}[role] for role in GENERATION_ROLES]
    assert all(client.request_semaphore is semaphore for client in clients)
    assert built["embedding"].metered.request_semaphore is semaphore
    assert (semaphore is not None) == capped


QUERY_EMBEDDING_MODEL = {"model": "text-embedding-3-small", "version": "v1", "dimensions": 1536}
QUERY_EMBEDDING_CAP = {"maxCalls": 2, "maxReservedTokens": 1000}
ABSENT = object()


def _embedding_claim(model=ABSENT, cap=ABSENT):
    import json
    from pathlib import Path

    from app.validation.client import ValidationClaim
    wire = json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json")
                      .read_text())["execution"]["responses"]["claim"]
    assert "QUERY_EMBEDDING" not in wire["models"]  # The shared fixture has no optional role.
    if model is not ABSENT:
        wire["models"]["QUERY_EMBEDDING"] = model
    if cap is not ABSENT:
        wire["budgets"]["roles"]["QUERY_EMBEDDING"] = cap
    return ValidationClaim.from_wire(wire)


def _metered_embedding(claim, raw=ABSENT):
    from types import SimpleNamespace

    from app.retrieval.contracts import IndexSpec
    from app.validation.execution import metered_embedding

    if raw is ABSENT:  # The raw embeddings client is never called here; only its index identity matters.
        raw = SimpleNamespace(model="text-embedding-3-small", version="v1", dimensions=1536)
    return metered_embedding(raw, FakeLedger(), claim, IndexSpec(version="v1"))


@pytest.mark.parametrize(("model", "cap"), [(ABSENT, ABSENT), (None, None)], ids=["absent", "null"])
def test_claim_without_query_embedding_role_runs_without_vector(model, cap):
    """GH344: an API without the optional QUERY_EMBEDDING role means keyword-only, not a failed job."""
    assert _metered_embedding(_embedding_claim(model, cap)) is None
    assert _metered_embedding(_embedding_claim(QUERY_EMBEDDING_MODEL, QUERY_EMBEDDING_CAP), raw=None) is None


def test_configured_query_embedding_role_builds_the_metered_client():
    from app.validation.execution import ValidationQueryEmbedding

    client = _metered_embedding(_embedding_claim(QUERY_EMBEDDING_MODEL, QUERY_EMBEDDING_CAP))
    assert isinstance(client, ValidationQueryEmbedding)
    assert (client.model, client.version, client.dimensions) == ("text-embedding-3-small", "v1", 1536)


@pytest.mark.parametrize(("model", "cap"), [
    ({**QUERY_EMBEDDING_MODEL, "model": "text-embedding-3-large"}, QUERY_EMBEDDING_CAP),
    ({**QUERY_EMBEDDING_MODEL, "version": "v2"}, QUERY_EMBEDDING_CAP),
    ({**QUERY_EMBEDDING_MODEL, "dimensions": 3072}, QUERY_EMBEDDING_CAP),
    (QUERY_EMBEDDING_MODEL, {**QUERY_EMBEDDING_CAP, "maxCalls": 0}),
    (QUERY_EMBEDDING_MODEL, {**QUERY_EMBEDDING_CAP, "maxReservedTokens": 0}),
    (QUERY_EMBEDDING_MODEL, ABSENT),
    (ABSENT, QUERY_EMBEDDING_CAP),
    ({}, QUERY_EMBEDDING_CAP),
    ("text-embedding-3-small", QUERY_EMBEDDING_CAP),
], ids=["model", "version", "dimensions", "zero-calls", "zero-tokens", "model-without-cap", "cap-without-model",
        "empty-model", "not-an-object"])
def test_present_but_mismatched_query_embedding_role_still_fails(model, cap):
    from app.validation.contracts import IncompleteInspection

    with pytest.raises(IncompleteInspection, match="EMBEDDING_CONFIGURATION_INVALID"):
        _metered_embedding(_embedding_claim(model, cap))


def test_worker_with_query_embedding_passes_none_when_the_claim_has_no_role(monkeypatch):
    """The job proceeds past embedding setup and the inspection is built without a query embedding client."""
    import app.worker.episode_validation_worker as worker_module
    from app.embeddings.client import OpenAIEmbeddingsClient
    from app.validation.client import ValidationExecutionClient
    from app.validation.contracts import IncompleteInspection

    built = []
    real = worker_module.metered_embedding
    monkeypatch.setattr(worker_module, "metered_embedding",
                        lambda *args, **kwargs: built.append(real(*args, **kwargs)) or built[-1])

    async def never_called(request):
        raise AssertionError("no backend or provider request is expected")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(never_called)) as http:
            raw = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses",
                                        http_client=http)
            embedding = OpenAIEmbeddingsClient("synthetic", "text-embedding-3-small", 1536, "v1",
                                               "https://provider.invalid/embeddings", http)
            client = ValidationExecutionClient("https://spring.invalid", "synthetic", http)

            async def rejected_checkpoint(claim, gate=None):
                raise IncompleteInspection("EXECUTION_HTTP_REJECTED")

            client.load_checkpoint = rejected_checkpoint
            worker = worker_module.EpisodeValidationWorker(client, raw, enabled=True, embedding_client=embedding)
            try:
                # Before GH344 this claim failed here with EMBEDDING_CONFIGURATION_INVALID.
                with pytest.raises(IncompleteInspection, match="EXECUTION_HTTP_REJECTED"):
                    await worker.process_claimed(_embedding_claim())
            finally:
                await worker.aclose()

    asyncio.run(run())
    assert built == [None]


def _second_claim(backend):
    from uuid import uuid4

    from app.validation.client import ValidationClaim
    return ValidationClaim.from_wire({**backend.wire, "jobId": str(uuid4()), "leaseToken": str(uuid4())})


def test_concurrent_jobs_in_one_worker_keep_separate_gates_and_ledgers():
    """GH343: one job's gate stop ends only that job; the other keeps its lease, gate and usage."""
    from app.validation.contracts import IncompleteInspection

    async def run():
        backend, provider, worker, first = await shutdown_runtime("provider")
        backend.release.set()  # Finalizations are not withheld in this scenario.
        second = _second_claim(backend)
        jobs = {}
        try:
            jobs = {claim.job_id: asyncio.create_task(worker.process_claimed(claim)) for claim in (first, second)}
            await _until(lambda: sorted(backend.provider_calls) == ["PLANNER", "PLANNER", "TYPO", "TYPO"])
            ledgers = {ledger.claim.job_id: ledger for ledger in worker._ledgers}
            assert set(ledgers) == {first.job_id, second.job_id}
            assert ledgers[first.job_id].gate is not ledgers[second.job_id].gate
            assert len(ledgers[first.job_id].requests) == len(ledgers[second.job_id].requests) == 2

            ledgers[first.job_id].gate.stop()
            result = (await asyncio.wait_for(asyncio.gather(jobs[first.job_id], return_exceptions=True), 2))[0]
            assert isinstance(result, IncompleteInspection) and result.code == "EXECUTION_STOPPED"
            assert ledgers[first.job_id].requests == set()
            await asyncio.sleep(0.02)
            assert not jobs[second.job_id].done()
            assert not ledgers[second.job_id].gate.stopped.is_set()
            assert len(ledgers[second.job_id].requests) == 2
            assert {ledger.claim.job_id for ledger in worker._ledgers} == {second.job_id}
            assert not backend.closed

            await asyncio.wait_for(worker.aclose(), 2)
            result = (await asyncio.gather(jobs[second.job_id], return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert not any(state == "RESERVED" for state in backend.states.values())
        finally:
            backend.release.set()
            for job in jobs.values():
                job.cancel()
            await asyncio.gather(*jobs.values(), return_exceptions=True)
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())


def test_llm_request_semaphore_caps_provider_calls_across_concurrent_jobs():
    """GH343: with a cap of one, two jobs (four role branches) never run two provider calls at once."""
    async def run():
        backend, provider, worker, first = await shutdown_runtime(
            "provider", llm_request_semaphore=asyncio.Semaphore(1))
        backend.release.set()
        jobs = []
        try:
            jobs = [asyncio.create_task(worker.process_claimed(claim)) for claim in (first, _second_claim(backend))]
            await _until(lambda: len(backend.provider_calls) == 1)
            await asyncio.sleep(0.05)
            # The holder never returns; every other branch waits before reserving or calling.
            assert len(backend.provider_calls) == 1 and len(backend.reservations) == 1
            assert not any(job.done() for job in jobs)
            await asyncio.wait_for(worker.aclose(), 2)
            assert not any(state == "RESERVED" for state in backend.states.values())
        finally:
            backend.release.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await worker.aclose()
            await provider.aclose()
    asyncio.run(run())


class GridBackend(httpx.AsyncBaseTransport):
    """Grid v4 synthetic Backend: the SETTING grid appears only after the first plan, as in Java."""

    def __init__(self, grid=None):
        import json
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        fixture = json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json").read_text())
        self.responses = fixture["execution"]["responses"]
        self.wire = self.responses["claim"]
        self.wire["leaseExpiresAt"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        self.target = self.responses["dataReadTarget"]["text"]
        # "😀가😀가": two current rows of S1 and a history row the judge must never receive.
        self.settings = [{"entry_ref": "B1", "subject_ref": "S1", "domain": "characters", "name": "가", "aliases": [],
                          "state": {"name": "가"}, "author_correction": None},
                         {"entry_ref": "B3", "subject_ref": "S1", "domain": "characters", "name": "가", "aliases": [],
                          "state": {"name": "가", "aliases": ["가가"]}, "author_correction": None}]
        self.responses["dataInput"]["entryCount"] = 3
        self.responses["dataSubjects"].update(
            subjects=[{"subject_ref": "S1", "domain": "characters", "name": "가", "aliases": ["가가"]}],
            subjectCount=1, entryCount=3)
        self.grid = grid or {"gridVersion": 4, "segments": [{"ref": "TSEG1", "start": 0, "end": len(self.target),
                                                             "contextStart": 0, "contextEnd": len(self.target)}],
                             "units": [{"stage": "PLANNER", "ref": "TSEG1"}, {"stage": "MERGED_PLAN", "ref": "PLAN"}]}
        self.published, self.units, self.data_operations, self.checkpoint_reads = [], [], [], 0
        self.reserved, self.provider_roles, self.completed, self.judge_payloads = [], [], [], []

    def view(self):
        view = dict(self.wire["checkpoint"])
        if self.published:
            view["requiredGrid"] = {"SETTING": self.grid}
        return view

    async def handle_async_request(self, request):
        import json
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        data = None
        if path.endswith("/claim"):
            data = self.wire
        elif path.endswith("/checkpoint") and request.method == "GET":
            self.checkpoint_reads += 1
            data = self.view()
        elif path.endswith("/data"):
            operation = body["operation"]
            self.data_operations.append((operation, body.get("subjectRefs")))
            data = self.responses[{"INPUT": "dataInput", "SUBJECTS": "dataSubjects", "SETTINGS": "dataSettings",
                                   "READ_SOURCE": "dataReadTarget", "VALIDATE": "dataInput"}[operation]]
            if operation == "SETTINGS":
                data = {**data, "settings": self.settings}
        elif path.endswith("usage/reserve"):
            self.reserved.append(body["role"])
            data = {}
        elif "/usage/" in path or path.endswith("/heartbeat"):
            data = {}
        elif path.endswith("/plans"):
            self.published.append((body["stage"], body["ref"], body["payload"]))
            checks = []
            if body["stage"] == "MERGED_PLAN":
                checks = [{"checkId": f"00000000-0000-4000-8000-00000000002{index}", "localRef": row["localRef"],
                           "originalRefs": row["originalRefs"], "revision": 1, "checkInputDigest": "d" * 64,
                           "state": row["state"], "outcome": None, "decision": None, "changeReason": None}
                          for index, row in enumerate(body["payload"]["questions"])]
            data = {"stageId": "00000000-0000-4000-8000-000000000010", "branch": "SETTING", "stage": body["stage"],
                    "ref": body["ref"], "successful": True, "checks": checks}
        elif path.endswith("/units"):
            self.units.append(body)
            data = self.responses["typoUnit"]
        elif path.endswith("/complete"):
            self.completed.append(body["failureCode"])
            data = {"status": "COMPLETED" if body["failureCode"] is None else "PARTIAL"}
        if data is None:
            raise AssertionError("unexpected synthetic backend route")
        return httpx.Response(200, json={"data": data})

    async def provider(self, request):
        import json
        body = json.loads(request.content)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 23})
        payload = json.loads(body["input"][1]["content"][0]["text"])
        role = payload.get("role", "TYPO")
        self.provider_roles.append(role)
        if role == "TYPO":
            result = {"segments": [{"segment_ref": payload["segment_ref"], "status": "NO_CHECK", "reason": "합성"}],
                      "suggestions": []}
        elif role == "PLANNER":
            assert "settings" not in payload and payload["subject_catalogue"][0]["subject_ref"] == "S1"
            result = {"questions": [{"local_ref": "q", "target_spans": [
                {"source_ref": "T1", "start": 0, "end": 2, "quote": self.target[:2]}], "subject_refs": ["S1"],
                "subject_mentions": ["가"], "property_or_rule": "합성 속성", "time_context": "현재", "conditions": [],
                "assertion_mode": "NARRATION", "check_intent": "합성", "reason": "합성"}],
                "segments": [{"segment_ref": "TSEG1", "status": "PLANNED", "reason": "합성"}]}
        else:
            self.judge_payloads.append(payload)
            refs = ["T1", *(row["entry_ref"] for row in payload["settings"])]
            assessment = {"reason": "합성 확인", "evidence_refs": refs,
                          "receipt_refs": [row["ref"] for row in payload["receipts"]]}
            result = {"action": "DECIDED", "outcome": "NO_CONFLICT", "basis": "COMPATIBLE", "reason": "합성",
                      "impact": None, "impact_reason": None, "target_spans": payload["question"]["target_spans"],
                      "evidence_refs": refs, "completion": {key: assessment for key in (
                          "comparison_context", "changes_exceptions", "no_pending_clues", "required_reads")},
                      "disagreements": [], "search_queries": [], "context_requests": []}
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(result, ensure_ascii=False),
                                         "usage": {"input_tokens": 23, "output_tokens": 7}})


class BrokenRoleBackend(GridBackend):
    """GridBackend whose provider answers the given roles with invalid JSON (a question/segment-level failure)."""

    def __init__(self, grid=None, broken=()):
        super().__init__(grid)
        self.broken = set(broken)

    async def provider(self, request):
        import json
        body = json.loads(request.content)
        if not request.url.path.endswith("input_tokens"):
            payload = json.loads(body["input"][1]["content"][0]["text"])
            role = payload.get("role", "TYPO")
            if role in self.broken:
                self.provider_roles.append(role)
                return httpx.Response(200, json={"status": "completed", "output_text": "{not json",
                                                 "usage": {"input_tokens": 23, "output_tokens": 7}})
        return await super().provider(request)


def run_grid_backend(grid=None, broken=()):
    from app.validation.client import ValidationClaim, ValidationExecutionClient
    from app.worker.episode_validation_worker import EpisodeValidationWorker

    async def run():
        backend = BrokenRoleBackend(grid, broken) if broken else GridBackend(grid)
        async with (httpx.AsyncClient(transport=backend) as http,
                    httpx.AsyncClient(transport=httpx.MockTransport(backend.provider)) as provider):
            client = ValidationExecutionClient("https://spring.invalid", "synthetic", http)
            raw = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses",
                                        http_client=provider)
            worker = EpisodeValidationWorker(client, raw, enabled=True)
            try:
                await worker.process_claimed(ValidationClaim.from_wire(backend.wire))
            finally:
                await worker.aclose()
        return backend
    return asyncio.run(run())


def test_worker_plans_checks_the_v4_grid_merges_and_publishes_the_judged_unit():
    backend = run_grid_backend()
    assert backend.completed == [None]
    assert [(stage, ref) for stage, ref, _ in backend.published] == [("PLANNER", "TSEG1"), ("MERGED_PLAN", "PLAN")]
    assert set(backend.published[0][2]) == {"questions", "segments"}
    assert backend.published[1][2]["questions"][0]["state"] == {
        "draft": backend.published[0][2]["questions"][0], "lookup_obligations": []}
    operations = [operation for operation, _ in backend.data_operations]
    assert operations[:3] == ["INPUT", "READ_SOURCE", "SUBJECTS"] or "SUBJECTS" in operations
    assert ("SETTINGS", ["S1"]) in backend.data_operations and "ENTRIES" not in operations
    (judge,) = backend.judge_payloads
    assert judge["form"] == "OPEN" and [row["entry_ref"] for row in judge["settings"]] == ["B1", "B3"]
    (unit,) = [row for row in backend.units if row["branch"] == "SETTING"]
    assert unit["payload"]["evidence_bundle"]["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"},
                                                             {"entry_ref": "B3", "subject_ref": "S1"}]
    # Reloaded once when the first publication created the grid; three generation roles are metered.
    assert backend.checkpoint_reads == 2
    assert sorted(backend.reserved) == ["JUDGE", "PLANNER", "TYPO"]


def test_worker_publishes_an_incomplete_question_and_completes_with_the_questions_code():
    """Decision ③: a judge format error ends that question only; Complete never reports a clean success."""
    backend = run_grid_backend(broken=("JUDGE",))
    assert backend.completed == ["SETTING_QUESTIONS_INCOMPLETE"]
    (unit,) = [row for row in backend.units if row["branch"] == "SETTING"]
    assert unit["outcome"] == "INCOMPLETE" and unit["payload"]["failure_code"] == "MODEL_CONTRACT_INVALID"
    assert unit["payload"]["target_spans"] == backend.published[0][2]["questions"][0]["target_spans"]
    assert unit["citations"] and all(row["sourceRef"] == "T1" for row in unit["citations"])


def test_a_typo_branch_failure_outranks_the_incomplete_questions_code():
    backend = run_grid_backend(broken=("JUDGE", "TYPO"))
    assert len(backend.completed) == 1 and backend.completed[0] not in {None, "SETTING_QUESTIONS_INCOMPLETE"}


@pytest.mark.parametrize(("grid", "code"), [
    ({"gridVersion": 3, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": []}]}, "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": 4, "segments": [{"ref": "TSEG1", "start": 0, "end": 2, "contextStart": 0, "contextEnd": 4},
                                     {"ref": "TSEG2", "start": 2, "end": 4, "contextStart": 0, "contextEnd": 4}],
      "units": []}, "GRID_SEGMENTS_MISMATCH"),
])
def test_worker_on_a_different_server_grid_stops_before_merging(grid, code):
    backend = run_grid_backend(grid)
    assert backend.completed == [code]
    assert [(stage, ref) for stage, ref, _ in backend.published] == [("PLANNER", "TSEG1")]
    assert "JUDGE" not in backend.provider_roles


@pytest.mark.parametrize("mode", ["zero", "normal", "vector-judge", "followup-crash"])
def test_java_http_synthetic_provider_answers_grid_v4_roles(mode):
    import json

    from tests.support.validation_execution_http_client import SyntheticProvider

    provider = SyntheticProvider(mode)

    def call(payload, effort):
        model = "gpt-6-luna" if payload["role"] == "JUDGE" else "gpt-6.1-terra"
        body = {"model": model, "reasoning": {"effort": effort},
                "input": [{}, {"content": [{"text": json.dumps(payload, ensure_ascii=False)}]}]}
        request = httpx.Request("POST", "https://provider.invalid/responses", json=body)
        return json.loads(asyncio.run(provider(request)).json()["output_text"])

    planner = call({"role": "PLANNER", "segment": {"ref": "TSEG1", "start": 0}, "text": "가나다", "text_start": 0,
                    "subject_catalogue": [{"subject_ref": "S1"}]}, "medium")
    assert set(planner) == {"questions", "segments"}
    assert bool(planner["questions"]) == (mode != "zero")
    question = {"target_spans": [{"source_ref": "T1", "start": 0, "end": 2, "quote": "가나"}]}
    judge = call({"role": "JUDGE", "form": "OPEN", "question_ref": "Q3", "question": question,
                  "settings": [], "evidence": [], "receipts": [{"ref": "L1"}]}, "high")
    expected = "FOLLOW_UP" if mode in {"vector-judge", "followup-crash"} else "DECIDED"
    assert judge["action"] == expected and "follow_ups" not in judge
