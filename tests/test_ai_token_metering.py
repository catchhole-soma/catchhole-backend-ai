from uuid import UUID

import pytest

from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.responses import LlmTextResponse
from app.usage.metering import MeteredEmbeddingClient, MeteredTextGenerationClient

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")


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
    )

    with pytest.raises(TimeoutError, match="provider timeout"):
        client.create_text_response("규칙", "문맥")

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == []
    assert ledger.releases == [(request_id, "USAGE_UNAVAILABLE")]


def test_embedding_request_is_metered_separately() -> None:
    ledger = FakeLedger()
    client = MeteredEmbeddingClient(
        delegate=FakeEmbeddingClient(),
        ledger=ledger,
        analysis_job_id=ANALYSIS_JOB_ID,
        model_name="text-embedding-3-small",
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
    )

    client.create_text_response("규칙", "원고")

    request_id = ledger.reservations[0]["request_id"]
    assert ledger.settlements == []
    assert ledger.releases == [(request_id, "USAGE_UNAVAILABLE")]


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
