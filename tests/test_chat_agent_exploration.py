"""합성 단위 회귀: 원문 탐색의 응답 검증·읽은 묶음 예산·회차 통째 읽기·단계 한도.

모델·검색·DB 없이 세션 메서드만 부른다. 지어낸 조각으로 한도 계산과 분기를 확인하며 검색 품질은
측정하지 않는다.
"""

import asyncio
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.chatbot import qa as qa_module
from app.chatbot.hybrid import _Evidence, _HybridSession
from app.chatbot.hybrid_agent import EPISODE_READ_LIMIT, POOL_CHUNK_LIMIT
from app.chatbot.hybrid_metrics import HybridMetrics
from app.chatbot.qa import ModelTimeBudgetExceeded
from app.chatbot.runtime import ProviderResult
from app.llm.responses import LlmTextResponse
from app.schemas.chat_execution import Usage
from app.schemas.chat_hybrid import (
    RETRIEVAL_SOURCE_LIMIT,
    ExplorationStep,
    GroundedAnswer,
    GroundedClaim,
)
from tests.test_chat_agent_retrieval import (
    QUESTION,
    STORY,
    agent_hybrid,
    agent_qa,
    step,
    story_chunk,
    story_chunks,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("이 회귀는 외부 서비스에 접근하지 않는다")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def agent_session(*, hybrid=None, config=None, seconds=600, max_calls=24, max_tool_calls=60):
    context = SimpleNamespace(
        question=QUESTION,
        episode_from=None,
        episode_to=None,
        conversation=[],
        conversation_truncated=False,
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
        max_calls=max_calls,
        max_tool_calls=max_tool_calls,
        attempt_id=None,
    )
    execution = SimpleNamespace(context=context, tools=lambda: None, calls=0)
    handler = SimpleNamespace(
        settings=agent_qa(**(config or {})),
        hybrid=agent_hybrid(**(hybrid or {})),
        metrics=HybridMetrics(),
    )
    session = _HybridSession(handler, execution)
    session.agent_active = True
    session.agent_span = (1, 8, 8)
    return session


def tokens(session, *chunks):
    return sum(session.agent_count_tokens(c.text) for c in chunks)


def test_exploration_step_schema_bounds_model_chosen_actions():
    ExplorationStep.model_validate(step(searches=["가"] * 6, read=[1, 2, 3, 4, 5]))
    ExplorationStep.model_validate(step(enough=True))
    for invalid in (
        step(searches=["가"] * 7),
        step(read=[1, 2, 3, 4, 5, 6]),
        step(read=[0]),
        step(read=[2147483648]),
        step(searches=["가" * 201]),
        {**step(), "unsupportedParts": ["P" * 21]},
        {**step(), "unsupportedParts": ["P1"] * 5},
        {**step(), "note": "가" * 601},
        {**step(), "enough": "true"},
        {**step(), "searchOrder": "RANDOM"},
        {**step(), "workId": "other"},
        {k: v for k, v in step().items() if k != "readEpisodes"},
    ):
        with pytest.raises(ValidationError):
            ExplorationStep.model_validate(invalid)


def test_actions_are_cut_to_server_limits_and_the_readable_range():
    session = agent_session(hybrid={"agent_max_searches": 2, "agent_max_read_episodes": 2})
    session.agent_read = {2: "FULL", 4: "MISSING", 5: "PARTIAL"}
    chosen = ExplorationStep.model_validate(
        step(
            searches=["  등대   문 ", "등대 문", "", "가" * 200, "지도"],
            read=[2, 4, 9, 5, 3],
            order="EARLIEST",
        )
    )
    searches, order, episodes = session.agent_actions(chosen)
    # 공백을 정리하고 같은 검색어는 한 번만, 120자 넘는 검색어는 잘라 설정 개수만큼만 실행한다.
    assert searches == ["등대 문", "가" * 120]
    assert order == "EARLIEST"
    # 범위 밖(9화), 통째로 읽은 회차(2화), 없는 회차(4화)는 빼고 일부만 읽은 회차(5화)는 다시 읽는다.
    assert episodes == [5, 3]


def test_a_repeated_search_runs_again_only_while_matching_chunks_remain_unread():
    session = agent_session()
    session.agent_history = [
        {"query": "나침반", "order": "RELEVANCE", "hits": [(1, 2), (2, 2)], "keywordNew": 1},
        {"query": "지도", "order": "RELEVANCE", "hits": [(4, 1)], "keywordNew": 1},
    ]
    session.agent_pool_add([story_chunk(1, 0), story_chunk(4, 0)])
    chosen = ExplorationStep.model_validate(step(searches=["나침반", "지도", "지도"]))
    # '지도'는 일치 조각을 모두 읽었고, '나침반'은 읽지 않은 일치 조각이 남아 이어 읽는다.
    assert session.agent_actions(chosen)[0] == ["나침반"]
    view = session.agent_history_view()
    assert view[0]["unread"] == "1×1 2×2" and view[1]["unread"] == ""
    # 정렬이 다르면 다른 검색이다.
    latest = ExplorationStep.model_validate(step(searches=["지도"], order="LATEST"))
    assert session.agent_actions(latest)[0] == ["지도"]


def test_pool_budget_skips_chunks_without_cutting_text_and_counts_them():
    chunks = story_chunks()
    session = agent_session()
    budget = tokens(session, chunks[0], chunks[1]) + 1
    session = agent_session(hybrid={"agent_pool_tokens": 2000})
    session.hcfg = session.hcfg.model_copy(update={"agent_pool_tokens": budget})
    added, omitted, spent = session.agent_pool_add(chunks[:4])
    assert (added, omitted) == (2, 2)
    assert spent == session.agent_pool_tokens == budget - 1
    assert [e.ref for e in session.evidences] == ["E1", "E2"]
    assert [e.text for e in session.evidences] == [chunks[0].text, chunks[1].text]
    assert "EVIDENCE_CONTEXT_LIMIT" in session.codes
    # 이미 읽은 조각은 다시 세지 않고 예산도 쓰지 않는다.
    assert session.agent_pool_add(chunks[:2]) == (0, 0, 0)


def test_step_allowance_and_search_share_leave_room_for_whole_episode_reads():
    chunks = story_chunks()
    session = agent_session(hybrid={"agent_pool_tokens": 2000, "agent_search_share": 0.5})
    assert session.agent_search_room() == 1000
    one = tokens(session, chunks[0])
    added, omitted, spent = session.agent_pool_add(chunks[:3], allowance=one)
    assert (added, omitted, spent) == (1, 2, one)
    session.agent_search_tokens += spent
    assert session.agent_search_room() == 1000 - one
    # 검색 몫을 다 써도 회차 통째 읽기는 묶음 전체 예산까지 쓸 수 있다.
    session.agent_search_tokens = 1000
    assert session.agent_search_room() == 0
    assert session.agent_pool_add(chunks[1:3])[0] == 2
    # 조각 수 상한을 넘으면 예산이 남아도 넣지 않는다.
    session.agent_pool = [chunks[0]] * POOL_CHUNK_LIMIT
    assert session.agent_pool_add(chunks[3:4]) == (0, 1, 0)


def read_with(session, chunks):
    calls = []

    async def search(method, numbers, limit):
        calls.append((method, list(numbers), limit))
        return [replace(c, methods=("EPISODE",)) for c in chunks]

    session.search = search
    return calls


def test_whole_episode_read_marks_full_partial_and_missing_episodes():
    session = agent_session()
    chunks = [c for c in story_chunks() if c.episode_no in {3, 4}]
    calls = read_with(session, chunks)
    asyncio.run(session.agent_read_episodes([3, 4, 6]))
    assert calls == [("episodes", [3, 4, 6], EPISODE_READ_LIMIT)]
    # 조각을 모두 넣은 회차는 FULL, 조각이 하나도 없는 회차는 MISSING이다.
    assert session.agent_read == {3: "FULL", 4: "FULL", 6: "MISSING"}
    assert [e.chunk.methods for e in session.evidences] == [("EPISODE",)] * 4

    tight = agent_session()
    tight.hcfg = tight.hcfg.model_copy(update={"agent_pool_tokens": tokens(tight, chunks[0])})
    read_with(tight, chunks)
    asyncio.run(tight.agent_read_episodes([3, 4]))
    # 예산 때문에 일부만 들어간 회차는 PARTIAL이라 통째로 읽은 회차로 알리지 않는다.
    assert tight.agent_read == {3: "PARTIAL", 4: "PARTIAL"}
    assert len(tight.agent_pool) == 1


def test_a_truncated_read_keeps_later_episodes_requestable():
    session = agent_session()
    episode = story_chunk(3, 0)
    many = [
        replace(episode, chunk_id=replace_id(n), chunk_index=n, episode_no=3 if n < 60 else 4)
        for n in range(EPISODE_READ_LIMIT)
    ]
    read_with(session, many)
    asyncio.run(session.agent_read_episodes([3, 4, 5]))
    # 개수 상한에서 잘리면 마지막으로 받은 회차는 일부만, 그 뒤 회차는 아직 읽지 않은 것이다.
    # 없는 회차로 표시하면 다시 요청할 수 없으므로 표시하지 않는다.
    assert session.agent_read == {3: "FULL", 4: "PARTIAL"}
    chosen = ExplorationStep.model_validate(step(read=[3, 4, 5]))
    assert session.agent_actions(chosen)[2] == [4, 5]


def replace_id(number):
    return UUID(int=880000 + number)


def test_no_tool_budget_means_no_episode_read():
    session = agent_session(max_tool_calls=3)
    session.tool_count = 3
    calls = read_with(session, story_chunks())
    asyncio.run(session.agent_read_episodes([3]))
    assert calls == [] and session.agent_read == {} and "SEARCH_LIMIT" in session.codes


def test_tool_reserve_covers_registration_batches_and_final_checks():
    session = agent_session()
    assert session.agent_tail_tools() == 3
    session.agent_pool = [replace(story_chunk(1, 0), chunk_id=replace_id(n)) for n in range(61)]
    assert session.agent_tail_tools() == 5
    session.agent_registered = {c.chunk_id for c in session.agent_pool}
    assert session.agent_tail_tools() == 3
    assert session.agent_min_tools() == 13
    plan = SimpleNamespace(source_need="SCENE", ordering="EARLIEST")
    assert session.source_tool_reserve(plan) == 14
    assert session.source_tool_reserve(SimpleNamespace(source_need="NONE", ordering="NONE")) == 0


def test_each_limit_alone_stops_a_new_exploration_step():
    def ready(**kwargs):
        session = agent_session(**kwargs)
        assert isinstance(session.agent_can_step(), bool)
        return session

    assert ready().agent_can_step()

    session = ready()
    session.agent_steps = session.hcfg.agent_max_steps
    assert not session.agent_can_step()

    # 탐색 호출 1 + 답변(추출 1 + 작성 1)을 부를 모델 호출 몫이 있어야 한다.
    session = ready(hybrid={"max_model_calls": 4})
    session.model_calls = 1
    assert session.agent_can_step()
    session.model_calls = 2
    assert not session.agent_can_step()

    # 서버 실행의 호출 상한에는 임베딩 호출 하나를 더 센다.
    session = ready(max_calls=5)
    session.execution.calls = 1
    assert session.agent_can_step()
    session.execution.calls = 2
    assert not session.agent_can_step()

    # 읽은 원문이 많으면 추출 묶음 수만큼 답변 호출 몫이 늘어난다.
    session = ready(hybrid={"max_model_calls": 6})
    session.agent_pool_tokens = 45000
    assert session.agent_answer_calls() == 6
    assert not session.agent_can_step()
    session.hcfg = session.hcfg.model_copy(update={"agent_extract": False})
    assert session.agent_answer_calls() == 1 and session.agent_can_step()

    # 마감에서 마무리 10초와 답변 몫 75초, 탐색 최소 8초를 빼고 남아야 한다.
    assert not ready(seconds=92).agent_can_step()
    assert ready(seconds=96).agent_can_step()
    session = ready(seconds=190)
    session.agent_answer_seconds = 150  # 직전 답변이 오래 걸렸으면 그 1.2배(180초)를 남긴다
    assert not session.agent_can_step()

    session = ready()
    session.tool_count = session.cfg.max_tool_calls - 4  # 조회 2 + 마무리 3이 안 남는다
    assert not session.agent_can_step()

    session = ready()
    session.agent_pool_tokens = session.hcfg.agent_pool_tokens - 399
    assert not session.agent_can_step()

    session = ready(hybrid={"agent_explore_seconds": 30})
    session.agent_started -= 31
    assert not session.agent_can_step()
    session = ready(hybrid={"agent_explore_seconds": 0})  # 0이면 탐색 시간 상한이 없다
    session.agent_started -= 500
    assert session.agent_can_step()


def test_zero_read_episodes_setting_never_reads_whole_episodes_as_a_seed():
    async def run(maximum):
        session = agent_session(hybrid={"agent_max_read_episodes": maximum})
        session.agent_span = (3, 3, 1)
        called = []

        async def execute(searches, order, episodes):
            called.append(("read", episodes))
            return 0

        async def seed_search(plan, query, lists):
            called.append(("search", query))

        async def collect(lists, *, label):
            return 0

        session.agent_execute, session.agent_seed_search = execute, seed_search
        session.agent_collect = collect
        plan = SimpleNamespace(search_terms=["등대"])
        await session.agent_seed(plan)
        return called

    assert asyncio.run(run(1)) == [("read", [3])]
    assert asyncio.run(run(0)) == [("search", "등대")]


def test_answers_citing_more_chunks_than_the_result_contract_are_not_published():
    session = agent_session()
    base = story_chunk(1, 0)
    session.evidences = [
        _Evidence(f"E{n}", base.text, 1, chunk=replace(base, chunk_id=replace_id(n)))
        for n in range(1, RETRIEVAL_SOURCE_LIMIT + 2)
    ]

    def answer(count):
        # schema는 문장 하나에 인용 6개까지만 받으므로 문장을 나눠 61개 조각을 인용한다.
        refs = [f"E{n}" for n in range(1, count + 1)]
        claims = [
            GroundedClaim(text="합성 문장", evidence_refs=refs[i : i + 6])
            for i in range(0, count, 6)
        ]
        return GroundedAnswer.model_construct(
            coverage="COMPLETE",
            claims=claims,
            conflicts=[],
            unanswered_row_refs=[],
            item_answers=[],
            missing_facets=[],
            missing_explanations=[],
        )

    result = asyncio.run(session.finish_answer(answer(RETRIEVAL_SOURCE_LIMIT + 1)))
    assert result.result.outcome == "INSUFFICIENT_EVIDENCE"
    assert session.tool_count == 0  # 재검증·등록을 시작하기 전에 멈춘다
    assert any("답변 길이 한도" in text for text in result.result.limitations)


class OneShotExecution:
    """예약·정산 없이 호출 수만 세는 실행 대역. time_limit 검사 전용이다."""

    def __init__(self, context):
        self.context, self.calls = context, 0

    def tools(self):
        return None

    async def call(self, purpose, model, maximum, invoke, **kwargs):
        self.calls += 1
        result = await invoke()
        assert isinstance(result, ProviderResult) and isinstance(result.usage, Usage)
        return result.value


def test_call_time_limit_covers_retries_and_output_cap_doubles_only_on_repair(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(qa_module.time, "monotonic", lambda: clock["now"])
    requests = []

    class Delegate:
        def __init__(self, outputs, seconds):
            self.outputs, self.seconds = list(outputs), seconds

        async def create_text_response(self, system_prompt, user_prompt, **kwargs):
            requests.append(kwargs["max_output_tokens"])
            clock["now"] += self.seconds
            return LlmTextResponse(self.outputs.pop(0), 10, 0, 5)

    def session_with(delegate):
        session = agent_session()
        session.execution = OneShotExecution(session.execution.context)
        session.handler.delegate = delegate
        return session

    valid = json.dumps(step(enough=True), ensure_ascii=False)

    async def call(session, **kwargs):
        return await session.model(
            ExplorationStep, "합성 지시", {"question": "합성"}, "CHAT_INTERPRETATION", "v", **kwargs
        )

    # 첫 시도가 형식 오류로 3초를 쓰면 5초 한도 안의 남은 2초로 한 번 더 시도한다.
    session = session_with(Delegate(["{}", valid], 3))
    parsed = asyncio.run(call(session, output_tokens=3000, time_limit=5))
    assert parsed.enough is True and session.execution.calls == 2
    assert requests == [3000, 6000]

    # 첫 시도가 한도를 다 쓰면 복구 시도를 예약하지 않는다.
    requests.clear()
    session = session_with(Delegate(["{}", valid], 6))
    with pytest.raises(ModelTimeBudgetExceeded):
        asyncio.run(call(session, output_tokens=3000, time_limit=5))
    assert session.execution.calls == 1 and requests == [3000]

    # 남은 시간이 없으면 provider를 한 번도 부르지 않는다.
    requests.clear()
    session = session_with(Delegate([valid], 1))
    with pytest.raises(ModelTimeBudgetExceeded):
        asyncio.run(call(session, time_limit=0))
    assert session.execution.calls == 0 and requests == []

    # 인자를 주지 않으면 목적별 기본 출력 상한 그대로다.
    session = session_with(Delegate([valid], 1))
    asyncio.run(call(session))
    assert requests == [session.cfg.max_output_tokens]


def test_story_fixture_is_synthetic_and_chunks_touch():
    chunks = story_chunks()
    assert len({c.chunk_id for c in chunks}) == len(chunks) == sum(len(v) for v in STORY.values())
    for first, second in pairwise(chunks):
        if first.episode_no == second.episode_no:
            assert first.end_offset == second.start_offset
