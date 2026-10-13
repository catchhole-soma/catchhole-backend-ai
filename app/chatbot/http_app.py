"""Run with uvicorn app.chatbot.http_app:app --no-access-log --workers 1."""

import asyncio
import contextlib
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
import fcntl
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.chatbot.outbox import Outbox
from app.chatbot.runtime import ChatRuntime, BusyError, unconfigured_handler
from app.chatbot.qa import ChatQaHandler
from app.chatbot.qa_config import ChatQaSettings
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.chat_execution import Dispatch


def create_app(*, runtime: ChatRuntime | None = None, api_key: str | None = None):
    key = api_key if api_key is not None else os.getenv("SPRING_INTERNAL_API_KEY", "")

    @asynccontextmanager
    async def lifespan(app):
        if len(key) < 16:
            raise RuntimeError("A private internal API key is required")
        http = None
        provider_http = None
        lock = None
        if runtime is None:
            qa = ChatQaSettings()
            provider_key = os.getenv("LLM_API_KEY", "") if qa.enabled else ""
            if qa.enabled and not provider_key:
                raise RuntimeError("LLM_API_KEY is required when CHAT_QA_ENABLED is true")
            path = os.getenv("CHAT_OUTBOX_PATH", "/tmp/catchhole-chat/outbox.sqlite3")
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = open(path + ".lock", "a")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            http = httpx.AsyncClient(limits=httpx.Limits(max_connections=16), timeout=10)
            url = os.environ["SPRING_INTERNAL_API_BASE_URL"]
            handler = unconfigured_handler
            if qa.enabled:
                provider_http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10))
                delegate = OpenAIResponsesClient(
                    api_key=provider_key,
                    model=qa.model,
                    responses_api_url="https://api.openai.com/v1/responses",
                    reasoning_effort=qa.reasoning_effort,
                    http_client=provider_http,
                    store_responses=False,
                )
                handler = ChatQaHandler(delegate, qa)
            app.state.runtime = ChatRuntime(
                ChatExecutionClient(url, key, http),
                Outbox(path),
                capacity=int(os.getenv("CHAT_CAPACITY", "4")),
                handler=handler,
            )
        else:
            app.state.runtime = runtime

        async def recover():
            while True:
                app.state.runtime.report_ready.clear()
                try:
                    await app.state.runtime.deliver()
                except Exception:
                    app.state.runtime.completed.labels("delivery_failure").inc()
                try:
                    await asyncio.wait_for(app.state.runtime.report_ready.wait(), timeout=1)
                except TimeoutError:
                    pass

        delivery = asyncio.create_task(recover())
        try:
            yield
        finally:
            delivery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delivery
            await app.state.runtime.shutdown(float(os.getenv("CHAT_SHUTDOWN_GRACE", "15")))
            if runtime is None:
                app.state.runtime.outbox.close()
                await http.aclose()
                if provider_http is not None:
                    await provider_http.aclose()
                lock.close()

    app = FastAPI(
        title="CatchHole Chat execution",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if runtime is not None:
        app.state.runtime = runtime

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.url.path != "/healthz":
            supplied = request.headers.get("X-Internal-Api-Key", "")
            if not key or not hmac.compare_digest(supplied.encode(), key.encode()):
                return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    return JSONResponse({"code": "BODY_TOO_LARGE"}, status_code=413)
                body.extend(chunk)
            request._body = bytes(body)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"code": "INVALID_EXECUTION"}, status_code=422)

    @app.get("/healthz")
    async def health():
        ready = getattr(app.state, "runtime", None)
        return JSONResponse(
            {"status": "ready" if ready and ready.accepting else "draining"},
            status_code=200 if ready and ready.accepting else 503,
        )

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(app.state.runtime.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/v1/chat-executions", status_code=202)
    async def submit(dispatch: Dispatch):
        try:
            app.state.runtime.submit(dispatch)
        except ChatTransportError:
            return JSONResponse({"code": "EXECUTION_EXPIRED"}, status_code=409)
        except (BusyError, RuntimeError):
            return JSONResponse({"code": "CAPACITY_EXCEEDED"}, status_code=429)
        return {"schemaVersion": 1, "attemptId": str(dispatch.attempt_id), "accepted": True}

    return app


app = create_app()
