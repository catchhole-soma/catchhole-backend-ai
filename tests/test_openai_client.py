import json
import logging

import httpx
import pytest

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
        client.http_client.close()


def test_create_text_response_calls_openai_responses_api() -> None:
    requests: list[httpx.Request] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: _response(request, requests))),
    )

    response = client.create_text_response(
        system_prompt="JSON만 반환하세요.",
        user_prompt="원문",
        max_output_tokens=100,
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
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level(logging.DEBUG, logger="app.llm.openai_client"):
        response = client.create_text_response(
            system_prompt="규칙",
            user_prompt="원문",
            prompt_cache_key="setting-extraction:v1:abc123",
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
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: _response(request, requests))
        ),
    )

    client.create_text_response(system_prompt="규칙", user_prompt="원문")

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
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: _response(request, requests))
        ),
    )

    client.create_text_response(
        system_prompt="규칙",
        user_prompt="원문",
        model="gpt-4.1-mini",
    )

    request_body = json.loads(requests[0].content)
    assert request_body["model"] == "gpt-4.1-mini"
    assert "reasoning" not in request_body


def test_create_text_response_requires_api_key() -> None:
    client = OpenAIResponsesClient(
        api_key="",
        model="gpt-4.1-mini",
        responses_api_url="https://api.openai.test/v1/responses",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )

    with pytest.raises(ValueError):
        client.create_text_response(system_prompt="system", user_prompt="user")


def _response(request: httpx.Request, requests: list[httpx.Request]) -> httpx.Response:
    requests.append(request)
    return httpx.Response(
        status_code=200,
        json={
            "output_text": '{"candidates":[]}',
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
            },
        },
    )
