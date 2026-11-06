"""Spring 채팅 경로의 본문 상한(413)은 다시 보내지 않는 실행 한도 실패다.

인용을 많이 싣는 원문 탐색 답변이 CHAT_MAX_BODY_BYTES보다 큰 등록·완료 본문을 만들 수 있다. 같은
본문은 다시 보내도 받지 않으므로 재전송하지 않고, 실행은 한도 초과 실패로 끝나야 한다. 합성 본문과
transport 대역만 쓰며 실제 Spring 필터와의 연결은 검증하지 않는다.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.chatbot.hybrid_prompts import EXPLORE_VERSION, PLAN_VERSION
from app.chatbot.outbox import Outbox
from app.schemas.chat_execution import ChatResult, Dispatch, Finish
from tests.test_chat_agent_retrieval import agent_replay, plan_output, seed_compass, step

LIMIT_FAILURE = {"result": None, "failure": "EXECUTION_LIMIT_EXCEEDED"}


def dispatch(seconds=60):
    return Dispatch(
        schema_version=1,
        attempt_id=uuid4(),
        capability="a" * 64,
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def too_large(with_body=True):
    if not with_body:
        return httpx.Response(413, content=b"")
    return httpx.Response(
        413,
        json={"success": False, "error": {"code": "REQUEST_BODY_TOO_LARGE", "status": 413}},
    )


@pytest.mark.parametrize("with_body", [True, False])
def test_body_limit_response_is_a_terminal_execution_limit(with_body):
    async def run():
        transport = httpx.MockTransport(lambda request: too_large(with_body))
        async with httpx.AsyncClient(transport=transport) as http:
            client = ChatExecutionClient("http://spring.test", "synthetic-internal-key", http)
            with pytest.raises(ChatTransportError) as caught:
                await client.retrieval_sources(dispatch(), uuid4(), [])
            return caught.value

    error = asyncio.run(run())
    assert (error.status, error.code, error.terminal) == (413, "EXECUTION_LIMIT_EXCEEDED", True)
    # 다른 상태는 그대로다. 429는 응답의 고정 코드 없이는 잔액 부족·한도 초과로 추정하지 않는다.
    assert ChatTransportError(429).terminal is False and ChatTransportError(429).code is None
    assert ChatTransportError(503).terminal is False
    assert ChatTransportError(409).terminal is True


class LimitedSpring:
    """본문이 limit 바이트를 넘는 보고를 413으로 거절하는 대역."""

    def __init__(self, limit):
        self.limit, self.attempts, self.accepted = limit, [], []

    async def request(self, d, runner, action, body, **kwargs):
        size = len(json.dumps(body, ensure_ascii=False).encode())
        self.attempts.append((action, size))
        if size > self.limit:
            raise ChatTransportError(413, "EXECUTION_LIMIT_EXCEEDED")
        self.accepted.append((action, body))
        return {}


def test_oversized_finish_report_is_replaced_by_a_small_failure_and_never_resent(tmp_path):
    answer = Finish(
        result=ChatResult(outcome="ANSWER", text="가" * 4000, sources=[], limitations=[])
    ).model_dump(by_alias=True, mode="json")
    usage = {"kind": "UNKNOWN"}
    d, runner = dispatch(), uuid4()
    spring = LimitedSpring(limit=1000)
    box = Outbox(str(tmp_path / "outbox.sqlite3"))
    try:
        box.put(f"finish:{d.attempt_id}", d, runner, "finish", answer, d.deadline.timestamp())
        box.put("call-1", d, runner, "calls/call-1", usage, d.deadline.timestamp() + 86400)
        asyncio.run(box.flush(spring))
        # 큰 완료 보고는 받지 않으므로 버리고, 같은 자리에 작은 실패 보고를 남긴다. 사용량 보고는 그대로 간다.
        assert spring.accepted == [("calls/call-1", usage)]
        assert box.count() == 1
        stored = box.db.execute("SELECT payload FROM reports").fetchone()[0]
        assert json.loads(stored) == LIMIT_FAILURE and "가" not in stored
        asyncio.run(box.flush(spring))
        assert spring.accepted[-1] == ("finish", LIMIT_FAILURE) and box.count() == 0
        asyncio.run(box.flush(spring))
        # 큰 본문은 한 번만 보냈다.
        assert [action for action, size in spring.attempts if size > 1000] == ["finish"]
        assert len(spring.attempts) == 3
    finally:
        box.close()


def test_body_limit_on_small_reports_is_dropped_without_a_retry_loop(tmp_path):
    d, runner = dispatch(), uuid4()
    spring = LimitedSpring(limit=1)  # 실패 보고와 사용량 보고까지 모두 거절한다
    box = Outbox(str(tmp_path / "outbox.sqlite3"))
    try:
        box.put(
            f"finish:{d.attempt_id}", d, runner, "finish", LIMIT_FAILURE, d.deadline.timestamp()
        )
        box.put("call-1", d, runner, "calls/call-1", {"kind": "UNKNOWN"}, d.deadline.timestamp())
        asyncio.run(box.flush(spring))
        # 이미 실패 보고인 완료 보고는 바꿀 것이 없으므로 버린다. Spring의 마감 복구가 실행을 닫는다.
        assert box.count() == 0 and spring.accepted == []
        asyncio.run(box.flush(spring))
        assert len(spring.attempts) == 2
    finally:
        box.close()


def test_body_limit_during_citation_registration_ends_as_an_execution_limit(tmp_path):
    def reject_registration(spring):
        original = spring.respond

        def respond(request):
            if request.url.path.endswith("/retrieval-sources"):
                spring.receipts.append(json.loads(request.content))
                return too_large()
            return original(request)

        spring.respond = respond

    def setup(search):
        seed_compass(search)

    outputs = {PLAN_VERSION: [plan_output()], EXPLORE_VERSION: [step(enough=True)]}
    result = asyncio.run(
        agent_replay(tmp_path, outputs, search_setup=setup, spring_setup=reject_registration)
    )
    # 네트워크 실패가 아니라 실행 한도 초과로 끝나고, 등록 요청은 한 번만 보냈다.
    assert result["finish"] == LIMIT_FAILURE
    assert len(result["spring"].receipts) == 1
