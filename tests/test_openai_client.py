import asyncio
import json
import logging

import httpx
import pytest

from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)
from app.llm.openai_client import OpenAIResponsesClient


def test_default_http_client_uses_120_second_read_timeout() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
    )

    try:
        assert client.http_client.timeout.read == 120
    finally:
        asyncio.run(client.aclose())


def test_create_text_response_calls_openai_responses_api() -> None:
    requests: list[httpx.Request] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _response(request, requests))
        ),
    )

    response = asyncio.run(
        client.create_text_response(
            system_prompt="JSON만 반환하세요.",
            user_prompt="원문",
            max_output_tokens=100,
        )
    )

    request = requests[0]
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.url.path == "/v1/responses"
    assert json.loads(request.content) == {
        "model": "gpt-4.1-mini",
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": "JSON만 반환하세요."}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "원문"}],
            },
        ],
        "max_output_tokens": 100,
    }
    assert response.text == '{"candidates":[]}'
    assert response.input_token_count == 10
    assert response.output_token_count == 5


def test_create_text_response_sends_cache_key_and_logs_cache_usage(caplog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=200,
            json={
                "status": "completed",
                "output_text": "{}",
                "usage": {
                    "input_tokens": 1400,
                    "input_tokens_details": {"cached_tokens": 1024},
                    "output_tokens": 10,
                },
            },
        )

    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level(logging.DEBUG, logger="app.llm.openai_client"):
        response = asyncio.run(
            client.create_text_response(
                system_prompt="규칙",
                user_prompt="원문",
                prompt_cache_key="setting-extraction:v1:abc123",
            )
        )

    request_body = json.loads(requests[0].content)
    assert request_body["prompt_cache_key"] == "setting-extraction:v1:abc123"
    assert response.cached_input_token_count == 1024
    assert "cached_tokens_present=True" in caplog.text
    assert "cached_tokens=1024" in caplog.text


def test_create_text_response_sends_configured_reasoning_effort() -> None:
    requests: list[httpx.Request] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        reasoning_effort="none",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _response(request, requests))
        ),
    )

    asyncio.run(client.create_text_response(system_prompt="규칙", user_prompt="원문"))

    request_body = json.loads(requests[0].content)
    assert request_body["model"] == "gpt-5.6-terra"
    assert request_body["reasoning"] == {"effort": "none"}


def test_create_text_response_omits_reasoning_for_non_reasoning_model_override() -> None:
    requests: list[httpx.Request] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        reasoning_effort="none",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _response(request, requests))
        ),
    )

    asyncio.run(
        client.create_text_response(
            system_prompt="규칙",
            user_prompt="원문",
            model="gpt-4.1-mini",
        )
    )

    request_body = json.loads(requests[0].content)
    assert request_body["model"] == "gpt-4.1-mini"
    assert "reasoning" not in request_body


def test_create_text_response_does_not_inherit_none_for_o_series_override() -> None:
    requests: list[httpx.Request] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        reasoning_effort="none",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _response(request, requests))
        ),
    )

    asyncio.run(client.create_text_response(system_prompt="규칙", user_prompt="원문", model="o3"))

    request_body = json.loads(requests[0].content)
    assert request_body["model"] == "o3"
    assert "reasoning" not in request_body


def test_malformed_success_response_preserves_reported_usage() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code=200,
                    json={
                        "status": "completed",
                        "output": [{"content": "invalid"}],
                        "usage": {
                            "input_tokens": 120,
                            "input_tokens_details": {"cached_tokens": 20},
                            "output_tokens": 30,
                        },
                    },
                )
            )
        ),
    )

    with pytest.raises(LlmResponseValidationError) as exc_info:
        asyncio.run(client.create_text_response(system_prompt="규칙", user_prompt="원문"))

    assert exc_info.value.input_token_count == 120
    assert exc_info.value.cached_input_token_count == 20
    assert exc_info.value.output_token_count == 30


@pytest.mark.parametrize("reason", ["max_tokens", "max_output_tokens"])
def test_incomplete_output_limit_response_is_typed_as_truncation(reason: str) -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code=200,
                    request=request,
                    json={
                        "status": "incomplete",
                        "incomplete_details": {"reason": reason},
                        "output_text": '{"candidates": [',
                        "usage": {
                            "input_tokens": 2522,
                            "input_tokens_details": {"cached_tokens": 1200},
                            "output_tokens": 4000,
                        },
                    },
                )
            )
        ),
    )

    with pytest.raises(LlmOutputTruncatedError) as exc_info:
        asyncio.run(
            client.create_text_response(
                system_prompt="규칙",
                user_prompt="원문",
                max_output_tokens=4000,
            )
        )

    assert exc_info.value.incomplete_reason == reason
    assert exc_info.value.max_output_tokens == 4000
    assert exc_info.value.input_token_count == 2522
    assert exc_info.value.cached_input_token_count == 1200
    assert exc_info.value.output_token_count == 4000


def test_other_incomplete_response_is_never_treated_as_success() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code=200,
                    request=request,
                    json={
                        "status": "incomplete",
                        "incomplete_details": {"reason": "content_filter"},
                        "output_text": "{}",
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                )
            )
        ),
    )

    with pytest.raises(LlmIncompleteResponseError):
        asyncio.run(client.create_text_response(system_prompt="규칙", user_prompt="원문"))


def test_missing_response_status_is_never_treated_as_success() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code=200,
                    request=request,
                    json={
                        "output_text": "{}",
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                )
            )
        ),
    )

    with pytest.raises(LlmIncompleteResponseError):
        asyncio.run(client.create_text_response(system_prompt="규칙", user_prompt="원문"))


def test_output_at_cap_with_incomplete_json_is_typed_as_truncation() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code=200,
                    request=request,
                    json={
                        "status": "completed",
                        "output_text": '{"candidates": [',
                        "usage": {"input_tokens": 2522, "output_tokens": 4000},
                    },
                )
            )
        ),
    )

    with pytest.raises(LlmOutputTruncatedError) as exc_info:
        asyncio.run(
            client.create_text_response(
                system_prompt="규칙",
                user_prompt="원문",
                max_output_tokens=4000,
            )
        )

    assert exc_info.value.incomplete_reason == "output_token_limit_reached"


def test_complete_json_at_exact_cap_remains_a_success() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code=200,
                    request=request,
                    json={
                        "status": "completed",
                        "output_text": '{"candidates": []}',
                        "usage": {"input_tokens": 2522, "output_tokens": 4000},
                    },
                )
            )
        ),
    )

    response = asyncio.run(
        client.create_text_response(
            system_prompt="규칙",
            user_prompt="원문",
            max_output_tokens=4000,
        )
    )

    assert response.text == '{"candidates": []}'


def test_create_text_response_requires_api_key() -> None:
    client = OpenAIResponsesClient(
        api_key="",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ),
    )

    with pytest.raises(ValueError):
        asyncio.run(client.create_text_response(system_prompt="system", user_prompt="user"))


def _response(request: httpx.Request, requests: list[httpx.Request]) -> httpx.Response:
    requests.append(request)
    return httpx.Response(
        status_code=200,
        json={
            "status": "completed",
            "output_text": '{"candidates":[]}',
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
            },
        },
    )
