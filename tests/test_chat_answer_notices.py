"""Which notices reach the author, and where the hidden ones stay traceable.

Offline scripted replays: these check publication plumbing, not model quality.
"""

import asyncio
import logging
import socket
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.chatbot.qa_render import (
    ANSWER_ONLY_NOTICES,
    NOTICES,
    PUBLIC_NOTICES,
    UNREGISTERED_NOTICE,
    message,
    publish,
)
from app.chatbot.replay import replay_case
from app.retrieval.search_contracts import SearchReadiness
from app.schemas.chat_execution import ChatResult, Finish
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid import scenario as hybrid_scenario
from tests.test_chat_qa import scenario

SEARCH_NOT_READY = "아직 검색 준비가 끝나지 않은 회차가 있어서, 작품 전체를 찾아본 결과가 아니에요."
SOURCE_UNAVAILABLE = (
    "원고를 불러오는 데 문제가 있어 일부를 확인하지 못했어요. 잠시 후 다시 질문해 주세요."
)
PARTIAL = "질문의 일부에만 답했어요."
CONFLICT = "등록된 설정과 원고 내용이 서로 달라요. 둘 다 보여 드렸으니 확인해 주세요."
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
    shown, record = published(caplog, "CLARIFICATION", ["CONVERSATION_LIMIT", "PARTIAL_RESULTS"])
    assert shown == []
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f"chat_answer_notices attempt={ATTEMPT} outcome=CLARIFICATION "
        "codes=['CONVERSATION_LIMIT', 'PARTIAL_RESULTS'] shown=[]"
    )
    assert record.notice_attempt == str(ATTEMPT)
    assert record.notice_outcome == "CLARIFICATION"
    assert record.notice_codes == ["CONVERSATION_LIMIT", "PARTIAL_RESULTS"]
    assert PRIVATE not in caplog.text
    assert not any(sentence in caplog.text for sentence in NOTICES.values())


def test_unregistered_sentence_is_dropped_and_recorded_without_its_text(caplog):
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
    assert result.limitations == []
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


# 공개 문구: 작가에게 보이는 것은 네 문장뿐이다.


def test_the_author_sees_exactly_four_sentences():
    assert set(PUBLIC_NOTICES) == {
        "PARTIAL_ANSWER",
        "SETTING_SOURCE_CONFLICT",
        "TEXT_INDEX_PARTIAL",
        "VECTOR_INDEX_PARTIAL",
        "SEARCH_UNAVAILABLE",
        "STORAGE_UNAVAILABLE",
    }
    assert set(PUBLIC_NOTICES.values()) == {PARTIAL, CONFLICT, SEARCH_NOT_READY, SOURCE_UNAVAILABLE}
    assert PUBLIC_NOTICES["TEXT_INDEX_PARTIAL"] == PUBLIC_NOTICES["VECTOR_INDEX_PARTIAL"]
    assert PUBLIC_NOTICES["SEARCH_UNAVAILABLE"] == PUBLIC_NOTICES["STORAGE_UNAVAILABLE"]
    assert ANSWER_ONLY_NOTICES == {"PARTIAL_ANSWER", "SETTING_SOURCE_CONFLICT"}
    # The result contract is unchanged: at most 20 notes of at most 200 characters.
    assert all(0 < len(note.strip()) <= 200 for note in PUBLIC_NOTICES.values())
    # A public sentence must never be mistaken for an internal one on the way out.
    assert not set(PUBLIC_NOTICES.values()) & set(NOTICES.values())


@pytest.mark.parametrize("code", sorted(set(NOTICES) - set(PUBLIC_NOTICES)))
@pytest.mark.parametrize("outcome", ["ANSWER", "INSUFFICIENT_EVIDENCE", "CLARIFICATION"])
def test_every_other_code_is_hidden_but_recorded(caplog, code, outcome):
    shown, record = published(caplog, outcome, [code])
    assert shown == [] and record.notice_shown == []
    # 대화 생략은 예전에도 되묻기 결과에만 붙었다. 그 밖의 코드는 결과 종류와 무관하게 남는다.
    untracked = code == "CONVERSATION_LIMIT" and outcome != "CLARIFICATION"
    assert record.notice_codes == ([] if untracked else [code])


@pytest.mark.parametrize("code", sorted(PUBLIC_NOTICES))
def test_public_code_is_shown_in_plain_words_on_an_answer(caplog, code):
    shown, record = published(caplog, "ANSWER", [code])
    assert shown == [PUBLIC_NOTICES[code]]
    assert record.notice_codes == record.notice_shown == [code]
    assert NOTICES[code] not in shown  # The internal wording never leaves the process.


@pytest.mark.parametrize("outcome", ["INSUFFICIENT_EVIDENCE", "CLARIFICATION"])
@pytest.mark.parametrize("code", sorted(ANSWER_ONLY_NOTICES))
def test_notes_about_a_given_answer_are_not_attached_to_a_withheld_one(caplog, outcome, code):
    # "일부에만 답했어요"·"둘 다 보여 드렸으니"는 답을 내보내지 않은 결과에서는 사실이 아니다.
    shown, record = published(caplog, outcome, [code, "STORAGE_UNAVAILABLE"])
    assert shown == [SOURCE_UNAVAILABLE]
    assert record.notice_codes == [code, "STORAGE_UNAVAILABLE"]
    assert record.notice_shown == ["STORAGE_UNAVAILABLE"]


def test_codes_sharing_a_sentence_show_it_once_and_keep_first_seen_order(caplog):
    codes = [
        "SOURCE_RANGE",
        "STORAGE_UNAVAILABLE",
        "TEXT_INDEX_PARTIAL",
        "VECTOR_INDEX_PARTIAL",
        "INDEX_PARTIAL",
        "SEARCH_UNAVAILABLE",
        "PARTIAL_RESULTS",
        "SETTING_SOURCE_CONFLICT",
        "PARTIAL_ANSWER",
        "TEXT_INDEX_PARTIAL",
    ]
    shown, record = published(caplog, "ANSWER", codes)
    assert shown == [SOURCE_UNAVAILABLE, SEARCH_NOT_READY, CONFLICT, PARTIAL]
    assert record.notice_codes == codes[:-1]
    assert record.notice_shown == [
        "STORAGE_UNAVAILABLE",
        "TEXT_INDEX_PARTIAL",
        "VECTOR_INDEX_PARTIAL",
        "SEARCH_UNAVAILABLE",
        "SETTING_SOURCE_CONFLICT",
        "PARTIAL_ANSWER",
    ]


def test_no_episode_in_scope_is_not_reported_as_unfinished_search_preparation(caplog):
    # 허용 범위에 회차가 하나도 없으면 INDEX_PARTIAL도 함께 뜬다. 준비가 덜 끝난 회차는 없다.
    shown, record = published(
        caplog, "INSUFFICIENT_EVIDENCE", ["NO_SUPPORTED_SOURCES", "INDEX_PARTIAL"]
    )
    assert shown == []
    assert record.notice_codes == ["NO_SUPPORTED_SOURCES", "INDEX_PARTIAL"]


@pytest.mark.anyio
async def test_hidden_code_leaves_the_process_only_in_the_diagnostic_record():
    case, catalog = scenario()
    result = await replay_case(case, catalog, config={"page_size": 1, "max_pages": 1})
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert result["finish"]["result"]["limitations"] == []
    assert result["noticeCodes"] == ["PARTIAL_RESULTS"]


@pytest.mark.anyio
async def test_unverifiable_quote_storage_failure_is_told_in_plain_words():
    case, catalog = scenario()
    catalog[0]["evidence"][0]["evidence"]["status"] = "STORAGE_UNAVAILABLE"
    result = await replay_case(case, catalog)
    assert result["finish"]["result"]["limitations"] == [SOURCE_UNAVAILABLE]
    assert "STORAGE_UNAVAILABLE" in result["noticeCodes"]


def test_search_failure_is_told_in_plain_words(tmp_path):
    def setup(search):
        search.failures["keyword"] = SQLAlchemyError("sensitive database details")

    result = asyncio.run(replay(tmp_path, search_setup=setup))
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert result["finish"]["result"]["limitations"] == [SOURCE_UNAVAILABLE]
    assert "SEARCH_UNAVAILABLE" in result["noticeCodes"]


def test_registered_value_and_source_conflict_is_told_in_plain_words(tmp_path):
    case = hybrid_scenario("mixed_current_and_scene")
    case["outputs"][-1]["conflicts"] = [{"rowRefs": ["R1"], "evidenceRefs": ["E2"]}]
    result = asyncio.run(replay(tmp_path, case))
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert CONFLICT in result["finish"]["result"]["limitations"]
    assert "SETTING_SOURCE_CONFLICT" in result["noticeCodes"]
    assert set(result["finish"]["result"]["limitations"]) <= set(PUBLIC_NOTICES.values())


def test_work_without_episodes_in_scope_gets_no_search_preparation_note(tmp_path):
    def no_sources(search):
        search.keyword_chunks = []
        search.readiness = SearchReadiness(0, 0, 0, 0, 0, ("NO_SUPPORTED_SOURCES",))

    result = asyncio.run(replay(tmp_path, search_setup=no_sources, hybrid={"max_rounds": 1}))
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert result["finish"]["result"]["limitations"] == []
    assert {"NO_SUPPORTED_SOURCES", "INDEX_PARTIAL"} <= set(result["noticeCodes"])


def test_withheld_partial_answer_is_not_described_as_partly_answered(tmp_path):
    # 부분 답변을 내보내기 직전에 원문이 바뀌면 답을 보류한다. 남은 PARTIAL_ANSWER 코드는
    # 기록에만 있고 "일부에만 답했어요"로 나가지 않는다.
    case = hybrid_scenario()
    case["outputs"][1]["coverage"] = "PARTIAL"

    def change_source(count, spring, search):
        if count == 2:
            search.changed = True

    result = asyncio.run(replay(tmp_path, case, hybrid={"max_rounds": 1}, model_hook=change_source))
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert {"PARTIAL_ANSWER", "SOURCE_CHANGED"} <= set(result["noticeCodes"])
    assert result["finish"]["result"]["limitations"] == []


def test_answer_model_still_reads_the_internal_notices(tmp_path):
    # 공개 문구를 줄여도 답변 모델의 입력은 바뀌지 않는다.
    case = hybrid_scenario()
    case["readiness"] = "partial"
    result = asyncio.run(replay(tmp_path, case))
    model_input = result["raw"].requests[-1]["input"]["limitations"]
    assert NOTICES["INDEX_PARTIAL"] in model_input
    assert NOTICES["TEXT_INDEX_PARTIAL"] in model_input
    assert NOTICES["NARRATIVE_SOURCE_RANGE"] in model_input
    assert not set(PUBLIC_NOTICES.values()) & set(model_input)
    assert result["finish"]["result"]["limitations"] == [SEARCH_NOT_READY]
