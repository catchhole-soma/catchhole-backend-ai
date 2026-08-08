from uuid import UUID

import httpx
import pytest

import app.usage.metering as metering
from app.embeddings.responses import EmbeddingBatchResponse
from app.embeddings.exceptions import (
    EmbeddingResponseValidationError,
    RecoverableEmbeddingProviderError,
)
from app.llm.exceptions import LlmResponseValidationError
from app.llm.responses import LlmTextResponse
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

    response = client.create_text_response("규칙", "원고", max_output_tokens=100)

    assert response.text == "{}"
    assert len(ledger.reservations) == 1
    request_id = ledger.reservations[0]["request_id"]
    assert ledger.reservations[0]["purpose"] == "SETTING_EXTRACTION"
    assert ledger.reservations[0]["attempt"] == 1
    assert ledger.reservations[0]["reserved_tokens"] >= 100
    assert ledger.settlements == [(request_id, 120, 20, 30, "SUCCESS")]
    assert ledger.releases == []


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
        client.create_text_response("규칙", "문맥")

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
    )

    with pytest.raises(RecoverableEmbeddingProviderError, match="temporary provider error"):
        client.create_embeddings(["첫 청크"])


def test_embedding_request_is_metered_separately() -> None:
    ledger = FakeLedger()
    client = MeteredEmbeddingClient(
        delegate=FakeEmbeddingClient(),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
        lease_token=LEASE_TOKEN,
    )

    response = client.create_embeddings(["첫 청크", "두 번째 청크"])

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

    client.create_text_response("규칙", "원고")

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
        client.create_text_response("규칙", "원고")

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == [(request_id, 120, 20, 30, "FAILURE")]
    assert ledger.releases == []


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
    )

    with pytest.raises(RecoverableEmbeddingProviderError):
        client.create_embeddings(["첫 청크"])

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
        client.create_embeddings(["첫 청크"])

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == [(request_id, 42, 0, 0, "FAILURE")]
    assert ledger.releases == []


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

    assert reservation == len("규칙원고".encode("utf-8")) + 100 + 512


class FakeLedger:
    def __init__(self) -> None:
        self.reservations: list[dict] = []
        self.settlements: list[tuple] = []
        self.releases: list[tuple] = []

    def reserve_ai_tokens(self, **kwargs) -> None:
        self.reservations.append(kwargs)

    def settle_ai_tokens(
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

    def release_ai_tokens(self, request_id, outcome) -> None:
        self.releases.append((request_id, outcome))


class FailingReleaseLedger(FakeLedger):
    def release_ai_tokens(self, request_id, outcome) -> None:
        raise httpx.ConnectError("spring unavailable")


class FakeTextClient:
    def __init__(
        self,
        response: LlmTextResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error

    def create_text_response(self, **kwargs) -> LlmTextResponse:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class FakeEmbeddingClient:
    version = "v1"

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        return EmbeddingBatchResponse(
            embeddings=[[0.1], [0.2]],
            model="text-embedding-3-small",
            input_token_count=42,
        )


class FailingEmbeddingClient:
    version = "v1"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        raise self.error


class FakeEncoding:
    def encode(self, text: str, disallowed_special=()) -> list[str]:
        return list(text)
