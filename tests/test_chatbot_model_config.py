"""Chat-specific model settings reach Responses without inheriting worker settings."""

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from app.chatbot.hybrid_prompts import PLAN_PROMPT as HYBRID_PLAN_PROMPT
from app.chatbot.qa import ChatQaHandler, _Session, response_schema
from app.chatbot.qa_config import ChatQaSettings
from app.chatbot.qa_prompts import PLAN_PROMPT
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.chat_hybrid import HybridPlan
from app.schemas.chat_qa import QuestionPlan
from app.usage import metering


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Chat model config test cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


@pytest.mark.parametrize("override", [None, "gpt-6-sol"])
def test_chat_model_and_reasoning_reach_structured_response_request(monkeypatch, override):
    monkeypatch.delenv("CHAT_QA_MODEL", raising=False)
    monkeypatch.delenv("CHAT_QA_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("LLM_MODEL", "worker-only-model")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    if override is not None:
        monkeypatch.setenv("CHAT_QA_MODEL", override)
    settings = ChatQaSettings()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output_text": "{}"})

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenAIResponsesClient(
                api_key="synthetic-only",
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                responses_api_url="https://provider.test/v1/responses",
                http_client=http,
            )
            await client.create_text_response(
                "Return the requested schema.",
                "Synthetic question.",
                max_output_tokens=settings.max_output_tokens,
                response_schema=response_schema(QuestionPlan),
            )

    asyncio.run(invoke())
    assert len(requests) == 1
    assert requests[0]["model"] == (override or "gpt-6-luna")
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["text"]["format"]["type"] == "json_schema"
    assert requests[0]["text"]["format"]["strict"] is True
    assert requests[0]["store"] is False


@pytest.mark.parametrize(
    ("prompt", "plan"), [(PLAN_PROMPT, QuestionPlan), (HYBRID_PLAN_PROMPT, HybridPlan)]
)
def test_default_planner_budget_accepts_conservative_unknown_tokenizer_estimate(
    monkeypatch, prompt, plan
):
    def unknown_encoding(model):
        assert model == "gpt-6-luna"
        raise KeyError(model)

    monkeypatch.setattr(metering, "_encoding_for_model", unknown_encoding)
    for name in ChatQaSettings.model_fields:
        monkeypatch.delenv(f"CHAT_QA_{name.upper()}", raising=False)
    context = SimpleNamespace(
        question="아리아의 현재 능력과 직접 근거를 알려줘",
        episode_from=None,
        episode_to=None,
        conversation=[],
        conversation_truncated=False,
    )
    execution = SimpleNamespace(context=context, tools=lambda: None)
    session = _Session(ChatQaHandler(object(), ChatQaSettings()), execution)
    schema = response_schema(plan)
    payload = session.plan_payload(prompt, schema)
    assert session.fits(prompt, payload, schema)
