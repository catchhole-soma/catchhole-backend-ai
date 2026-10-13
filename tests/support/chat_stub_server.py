"""Explicit local cross-repository test entry point. Never imported by production."""

import os
import httpx
import uvicorn
from app.chatbot.client import ChatExecutionClient
from app.chatbot.http_app import create_app
from app.chatbot.outbox import Outbox
from app.chatbot.runtime import ChatRuntime, ProviderResult
from app.schemas.chat_execution import ChatResult, Finish, Usage
from app.schemas.setting_tools import SettingToolQuery


async def handler(execution):
    await execution.tools().query(SettingToolQuery(tool="FIND_CHARACTERS"))

    async def stub_provider():
        return ProviderResult(
            "stub",
            Usage(
                kind="SETTLE",
                input_tokens=12,
                cached_input_tokens=4,
                output_tokens=3,
                outcome="SUCCESS",
            ),
        )

    await execution.call("CHAT_ANSWER", "test-stub-only", 100, stub_provider)
    return Finish(
        result=ChatResult(
            outcome="INSUFFICIENT_EVIDENCE",
            text="테스트 실행 완료",
            sources=[],
            limitations=["테스트 전용 고정 핸들러입니다."],
        )
    )


def main(*, handler_override=None):
    if os.getenv("CHAT_TEST_ONLY") != "true":
        raise RuntimeError("This entry point is only for integration tests")
    key = os.environ["SPRING_INTERNAL_API_KEY"]
    runtime = ChatRuntime(
        ChatExecutionClient(os.environ["SPRING_INTERNAL_API_BASE_URL"], key, httpx.AsyncClient()),
        Outbox(os.environ["CHAT_OUTBOX_PATH"]),
        handler=handler_override or handler,
    )
    uvicorn.run(
        create_app(runtime=runtime, api_key=key),
        host="127.0.0.1",
        port=int(os.environ["CHAT_TEST_PORT"]),
        access_log=False,
    )


if __name__ == "__main__":
    main()
