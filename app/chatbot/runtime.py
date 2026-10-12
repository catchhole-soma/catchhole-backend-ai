"""Bounded single-question HTTP runtime, independent of analysis claim loops."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, Any
from uuid import UUID, uuid4
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.chatbot.outbox import Outbox
from app.schemas.chat_execution import Dispatch, Finish, Usage
from app.clients.setting_tools_client import SettingToolContext, SettingToolsClient


class BusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    value: Any
    usage: Usage


class ProviderFailure(RuntimeError):
    """Provider adapter must pass measured usage even for truncation/errors."""

    def __init__(self, usage: Usage | None = None):
        self.usage = usage
        super().__init__("Provider call failed")


class Execution:
    def __init__(self, dispatch, runner, context, client, outbox):
        self.dispatch, self.runner, self.context = dispatch, runner, context
        self.client, self.outbox = client, outbox
        self.calls = 0

    async def call(
        self,
        purpose: str,
        model: str,
        maximum_tokens: int,
        invoke: Callable[[], Awaitable[ProviderResult]],
    ) -> Any:
        """One reservation per actual provider invocation; automatic provider retries are forbidden.

        Adapters in #238 must enforce maximum_tokens, disable SDK retries, and wrap
        each schema retry/embedding request with this method.
        """
        if self.calls >= self.context.max_calls:
            raise ChatTransportError(429)
        self.outbox.reserve_slot()
        try:
            call_id = uuid4()
            await self.client.reserve(
                self.dispatch, self.runner, call_id, purpose, model, maximum_tokens
            )
            self.calls += 1
            usage = Usage(kind="UNKNOWN")
            try:
                response = await invoke()
                measured = Usage.model_validate(response.usage.model_dump())
                if measured.kind == "RELEASE":
                    raise ProviderFailure()
                usage = measured
                return response.value
            except ProviderFailure as exc:
                if exc.usage is not None and exc.usage.kind == "SETTLE":
                    usage = exc.usage.model_copy(update={"outcome": "FAILURE"})
                raise
            finally:
                self.outbox.put(
                    str(call_id),
                    self.dispatch,
                    self.runner,
                    f"calls/{call_id}",
                    usage.model_dump(by_alias=True, mode="json"),
                    self.dispatch.deadline.timestamp() + 86400,
                    reserved=True,
                )
        finally:
            self.outbox.release_slot()

    def tools(self) -> SettingToolsClient:
        return SettingToolsClient(
            self.client.base_url,
            self.client.api_key,
            SettingToolContext(
                self.context.member_id,
                self.context.work_id,
                self.dispatch.attempt_id,
                self.dispatch.capability,
                self.runner,
            ),
            http_client=self.client.http,
        )


async def unconfigured_handler(execution: Execution) -> Finish:
    return Finish(failure="HANDLER_UNAVAILABLE")


class ChatRuntime:
    def __init__(
        self,
        client: ChatExecutionClient,
        outbox: Outbox,
        *,
        capacity=4,
        handler=unconfigured_handler,
        registry: CollectorRegistry | None = None,
    ):
        if capacity < 1:
            raise ValueError("Invalid chat capacity")
        self.client, self.outbox, self.capacity, self.handler = client, outbox, capacity, handler
        self.tasks: dict[UUID, asyncio.Task] = {}
        self.accepting = True
        self.report_ready = asyncio.Event()
        self.registry = registry or CollectorRegistry()
        self.active = Gauge("chat_active", "Active HTTP executions", registry=self.registry)
        self.pending = Gauge(
            "chat_pending_reports", "Reports awaiting delivery", registry=self.registry
        )
        self.completed = Counter(
            "chat_executions", "Execution outcomes", ["outcome"], registry=self.registry
        )
        self.latency = Histogram(
            "chat_execution_seconds", "Handler latency", registry=self.registry
        )

    def submit(self, dispatch: Dispatch):
        dispatch = Dispatch.model_validate(dispatch.model_dump())
        if dispatch.attempt_id in self.tasks:
            return
        if not self.accepting or len(self.tasks) >= self.capacity:
            raise BusyError()
        if dispatch.deadline.tzinfo is None or dispatch.deadline <= datetime.now(UTC):
            raise ChatTransportError(409)
        self.outbox.reserve_slot()
        task = asyncio.create_task(self._run(dispatch))
        self.tasks[dispatch.attempt_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(dispatch.attempt_id, None))

    async def _run(self, dispatch):
        runner = uuid4()
        started = time.monotonic()
        self.active.inc()
        finish = None
        try:
            async with asyncio.timeout(max(0, dispatch.deadline.timestamp() - time.time())):
                # On a lost start response do not invoke the handler. Spring owns recovery.
                context = await self.client.start(dispatch, runner)
                finish = Finish.model_validate(
                    (
                        await self.handler(
                            Execution(
                                dispatch,
                                runner,
                                context,
                                self.client,
                                self.outbox,
                            )
                        )
                    ).model_dump()
                )
        except TimeoutError:
            self.completed.labels("timeout").inc()
        except asyncio.CancelledError:
            finish = Finish(failure="INTERRUPTED")
        except ChatTransportError as exc:
            finish = Finish(
                failure="QUOTA_EXHAUSTED"
                if exc.status == 429 or exc.code == "QUOTA_EXHAUSTED"
                else "NETWORK_FAILURE"
            )
        except Exception:
            finish = Finish(failure="PROVIDER_FAILURE")
        finally:
            if finish is not None:
                try:
                    self.outbox.put(
                        f"finish:{dispatch.attempt_id}",
                        dispatch,
                        runner,
                        "finish",
                        finish.model_dump(by_alias=True, mode="json"),
                        dispatch.deadline.timestamp(),
                        reserved=True,
                    )
                    self.completed.labels(finish.failure or "success").inc()
                except Exception:
                    # No raw exception logging. Spring's deadline recovery remains authoritative.
                    self.completed.labels("report_storage_failure").inc()
            self.outbox.release_slot()
            self.report_ready.set()
            self.active.dec()
            self.latency.observe(time.monotonic() - started)

    async def deliver(self):
        await self.outbox.flush(self.client)
        self.pending.set(self.outbox.count())

    async def shutdown(self, grace: float):
        self.accepting = False
        tasks = list(self.tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self.deliver(), timeout=2)
        except TimeoutError:
            pass  # Reports remain durable for the next process.
