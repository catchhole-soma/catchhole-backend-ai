import json

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient


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
