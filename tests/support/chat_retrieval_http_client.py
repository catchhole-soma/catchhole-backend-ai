"""GH240 integration client: real Spring HTTP, synthetic source, no model provider."""

import asyncio
import json
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.chatbot.client import ChatExecutionClient
from app.schemas.chat_execution import ChatResult, Dispatch, Finish
from app.schemas.chat_retrieval import RetrievalReference


async def main():
    if os.getenv("CHAT_TEST_ONLY") != "true":
        raise RuntimeError("Test-only client")
    base_url = os.environ["SPRING_INTERNAL_API_BASE_URL"]
    if urlsplit(base_url).hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Local synthetic Spring server required")
    payload = json.load(sys.stdin)
    dispatch = Dispatch.model_validate(payload["dispatch"])
    sources = [RetrievalReference.model_validate(item) for item in payload["sources"]]
    async with httpx.AsyncClient() as http:
        client = ChatExecutionClient(base_url, os.environ["SPRING_INTERNAL_API_KEY"], http)
        runner = uuid4()
        await client.start(dispatch, runner)
        verified = await client.retrieval_sources(dispatch, runner, sources)
        finish = Finish(
            result=ChatResult(
                outcome="ANSWER",
                text="검증한 원문 인용입니다. [E1]",
                sources=[],
                limitations=[],
                retrieval_sources=verified,
            )
        )
        view = await client.request(
            dispatch, runner, "finish", finish.model_dump(by_alias=True, mode="json")
        )
        assert view["state"] == "SUCCEEDED"
        assert view["result"]["retrievalSources"] == [
            source.model_dump(by_alias=True, mode="json") for source in sources
        ]
        print(json.dumps({"state": view["state"], "verifiedSources": len(verified)}))


if __name__ == "__main__":
    asyncio.run(main())
