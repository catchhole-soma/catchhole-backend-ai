import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app.clients.exceptions import SpringWorkerTransportError
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import Settings
from app.domain.enums import AnalysisJobType
from app.worker.lease_heartbeat import WorkerLeaseHeartbeat

JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
HEARTBEAT_DATA = {
    "data": {"leaseToken": str(LEASE_TOKEN), "leaseExpiresAt": "2026-10-29T12:00:00"},
}


def test_processing_read_timeout_defaults_to_120_seconds(monkeypatch):
    monkeypatch.delenv("SPRING_WORKER_READ_TIMEOUT_SECONDS", raising=False)
    assert Settings(_env_file=None).spring_worker_read_timeout_seconds == 120.0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_invalid_read_timeout_is_rejected_before_startup(monkeypatch, value):
    monkeypatch.setenv("SPRING_WORKER_READ_TIMEOUT_SECONDS", value)
    with pytest.raises(ValidationError, match="spring_worker_read_timeout_seconds"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_direct_client_cannot_disable_read_timeout(value):
    with pytest.raises(ValueError, match="finite and greater than zero"):
        SpringWorkerClient("http://spring.test", "test-key", read_timeout_seconds=value)


def test_environment_setting_reaches_initial_claim_without_extending_other_io(monkeypatch):
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(204)

        async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs),
        )
        monkeypatch.setenv("SPRING_WORKER_READ_TIMEOUT_SECONDS", "180.5")
        client = SpringWorkerClient.from_settings(Settings(_env_file=None))
        try:
            assert await client.claim([AnalysisJobType.SETTING_EXTRACTION]) is None
            assert requests[0].extensions["timeout"] == {
                "connect": 30, "read": 180.5, "write": 30, "pool": 30,
            }
            assert client.http_client.timeout == httpx.Timeout(30)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_processing_override_keeps_heartbeat_and_token_ledger_limits():
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/heartbeat"):
                return httpx.Response(200, json=HEARTBEAT_DATA)
            return httpx.Response(204)

        client = SpringWorkerClient(
            "http://spring.test", "test-key",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                timeout=httpx.Timeout(30, connect=2, write=3, pool=4),
            ),
        )
        try:
            await client.claim_next_character_fact_comparison_batch(JOB_ID, LEASE_TOKEN)
            await client.heartbeat(JOB_ID, LEASE_TOKEN)
            await client.release_ai_tokens(JOB_ID, "FAILURE")
            await client.claim_next_world_setting_comparison_batch(JOB_ID, LEASE_TOKEN)
            assert [request.extensions["timeout"] for request in requests] == [
                {"connect": 2, "read": 120, "write": 3, "pool": 4},
                {"connect": 2, "read": 30, "write": 3, "pool": 4},
                {"connect": 2, "read": 30, "write": 3, "pool": 4},
                {"connect": 2, "read": 120, "write": 3, "pool": 4},
            ]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_real_http_delayed_claim_survives_old_read_limit_without_retransmission():
    async def scenario():
        # Scale 30s -> 120s to 0.1s -> 0.4s; the server responds at 0.2s.
        async with _spring_server(processing_delay=0.2) as (url, requests):
            for read_timeout, expected_success in [(0.1, False), (0.4, True)]:
                client = _local_client(url, read_timeout)
                try:
                    if expected_success:
                        result = await client.claim_next_character_fact_comparison_batch(
                            JOB_ID, LEASE_TOKEN,
                        )
                        assert result is None
                    else:
                        with pytest.raises(SpringWorkerTransportError) as exc:
                            await client.claim_next_character_fact_comparison_batch(
                                JOB_ID, LEASE_TOKEN,
                            )
                        assert isinstance(exc.value.__cause__, httpx.ReadTimeout)
                finally:
                    await client.aclose()
            assert len(requests) == 2  # One POST for each attempt; no hidden retry.

    asyncio.run(scenario())


def test_real_http_heartbeat_stays_live_during_slow_processing_response():
    async def scenario():
        async with _spring_server(processing_delay=0.2) as (url, requests):
            client = _local_client(url, 1)
            try:
                async with WorkerLeaseHeartbeat(
                    client, JOB_ID, LEASE_TOKEN, interval_seconds=0.02,
                ) as heartbeat:
                    assert await client.claim_next_character_fact_comparison_batch(
                        JOB_ID, LEASE_TOKEN,
                    ) is None
                    heartbeat.raise_if_failed()
                assert any(path.endswith("/heartbeat") for path in requests)
                assert sum(path.endswith("/claim-next") for path in requests) == 1
            finally:
                await client.aclose()

    asyncio.run(scenario())


def test_real_http_failed_heartbeat_cancels_long_read_before_it_can_complete():
    async def scenario():
        async with _spring_server(processing_delay=2, heartbeat_delay=2) as (url, requests):
            client = _local_client(url, 3)
            heartbeat = WorkerLeaseHeartbeat(
                client, JOB_ID, LEASE_TOKEN, interval_seconds=0.01, retry_base_seconds=0,
            )

            async def processing_job():
                async with heartbeat:
                    await client.claim_next_character_fact_comparison_batch(JOB_ID, LEASE_TOKEN)

            try:
                with pytest.raises(asyncio.CancelledError, match="heartbeat failed"):
                    await asyncio.create_task(processing_job())
                with pytest.raises(RuntimeError, match="heartbeat failed") as exc:
                    heartbeat.raise_if_failed()
                assert isinstance(exc.value.__cause__, SpringWorkerTransportError)
                assert isinstance(exc.value.__cause__.__cause__, httpx.ReadTimeout)
                assert sum(path.endswith("/heartbeat") for path in requests) == 3
                assert sum(path.endswith("/claim-next") for path in requests) == 1
            finally:
                await client.aclose()

    asyncio.run(scenario())


def _local_client(url, read_timeout):
    return SpringWorkerClient(
        url, "test-key", read_timeout_seconds=read_timeout,
        http_client=httpx.AsyncClient(timeout=0.1, trust_env=False),
    )


@asynccontextmanager
async def _spring_server(*, processing_delay, heartbeat_delay=0):
    """Real loopback TCP: exercise HTTPX timeouts without a provider or live Spring."""
    requests = []
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1].decode("ascii")
            requests.append(path)
            is_heartbeat = path.endswith("/heartbeat")
            await asyncio.sleep(heartbeat_delay if is_heartbeat else processing_delay)
            body = json.dumps(HEARTBEAT_DATA).encode() if is_heartbeat else b""
            status = "200 OK" if is_heartbeat else "204 No Content"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        await server.wait_closed()
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
