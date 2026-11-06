"""합성 회귀: 모델 주도 원문 탐색과 두 단계 답변(CHAT_RETRIEVAL_AGENT).

제공자·임베딩·DB·실제 원고 없이, 지어낸 짧은 이야기와 고정한 모델 출력으로 실제 핸들러·실행 원장·
SQLite outbox를 지난다. 연결과 경계(예약·정산, 도구·호출·시간 한도, 인용 검증, 로그)를 검사하며
모델이 좋은 검색어를 고르는지, 답이 맞는지는 측정하지 않는다.
"""

import asyncio
import copy
import json
import logging
import socket
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.chatbot.client import ChatExecutionClient, ChatTransportError
from app.chatbot.hybrid import HybridQaHandler, _HybridSession
from app.chatbot.hybrid_metrics import HybridMetrics
from app.chatbot.hybrid_prompts import (
    AGENT_ANSWER_PROMPT,
    AGENT_ANSWER_VERSION,
    ANSWER_PROMPT,
    ANSWER_VERSION,
    COMPOSE_VERSION,
    EXPLORE_VERSION,
    EXTRACT_VERSION,
    PLAN_VERSION,
)
from app.chatbot.outbox import Outbox
from app.chatbot.qa import ModelTimeBudgetExceeded, response_schema
from app.chatbot.qa_render import PUBLIC_NOTICES, UNREGISTERED_NOTICE
from app.chatbot.replay import NoticeCodes, offline_settings, recorded_notice_codes
from app.chatbot.runtime import ChatRuntime, ProviderFailure
from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.responses import LlmTextResponse
from app.retrieval.chunking import POLICY
from app.retrieval.search_contracts import (
    ChunkEvidence,
    SearchReadiness,
    SearchResult,
    SearchScopeUnavailable,
)
from app.schemas.chat_execution import Dispatch, ExecutionContext, Finish
from app.schemas.chat_hybrid import AgentGroundedAnswer, GroundedAnswer
from tests.test_chat_hybrid import (
    CATALOG,
    FakeEmbedding,
    FakeSearch,
    HybridSpring,
    hybrid_settings,
    replay,
    scenario,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("이 회귀는 외부 서비스에 접근하지 않는다")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


# 지어낸 이야기. 회차마다 이어 붙는 조각 두세 개로 나눈다(실제 원고·평가 질문과 무관하다).
STORY = {
    1: [
        "가온은 새벽 시장에서 낡은 나침반을 은화 세 닢에 샀다. ",
        "상인은 그 나침반이 북쪽이 아니라 잃어버린 것을 가리킨다고 말했다. ",
    ],
    2: [
        "나루는 가온에게 나침반을 빌려 달라고 했지만 가온은 거절했다. ",
        "그날 밤 나침반 바늘이 처음으로 강 쪽을 가리켰다. ",
    ],
    3: [
        "가온은 강바닥에서 푸른 열쇠를 건져 올렸다. ",
        "나루는 그 열쇠가 등대 문에 맞는다고 알려 주었다. ",
        "두 사람은 해가 지기 전에 등대로 가기로 했다. ",
    ],
    4: ["두 사람은 등대 문을 열고 꼭대기에서 오래된 지도를 찾았다. "],
    5: ["가온은 지도를 나루에게 맡기고 마을을 떠났다. "],
    6: ["나루는 지도에 적힌 섬의 이름을 소리 내어 읽었다. "],
    7: ["마을 사람들은 등대의 불이 다시 켜진 것을 보았다. "],
    8: ["가온은 먼 항구에서 나루의 편지를 받았다. "],
}
QUESTION = "가온은 나침반을 어떻게 얻었고 열쇠는 어디서 찾았어?"


def story_chunks(story=None):
    chunks = []
    for episode, texts in (story or STORY).items():
        start = 0
        for index, text in enumerate(texts):
            chunks.append(
                ChunkEvidence(
                    chunk_id=UUID(int=70000 + episode * 100 + index),
                    episode_id=UUID(int=71000 + episode),
                    episode_no=episode,
                    chunk_index=index,
                    text=text,
                    start_offset=start,
                    end_offset=start + len(text),
                    chunk_hash=sha256(text.encode()).hexdigest(),
                    source_key=f"synthetic/story/{episode}",
                    source_version="v1",
                    source_hash="b" * 64,
                    policy=POLICY,
                    methods=("KEYWORD",),
                    score=0.9,
                )
            )
            start += len(text)
    return chunks


def padded_story():
    """조각마다 1,000 cl100k 토큰을 넘게 지어낸 문장을 덧붙인 이야기. 추출 묶음이 회차별로 나뉜다."""
    filler = "바람이 조용히 불었고 물결은 천천히 흔들렸다. " * 120
    return {episode: [text + filler for text in texts] for episode, texts in STORY.items()}


def story_chunk(episode, index):
    return next(c for c in story_chunks() if (c.episode_no, c.chunk_index) == (episode, index))


class AgentSearch(FakeSearch):
    """FakeSearch에 탐색 전용 조회 세 개를 더한다. 범위는 넘겨받은 scope만 쓴다."""

    def __init__(self, corpus):
        super().__init__({})
        self.corpus = list(corpus)
        self.explore_calls, self.episode_calls = [], []
        self.episode_limit_override = None

    def visible(self, scope):
        return [c for c in self.corpus if scope.episode_from <= c.episode_no <= scope.episode_to]

    async def episode_span(self, scope):
        self.record("episode_span", scope)
        numbers = sorted({c.episode_no for c in self.visible(scope)})
        return (numbers[0], numbers[-1], len(numbers)) if numbers else (None, None, 0)

    async def episodes(self, scope, episode_nos, limit=100):
        self.record("episodes", scope)
        self.episode_calls.append(list(episode_nos))
        limit = self.episode_limit_override or limit
        chunks = sorted(
            (
                replace(c, methods=("EPISODE",), score=1.0)
                for c in self.visible(scope)
                if c.episode_no in episode_nos
            ),
            key=lambda c: (c.episode_no, c.chunk_index),
        )
        return SearchResult(tuple(chunks[:limit]), self.readiness, len(chunks) >= limit)

    async def keyword_explore(self, scope, query, *, limit=20, order="RELEVANCE"):
        self.record("keyword_explore", scope)
        self.explore_calls.append((query, order, limit))
        words = query.split()
        matched = sorted(
            (c for c in self.visible(scope) if all(word in c.text for word in words)),
            key=lambda c: (c.episode_no, c.chunk_index),
            reverse=order == "LATEST",
        )
        hits = Counter(c.episode_no for c in matched)
        return SearchResult(
            tuple(matched[:limit]),
            self.readiness,
            len(matched) >= limit,
            episode_hits=tuple(sorted(hits.items())),
        )


class AgentEmbedding(FakeEmbedding):
    async def create_embeddings(self, inputs):
        self.inputs.append(list(inputs))
        if self.failure:
            raise self.failure
        return EmbeddingBatchResponse(
            [[1.0, 0.0] + [0.0] * 1534 for _ in inputs], "text-embedding-3-small", 7
        )


class RoutedTextClient:
    """프롬프트 버전(cache key)별 대기열에서 고정 출력을 꺼낸다.

    병렬 추출은 도착 순서에 기대지 않도록 입력을 받아 출력을 만드는 함수도 허용한다.
    예외 객체를 넣으면 그 호출에서 그대로 던진다(provider 오류 대역).
    """

    def __init__(self, outputs):
        self.outputs = {key: list(values) for key, values in outputs.items()}
        self.requests = []
        self.unexpected = []

    def of(self, version):
        return [r for r in self.requests if r["prompt_cache_key"] == version]

    async def create_text_response(self, system_prompt, user_prompt, **kwargs):
        body = json.loads(user_prompt)
        self.requests.append({"system": system_prompt, "input": body, **kwargs})
        queue = self.outputs.get(kwargs["prompt_cache_key"])
        if not queue:
            # provider 대역의 예외는 실행이 삼킬 수 있으므로 따로 적어 두고 재생 끝에서 확인한다.
            self.unexpected.append(kwargs["prompt_cache_key"])
            raise AssertionError("예상하지 못한 모델 호출")
        value = queue[0] if callable(queue[0]) and len(queue) == 1 else queue.pop(0)
        if callable(value):
            value = value(body)
        if isinstance(value, Exception):
            raise value
        return LlmTextResponse(
            text=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            input_token_count=10,
            cached_input_token_count=0,
            output_token_count=5,
        )


def plan_output(**overrides):
    plan = {
        "disposition": "PROCEED",
        "registeredCurrentRequired": False,
        "rdbMode": "NONE",
        "sourceNeed": "SCENE",
        "subjects": [],
        "searchTerms": ["나침반"],
        "includeEvidence": True,
        "episodeFrom": None,
        "episodeTo": None,
        "requestedParts": [
            {"ref": "P1", "description": "가온이 나침반을 얻은 경위"},
            {"ref": "P2", "description": "열쇠를 찾은 곳"},
        ],
    }
    plan.update(overrides)
    return plan


def step(*, enough=False, searches=(), read=(), order="RELEVANCE", note="합성 메모"):
    return {
        "note": note,
        "unsupportedParts": [] if enough else ["P2"],
        "enough": enough,
        "searches": list(searches),
        "searchOrder": order,
        "readEpisodes": list(read),
    }


def facts_from(quotes, *, extra=()):
    """입력 passages에서 주어진 구절이 든 덩어리를 찾아 사실을 만든다(병렬 호출 순서와 무관)."""

    def build(body):
        facts = []
        for quote in quotes:
            for passage in body["passages"]:
                if quote in passage["text"]:
                    facts.append(
                        {
                            "text": f"{passage['episodeNo']}화: {quote}",
                            "evidenceRef": passage["ref"],
                            "quote": quote,
                        }
                    )
        if body["passages"]:
            facts.extend(
                {"text": text, "evidenceRef": body["passages"][0]["ref"], "quote": quote}
                for text, quote in extra
            )
        return {"facts": facts}

    return build


def composed(claims, *, missing=(), explanations=()):
    return {
        "working": "합성 정리",
        "relevantFactRefs": sorted({ref for claim in claims for ref in claim["factRefs"]}),
        "claims": [
            {
                "rowRefs": [],
                "relatedFacets": [],
                "eventStatus": None,
                "eventMatch": None,
                **claim,
            }
            for claim in claims
        ],
        "conflicts": [],
        "unansweredRowRefs": [],
        "missingFacets": list(missing),
        "missingExplanations": [{"facet": f, "text": t} for f, t in explanations],
    }


def compose_by_quote(mapping, **kwargs):
    """facts 입력에서 구절로 F*를 찾아 답 문장을 만든다. mapping: 구절 -> (문장, 답한 요구)."""

    def build(body):
        claims = []
        for quote, (text, answered) in mapping.items():
            refs = [f["factRef"] for f in body["facts"] if f["quote"] == quote]
            if refs:
                claims.append({"text": text, "factRefs": refs, "answeredFacets": answered})
        return composed(claims, **kwargs)

    return build


def agent_hybrid(**overrides):
    values = dict(agent=True, max_model_calls=24, max_query_embeddings=24)
    values.update(overrides)
    return hybrid_settings(**values)


def agent_qa(**overrides):
    values = dict(max_input_tokens=160000, max_context_characters=400000, max_tool_calls=60)
    values.update(overrides)
    return offline_settings(**values)


async def agent_replay(
    tmp_path,
    outputs,
    *,
    question=QUESTION,
    corpus=None,
    hybrid=None,
    config=None,
    max_calls=24,
    max_tool_calls=60,
    seconds=600,
    episode_from=None,
    episode_to=None,
    search_setup=None,
    spring_setup=None,
    embedding_setup=None,
):
    deadline = datetime.now(UTC) + timedelta(seconds=seconds)
    dispatch = Dispatch(
        schema_version=1, attempt_id=UUID(int=9101), capability="a" * 64, deadline=deadline
    )
    context = ExecutionContext(
        schema_version=1,
        question_id=UUID(int=9102),
        attempt_id=dispatch.attempt_id,
        work_id=UUID(int=9103),
        member_id=42,
        question=question,
        episode_from=episode_from,
        episode_to=episode_to,
        deadline=deadline,
        token_budget=600000,
        max_calls=max_calls,
        max_tool_calls=max_tool_calls,
        operational=False,
    )
    spring = HybridSpring(context, copy.deepcopy(CATALOG["catalogs"]["base"]))
    search = AgentSearch(story_chunks() if corpus is None else corpus)
    embedding = AgentEmbedding()
    for setup, target in [
        (spring_setup, spring),
        (search_setup, search),
        (embedding_setup, embedding),
    ]:
        if setup:
            setup(target)
    raw = RoutedTextClient(outputs)
    metrics = HybridMetrics()
    async with httpx.AsyncClient(transport=httpx.MockTransport(spring.respond)) as http:
        box = Outbox(str(tmp_path / "agent-outbox.sqlite3"))
        runtime = ChatRuntime(
            ChatExecutionClient("http://spring.test", "synthetic-internal-key", http),
            box,
            handler=HybridQaHandler(
                raw,
                search,
                embedding,
                agent_qa(**(config or {})),
                agent_hybrid(**(hybrid or {})),
                metrics,
            ),
        )
        try:
            with recorded_notice_codes() as notice_codes:
                runtime.submit(dispatch)
                await asyncio.gather(*list(runtime.tasks.values()))
            # outbox는 한 번에 종류별 8건씩 보낸다. 호출이 많은 실행은 여러 번에 걸쳐 비운다.
            for _ in range(8):
                await runtime.deliver()
                if box.count() == 0:
                    break
            assert len(spring.finishes) == 1
            assert box.count() == 0
            assert box._reserved_slots == 0
            assert raw.unexpected == []
        finally:
            box.close()
    codes = notice_codes[-1] if notice_codes else NoticeCodes()
    published = spring.finishes[0]["result"]
    if published is not None:
        # 탐색 경로가 붙이는 안내 코드는 모두 등록된 내부 코드이고, 작가에게는 공개 표의 문장만 보인다.
        assert UNREGISTERED_NOTICE not in codes
        assert set(published["limitations"]) <= set(PUBLIC_NOTICES.values())
    return {
        "finish": spring.finishes[0],
        # 이 결과에 붙은 안내 코드 이름 전체다. 화면 문구가 아니라 진단 기록에서 읽는다.
        "noticeCodes": codes,
        "spring": spring,
        "search": search,
        "embedding": embedding,
        "raw": raw,
        "metrics": metrics,
    }


COMPASS = "낡은 나침반을 은화 세 닢에 샀다"
KEY = "강바닥에서 푸른 열쇠를 건져 올렸다"
ANSWER_CLAIMS = {
    COMPASS: ("가온은 새벽 시장에서 나침반을 은화 세 닢에 샀습니다.", ["P1"]),
    KEY: ("열쇠는 강바닥에서 건져 올렸습니다.", ["P2"]),
}


def full_outputs():
    return {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [step(searches=["열쇠"], read=[3]), step(enough=True)],
        EXTRACT_VERSION: [
            facts_from([COMPASS, KEY], extra=[("원문에 없는 내용", "나침반을 금화 열 닢에 샀다")])
        ],
        COMPOSE_VERSION: [compose_by_quote(ANSWER_CLAIMS)],
    }


def seed_compass(search):
    search.keyword_chunks = [story_chunk(1, 0), story_chunk(2, 0)]


def assert_ledger_balanced(result):
    """provider 호출마다 예약 하나와 정산 보고 하나가 있고, 호출 ID가 겹치지 않는다."""
    spring, raw, embedding = result["spring"], result["raw"], result["embedding"]
    calls = len(raw.requests) + len(embedding.inputs)
    assert len(spring.reservations) == len(spring.reports) == calls
    assert len({r["callId"] for r in spring.reservations}) == calls


def test_switch_is_off_by_default_and_keeps_the_fixed_round_request_unchanged(tmp_path):
    assert hybrid_settings().agent is False

    class NoExploration(FakeSearch):
        async def forbidden(self, *args, **kwargs):
            raise AssertionError("스위치가 꺼져 있으면 탐색 조회를 부르지 않는다")

        episode_span = episodes = keyword_explore = forbidden

    case = scenario("lexical_scene")
    holder = {}

    def swap(search):
        holder["original"] = search

    result = asyncio.run(replay(tmp_path, case, search_setup=swap))
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert result["search"].calls == case["expected"]["searches"]
    assert not hasattr(FakeSearch, "episode_span") and hasattr(NoExploration, "episode_span")
    keys = [request["prompt_cache_key"] for request in result["raw"].requests]
    assert keys == [PLAN_VERSION, ANSWER_VERSION]
    answer_request = result["raw"].requests[-1]
    assert answer_request["system"] == ANSWER_PROMPT
    assert "exploration" not in answer_request["input"]
    # 꺼진 상태의 provider schema는 기존 모델 그대로다(이름·문장 수·인용 수 상한).
    schema = answer_request["response_schema"]
    assert schema == response_schema(GroundedAnswer)
    assert schema.name == "GroundedAnswer"
    claim = schema.schema["$defs"]["GroundedClaim"]["properties"]
    assert schema.schema["properties"]["claims"]["maxItems"] == 8
    assert claim["evidenceRefs"]["maxItems"] == claim["supports"]["maxItems"] == 6
    assert "AgentGroundedClaim" not in schema.schema["$defs"]


def test_exploration_reads_hits_and_a_whole_episode_then_answers_from_verified_facts(tmp_path):
    result = asyncio.run(agent_replay(tmp_path, full_outputs(), search_setup=seed_compass))
    finished = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert finished["outcome"] == "ANSWER"
    assert "은화 세 닢" in finished["text"] and "강바닥" in finished["text"]
    search, raw = result["search"], result["raw"]
    # 범위 확인 → 씨앗(키워드·벡터·이웃) → 탐색(키워드 분포·벡터·이웃·회차 통째) → 마지막 재검증.
    assert search.calls == [
        "episode_span",
        "keyword",
        "vector",
        "neighbors",
        "keyword_explore",
        "vector",
        "episodes",
        "revalidate",
    ]
    assert search.explore_calls == [("열쇠", "RELEVANCE", 60)]
    assert search.episode_calls == [[3]]
    assert all(s.member_id == 42 and s.work_id == UUID(int=9103) for s in search.scopes)
    # 한 단계의 검색어는 임베딩 호출 한 번으로 묶인다(씨앗 1 + 탐색 1).
    assert [len(inputs) for inputs in result["embedding"].inputs] == [1, 1]

    first, second = raw.of(EXPLORE_VERSION)
    assert first["input"]["episodeRange"] == {"from": 1, "to": 8, "count": 8}
    assert first["input"]["fullyReadEpisodes"] == [] and first["input"]["searchHistory"] == []
    assert first["input"]["budget"]["stepsLeft"] == 4
    assert [p["episodeNo"] for p in first["input"]["passages"]] == [1, 2]
    # 두 번째 단계는 실행한 검색의 회차 분포와 통째로 읽은 회차를 본다. 읽은 묶음은 회차 순서다.
    assert second["input"]["fullyReadEpisodes"] == [3]
    history = second["input"]["searchHistory"]
    assert [
        (h["query"], h["matchedChunks"], h["firstEpisode"], h["lastEpisode"]) for h in history
    ] == [("열쇠", 2, 3, 3)]
    assert history[0]["episodes"] == "3×2" and history[0]["unread"] == ""
    assert second["input"]["previousNote"] == "합성 메모"
    assert [p["episodeNo"] for p in second["input"]["passages"]] == [1, 2, 3]
    assert second["input"]["passages"][2]["chunks"] == [0, 2]
    assert "outline" not in first["input"] and "outline" not in second["input"]

    # 추출은 원문 덩어리만, 답 작성은 검증된 사실만 받는다.
    extract = raw.of(EXTRACT_VERSION)
    assert len(extract) == 1
    assert set(extract[0]["input"]) == {
        "question",
        "requestedParts",
        "eventContext",
        "subjects",
        "passages",
    }
    assert all(set(p) == {"ref", "episodeNo", "text"} for p in extract[0]["input"]["passages"])
    compose = raw.of(COMPOSE_VERSION)[0]["input"]
    assert "passages" not in compose and "items" not in compose
    assert [(f["factRef"], f["episodeNo"], f["quote"]) for f in compose["facts"]] == [
        ("F1", 1, COMPASS),
        ("F2", 3, KEY),
    ]
    assert compose["exploration"]["fullyReadEpisodes"] == [3]
    assert "order" not in compose["exploration"]["searches"][0]

    # 인용한 조각만 결과에 남고, 답변 전 등록한 인용은 읽은 조각 전부다.
    assert [s["sourceEpisodeNo"] for s in finished["retrievalSources"]] == [1, 3]
    assert finished["retrievalSources"][0]["quote"] == STORY[1][0]
    receipts = result["spring"].receipts
    assert [len(r["sources"]) for r in receipts] == [5, 2]
    assert_ledger_balanced(result)
    assert all(r["kind"] == "SETTLE" for r in result["spring"].reports)
    purposes = Counter(r["purpose"] for r in result["spring"].reservations)
    assert purposes == {
        "CHAT_INTERPRETATION": 3,  # 계획 1 + 탐색 2
        "CHAT_QUERY_EMBEDDING": 2,
        "CHAT_ANSWER": 2,  # 추출 1 + 답 작성 1
    }
    for request in raw.requests:
        encoded = json.dumps(request["input"], ensure_ascii=False)
        assert "memberId" not in encoded and "workId" not in encoded
        assert "synthetic/story" not in encoded and str(UUID(int=70100)) not in encoded


def test_unverified_fact_quotes_are_dropped_before_the_answer_is_written(tmp_path):
    outputs = full_outputs()
    outputs[COMPOSE_VERSION] = [
        # 검증에서 떨어진 사실(F3는 없음)을 인용한 문장은 거절되고, 고친 출력만 받아들인다.
        composed(
            [{"text": "없는 사실을 인용한 문장", "factRefs": ["F3"], "answeredFacets": ["P1"]}]
        ),
        compose_by_quote(ANSWER_CLAIMS),
    ]
    result = asyncio.run(agent_replay(tmp_path, outputs, search_setup=seed_compass))
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    first, second = result["raw"].of(COMPOSE_VERSION)
    assert [f["factRef"] for f in first["input"]["facts"]] == ["F1", "F2"]
    assert "금화" not in json.dumps(first["input"]["facts"], ensure_ascii=False)
    assert "이전 출력이 계약 검증에 실패했습니다" in second["system"]
    assert "금화" not in result["finish"]["result"]["text"]
    assert_ledger_balanced(result)


def test_small_allowed_range_is_read_whole_instead_of_searched(tmp_path):
    outputs = {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [step(enough=True), step(enough=True)],
        EXTRACT_VERSION: [facts_from([KEY])],
        COMPOSE_VERSION: [
            compose_by_quote(
                {KEY: ("열쇠는 강바닥에서 건져 올렸습니다.", ["P2"])},
                missing=["P1"],
                explanations=[("P1", "나침반을 얻은 경위는 읽은 범위에 나오지 않습니다.")],
            )
        ],
    }
    result = asyncio.run(agent_replay(tmp_path, outputs, episode_from=3, episode_to=4))
    search = result["search"]
    assert search.calls == ["episode_span", "episodes", "revalidate"]
    assert search.episode_calls == [[3, 4]]
    assert {(s.episode_from, s.episode_to) for s in search.scopes} == {(3, 4)}
    explore, again = (request["input"] for request in result["raw"].of(EXPLORE_VERSION))
    assert explore["fullyReadEpisodes"] == [3, 4]
    assert explore["episodeRange"] == {"from": 3, "to": 4, "count": 2}
    assert "previousAnswer" not in explore
    assert result["embedding"].inputs == []
    # 일부 요구만 답한 초안 뒤에는 빠진 요구를 알려 주고 한 번 더 탐색한다. 답 문장은 넘기지 않는다.
    assert again["previousAnswer"]["coverage"] == "PARTIAL"
    assert again["previousAnswer"]["answered"] == ["P2"]
    assert [m["ref"] for m in again["previousAnswer"]["missing"]] == ["P1"]
    assert "강바닥에서 건져 올렸습니다" not in json.dumps(
        again["previousAnswer"], ensure_ascii=False
    )
    # 재탐색에서 새로 읽은 것이 없으면 같은 자료로 다시 답하지 않고 부분 답으로 마감한다.
    assert len(result["raw"].of(COMPOSE_VERSION)) == 1
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER" and "강바닥" in finished["text"]
    assert "나침반을 얻은 경위는 읽은 범위에 나오지 않습니다." in finished["text"]


@pytest.mark.parametrize("relax", [True, False])
def test_plan_narrowed_range_is_read_first_within_the_server_range(tmp_path, relax):
    question = "3화에서 가온은 열쇠를 어디서 찾았어? 나침반은 어떻게 얻었어?"
    outputs = {
        PLAN_VERSION: [plan_output(episodeFrom=3, episodeTo=3, searchTerms=["열쇠"])],
        EXPLORE_VERSION: [step(searches=["나침반 은화"]), step(enough=True)],
        EXTRACT_VERSION: [facts_from([COMPASS, KEY])],
        COMPOSE_VERSION: [compose_by_quote(ANSWER_CLAIMS)],
    }
    if not relax:
        # 좁힌 범위에는 나침반 장면이 없어 부분 답이 되고, 재탐색도 더 읽을 것이 없다.
        outputs[EXPLORE_VERSION].append(step(enough=True))
        outputs[COMPOSE_VERSION] = [
            compose_by_quote({KEY: ANSWER_CLAIMS[KEY]}, missing=["P1"]),
        ]
    result = asyncio.run(
        agent_replay(
            tmp_path,
            outputs,
            question=question,
            hybrid={"agent_relax_plan_range": relax},
            episode_from=1,
            episode_to=6,
        )
    )
    search = result["search"]
    explore = result["raw"].of(EXPLORE_VERSION)[0]["input"]
    assert search.episode_calls[0] == [3]
    if relax:
        # 계획이 좁힌 3화를 먼저 읽고, 탐색은 서버가 허용한 1~6화 안에서 한다(그 밖으로는 못 나간다).
        assert explore["focusEpisodes"] == {"from": 3, "to": 3}
        assert explore["episodeRange"] == {"from": 1, "to": 6, "count": 6}
        assert {(s.episode_from, s.episode_to) for s in search.scopes} == {(1, 6)}
        sources = result["finish"]["result"]["retrievalSources"]
        assert sorted(s["sourceEpisodeNo"] for s in sources) == [1, 3]
    else:
        assert "focusEpisodes" not in explore
        assert explore["episodeRange"] == {"from": 3, "to": 3, "count": 1}
        assert {(s.episode_from, s.episode_to) for s in search.scopes} == {(3, 3)}
        assert [s["sourceEpisodeNo"] for s in result["finish"]["result"]["retrievalSources"]] == [3]
    assert result["finish"]["result"]["outcome"] == "ANSWER"


def test_empty_two_stage_answer_is_retried_once_as_a_single_stage_answer(tmp_path):
    def single_stage(body):
        passage = next(p for p in body["passages"] if COMPASS in p["text"])
        return {
            "coverage": "PARTIAL",
            "claims": [
                {
                    "text": "가온은 나침반을 은화 세 닢에 샀습니다.",
                    "evidenceRefs": [passage["references"][0]["evidenceRef"]],
                    "answeredFacets": ["P1"],
                    "supports": [
                        {"evidenceRef": passage["references"][0]["evidenceRef"], "quote": COMPASS}
                    ],
                }
            ],
            "conflicts": [],
            "missingFacets": ["P2"],
        }

    outputs = {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [step(enough=True)],
        # 뽑힌 사실이 하나도 검증되지 않으면 답 작성 호출 없이 빈 답이다.
        EXTRACT_VERSION: [facts_from([], extra=[("지어낸 사실", "원문에 없는 구절")])],
        AGENT_ANSWER_VERSION: [single_stage],
    }
    result = asyncio.run(
        agent_replay(tmp_path, outputs, search_setup=seed_compass, hybrid={"agent_max_steps": 1})
    )
    raw = result["raw"]
    assert raw.of(COMPOSE_VERSION) == []
    assert len(raw.of(EXTRACT_VERSION)) == 1
    answer = raw.of(AGENT_ANSWER_VERSION)[0]
    # 한 단계 답변은 조회 범위 사용법을 덧붙인 프롬프트·별도 버전·넓힌 모델을 쓴다.
    assert answer["system"] == AGENT_ANSWER_PROMPT and answer["system"].startswith(ANSWER_PROMPT)
    assert answer["response_schema"] == response_schema(AgentGroundedAnswer)
    assert answer["response_schema"].schema["properties"]["claims"]["maxItems"] == 12
    assert "exploration" in answer["input"] and answer["input"]["passages"]
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER" and "은화 세 닢" in finished["text"]
    assert_ledger_balanced(result)


def test_one_failed_extraction_group_does_not_lose_the_other_groups(tmp_path):
    def extract(body):
        if any(p["episodeNo"] == 3 for p in body["passages"]):
            return RuntimeError("synthetic provider failure")
        return facts_from([COMPASS])(body)

    outputs = {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [step(enough=True), step(enough=True)],
        EXTRACT_VERSION: [extract],
        COMPOSE_VERSION: [compose_by_quote({COMPASS: ANSWER_CLAIMS[COMPASS]}, missing=["P2"])],
    }
    result = asyncio.run(
        agent_replay(
            tmp_path,
            outputs,
            corpus=story_chunks(padded_story()),
            episode_from=1,
            episode_to=3,
            # 회차 하나가 묶음 하나가 되게 해 회차별로 따로 추출한다.
            hybrid={"agent_extract_group_tokens": 1000},
        )
    )
    raw = result["raw"]
    assert len(raw.of(EXTRACT_VERSION)) == 3
    compose = raw.of(COMPOSE_VERSION)[0]["input"]
    assert [f["quote"] for f in compose["facts"]] == [COMPASS]
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER" and "은화 세 닢" in finished["text"]
    # 실패한 호출도 예약·보고가 하나씩 있고, 측정값이 없으면 0이 아니라 UNKNOWN으로 남는다.
    assert_ledger_balanced(result)
    assert Counter(r["kind"] for r in result["spring"].reports)["UNKNOWN"] == 1


def test_parallel_extraction_stays_inside_the_remaining_call_budget(tmp_path):
    outputs = {
        PLAN_VERSION: [plan_output()],
        EXTRACT_VERSION: [facts_from([COMPASS, KEY])],
        COMPOSE_VERSION: [compose_by_quote(ANSWER_CLAIMS)],
    }
    # 계획 1 + 임베딩 0 뒤 남은 호출 5개 중 답 작성 2개를 빼면 추출은 3묶음까지다.
    result = asyncio.run(
        agent_replay(
            tmp_path,
            outputs,
            corpus=story_chunks(padded_story()),
            max_calls=6,
            episode_from=1,
            episode_to=4,
            hybrid={"agent_extract_group_tokens": 1000},
        )
    )
    raw = result["raw"]
    assert raw.of(EXPLORE_VERSION) == []  # 탐색 단계를 돌 호출 몫이 없다
    assert len(raw.of(EXTRACT_VERSION)) == 3
    refs = [p["ref"] for request in raw.of(EXTRACT_VERSION) for p in request["input"]["passages"]]
    assert len(refs) == len(set(refs)) == 4  # 읽은 덩어리가 빠짐없이, 겹치지 않게 나뉜다
    assert len(result["spring"].reservations) == 5  # 서버 호출 상한 6 안이다
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert_ledger_balanced(result)


def test_reservation_rejection_during_extraction_fails_the_execution(tmp_path):
    def reject_later(spring):
        spring.reserve_status, spring.reserve_code = 429, "CHAT_LIMIT_EXCEEDED"
        spring.reserve_after = 2  # 계획과 추출 한 묶음만 예약된다

    outputs = {
        PLAN_VERSION: [plan_output()],
        EXTRACT_VERSION: [facts_from([COMPASS, KEY])],
        COMPOSE_VERSION: [compose_by_quote(ANSWER_CLAIMS)],
    }
    result = asyncio.run(
        agent_replay(
            tmp_path,
            outputs,
            corpus=story_chunks(padded_story()),
            spring_setup=reject_later,
            episode_from=1,
            episode_to=4,
            hybrid={"agent_extract_group_tokens": 1000, "agent_max_steps": 1},
            max_calls=7,
        )
    )
    # 예약 거절은 한 묶음의 실패로 숨기지 않고 기존 실행 한도 실패로 올린다.
    assert result["finish"] == {"result": None, "failure": "EXECUTION_LIMIT_EXCEEDED"}
    assert result["raw"].of(COMPOSE_VERSION) == []
    # 예약된 호출은 모두 보고됐다(거절된 예약에는 보고가 없다).
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2


@pytest.mark.parametrize(
    "hybrid,seconds",
    [
        ({"agent_explore_seconds": 0.000001}, 600),  # 탐색 시간 상한
        ({"agent_max_steps": 1, "max_model_calls": 3}, 600),  # 모델 호출 상한
        ({}, 80),  # 답변 몫 시간(75초)을 남기면 탐색할 시간이 없다
    ],
)
def test_exploration_is_skipped_when_time_or_call_limits_leave_no_room(tmp_path, hybrid, seconds):
    outputs = {
        PLAN_VERSION: [plan_output()],
        EXTRACT_VERSION: [facts_from([COMPASS])],
        COMPOSE_VERSION: [compose_by_quote({COMPASS: ANSWER_CLAIMS[COMPASS]}, missing=["P2"])],
        AGENT_ANSWER_VERSION: [],
    }
    result = asyncio.run(
        agent_replay(tmp_path, outputs, search_setup=seed_compass, hybrid=hybrid, seconds=seconds)
    )
    assert result["raw"].of(EXPLORE_VERSION) == []
    assert "keyword_explore" not in result["search"].calls
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER" and "은화 세 닢" in finished["text"]
    assert_ledger_balanced(result)


def test_invalid_exploration_output_ends_exploration_and_answers_from_what_was_read(tmp_path):
    invalid = {**step(searches=["열쇠"]), "readEpisodes": [0]}
    outputs = {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [invalid, {**invalid, "workId": "other"}],
        EXTRACT_VERSION: [facts_from([COMPASS])],
        COMPOSE_VERSION: [compose_by_quote({COMPASS: ANSWER_CLAIMS[COMPASS]}, missing=["P2"])],
    }
    result = asyncio.run(
        agent_replay(tmp_path, outputs, search_setup=seed_compass, hybrid={"agent_max_steps": 1})
    )
    assert len(result["raw"].of(EXPLORE_VERSION)) == 2
    assert "keyword_explore" not in result["search"].calls
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert_ledger_balanced(result)


def no_indexed_chunks(search):
    """검색 색인이 아직 없는 작품: 읽을 회차가 없고 벡터도 없다."""
    search.readiness = SearchReadiness(0, 0, 0, 0, 0, ("NO_SUPPORTED_SOURCES",))
    if not hasattr(search, "episode_span"):

        async def episode_span(scope):
            search.record("episode_span", scope)
            return None, None, 0

        search.episode_span = episode_span


def test_without_searchable_chunks_linked_quotes_still_answer_through_the_existing_path(tmp_path):
    def run(name, **hybrid):
        case = scenario("direct_quote_sufficient")
        case["maxToolCalls"] = 60
        return asyncio.run(
            replay(
                tmp_path / name,
                case,
                hybrid=hybrid,
                config={"max_tool_calls": 60},
                search_setup=no_indexed_chunks,
            )
        )

    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on, off = run("on", agent=True), run("off", agent=False)
    # 조각이 없으면 탐색에 들어가지 않고, 등록 설정에 연결된 인용으로 기존 경로가 답한다.
    assert on["finish"]["result"]["outcome"] == "ANSWER"
    assert "화염구" in on["finish"]["result"]["text"]
    assert on["finish"] == off["finish"] and on["noticeCodes"] == off["noticeCodes"]
    assert on["search"].calls == ["episode_span"] and off["search"].calls == []
    keys = [request["prompt_cache_key"] for request in on["raw"].requests]
    assert keys == [request["prompt_cache_key"] for request in off["raw"].requests]
    assert keys[-1] == ANSWER_VERSION and EXPLORE_VERSION not in keys
    # 기존 답변 계약(프롬프트·응답 모델·입력)을 그대로 쓴다.
    answer = on["raw"].requests[-1]
    assert answer["system"] == ANSWER_PROMPT
    assert answer["response_schema"] == response_schema(GroundedAnswer)
    assert "exploration" not in answer["input"]
    assert answer["input"] == off["raw"].requests[-1]["input"]


@pytest.mark.parametrize(
    "question,plan",
    [
        (QUESTION, {}),
        # 계획이 회차를 좁힌 질문: 탐색이었다면 범위를 넓히고 '회차 범위' 안내를 지웠을 실행이다.
        (
            "3화에서 가온은 열쇠를 어디서 찾았어? 나침반은 어떻게 얻었어?",
            {"episodeFrom": 3, "episodeTo": 3, "searchTerms": ["열쇠"]},
        ),
    ],
)
def test_without_searchable_chunks_or_linked_quotes_the_existing_path_reports_the_gap(
    tmp_path, question, plan
):
    def run(name, **hybrid):
        (tmp_path / name).mkdir()
        return asyncio.run(
            agent_replay(
                tmp_path / name,
                {PLAN_VERSION: [plan_output(**plan)]},
                question=question,
                corpus=[],
                hybrid=hybrid,
                search_setup=no_indexed_chunks,
            )
        )

    on, off = run("on", agent=True), run("off", agent=False)
    finished = on["finish"]["result"]
    assert finished["outcome"] == "INSUFFICIENT_EVIDENCE"
    # 범위 확인 한 번 뒤에는 꺼진 실행과 같은 조회·같은 안내 코드다. 탐색 전용 조회와 상태가 남지 않는다.
    assert on["finish"] == off["finish"] and on["noticeCodes"] == off["noticeCodes"]
    assert on["search"].calls == ["episode_span", *off["search"].calls]
    assert off["search"].calls == ["keyword", "vector"]
    assert [r["prompt_cache_key"] for r in on["raw"].requests] == [PLAN_VERSION]
    assert on["embedding"].inputs == []
    if plan:
        # 범위 확인만 서버 허용 범위로 하고, 그 뒤 기존 경로는 계획이 좁힌 범위를 그대로 쓴다.
        assert [(s.episode_from, s.episode_to) for s in on["search"].scopes] == [
            (1, 2147483647),
            (3, 3),
            (3, 3),
        ]
        # 탐색이었다면 지웠을 '회차 범위' 안내 코드가 그대로 남는다. 화면에는 보이지 않는 코드다.
        assert "SOURCE_RANGE" in on["noticeCodes"] and finished["limitations"] == []


def test_trace_logs_keep_counts_and_numbers_but_no_text(tmp_path, caplog):
    outputs = full_outputs()
    outputs[EXPLORE_VERSION] = [
        step(searches=["열쇠 강바닥"], read=[3], note="비공개 탐색 메모"),
        step(enough=True),
    ]
    with caplog.at_level(logging.INFO, logger="app.chatbot.hybrid"):
        result = asyncio.run(agent_replay(tmp_path, outputs, search_setup=seed_compass))
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for stage in ("agent_start", "agent_seed", "agent_step", "agent_search", "agent_read"):
        assert f"stage={stage}" in logged
    assert "agent_extract" in logged and "agent_compose" in logged
    for secret in (
        QUESTION,
        "열쇠",
        "강바닥",
        "나침반",
        "비공개 탐색 메모",
        "은화",
        "가온",
        "synthetic/story",
    ):
        assert secret not in logged
    assert "memberId" not in logged and str(UUID(int=9103)) not in logged


def test_registered_history_answer_is_kept_unless_the_source_supplement_answers(
    monkeypatch, tmp_path
):
    registered = Finish.model_validate(
        {
            "result": {
                "outcome": "ANSWER",
                "text": "등록 이력 답",
                "sources": [],
                "limitations": [],
            }
        }
    )
    supplement = Finish.model_validate(
        {"result": {"outcome": "ANSWER", "text": "원문 보완 답", "sources": [], "limitations": []}}
    )
    insufficient = Finish.model_validate(
        {
            "result": {
                "outcome": "INSUFFICIENT_EVIDENCE",
                "text": "원문 부족",
                "sources": [],
                "limitations": [],
            }
        }
    )
    seen = {}

    async def answer_plan(self, plan):
        return registered

    monkeypatch.setattr(_HybridSession, "answer_plan", answer_plan)
    monkeypatch.setattr(_HybridSession, "tools_available", lambda self, cost, **kwargs: True)
    plan = plan_output(
        rdbMode="HISTORY",
        sourceNeed="NONE",
        requestedParts=[],
        searchTerms=[],
        subjects=[
            {"domain": "CHARACTER", "name": "가온", "focus": {"factType": None, "attribute": None}}
        ],
    )

    def run(outcome, **hybrid):
        async def run_source(self, converted, *, collect=True):
            seen["plan"], seen["collect"] = converted, collect
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(_HybridSession, "run_source", run_source)
        seen.clear()
        result = asyncio.run(agent_replay(tmp_path, {PLAN_VERSION: [plan]}, hybrid=hybrid))
        return result["finish"]

    assert run(supplement)["result"]["text"] == "원문 보완 답"
    # 계획이 요구 항목 없이 등록 이력만 골랐으면 질문 자체를 하나의 요구로 원문 탐색에 넘긴다.
    assert seen["collect"] is False and seen["plan"].source_need == "SCENE"
    assert [p.ref for p in seen["plan"].requested_parts] == ["P1"]
    for outcome in (
        insufficient,
        ProviderFailure(),
        ModelTimeBudgetExceeded(),
        ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED"),
        SQLAlchemyError("synthetic statement timeout"),
    ):
        assert run(outcome)["result"]["text"] == "등록 이력 답"
    # 작품·회원 범위를 확인하지 못했으면 등록 답으로 덮지 않는다.
    assert run(SearchScopeUnavailable())["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    # 잔액 부족·원문 변경은 등록 답으로 덮지 않고 기존 경계대로 올린다.
    assert run(ChatTransportError(429, "QUOTA_EXHAUSTED"))["failure"] == "QUOTA_EXHAUSTED"
    assert run(ChatTransportError(403, "SOURCE_CHANGED"))["result"]["outcome"] == (
        "INSUFFICIENT_EVIDENCE"
    )
    # 옵션이나 스위치가 꺼져 있으면 보완을 시작하지 않는다.
    assert run(supplement, agent_verify_registered=False)["result"]["text"] == "등록 이력 답"
    assert seen == {}
    assert run(supplement, agent=False)["result"]["text"] == "등록 이력 답"
    assert seen == {}


# ---- PR #137 리뷰 반영 ----

# 조각 일곱 개를 인용하는 한 문장. 다섯째 구절은 3화의 두 조각에 걸쳐 있어 인용문은 여섯 개(기존
# 응답 모델도 받는 개수)지만 인용 조각은 일곱 개다.
SEVEN_CHUNK_QUOTES = [
    COMPASS,
    "잃어버린 것을 가리킨다고 말했다",
    "가온은 거절했다",
    "처음으로 강 쪽을 가리켰다",
    "건져 올렸다. 나루는 그 열쇠가",
    "오래된 지도를 찾았다",
]
SEVEN_CHUNK_CLAIM = "가온이 나침반을 사고 열쇠와 지도를 찾기까지의 합성 요약입니다."


def seven_chunk_answer(body):
    supports = []
    for quote in SEVEN_CHUNK_QUOTES:
        passage = next(p for p in body["passages"] if quote in p["text"])
        supports.append({"evidenceRef": passage["references"][0]["evidenceRef"], "quote": quote})
    return {
        "coverage": "COMPLETE",
        "claims": [
            {
                "text": SEVEN_CHUNK_CLAIM,
                "evidenceRefs": list(dict.fromkeys(s["evidenceRef"] for s in supports)),
                "answeredFacets": ["P1", "P2"],
                "supports": supports,
            }
        ],
        "conflicts": [],
    }


def test_single_stage_exploration_answer_may_cite_what_its_own_contract_allows(tmp_path):
    outputs = {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [step(enough=True)],
        AGENT_ANSWER_VERSION: [seven_chunk_answer],
    }
    result = asyncio.run(
        agent_replay(
            tmp_path, outputs, episode_from=1, episode_to=4, hybrid={"agent_extract": False}
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER" and SEVEN_CHUNK_CLAIM in finished["text"]
    assert len(finished["retrievalSources"]) == 7
    answers = result["raw"].of(AGENT_ANSWER_VERSION)
    # provider에 준 응답 모델이 문장당 12개를 받으므로 7개 인용은 수정 재시도 없이 통과한다.
    assert len(answers) == 1
    claim = answers[0]["response_schema"].schema["$defs"]["AgentGroundedClaim"]["properties"]
    assert claim["evidenceRefs"]["maxItems"] == 12
    assert_ledger_balanced(result)


def test_fixed_round_answer_keeps_the_six_chunk_limit_per_claim(tmp_path):
    def read_four_episodes(search):
        search.keyword_chunks = [c for c in story_chunks() if c.episode_no <= 4]

    outputs = {PLAN_VERSION: [plan_output()], ANSWER_VERSION: [seven_chunk_answer]}
    result = asyncio.run(
        agent_replay(tmp_path, outputs, hybrid={"agent": False}, search_setup=read_four_episodes)
    )
    answers = result["raw"].of(ANSWER_VERSION)
    # 같은 출력이 schema(인용문 6개)는 지나지만 인용 조각이 7개라 꺼진 실행에서는 두 번 모두 거절된다.
    assert len(answers) == 2
    assert answers[0]["response_schema"] == response_schema(GroundedAnswer)
    assert len(answers[0]["input"]["passages"]) == 4
    finished = result["finish"]["result"]
    assert finished["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert SEVEN_CHUNK_CLAIM not in finished["text"] and "retrievalSources" not in finished


def unknown_fact_claim(body):
    return composed(
        [
            {
                "text": "가온은 나침반을 사면서 금화 열 닢을 더 냈습니다.",
                "factRefs": [body["facts"][0]["factRef"], "F999"],
                "answeredFacets": ["P1"],
            }
        ],
        missing=["P2"],
    )


def test_a_claim_mixing_known_and_unknown_facts_goes_back_for_a_rewrite(tmp_path):
    outputs = full_outputs()
    outputs[COMPOSE_VERSION] = [unknown_fact_claim, compose_by_quote(ANSWER_CLAIMS)]
    result = asyncio.run(agent_replay(tmp_path, outputs, search_setup=seed_compass))
    first, second = result["raw"].of(COMPOSE_VERSION)
    assert [f["factRef"] for f in first["input"]["facts"]] == ["F1", "F2"]  # F999는 입력에 없다
    assert "이전 출력이 계약 검증에 실패했습니다" in second["system"]
    finished = result["finish"]["result"]
    # 모르는 참조만 빼고 F1의 인용을 단 채 게시하지 않는다. 고쳐 쓴 답만 게시한다.
    assert finished["outcome"] == "ANSWER" and "금화" not in finished["text"]
    assert "은화 세 닢" in finished["text"]
    assert_ledger_balanced(result)


def test_an_answer_that_keeps_citing_unknown_facts_is_never_published(tmp_path):
    outputs = full_outputs()
    outputs[COMPOSE_VERSION] = [unknown_fact_claim]
    result = asyncio.run(
        agent_replay(tmp_path, outputs, search_setup=seed_compass, hybrid={"agent_max_answers": 1})
    )
    assert len(result["raw"].of(COMPOSE_VERSION)) == 2
    finished = result["finish"]["result"]
    assert finished["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "금화" not in finished["text"] and "retrievalSources" not in finished
    assert_ledger_balanced(result)


THREE_PARTS = [
    {"ref": "P1", "description": "가온이 나침반을 얻은 경위"},
    {"ref": "P2", "description": "열쇠를 찾은 곳"},
    {"ref": "P3", "description": "섬에 도착한 날"},
]


def two_attempt_outputs(parts=None):
    """첫 시도는 P1만, 3화를 더 읽은 둘째 시도는 P2만 답하는 출력."""
    plan = plan_output() if parts is None else plan_output(requestedParts=parts)
    refs = [part["ref"] for part in plan["requestedParts"]]
    return {
        PLAN_VERSION: [plan],
        EXPLORE_VERSION: [step(enough=True), step(read=[3]), step(enough=True)],
        EXTRACT_VERSION: [facts_from([COMPASS, KEY])],
        COMPOSE_VERSION: [
            compose_by_quote(
                {COMPASS: ANSWER_CLAIMS[COMPASS]}, missing=[r for r in refs if r != "P1"]
            ),
            compose_by_quote({KEY: ANSWER_CLAIMS[KEY]}, missing=[r for r in refs if r != "P2"]),
        ],
    }


def test_partial_answers_from_two_attempts_are_merged_instead_of_replaced(tmp_path):
    result = asyncio.run(agent_replay(tmp_path, two_attempt_outputs(), search_setup=seed_compass))
    raw, finished = result["raw"], result["finish"]["result"]
    # 둘째 탐색에는 지금까지 검증된 답 기준으로 빠진 요구(P2)만 알린다.
    again = raw.of(EXPLORE_VERSION)[1]["input"]["previousAnswer"]
    assert again["answered"] == ["P1"] and [m["ref"] for m in again["missing"]] == ["P2"]
    assert len(raw.of(COMPOSE_VERSION)) == 2
    # 첫 시도가 답한 P1을 둘째 초안(P2만 답함)으로 바꿔 잃지 않는다. 합치면 모두 답했으므로 게시한다.
    assert finished["outcome"] == "ANSWER"
    assert "은화 세 닢" in finished["text"] and "강바닥" in finished["text"]
    assert "확정하기 어려워요" not in finished["text"]
    assert sorted(s["sourceEpisodeNo"] for s in finished["retrievalSources"]) == [1, 3]
    # 합친 답도 기존 마무리를 지난다: 인용한 조각의 현재 원문 재검증과 마지막 인용 등록.
    assert result["search"].calls[-1] == "revalidate"
    assert [s["sourceEpisodeNo"] for s in result["spring"].receipts[-1]["sources"]] == [1, 3]
    assert [s["quote"] for s in result["spring"].receipts[-1]["sources"]] == [
        s["quote"] for s in finished["retrievalSources"]
    ]
    assert_ledger_balanced(result)


def test_merged_partial_answer_reports_only_the_parts_no_attempt_answered(tmp_path):
    result = asyncio.run(
        agent_replay(
            tmp_path,
            two_attempt_outputs(THREE_PARTS),
            search_setup=seed_compass,
            hybrid={"agent_max_answers": 2},
        )
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    # 답변 시도가 여기서 끝나도 두 시도가 검증한 P1·P2는 모두 남고, 못 답한 것은 P3뿐이다.
    assert "은화 세 닢" in finished["text"] and "강바닥" in finished["text"]
    gap = next(line for line in finished["text"].splitlines() if "확정하기 어려워요" in line)
    assert "섬에 도착한 날" in gap
    assert "나침반을 얻은 경위" not in gap and "열쇠를 찾은 곳" not in gap
    assert sorted(s["sourceEpisodeNo"] for s in finished["retrievalSources"]) == [1, 3]


def test_single_stage_partial_answers_are_merged_the_same_way(tmp_path):
    def answer_with(quote, text, part, missing):
        def build(body):
            passage = next(p for p in body["passages"] if quote in p["text"])
            ref = passage["references"][0]["evidenceRef"]
            return {
                "coverage": "PARTIAL",
                "claims": [
                    {
                        "text": text,
                        "evidenceRefs": [ref],
                        "answeredFacets": [part],
                        "supports": [{"evidenceRef": ref, "quote": quote}],
                    }
                ],
                "conflicts": [],
                "missingFacets": [missing],
            }

        return build

    outputs = {
        PLAN_VERSION: [plan_output()],
        EXPLORE_VERSION: [step(enough=True), step(read=[3]), step(enough=True)],
        AGENT_ANSWER_VERSION: [
            answer_with(COMPASS, ANSWER_CLAIMS[COMPASS][0], "P1", "P2"),
            answer_with(KEY, ANSWER_CLAIMS[KEY][0], "P2", "P1"),
        ],
    }
    result = asyncio.run(
        agent_replay(tmp_path, outputs, search_setup=seed_compass, hybrid={"agent_extract": False})
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert "은화 세 닢" in finished["text"] and "강바닥" in finished["text"]
    assert "확정하기 어려워요" not in finished["text"]
    # 합친 답의 인용문은 각자 읽었던 원문 조각에 그대로 이어진다.
    assert [s["quote"] for s in finished["retrievalSources"]] == [STORY[1][0], STORY[3][0]]
