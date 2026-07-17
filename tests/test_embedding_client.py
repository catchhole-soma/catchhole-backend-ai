import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.embeddings.client import OpenAIEmbeddingsClient
from app.embeddings.exceptions import RecoverableEmbeddingProviderError


def test_create_embeddings_calls_openai_api_and_orders_response_by_index() -> None:
    requests: list[httpx.Request] = []
    client = _client(
        handler=lambda request: _embedding_response(request, requests),
    )

    response = client.create_embeddings(["첫 번째 청크", "두 번째 청크"])

    request = requests[0]
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.url.path == "/v1/embeddings"
    assert json.loads(request.content) == {
        "model": "text-embedding-3-small",
        "input": ["첫 번째 청크", "두 번째 청크"],
        "dimensions": 3,
        "encoding_format": "float",
    }
    assert response.embeddings == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert response.model == "text-embedding-3-small"
    assert response.input_token_count == 12


def test_create_embedding_returns_single_vector() -> None:
    client = _client(
        handler=lambda request: httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "model": "text-embedding-3-small",
            },
            request=request,
        ),
    )

    assert client.create_embedding("query") == [0.1, 0.2, 0.3]


@pytest.mark.parametrize("inputs", [[], [""], ["  "]])
def test_create_embeddings_rejects_empty_or_blank_inputs(inputs: list[str]) -> None:
    client = _client(handler=lambda request: httpx.Response(200, request=request))

    with pytest.raises(ValueError):
        client.create_embeddings(inputs)


def test_create_embeddings_requires_api_key() -> None:
    client = _client(
        api_key="",
        handler=lambda request: httpx.Response(200, request=request),
    )

    with pytest.raises(ValueError, match="LLM_API_KEY"):
        client.create_embedding("query")


def test_create_embeddings_wraps_transport_error_as_recoverable() -> None:
    # timeout·연결 실패가 Worker에서 후속 backfill 가능한 provider 장애로 구분되는지 확인한다.
    def raise_connection_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    client = _client(handler=raise_connection_error)

    with pytest.raises(RecoverableEmbeddingProviderError, match="temporarily"):
        client.create_embedding("query")


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503])
def test_create_embeddings_wraps_retryable_http_status_as_recoverable(
    status_code: int,
) -> None:
    # 요청 제한과 provider 서버 오류를 인증·요청 오류와 다른 예외로 변환하는지 검증한다.
    client = _client(
        handler=lambda request: httpx.Response(status_code, request=request),
    )

    with pytest.raises(RecoverableEmbeddingProviderError, match=f"status={status_code}"):
        client.create_embedding("query")


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_create_embeddings_keeps_non_retryable_http_status_fatal(
    status_code: int,
) -> None:
    # 잘못된 요청과 인증·권한 오류는 backfill로 해결되지 않으므로 원래 HTTP 예외를 전달한다.
    client = _client(
        handler=lambda request: httpx.Response(status_code, request=request),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.create_embedding("query")


def test_create_embeddings_rejects_dimension_mismatch() -> None:
    client = _client(
        handler=lambda request: httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "model": "text-embedding-3-small",
            },
            request=request,
        ),
    )

    with pytest.raises(ValueError, match="dimensions"):
        client.create_embedding("query")


@pytest.mark.parametrize(
    "item",
    [
        {"embedding": [0.1, 0.2, 0.3]},
        {"index": None, "embedding": [0.1, 0.2, 0.3]},
        {"index": "0", "embedding": [0.1, 0.2, 0.3]},
    ],
)
def test_create_embeddings_rejects_missing_or_non_integer_index(item: dict) -> None:
    client = _client(
        handler=lambda request: httpx.Response(
            200,
            json={"data": [item], "model": "text-embedding-3-small"},
            request=request,
        ),
    )

    with pytest.raises(ValueError, match="indices"):
        client.create_embedding("query")


def test_from_settings_uses_embedding_contract() -> None:
    settings = Settings(
        llm_api_key="settings-key",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        embedding_version="v1",
        openai_embeddings_api_url="https://api.openai.test/v1/embeddings",
    )

    client = OpenAIEmbeddingsClient.from_settings(settings)

    assert client.api_key == "settings-key"
    assert client.model == "text-embedding-3-small"
    assert client.dimensions == 1536
    assert client.version == "v1"
    assert client.embeddings_api_url == "https://api.openai.test/v1/embeddings"


def test_settings_reject_dimensions_that_do_not_match_database_vector() -> None:
    with pytest.raises(ValidationError, match="must match episode_chunks.embedding"):
        Settings(embedding_dimensions=1024)


def test_settings_parses_embedding_dimensions_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")

    settings = Settings(_env_file=None)

    assert settings.embedding_dimensions == 1536


def _client(
    handler,
    api_key: str = "test-key",
) -> OpenAIEmbeddingsClient:
    return OpenAIEmbeddingsClient(
        api_key=api_key,
        model="text-embedding-3-small",
        dimensions=3,
        version="v1",
        embeddings_api_url="https://api.openai.test/v1/embeddings",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _embedding_response(
    request: httpx.Request,
    requests: list[httpx.Request],
) -> httpx.Response:
    requests.append(request)
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 12, "total_tokens": 12},
        },
        request=request,
    )
