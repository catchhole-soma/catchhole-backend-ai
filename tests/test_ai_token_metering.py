import asyncio
import logging
from uuid import UUID

import httpx
import pytest

from app.embeddings.exceptions import (
    EmbeddingResponseValidationError,
    RecoverableEmbeddingProviderError,
)
from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)
from app.llm.protocols import LlmResponseSchema
from app.llm.responses import LlmTextResponse
from app.usage import metering
from app.usage.metering import (
    MeteredEmbeddingClient,
    MeteredTextGenerationClient,
    _estimate_text_token_upper_bound,
)

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def use_offline_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_encoding_for_model(model: str):
        if model == "unknown-model":
            raise KeyError(model)
        return FakeEncoding()

    monkeypatch.setattr(metering, "_encoding_for_model", fake_encoding_for_model)


def test_text_generation_reserves_and_settles_actual_usage() -> None:
    ledger = FakeLedger()
    delegate = FakeTextClient(
        response=LlmTextResponse(
            text="{}",
            input_token_count=120,
            cached_input_token_count=20,
            output_token_count=30,
        )
    )
    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
    )

    response = asyncio.run(client.create_text_response("규칙", "원고", max_output_tokens=100))

    assert response.text == "{}"
    assert len(ledger.reservations) == 1
    request_id = ledger.reservations[0]["request_id"]
    assert ledger.reservations[0]["purpose"] == "SETTING_EXTRACTION"
    assert ledger.reservations[0]["attempt"] == 1
    assert ledger.reservations[0]["reserved_tokens"] >= 100
    assert ledger.settlements == [(request_id, 120, 20, 30, "SUCCESS")]
    assert ledger.releases == []


def test_text_generation_forwards_and_reserves_structured_output_schema() -> None:
    ledger = FakeLedger()
    delegate = FakeTextClient(
        response=LlmTextResponse(
            text='{"candidates":[]}',
            input_token_count=20,
            output_token_count=5,
        )
    )
    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-5.6-terra",
        lease_token=LEASE_TOKEN,
    )
    response_schema = LlmResponseSchema(
        name="character_setting_extraction",
        schema={
            "type": "object",
            "properties": {"candidates": {"type": "array"}},
            "required": ["candidates"],
            "additionalProperties": False,
        },
    )

    asyncio.run(
        client.create_text_response(
            "규칙",
            "원고",
            max_output_tokens=100,
            response_schema=response_schema,
        )
    )

    without_schema = _estimate_text_token_upper_bound(
        "규칙",
        "원고",
        "gpt-5.6-terra",
        100,
    )
    assert delegate.requests[0]["response_schema"] is response_schema
    assert ledger.reservations[0]["reserved_tokens"] > without_schema


def test_text_generation_releases_reservation_when_usage_is_unavailable() -> None:
    ledger = FakeLedger()
    client = MeteredTextGenerationClient(
        delegate=FakeTextClient(error=TimeoutError("provider timeout")),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SUBJECT_RESOLUTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
    )

    with pytest.raises(TimeoutError, match="provider timeout"):
        asyncio.run(client.create_text_response("규칙", "문맥"))

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == []
    assert ledger.releases == [(request_id, "USAGE_UNAVAILABLE")]


def test_release_failure_does_not_mask_original_provider_error() -> None:
    provider_error = RecoverableEmbeddingProviderError("temporary provider error")
    client = MeteredEmbeddingClient(
        delegate=FailingEmbeddingClient(provider_error),
        ledger=FailingReleaseLedger(),
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
        lease_token=LEASE_TOKEN,
        max_retries=0,
    )

    with pytest.raises(RecoverableEmbeddingProviderError, match="temporary provider error"):
        asyncio.run(client.create_embeddings(["첫 청크"]))


def test_embedding_request_is_metered_separately() -> None:
    ledger = FakeLedger()
    client = MeteredEmbeddingClient(
        delegate=FakeEmbeddingClient(),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
        lease_token=LEASE_TOKEN,
    )

    response = asyncio.run(client.create_embeddings(["첫 청크", "두 번째 청크"]))

    assert response.input_token_count == 42
    request_id = ledger.reservations[0]["request_id"]
    assert ledger.reservations[0]["purpose"] == "CHUNK_EMBEDDING"
    assert ledger.settlements == [(request_id, 42, 0, 0, "SUCCESS")]


def test_success_without_provider_usage_releases_reservation() -> None:
    ledger = FakeLedger()
    client = MeteredTextGenerationClient(
        delegate=FakeTextClient(response=LlmTextResponse(text="{}")),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
    )

    asyncio.run(client.create_text_response("규칙", "원고"))

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == []
    assert ledger.releases == [(request_id, "USAGE_UNAVAILABLE")]


def test_text_validation_error_settles_reported_usage() -> None:
    ledger = FakeLedger()
    client = MeteredTextGenerationClient(
        delegate=FakeTextClient(
            error=LlmResponseValidationError(
                "invalid output structure",
                input_token_count=120,
                cached_input_token_count=20,
                output_token_count=30,
            )
        ),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
    )

    with pytest.raises(LlmResponseValidationError, match="invalid output structure"):
        asyncio.run(client.create_text_response("규칙", "원고"))

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == [(request_id, 120, 20, 30, "FAILURE")]
    assert ledger.releases == []


def test_output_truncation_is_settled_once_without_provider_retry_or_prompt_logging(
    caplog,
) -> None:
    ledger = FakeLedger()
    delegate = SequencedTextClient(
        [
            LlmOutputTruncatedError(
                "provider output truncated",
                incomplete_reason="max_output_tokens",
                max_output_tokens=4000,
                input_token_count=2522,
                cached_input_token_count=1200,
                output_token_count=4000,
            )
        ]
    )
    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-5.6-terra",
        lease_token=LEASE_TOKEN,
        max_retries=3,
    )

    with (
        caplog.at_level(logging.WARNING, logger="app.usage.metering"),
        pytest.raises(LlmOutputTruncatedError),
    ):
        asyncio.run(
            client.create_text_response(
                "SECRET_SYSTEM_PROMPT",
                "SECRET_NOVEL_BODY",
                max_output_tokens=4000,
            )
        )

    request_id = ledger.reservations[0]["request_id"]
    assert delegate.call_count == 1
    assert ledger.settlements == [(request_id, 2522, 1200, 4000, "FAILURE")]
    assert ledger.releases == []
    assert "purpose=SETTING_EXTRACTION" in caplog.text
    assert "attempt=1" in caplog.text
    assert "max_output_tokens=4000" in caplog.text
    assert "input_tokens=2522" in caplog.text
    assert "cached_input_tokens=1200" in caplog.text
    assert "output_tokens=4000" in caplog.text
    assert "reason=max_output_tokens" in caplog.text
    assert "SECRET_SYSTEM_PROMPT" not in caplog.text
    assert "SECRET_NOVEL_BODY" not in caplog.text


def test_incomplete_response_logs_reason_and_usage_without_retry_or_prompt_logging(caplog) -> None:
    ledger = FakeLedger()
    delegate = SequencedTextClient(
        [
            LlmIncompleteResponseError(
                "provider response incomplete",
                incomplete_reason="content_filter",
                input_token_count=510,
                cached_input_token_count=200,
                output_token_count=12,
            )
        ]
    )
    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SUBJECT_RESOLUTION",
        default_model="gpt-5.6-luna",
        lease_token=LEASE_TOKEN,
        max_retries=3,
    )

    with (
        caplog.at_level(logging.WARNING, logger="app.usage.metering"),
        pytest.raises(LlmIncompleteResponseError),
    ):
        asyncio.run(
            client.create_text_response(
                "SECRET_SYSTEM_PROMPT",
                "SECRET_NOVEL_BODY",
                max_output_tokens=1000,
            )
        )

    request_id = ledger.reservations[0]["request_id"]
    assert delegate.call_count == 1
    assert ledger.settlements == [(request_id, 510, 200, 12, "FAILURE")]
    assert ledger.releases == []
    assert "purpose=SUBJECT_RESOLUTION" in caplog.text
    assert "attempt=1" in caplog.text
    assert "max_output_tokens=1000" in caplog.text
    assert "input_tokens=510" in caplog.text
    assert "cached_input_tokens=200" in caplog.text
    assert "output_tokens=12" in caplog.text
    assert "reason=content_filter" in caplog.text
    assert "SECRET_SYSTEM_PROMPT" not in caplog.text
    assert "SECRET_NOVEL_BODY" not in caplog.text


def test_wrapped_embedding_http_error_settles_reported_usage() -> None:
    ledger = FakeLedger()
    request = httpx.Request("POST", "https://api.openai.test/v1/embeddings")
    response = httpx.Response(
        503,
        request=request,
        json={"usage": {"prompt_tokens": 42}},
    )
    http_error = httpx.HTTPStatusError("temporary", request=request, response=response)
    wrapped_error = RecoverableEmbeddingProviderError("temporary provider error")
    wrapped_error.__cause__ = http_error
    client = MeteredEmbeddingClient(
        delegate=FailingEmbeddingClient(wrapped_error),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
        lease_token=LEASE_TOKEN,
        max_retries=0,
    )

    with pytest.raises(RecoverableEmbeddingProviderError):
        asyncio.run(client.create_embeddings(["첫 청크"]))

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == [(request_id, 42, 0, 0, "FAILURE")]
    assert ledger.releases == []


def test_embedding_validation_error_settles_reported_usage() -> None:
    ledger = FakeLedger()
    client = MeteredEmbeddingClient(
        delegate=FailingEmbeddingClient(
            EmbeddingResponseValidationError("invalid dimensions", input_token_count=42)
        ),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
        lease_token=LEASE_TOKEN,
    )

    with pytest.raises(EmbeddingResponseValidationError, match="invalid dimensions"):
        asyncio.run(client.create_embeddings(["첫 청크"]))

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == [(request_id, 42, 0, 0, "FAILURE")]
    assert ledger.releases == []


def test_text_generation_retries_transient_provider_errors_with_exponential_backoff() -> None:
    ledger = FakeLedger()
    delegate = SequencedTextClient(
        [
            _http_status_error(503),
            _http_status_error(500),
            httpx.ReadTimeout("provider timeout", request=_provider_request()),
            LlmTextResponse(text="{}", input_token_count=10, output_token_count=5),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
        max_retries=3,
        retry_base_seconds=2.0,
        sleeper=record_sleep,
        random_source=lambda: 0.0,
    )

    response = asyncio.run(client.create_text_response("규칙", "원고"))

    assert response.text == "{}"
    assert sleeps == [2.0, 4.0, 8.0]
    assert [reservation["attempt"] for reservation in ledger.reservations] == [1, 2, 3, 4]
    assert len({reservation["request_id"] for reservation in ledger.reservations}) == 4
    assert len(ledger.releases) == 3
    assert len(ledger.settlements) == 1


def test_text_generation_uses_retry_after_before_local_backoff() -> None:
    ledger = FakeLedger()
    delegate = SequencedTextClient(
        [
            _http_status_error(
                429,
                error_code="rate_limit_exceeded",
                headers={"Retry-After": "7.5"},
            ),
            LlmTextResponse(text="{}", input_token_count=10, output_token_count=5),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
        sleeper=record_sleep,
        random_source=lambda: 1.0,
    )

    asyncio.run(client.create_text_response("규칙", "원고"))

    assert sleeps == [7.5]
    assert delegate.call_count == 2


def test_text_generation_retries_request_timeout_response() -> None:
    ledger = FakeLedger()
    delegate = SequencedTextClient(
        [
            _http_status_error(408),
            LlmTextResponse(text="{}", input_token_count=10, output_token_count=5),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
        max_retries=1,
        sleeper=record_sleep,
        random_source=lambda: 0.0,
    )

    asyncio.run(client.create_text_response("규칙", "원고"))

    assert delegate.call_count == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize("status_code", [408, 409])
def test_embedding_retries_wrapped_recoverable_http_response(status_code: int) -> None:
    http_error = _http_status_error(status_code)
    wrapped_error = RecoverableEmbeddingProviderError("temporary provider error")
    wrapped_error.__cause__ = http_error
    delegate = SequencedEmbeddingClient(
        [
            wrapped_error,
            EmbeddingBatchResponse(
                embeddings=[[0.1]],
                model="text-embedding-3-small",
                input_token_count=1,
            ),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = MeteredEmbeddingClient(
        delegate=delegate,
        ledger=FakeLedger(),
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
        lease_token=LEASE_TOKEN,
        max_retries=1,
        sleeper=record_sleep,
        random_source=lambda: 0.0,
    )

    asyncio.run(client.create_embeddings(["첫 청크"]))

    assert delegate.call_count == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize(
    "error_code",
    ["insufficient_quota", "billing_hard_limit_reached", "billing_not_active"],
)
def test_text_generation_does_not_retry_non_transient_429(error_code: str) -> None:
    ledger = FakeLedger()
    provider_error = _http_status_error(429, error_code=error_code)
    delegate = SequencedTextClient([provider_error])
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = MeteredTextGenerationClient(
        delegate=delegate,
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        purpose="SETTING_EXTRACTION",
        default_model="gpt-4.1-mini",
        lease_token=LEASE_TOKEN,
        sleeper=record_sleep,
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.create_text_response("규칙", "원고"))

    assert delegate.call_count == 1
    assert sleeps == []
    assert len(ledger.releases) == 1


def test_text_generation_cancellation_releases_reservation_and_semaphore() -> None:
    async def scenario() -> tuple[FakeLedger, asyncio.Semaphore]:
        ledger = FakeLedger()
        semaphore = asyncio.Semaphore(1)
        provider_started = asyncio.Event()

        class BlockingTextClient:
            async def create_text_response(self, **kwargs) -> LlmTextResponse:
                provider_started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        client = MeteredTextGenerationClient(
            delegate=BlockingTextClient(),
            ledger=ledger,
            analysis_job_id=ANALYSIS_JOB_ID,
            purpose="SETTING_EXTRACTION",
            default_model="gpt-4.1-mini",
            lease_token=LEASE_TOKEN,
            request_semaphore=semaphore,
        )
        task = asyncio.create_task(client.create_text_response("규칙", "원고"))
        await provider_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        replacement = MeteredTextGenerationClient(
            delegate=FakeTextClient(
                response=LlmTextResponse(text="{}", input_token_count=1, output_token_count=1)
            ),
            ledger=ledger,
            analysis_job_id=ANALYSIS_JOB_ID,
            purpose="SETTING_EXTRACTION",
            default_model="gpt-4.1-mini",
            lease_token=LEASE_TOKEN,
            request_semaphore=semaphore,
        )
        await asyncio.wait_for(replacement.create_text_response("규칙", "다음 원고"), timeout=1)
        return ledger, semaphore

    ledger, semaphore = asyncio.run(scenario())

    assert len(ledger.releases) == 1
    assert semaphore.locked() is False


def test_text_generation_cancellation_during_reserve_releases_after_reserve_finishes() -> None:
    async def scenario() -> tuple[FakeLedger, SequencedTextClient, asyncio.Semaphore]:
        class BlockingReserveLedger(FakeLedger):
            def __init__(self) -> None:
                super().__init__()
                self.reserve_started = asyncio.Event()
                self.allow_reserve_to_finish = asyncio.Event()
                self.release_finished = asyncio.Event()

            async def reserve_ai_tokens(self, **kwargs) -> None:
                self.reservations.append(kwargs)
                self.reserve_started.set()
                await self.allow_reserve_to_finish.wait()

            async def release_ai_tokens(self, request_id: UUID, outcome: str) -> None:
                await super().release_ai_tokens(request_id, outcome)
                self.release_finished.set()

        ledger = BlockingReserveLedger()
        semaphore = asyncio.Semaphore(1)
        delegate = SequencedTextClient(
            [LlmTextResponse(text="{}", input_token_count=1, output_token_count=1)]
        )
        client = MeteredTextGenerationClient(
            delegate=delegate,
            ledger=ledger,
            analysis_job_id=ANALYSIS_JOB_ID,
            purpose="SETTING_EXTRACTION",
            default_model="gpt-4.1-mini",
            lease_token=LEASE_TOKEN,
            request_semaphore=semaphore,
        )

        task = asyncio.create_task(client.create_text_response("규칙", "원고"))
        await ledger.reserve_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)

        assert semaphore.locked() is False
        assert ledger.releases == []
        ledger.allow_reserve_to_finish.set()
        await asyncio.wait_for(ledger.release_finished.wait(), timeout=1)
        return ledger, delegate, semaphore

    ledger, delegate, semaphore = asyncio.run(scenario())

    assert len(ledger.reservations) == 1
    assert len(ledger.releases) == 1
    assert delegate.call_count == 0
    assert semaphore.locked() is False


def test_ledger_call_cancellation_does_not_wait_for_ledger_completion() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        allow_completion = asyncio.Event()
        completed = asyncio.Event()

        async def blocking_ledger_call() -> None:
            started.set()
            await allow_completion.wait()
            completed.set()

        owner = asyncio.create_task(
            metering._complete_ledger_call_on_cancellation(blocking_ledger_call())
        )
        await started.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=0.1)

        assert completed.is_set() is False
        allow_completion.set()
        await asyncio.wait_for(completed.wait(), timeout=1)

    asyncio.run(scenario())


def test_shared_request_semaphore_limits_parallel_provider_calls_across_jobs() -> None:
    async def scenario() -> tuple[int, FakeLedger]:
        ledger = FakeLedger()
        semaphore = asyncio.Semaphore(3)
        three_calls_active = asyncio.Event()
        release_calls = asyncio.Event()
        active_calls = 0
        max_active_calls = 0

        class TrackingTextClient:
            async def create_text_response(self, **kwargs) -> LlmTextResponse:
                nonlocal active_calls, max_active_calls
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
                if active_calls == 3:
                    three_calls_active.set()
                try:
                    await release_calls.wait()
                finally:
                    active_calls -= 1
                return LlmTextResponse(text="{}", input_token_count=1, output_token_count=1)

        delegate = TrackingTextClient()
        clients = [
            MeteredTextGenerationClient(
                delegate=delegate,
                ledger=ledger,
                analysis_job_id=UUID(int=index + 10),
                purpose="SETTING_EXTRACTION",
                default_model="gpt-4.1-mini",
                lease_token=LEASE_TOKEN,
                request_semaphore=semaphore,
            )
            for index in range(8)
        ]
        tasks = [
            asyncio.create_task(client.create_text_response("규칙", f"원고 {index}"))
            for index, client in enumerate(clients)
        ]
        await asyncio.wait_for(three_calls_active.wait(), timeout=1)
        assert max_active_calls == 3
        assert sum(task.done() for task in tasks) == 0
        release_calls.set()
        await asyncio.gather(*tasks)
        return max_active_calls, ledger

    max_active_calls, ledger = asyncio.run(scenario())

    assert max_active_calls == 3
    assert len(ledger.reservations) == 8
    assert len(ledger.settlements) == 8


def test_known_model_reservation_uses_tokenizer_instead_of_utf8_bytes() -> None:
    system_prompt = "설정 추출 규칙입니다. " * 200
    user_prompt = "비요른은 새로운 기술을 익혔다. " * 500
    max_output_tokens = 4000
    legacy_byte_reservation = (
        len(system_prompt.encode("utf-8"))
        + len(user_prompt.encode("utf-8"))
        + max_output_tokens
        + 512
    )

    reservation = _estimate_text_token_upper_bound(
        system_prompt,
        user_prompt,
        "gpt-4.1-mini",
        max_output_tokens,
    )

    assert reservation >= max_output_tokens + 256
    assert reservation < legacy_byte_reservation


def test_unknown_model_reservation_keeps_conservative_byte_fallback() -> None:
    reservation = _estimate_text_token_upper_bound("규칙", "원고", "unknown-model", 100)

    assert reservation == len("규칙원고".encode()) + 100 + 512


class FakeLedger:
    def __init__(self) -> None:
        self.reservations: list[dict] = []
        self.settlements: list[tuple] = []
        self.releases: list[tuple] = []

    async def reserve_ai_tokens(self, **kwargs) -> None:
        self.reservations.append(kwargs)

    async def settle_ai_tokens(
        self,
        request_id,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        outcome,
    ) -> None:
        self.settlements.append(
            (request_id, input_tokens, cached_input_tokens, output_tokens, outcome)
        )

    async def release_ai_tokens(self, request_id, outcome) -> None:
        self.releases.append((request_id, outcome))


class FailingReleaseLedger(FakeLedger):
    async def release_ai_tokens(self, request_id, outcome) -> None:
        raise httpx.ConnectError("spring unavailable")


class FakeTextClient:
    def __init__(
        self,
        response: LlmTextResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class SequencedTextClient:
    def __init__(self, results: list[LlmTextResponse | Exception]) -> None:
        self.results = results
        self.call_count = 0

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        result = self.results[self.call_count]
        self.call_count += 1
        if isinstance(result, Exception):
            raise result
        return result


class FakeEmbeddingClient:
    version = "v1"

    async def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        return EmbeddingBatchResponse(
            embeddings=[[0.1], [0.2]],
            model="text-embedding-3-small",
            input_token_count=42,
        )


class FailingEmbeddingClient:
    version = "v1"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        raise self.error


class SequencedEmbeddingClient:
    version = "v1"

    def __init__(self, results: list[EmbeddingBatchResponse | Exception]) -> None:
        self.results = results
        self.call_count = 0

    async def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        result = self.results[self.call_count]
        self.call_count += 1
        if isinstance(result, Exception):
            raise result
        return result


class FakeEncoding:
    def encode(self, text: str, disallowed_special=()) -> list[str]:
        return list(text)


def _provider_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.test/v1/responses")


def _http_status_error(
    status_code: int,
    error_code: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    request = _provider_request()
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers,
        json={"error": {"code": error_code}} if error_code is not None else {},
    )
    return httpx.HTTPStatusError("provider failed", request=request, response=response)
