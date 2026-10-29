"""Dedicated independent validation branches; all durable state belongs to Java."""
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.llm.openai_client import OpenAIResponsesClient
from app.retrieval.contracts import IndexSpec
from app.validation.checkpoint import ValidationCheckpoint, payload_key
from app.validation.client import ExecutionGate, ValidationClaim
from app.validation.contracts import IncompleteInspection
from app.validation.data import ValidationContext, ValidationDataClient
from app.validation.execution import (
    ValidationLedger,
    ValidationRoleClients,
    claim_budgets,
    metered_embedding,
)
from app.validation.search import FrozenRetrievalSearchService
from app.validation.service import SettingValidationService
from app.validation.typo import check_typos

_SHARED_FAILURES = {"INPUT_CHANGED", "SOURCE_CHANGED", "SOURCE_PURGED", "EXECUTION_STOPPED"}


class EpisodeValidationWorker:
    def __init__(self, client, raw_model, *, enabled=False, worker_id="validation-worker",
                 readonly_sessions=None, embedding_client=None, index_spec=None):
        if not enabled:
            raise ValueError("Validation runtime is disabled; explicit enable and server budgets are required.")
        if type(raw_model) is not OpenAIResponsesClient:
            raise ValueError("Dedicated validation requires the one-shot raw Responses transport.")
        self.client, self.raw_model, self.worker_id = client, raw_model, worker_id
        self.spec = index_spec or IndexSpec()
        self.search = FrozenRetrievalSearchService(readonly_sessions, self.spec) if readonly_sessions else None
        self.embedding = embedding_client
        self._active = set()
        self._operations, self._ledgers = set(), set()
        self._close_task = None

    async def _run_owned(self, action, *args):
        if self._close_task is not None:
            raise IncompleteInspection("EXECUTION_STOPPED")
        operation = asyncio.create_task(action(*args))
        self._operations.add(operation)
        try:
            return await operation
        finally:
            self._operations.discard(operation)

    async def claim_next(self):
        return await self._run_owned(self._claim_next)

    async def _claim_next(self):
        claim = await self.client.claim_next(self.worker_id)
        if claim is not None:
            claim_budgets(claim)
        return claim

    async def process_claimed(self, payload):
        return await self._run_owned(self._process_claimed, payload)

    async def _process_claimed(self, payload):
        claim = payload if isinstance(payload, ValidationClaim) else ValidationClaim.from_wire(payload)
        limits = claim_budgets(claim)
        gate = ExecutionGate()
        ledger = ValidationLedger(self.client, claim, gate)
        self._ledgers.add(ledger)
        roles = ValidationRoleClients(self.raw_model, ledger, claim)
        embedding = metered_embedding(self.embedding, ledger, claim, self.spec)
        view = await self.client.load_checkpoint(claim, gate)
        setting = ValidationCheckpoint(self.client, claim, view, gate)
        typo = ValidationCheckpoint(self.client, claim, view, gate, "TYPO")
        wire = claim.wire
        context = ValidationContext(wire["memberId"], UUID(wire["workId"]), claim.scope.reportId,
            wire["baselineHash"], wire["targetHash"], baseline_revision=wire["baselineRevision"],
            job_id=claim.job_id, lease_token=claim.lease_token, manifest_hash=wire["manifestHash"],
            report_revision=wire["reportRevision"], target_revision=wire["targetRevision"],
            generation=claim.scope.generation, input_digest=claim.scope.inputDigest)
        # Independent clients keep page accumulation separate; the HTTP pool and lease are shared.
        def data():
            return ValidationDataClient(self.client.base_url, self.client.key, context, self.client.http)
        children = {
            asyncio.create_task(SettingValidationService(data(), roles, limits["SETTING"],
                search=self.search, embedding_client=embedding, checkpoint=setting).inspect(),
                name="validation-setting"),
            asyncio.create_task(check_typos(data(), roles.for_validation_role("TYPO"),
                budget=limits["TYPO"], checkpoint=typo), name="validation-typo"),
        }
        self._active.update(children)
        heartbeat = asyncio.create_task(self._heartbeat(claim, gate), name="validation-heartbeat")
        stopped = asyncio.create_task(gate.stopped.wait(), name="validation-stop")
        failure = None
        try:
            remaining = set(children)
            while remaining:
                done, _ = await asyncio.wait(remaining | {stopped}, return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    raise IncompleteInspection("EXECUTION_STOPPED")
                for task in done:
                    remaining.remove(task)
                    result = task.result()
                    if result.failure_code in _SHARED_FAILURES:
                        gate.stop()
                        raise IncompleteInspection(result.failure_code)
                    failure = failure or result.failure_code
            gate.require()
            return await self.client.leased(claim, "complete", gate=gate,
                idempotencyKey=payload_key("complete", {"generation": claim.scope.generation,
                                                        "failureCode": failure}), failureCode=failure)
        finally:
            gate.stop()
            for task in children | {heartbeat, stopped}:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, heartbeat, stopped, return_exceptions=True)
            self._active.difference_update(children)
            # Metering registers detached reservation/finalization chains before children finish.
            await ledger.drain()
            self._ledgers.discard(ledger)

    async def _heartbeat(self, claim, gate):
        expires = datetime.fromisoformat(claim.wire["leaseExpiresAt"])
        interval = max(0.01, (expires - datetime.now(UTC)).total_seconds() / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                await self.client.leased(claim, "heartbeat", gate=gate)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - safe shared cancellation, no provider/body logging.
            gate.stop()

    async def aclose(self):
        if self._close_task is None:
            for ledger in self._ledgers:
                ledger.gate.stop()
            # Stop registered work before yielding to the shutdown coordinator.
            for task in self._operations | self._active:
                if not task.done() and not task.cancelling():
                    task.cancel()
            self._close_task = asyncio.create_task(self._close())
        if asyncio.current_task() in self._operations | self._active:
            # Inline shutdown cannot await its own execution. The coordinator still drains it.
            raise asyncio.CancelledError()
        await asyncio.shield(self._close_task)

    async def _close(self):
        operations = tuple(self._operations)
        for task in operations:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        # A repeated external cancellation may interrupt an execution's finally; retain its ledger.
        for task in tuple(self._active):
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*self._active, return_exceptions=True)
        results = await asyncio.gather(*(ledger.drain() for ledger in self._ledgers), return_exceptions=True)
        await self.client.aclose()
        for result in results:
            if isinstance(result, BaseException):
                raise result
