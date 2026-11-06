"""Where the notice codes behind a chat result stay traceable.

Offline scripted replays: these check record plumbing, not model quality.
"""

import asyncio
import logging
import socket
from uuid import UUID

import pytest

from app.chatbot.qa_render import NOTICES, UNREGISTERED_NOTICE, message, publish
from app.chatbot.replay import replay_case
from app.schemas.chat_execution import ChatResult, Finish
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid import scenario as hybrid_scenario
from tests.test_chat_qa import scenario

ATTEMPT = UUID(int=7001)
PRIVATE = "비공개 합성 질문과 답변 본문"
LOGGER = "app.chatbot.qa_render"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Notice publication tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def published(caplog, outcome, codes):
    """Publish one internal result and return (published sentences, the diagnostic record)."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        finish = publish(message(outcome, PRIVATE, codes), ATTEMPT)
    records = [r for r in caplog.records if r.name == LOGGER]
    assert len(records) == 1
    return finish.result.limitations, records[0]


# 진단 기록: 결과에 붙은 안내 코드를 실행마다 이름으로 남긴다.


def test_record_names_codes_only_and_never_copies_text(caplog):
    _, record = published(caplog, "CLARIFICATION", ["CONVERSATION_LIMIT", "PARTIAL_RESULTS"])
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f"chat_answer_notices attempt={ATTEMPT} outcome=CLARIFICATION "
        "codes=['CONVERSATION_LIMIT', 'PARTIAL_RESULTS']"
    )
    assert record.notice_attempt == str(ATTEMPT)
    assert record.notice_outcome == "CLARIFICATION"
    assert record.notice_codes == ["CONVERSATION_LIMIT", "PARTIAL_RESULTS"]
    assert PRIVATE not in caplog.text
    assert not any(sentence in caplog.text for sentence in NOTICES.values())


def test_unregistered_sentence_is_recorded_without_its_text(caplog):
    finish = Finish(
        result=ChatResult(
            outcome="ANSWER",
            text=PRIVATE,
            sources=[],
            limitations=["표에 없는 임의 문장", NOTICES["PARTIAL_RESULTS"], "또 다른 임의 문장"],
        )
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = publish(finish, ATTEMPT).result
    assert result.text == PRIVATE and result.outcome == "ANSWER"
    record = caplog.records[-1]
    assert record.notice_codes == [UNREGISTERED_NOTICE, "PARTIAL_RESULTS"]
    assert "임의 문장" not in caplog.text


def test_failure_has_no_notices_to_publish_or_record(caplog):
    finish = Finish(failure="PROVIDER_FAILURE")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert publish(finish, ATTEMPT) is finish
    assert not [r for r in caplog.records if r.name == LOGGER]


@pytest.mark.anyio
async def test_replay_reads_the_codes_of_the_finished_result_from_the_record():
    case, catalog = scenario()
    result = await replay_case(case, catalog, config={"page_size": 1, "max_pages": 1})
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert result["noticeCodes"] == ["PARTIAL_RESULTS"]
    assert "PARTIAL_RESULTS" in result["noticeCodes"]
    assert "CONTEXT_LIMIT" not in result["noticeCodes"]
    # 없는 코드 이름으로는 "뜨지 않았다"는 단언이 통과하지 못한다.
    with pytest.raises(KeyError):
        assert "PARTIAL_RESULT" not in result["noticeCodes"]


def test_hybrid_replay_reads_the_codes_and_a_failure_has_none(tmp_path):
    case = hybrid_scenario()
    case["readiness"] = "partial"
    result = asyncio.run(replay(tmp_path, case))
    assert result["noticeCodes"] == ["TEXT_INDEX_PARTIAL", "VECTOR_INDEX_PARTIAL", "INDEX_PARTIAL"]

    def reject(spring):
        spring.reserve_status = 429
        spring.reserve_code = "AI_TOKEN_QUOTA_EXHAUSTED"

    failed = asyncio.run(replay(tmp_path, spring_setup=reject))
    assert failed["finish"]["failure"] == "QUOTA_EXHAUSTED"
    assert failed["noticeCodes"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [True, False])
async def test_http_process_writes_the_record_only_when_qa_is_enabled(
    tmp_path, monkeypatch, enabled
):
    from app.chatbot.http_app import create_app

    target = logging.getLogger(LOGGER)
    before = (target.level, target.propagate, list(target.handlers))
    monkeypatch.setenv("CHAT_QA_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CHAT_RETRIEVAL_ENABLED", "false")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-not-a-provider-key")
    monkeypatch.setenv("CHAT_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("SPRING_INTERNAL_API_BASE_URL", "http://spring.test")
    app = create_app(api_key="synthetic-internal-key")
    async with app.router.lifespan_context(app):
        added = [h for h in target.handlers if h not in before[2]]
        assert len(added) == int(enabled)
        if enabled:
            assert target.level == logging.INFO and target.propagate is False
    assert (target.level, target.propagate, list(target.handlers)) == before
