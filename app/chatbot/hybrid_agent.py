"""모델 주도 원문 탐색과 두 단계 답변(CHAT_RETRIEVAL_AGENT=true일 때만 실행된다).

계획과 등록 설정 조회, 답변 검증·마무리(answer → finish_answer)는 기존 그대로 쓰고, 고정 라운드
(키워드 → 벡터) 대신 아래 순서로 원문을 모은다.

  씨앗(계획 검색어 키워드 + 질문 벡터, 또는 지목된 회차 통째 읽기)
  → [탐색 호출 → 검색/회차 통째 읽기 → 읽은 묶음에 추가] × N → 답변
  → (PARTIAL/NONE이고 단계·시간이 남으면) 빠진 요구를 겨냥한 재탐색 → 답변

답변은 읽은 묶음을 회차 순 묶음으로 나눠 병렬로 사실과 인용문을 뽑고, 서버가 인용문을 원문과
대조한 뒤 검증된 사실만으로 답을 쓴다(agent_extract). 항목별(perItem) 답변과 두 단계 답변이 빈 답으로
끝난 재시도는 읽은 묶음을 한 번의 답변 호출에 넣는다.

지키는 경계
- 모든 provider 호출은 Execution.call(= self.model, ChatEmbeddingClient)을 지난다. 병렬 추출도 묶음마다
  self.model을 한 번씩 부르며, 묶음 수는 남은 호출 몫 안에서만 정한다. 임베딩은 한 단계의 검색어를
  한 번의 호출로 묶는다.
- 조회는 self.search()로 도구 예산(consume/tools_available)을 쓰고, 탐색 단계마다 마감 시간을 본다.
- 검색어는 모델이 자유롭게 쓰지만 조회 입력일 뿐이다. 회원·작품·회차 범위는 서버 scope가 정한다.
- 답변에 들어가는 조각은 답변 호출 전에 Spring 영수증으로 등록한다(한 번에 60개 이하).
- trace에는 단계·회차 번호·개수·시간만 남긴다. 질문, 원문, 모델이 쓴 검색어·메모·답은 남기지 않는다.
"""

import asyncio
import math
import time
from dataclasses import dataclass

import tiktoken

from app.chatbot.client import ChatTransportError
from app.chatbot.hybrid_grounding import resolve_support_quotes
from app.chatbot.hybrid_items import empty_item_answer, merge_item_answers, requested_facets
from app.chatbot.hybrid_metrics import ObservedEmbeddingClient
from app.chatbot.hybrid_prompts import (
    COMPOSE_PROMPT,
    COMPOSE_VERSION,
    EXPLORE_PROMPT,
    EXPLORE_VERSION,
    EXTRACT_PROMPT,
    EXTRACT_VERSION,
)
from app.chatbot.providers import ChatEmbeddingClient
from app.chatbot.qa import ModelTimeBudgetExceeded, response_schema
from app.chatbot.runtime import ProviderFailure
from app.schemas.chat_hybrid import (
    RETRIEVAL_SOURCE_LIMIT,
    AgentGroundedAnswer,
    AgentGroundedClaim,
    ComposedAnswer,
    EvidenceConflict,
    EvidenceSupport,
    ExplorationStep,
    FactExtraction,
)

# 답이 인용하는 서로 다른 조각 수를 결과 상한(60) 안에 두기 위한 여유 한도. 이 수를 넘으면 한 문장이
# 새 조각의 인용을 더 얹지 않는다. 문장마다 첫 인용 하나는 결과 상한까지 허용한다.
CITED_CHUNK_SOFT_LIMIT = RETRIEVAL_SOURCE_LIMIT - 4
# 답 작성 단계에 넘기는 검증된 사실 수 상한(ComposedAnswer.relevant_fact_refs와 같다).
FACT_LIMIT = 200
# 읽은 묶음의 조각 수 상한. compose_evidence가 한 번에 합칠 수 있는 개수와 같다.
POOL_CHUNK_LIMIT = 1000
# 탐색 모델에 알려 주는 회차 하나의 어림 분량(cl100k 토큰). 예산 안내용이며 실제 읽기를 제한하지 않는다.
EPISODE_TOKEN_ESTIMATE = 8000
# 회차 통째 읽기 한 번에 받는 조각 수 상한(검색 계약의 limit 상한).
EPISODE_READ_LIMIT = 100


@dataclass(frozen=True)
class _Quote:
    """기존 인용 검증기(resolve_support_quotes)에 넘기는 (참조, 구절) 한 쌍."""

    evidence_ref: str
    quote: str


@dataclass(frozen=True)
class _Fact:
    """서버가 인용문을 원문과 대조해 확인한 사실 하나. ref는 답 작성 단계에 보여 주는 F*다."""

    ref: str
    text: str
    evidence_ref: str
    quote: str
    episode: int | None


class _StepEmbeddings:
    """한 단계의 검색어 전부를 계량된 provider 호출 한 번으로 임베딩한다."""

    model = "text-embedding-3-small"

    def __init__(self, session):
        self.session = session
        self.version = session.handler.hybrid.embedding_version
        self.client = ChatEmbeddingClient(
            ObservedEmbeddingClient(session.handler.embedding, session.metrics),
            session.execution,
            model_name=self.model,
        )

    async def embed(self, texts):
        session = self.session
        if (
            getattr(session.handler.embedding, "model", None) != self.model
            or session.handler.embedding.version != self.version
        ):
            raise ProviderFailure()
        expected = len(texts)

        def validate(response):
            # Validate before the metered invocation settles its measured usage.
            if isinstance(response.input_token_count, int) and response.input_token_count >= 0:
                session.metrics.tokens.labels("embedding_input").inc(response.input_token_count)
            vectors = response.embeddings
            if (
                response.model != self.model
                or len(vectors) != expected
                or any(
                    len(vector) != 1536
                    or any(
                        isinstance(v, bool)
                        or not isinstance(v, (int, float))
                        or not math.isfinite(v)
                        for v in vector
                    )
                    or not any(vector)
                    for vector in vectors
                )
            ):
                raise ValueError("Invalid query embedding response")

        response = await self.client.create_embeddings(list(texts), validate=validate)
        return dict(zip(texts, response.embeddings, strict=True))


class _FixedEmbedding:
    """이미 계량해 받은 벡터를 기존 벡터 검색 서비스에 넘긴다. provider를 다시 부르지 않는다."""

    def __init__(self, model, version, vector):
        self.model, self.version, self.vector = model, version, vector

    async def create_embedding(self, text):
        return self.vector


class AgentRetrieval:
    """_HybridSession에 섞어 쓰는 탐색. 기존 세션의 예산·검증·답변 메서드를 그대로 호출한다."""

    # 기존 경로(scope, evidence_passages, answer)가 읽는 표시는 클래스 기본값을 둔다. 생성자를 거치지 않고
    # 만든 세션과 탐색을 쓰지 않는 실행은 이 기본값으로 기존 동작을 유지한다.
    agent_active = False
    agent_extract_off = False
    agent_wide_range = False
    agent_focus = None
    agent_span = None

    def agent_init(self):
        self.agent_pool = []  # 읽은 조각(ChunkEvidence), 읽은 순서. 줄이지 않고 더하기만 한다.
        self.agent_pool_ids = set()
        self.agent_pool_tokens = 0
        self.agent_search_tokens = 0  # 검색 결과·이웃으로 넣은 몫(회차 통째 읽기 제외)
        self.agent_ref_no = 0
        self.agent_steps = 0
        self.agent_answers = 0
        self.agent_stalls = 0
        self.agent_history = []  # 탐색 모델에 돌려주는 검색 기록(본문 없음)
        self.agent_read = {}  # 회차 번호 -> FULL / PARTIAL / MISSING
        self.agent_note = None
        self.agent_registered = set()
        self.agent_span = None
        self.agent_started = time.monotonic()
        self.agent_step_seconds = 0.0
        self.agent_answer_seconds = 0.0
        self.agent_token_cache = {}
        self.agent_encoding = None
        self.agent_folded = {}
        self.agent_active = False  # 이 실행이 실제로 탐색에 들어왔을 때만 True다.
        self.agent_wide_range = False  # agent_relax_plan_range가 계획 범위를 넓혔을 때만 True다.
        self.agent_focus = None  # 계획이 좁힌 (from, to). 넓은 범위로 읽을 때 먼저 읽을 곳이다.
        # 추출 묶음(원문 덩어리 구성) -> 검증 전 사실 목록. 같은 묶음은 다시 부르지 않는다.
        self.agent_fact_cache = {}
        self.agent_extract_off = False  # 두 단계 답변이 빈 답으로 끝나 한 단계 답변으로 돌아간 실행

    # ---- 공통 도우미 ----

    def agent_trace(self, stage, **data):
        self.trace(stage, t=round(time.monotonic() - self.agent_started, 1), **data)

    def agent_count_tokens(self, text):
        cached = self.agent_token_cache.get(text)
        if cached is None:
            if self.agent_encoding is None:
                self.agent_encoding = tiktoken.get_encoding("cl100k_base")
            cached = len(self.agent_encoding.encode(text, disallowed_special=()))
            self.agent_token_cache[text] = cached
        return cached

    def agent_pool_state(self):
        return {
            "chunks": len(self.agent_pool),
            "tokens": self.agent_pool_tokens,
            "search_tokens": self.agent_search_tokens,
            "episodes": len({c.episode_no for c in self.agent_pool}),
            "full": sorted(n for n, state in self.agent_read.items() if state == "FULL"),
        }

    def agent_tail_tools(self):
        # 답변 전 묶음 등록(60개씩) + 마지막 재검증 1 + 마지막 영수증 1. 이번 단계에 더 읽을 조각을
        # 감안해 등록 묶음 하나를 여유로 둔다.
        unregistered = sum(1 for c in self.agent_pool if c.chunk_id not in self.agent_registered)
        batches = math.ceil((unregistered + RETRIEVAL_SOURCE_LIMIT) / RETRIEVAL_SOURCE_LIMIT)
        return max(1, batches) + 2

    def agent_min_tools(self):
        # 등록 설정 조회가 남겨 둘 몫: 씨앗(키워드·벡터·이웃) + 한 단계(검색 2쌍·이웃·통째 읽기) + 마무리.
        return 3 + 6 + 4

    def agent_search_room(self):
        """검색 결과·이웃 조각으로 더 넣을 수 있는 토큰. 묶음의 일정 몫은 회차 통째 읽기를 위해 남긴다."""
        cap = int(self.hcfg.agent_pool_tokens * self.hcfg.agent_search_share)
        return max(
            0,
            min(
                cap - self.agent_search_tokens,
                self.hcfg.agent_pool_tokens - self.agent_pool_tokens,
            ),
        )

    def agent_call_room(self):
        """모델 호출 상한과 서버 실행의 호출 상한 중 더 적게 남은 쪽."""
        return min(
            self.hcfg.max_model_calls - self.model_calls,
            self.execution.context.max_calls - self.execution.calls,
        )

    def agent_answer_calls(self):
        """답변 한 번에 필요한 provider 호출 수의 어림값. 한 단계 답변은 1이다."""
        if not self.hcfg.agent_extract or self.logical_items:
            return 1
        groups = math.ceil(max(1, self.agent_pool_tokens) / self.hcfg.agent_extract_group_tokens)
        return 1 + min(self.hcfg.agent_extract_max_groups, groups)

    def agent_answer_reserve(self):
        return max(self.hcfg.agent_answer_reserve_seconds, self.agent_answer_seconds * 1.2)

    def agent_can_step(self):
        ctx = self.execution.context
        return (
            self.agent_steps < self.hcfg.agent_max_steps
            # 이 단계의 탐색 호출 + 임베딩 + 그 뒤의 답변 호출(두 단계 답변이면 추출 묶음 수 + 작성)이
            # 남아 있어야 한다.
            and self.model_calls + 1 + self.agent_answer_calls() <= self.hcfg.max_model_calls
            and self.execution.calls + 2 + self.agent_answer_calls() <= ctx.max_calls
            and self.model_time_left()
            > max(8.0, self.agent_step_seconds) + self.agent_answer_reserve()
            and self.tools_available(2 + self.agent_tail_tools())
            and self.agent_pool_tokens + 400 <= self.hcfg.agent_pool_tokens
            and (
                not self.hcfg.agent_explore_seconds
                or time.monotonic() - self.agent_started < self.hcfg.agent_explore_seconds
            )
        )

    def agent_pool_add(self, chunks, *, allowance=None):
        """새 조각을 읽은 묶음과 답변 근거(E*)에 더한다. 예산을 넘는 조각은 건너뛰고 개수만 센다."""
        added = omitted = spent = 0
        for chunk in chunks:
            if chunk.chunk_id in self.agent_pool_ids:
                continue
            tokens = self.agent_count_tokens(chunk.text)
            if (
                len(self.agent_pool) >= POOL_CHUNK_LIMIT
                or self.agent_pool_tokens + tokens > self.hcfg.agent_pool_tokens
                or (allowance is not None and spent + tokens > allowance)
            ):
                omitted += 1
                continue
            self.agent_pool.append(chunk)
            self.agent_pool_ids.add(chunk.chunk_id)
            self.agent_pool_tokens += tokens
            spent += tokens
            self.agent_ref_no += 1
            self.evidences.append(self.new_evidence(f"E{self.agent_ref_no}", chunk))
            added += 1
        if omitted:
            self.metrics.evidence_omissions.labels("agent", "TOKEN_LIMIT").inc(omitted)
            self.codes.append("EVIDENCE_CONTEXT_LIMIT")
        return added, omitted, spent

    @staticmethod
    def agent_interleave(lists):
        merged, seen = [], set()
        for rank in range(max((len(items) for items in lists), default=0)):
            for items in lists:
                if rank < len(items) and items[rank].chunk_id not in seen:
                    seen.add(items[rank].chunk_id)
                    merged.append(items[rank])
        return merged

    @staticmethod
    def agent_spread(hits, limit=30):
        """(회차, 조각 수) 목록을 모델이 읽기 쉬운 짧은 문자열로. 많으면 조각이 많은 회차만 남긴다."""
        shown = hits
        if len(hits) > limit:
            shown = sorted(sorted(hits, key=lambda h: (-h[1], h[0]))[:limit])
        text = " ".join(f"{episode}×{count}" for episode, count in shown)
        return text + (f" (+{len(hits) - len(shown)}개 회차)" if len(hits) > len(shown) else "")

    def agent_unread(self, entry):
        """검색어의 모든 단어가 든 조각 중 아직 묶음에 없는 것의 (회차, 개수). 본문은 내보내지 않는다."""
        words = [w.casefold() for w in entry["query"].split()][:12]
        read = {}
        for chunk in self.agent_pool:
            text = self.agent_folded.get(chunk.chunk_id)
            if text is None:
                text = self.agent_folded[chunk.chunk_id] = chunk.text.casefold()
            if all(word in text for word in words):
                read[chunk.episode_no] = read.get(chunk.episode_no, 0) + 1
        return [
            (episode, count - read.get(episode, 0))
            for episode, count in entry["hits"]
            if count > read.get(episode, 0)
        ]

    def agent_history_view(self, limit=24):
        """모델에 보여 주는 검색 기록: 검색어, 일치 조각의 회차 분포, 아직 읽지 않은 일치 조각."""
        view = []
        for entry in self.agent_history[-limit:]:
            hits = entry["hits"]
            item = {
                "query": entry["query"],
                "order": entry["order"],
                "matchedChunks": sum(count for _, count in hits),
                "firstEpisode": hits[0][0] if hits else None,
                "lastEpisode": hits[-1][0] if hits else None,
                "episodes": self.agent_spread(hits),
                "unread": self.agent_spread(self.agent_unread(entry)),
            }
            if "semanticEpisodes" in entry:
                item["semanticEpisodes"] = entry["semanticEpisodes"]
            view.append(item)
        return view

    def agent_scope_payload(self):
        """답변 모델에 알려 주는 조회 범위(통째로 읽은 회차와 검색어별 분포). 탐색이 아니면 비어 있다."""
        if not self.agent_active:
            return {}
        return {
            "exploration": {
                "fullyReadEpisodes": sorted(n for n, s in self.agent_read.items() if s == "FULL"),
                "searches": [
                    {k: v for k, v in item.items() if k not in {"order", "semanticEpisodes"}}
                    for item in self.agent_history_view()
                ],
            }
        }

    # ---- 조회 실행 ----

    async def agent_vectors(self, queries):
        """검색어들의 벡터를 한 번의 계량 호출로 받는다. 허용량·호출 한도를 넘으면 빈 결과다."""
        allowed = max(0, self.hcfg.max_query_embeddings - self.embedding_calls)
        queries = list(queries)[:allowed]
        if not queries or not self.models_available(embedding=True):
            self.codes.append("SEARCH_LIMIT")
            return {}
        self.embedding_calls += len(queries)
        return await _StepEmbeddings(self).embed(queries)

    async def agent_collect(self, hit_lists, *, label):
        """검색 결과를 순위대로 번갈아 묶음에 넣고, 새로 넣은 조각의 앞뒤 조각을 남은 몫만큼 더한다."""
        budget = min(self.hcfg.agent_step_tokens, self.agent_search_room())
        ranked = self.agent_interleave(hit_lists)
        added, omitted, spent = self.agent_pool_add(ranked, allowance=int(budget * 0.7))
        seeds = [c for c in ranked if c.chunk_id in self.agent_pool_ids][:12]
        neighbors_added = 0
        if (
            seeds
            and self.hcfg.neighbor_radius
            and budget - spent >= 400
            and self.tools_available(1 + self.agent_tail_tools())
        ):
            near = await self.search(
                "neighbors",
                seeds,
                radius=self.hcfg.neighbor_radius,
                limit=min(self.hcfg.max_neighbors, 100),
            )
            neighbors_added, more, near_spent = self.agent_pool_add(near, allowance=budget - spent)
            omitted += more
            spent += near_spent
        self.agent_search_tokens += spent
        self.agent_trace(
            "agent_pool",
            source=label,
            hits=len(ranked),
            added=added,
            neighbors=neighbors_added,
            omitted=omitted,
            **self.agent_pool_state(),
        )
        return added + neighbors_added

    async def agent_seed(self, plan):
        """탐색 모델이 첫 판단을 할 수 있게 계획 검색어 키워드와 질문 벡터로 첫 조각을 읽는다."""
        ctx = self.execution.context
        terms = list(plan.search_terms) or [ctx.question]
        query = " ".join(dict.fromkeys(terms))[:1000]
        lists = []
        # 질문이 지목했거나 허용된 회차가 몇 개뿐이면 검색 대신 그 회차를 먼저 통째로 읽는다.
        first, last = self.agent_focus or self.agent_span[:2]
        if 0 <= last - first < self.hcfg.agent_max_read_episodes:
            focus = list(range(first, last + 1))
            self.agent_trace("agent_seed", focus=focus)
            await self.agent_execute([], "RELEVANCE", focus)
            return
        # 씨앗 검색은 계획이 정한 범위에서 한다. 넓은 범위는 탐색 모델이 필요할 때 쓴다.
        wide, self.agent_wide_range = self.agent_wide_range, False
        try:
            await self.agent_seed_search(plan, query, lists)
        finally:
            self.agent_wide_range = wide
        await self.agent_collect(lists, label="seed")

    async def agent_seed_search(self, plan, query, lists):
        if self.tools_available(1 + self.agent_tail_tools()):
            hints = tuple(
                dict.fromkeys(
                    item.evidence.episode_id
                    for row in self.chosen
                    for _, item in row.evidence
                    if item.evidence.episode_id is not None
                )
            )
            found = list(
                await self.search(
                    "keyword", query, limit=self.hcfg.keyword_top_k, preferred_episode_ids=hints
                )
            )
            if plan.ordering != "NONE":
                found = await self.ordered_search(plan, found)
            lists.append(found)
        vector_query = self.expansion_query(plan, None, query)
        if self.tools_available(2 + self.agent_tail_tools()):
            vectors = await self.agent_vectors([vector_query])
            if vectors:
                found = await self.search(
                    "vector",
                    vector_query,
                    _FixedEmbedding(
                        _StepEmbeddings.model, self.hcfg.embedding_version, vectors[vector_query]
                    ),
                    limit=self.hcfg.vector_top_k,
                )
                lists.append(list(found))
        self.agent_trace(
            "agent_seed",
            keyword_terms=len(query.split()),
            ordering=plan.ordering,
            lists=[len(items) for items in lists],
        )

    async def agent_execute(self, searches, order, episodes):
        """탐색 모델이 고른 행동을 실행한다.

        검색 결과(조각 몇 개, 겨냥한 근거)를 먼저 넣고 회차 통째 읽기를 그 뒤에 넣는다. 묶음이 거의
        찼을 때 통째 읽기가 남은 자리를 다 써서 정확히 일치한 조각이 못 들어가는 일을 막는다.
        곧 통째로 읽을 회차의 조각은 검색 몫으로 따로 넣지 않는다.
        """
        before = len(self.agent_pool)
        if searches:
            await self.agent_run_searches(searches, order, skip=set(episodes))
        if episodes:
            await self.agent_read_episodes(episodes)
        return len(self.agent_pool) - before

    async def agent_read_episodes(self, episodes):
        if not self.tools_available(1 + self.agent_tail_tools()):
            self.codes.append("SEARCH_LIMIT")
            return
        chunks = await self.search("episodes", episodes, limit=EPISODE_READ_LIMIT)
        # 개수 상한에서 잘리면 마지막으로 받은 회차는 일부만, 그 뒤 회차는 아직 읽지 못한 것이다.
        cut = chunks[-1].episode_no if len(chunks) >= EPISODE_READ_LIMIT else None
        report = []
        for number in episodes:
            own = [c for c in chunks if c.episode_no == number]
            if cut is not None and number > cut:
                # 없는 회차로 표시하면 다시 요청할 수 없다. 표시하지 않고 다음 단계에 맡긴다.
                report.append({"episode": number, "chunks": 0, "added": 0, "state": "UNREAD"})
                continue
            added, omitted, _ = self.agent_pool_add(own)
            state = "MISSING" if not own else "PARTIAL" if omitted or number == cut else "FULL"
            self.agent_read[number] = state
            report.append({"episode": number, "chunks": len(own), "added": added, "state": state})
        self.agent_trace("agent_read", read=report, **self.agent_pool_state())

    def agent_fresh(self, chunks, limit, skip=()):
        """이미 읽었거나 곧 통째로 읽을 회차의 조각을 뺀 상위 limit개."""
        return [
            c for c in chunks if c.chunk_id not in self.agent_pool_ids and c.episode_no not in skip
        ][:limit]

    async def agent_run_searches(self, searches, order, *, skip=()):
        lists, entries = [], []
        keyword_k, vector_k = self.hcfg.agent_keyword_top_k, self.hcfg.agent_vector_top_k
        for query in searches:
            if not self.tools_available(1 + self.agent_tail_tools()):
                self.codes.append("SEARCH_LIMIT")
                break
            # 이미 읽은 일치 조각을 지나 다음 것을 가져올 수 있게 넉넉히 읽고 새 조각만 남긴다.
            result = await self.search_result(
                "keyword_explore", query, limit=max(60, min(100, keyword_k * 3)), order=order
            )
            fresh = self.agent_fresh(result.chunks, keyword_k, skip)
            lists.append(fresh)
            entries.append(
                {
                    "query": query,
                    "order": order,
                    "hits": list(result.episode_hits),
                    "keywordNew": len(fresh),
                }
            )
        ran = [entry["query"] for entry in entries]
        vectors = {}
        if ran and self.tools_available(1 + self.agent_tail_tools()):
            vectors = await self.agent_vectors(ran)
        for index, entry in enumerate(entries):
            vector = vectors.get(entry["query"])
            if vector is None:
                continue
            if not self.tools_available(1 + self.agent_tail_tools()):
                self.codes.append("SEARCH_LIMIT")
                break
            found = await self.search(
                "vector",
                entry["query"],
                _FixedEmbedding(_StepEmbeddings.model, self.hcfg.embedding_version, vector),
                limit=min(100, vector_k * 3),
            )
            fresh = self.agent_fresh(found, vector_k, skip)
            # 같은 검색어의 키워드 결과 바로 뒤에 두어 검색어별로 번갈아 들어가게 한다.
            lists.insert(2 * index + 1, fresh)
            entry["semanticEpisodes"] = list(dict.fromkeys(c.episode_no for c in found))[:8]
            entry["semanticNew"] = len(fresh)
        # 같은 검색어를 다시 실행한 경우(읽지 않은 일치 조각 이어 읽기)는 기록을 새것으로 바꾼다.
        rerun = {(e["query"].casefold(), e["order"]) for e in entries}
        self.agent_history = [
            e for e in self.agent_history if (e["query"].casefold(), e["order"]) not in rerun
        ] + entries
        await self.agent_collect(lists, label="search")
        # 검색어는 모델이 쓴 문자열이라 기록하지 않는다. 검색어마다 개수와 회차 번호만 남긴다.
        self.agent_trace(
            "agent_search",
            order=order,
            queries=[
                {
                    "words": len(e["query"].split()),
                    "matched": sum(count for _, count in e["hits"]),
                    "first": e["hits"][0][0] if e["hits"] else None,
                    "last": e["hits"][-1][0] if e["hits"] else None,
                    "unread": sum(count for _, count in self.agent_unread(e)),
                    "kw_new": e["keywordNew"],
                    "vec_new": e.get("semanticNew"),
                }
                for e in entries
            ],
        )

    # ---- 탐색 모델 호출 ----

    def agent_actions(self, step):
        """모델 출력을 서버 상한·허용 범위로 정리한다. 반복 검색어와 이미 통째로 읽은 회차는 뺀다."""
        order = step.search_order
        # 같은 검색어·정렬의 반복은 읽지 않은 일치 조각이 남아 있을 때만 다시 실행한다(이어 읽기).
        seen = {
            (e["query"].casefold(), e["order"])
            for e in self.agent_history
            if not self.agent_unread(e)
        }
        searches = []
        for term in step.searches:
            query = " ".join(term.split())[:120]
            key = (query.casefold(), order)
            if query and key not in seen:
                seen.add(key)
                searches.append(query)
        lo, hi, _ = self.agent_span
        episodes = [
            n
            for n in dict.fromkeys(step.read_episodes)
            if lo <= n <= hi and self.agent_read.get(n) not in {"FULL", "MISSING"}
        ]
        return (
            searches[: self.hcfg.agent_max_searches],
            order,
            episodes[: self.hcfg.agent_max_read_episodes],
        )

    def agent_gaps(self, plan, draft):
        """답변 시도 뒤 남은 요구를 탐색 모델에 알려 줄 요약. 답변 문장 자체는 넘기지 않는다."""
        labels = {p.ref: p.description for p in plan.requested_parts}
        facets = requested_facets(plan)
        if self.logical_items:
            names = {item.ref: item.label for item in self.logical_items}
            return {
                "coverage": draft.coverage,
                "validationFailed": self.answer_validation_failed,
                "missing": [
                    {
                        "item": names.get(item.item_ref, item.item_ref),
                        "parts": [
                            {"ref": facet, "description": labels.get(facet, facet)}
                            for facet in item.missing_facets
                        ],
                        "explanations": [e.text for e in item.missing_explanations],
                    }
                    for item in draft.item_answers
                    if item.missing_facets
                ][:40],
            }
        answered = {f for claim in draft.claims for f in claim.answered_facets}
        explanations = {e.facet: e.text for e in draft.missing_explanations}
        missing = draft.missing_facets or [f for f in facets if f not in answered]
        return {
            "coverage": draft.coverage,
            "validationFailed": self.answer_validation_failed,
            "answered": [f for f in facets if f in answered],
            "missing": [
                {
                    "ref": facet,
                    "description": labels.get(facet, facet),
                    **({"explanation": explanations[facet]} if facet in explanations else {}),
                }
                for facet in missing
            ],
        }

    def agent_explore_payload(self, plan, gaps):
        ctx = self.execution.context
        lo, hi, total = self.agent_span
        index = {e.ref: e.chunk.chunk_index for e in self.evidences if e.chunk}
        passages = []
        for passage in self.evidence_passages(self.evidences):
            numbers = [
                index[r["evidenceRef"]] for r in passage["references"] if r["evidenceRef"] in index
            ]
            entry = {"episodeNo": passage["sourceEpisodeNo"]}
            if numbers:
                entry["chunks"] = [min(numbers), max(numbers)]
            else:
                entry["linkedQuote"] = True
            entry["text"] = passage["text"]
            passages.append(entry)
        selected = {r.ref for r in self.chosen}
        shown = {"kind", "subjectName", "category", "displayName", "value", "sourceEpisodeNo"}
        rows = [
            {
                key: (value[:300] if isinstance(value, str) else value)
                for key, value in row.items()
                if key in shown and value is not None
            }
            for row in self.payload(ctx.question, plan)["rows"]
            if row["rowRef"] in selected
        ][:40]
        pool_left = max(0, self.hcfg.agent_pool_tokens - self.agent_pool_tokens)
        return {
            "question": ctx.question,
            **self.conversation_payload(),
            "requestedParts": [p.model_dump(by_alias=True) for p in plan.requested_parts],
            "eventContext": plan.event_context,
            "ordering": plan.ordering,
            "subjects": [s.name for s in plan.subjects],
            **(
                {"items": [item.label for item in self.logical_items][:40]}
                if self.logical_items
                else {}
            ),
            "episodeRange": {"from": lo, "to": hi, "count": total},
            **(
                {"focusEpisodes": {"from": self.agent_focus[0], "to": self.agent_focus[1]}}
                if self.agent_focus
                else {}
            ),
            **({"registeredRows": rows} if rows else {}),
            "passages": passages,
            "fullyReadEpisodes": sorted(n for n, s in self.agent_read.items() if s == "FULL"),
            "searchHistory": self.agent_history_view(),
            **({"previousAnswer": gaps} if gaps else {}),
            **({"previousNote": self.agent_note} if self.agent_note else {}),
            "budget": {
                "stepsLeft": self.hcfg.agent_max_steps - self.agent_steps + 1,
                "poolTokensLeft": pool_left,
                "searchTokensLeft": self.agent_search_room(),
                "tokensPerEpisode": EPISODE_TOKEN_ESTIMATE,
                "episodesAffordable": pool_left // EPISODE_TOKEN_ESTIMATE,
                "maxSearches": self.hcfg.agent_max_searches,
                "maxReadEpisodes": self.hcfg.agent_max_read_episodes,
            },
        }

    async def agent_decide(self, plan, gaps):
        """탐색 호출 한 번. 입력 초과·시간 부족·출력 오류는 실행 실패가 아니라 탐색 종료(None)다."""
        payload = self.agent_explore_payload(plan, gaps)
        if not self.fits(EXPLORE_PROMPT, payload, response_schema(ExplorationStep)):
            self.agent_trace("agent_stop", reason="EXPLORE_INPUT_LIMIT", step=self.agent_steps)
            return None
        # 답변 호출 한 번은 반드시 남긴다.
        attempts = min(self.cfg.max_attempts, self.agent_call_room() - 1)
        previous_seconds = getattr(self, "last_model_seconds", 0.0)
        started = time.monotonic()
        try:
            return await self.model(
                ExplorationStep,
                EXPLORE_PROMPT,
                payload,
                "CHAT_INTERPRETATION",
                EXPLORE_VERSION,
                max_attempts=max(1, attempts),
                output_tokens=self.hcfg.agent_step_output_tokens,
                time_limit=self.model_time_left() - self.agent_answer_reserve(),
            )
        except (ModelTimeBudgetExceeded, ProviderFailure) as exc:
            # 탐색 호출은 선택 단계다. 출력 잘림·형식 오류뿐 아니라 일시적인 provider 오류도 실행 실패로
            # 올리지 않고 지금까지 읽은 자료로 답변을 시도한다(사용량은 Execution.call이 이미 기록했다).
            # 예약 거절·실행 한도 같은 전송 오류(ChatTransportError)는 기존대로 올라간다.
            self.agent_trace("agent_stop", reason=type(exc).__name__, step=self.agent_steps)
            self.codes.append("SEARCH_LIMIT")
            # 답변 몫의 시간은 agent_answer_reserve로 따로 남겼다. 실패한 탐색 호출이 오래 걸렸다는
            # 이유로 답변 호출의 시간 판단이 막히지 않게 직전에 끝난 호출의 지연으로 되돌린다.
            self.last_model_seconds = previous_seconds
            return None
        finally:
            self.agent_step_seconds = time.monotonic() - started

    async def agent_explore(self, plan, gaps):
        while self.agent_can_step():
            self.agent_steps += 1
            self.rounds = self.agent_steps
            step = await self.agent_decide(plan, gaps)
            if step is None:
                return
            searches, order, episodes = self.agent_actions(step)
            self.agent_note = step.note.strip() or self.agent_note
            self.agent_trace(
                "agent_step",
                step=self.agent_steps,
                enough=step.enough,
                unsupported=len(step.unsupported_parts),
                order=order,
                searches=len(searches),
                read=episodes,
                requested={"searches": len(step.searches), "read": len(step.read_episodes)},
                seconds=round(self.agent_step_seconds, 1),
            )
            if step.enough or not (searches or episodes):
                return
            if await self.agent_execute(searches, order, episodes):
                self.agent_stalls = 0
            else:
                # 새로 읽은 것이 없으면 다른 표기를 한 번 더 시도하게 하고, 연속 두 번이면 멈춘다.
                self.agent_stalls += 1
                if self.agent_stalls >= 2:
                    self.agent_trace("agent_stop", reason="NO_NEW_EVIDENCE", step=self.agent_steps)
                    return
        self.agent_trace(
            "agent_stop",
            reason="BUDGET",
            step=self.agent_steps,
            time_left=round(self.model_time_left(), 1),
            model_calls=self.model_calls,
            provider_calls=self.execution.calls,
            tools=self.tool_count,
            **self.agent_pool_state(),
        )

    async def agent_register(self):
        """답변 모델에 보여 줄 조각을 Spring 영수증으로 등록한다. 이미 등록한 조각은 다시 보내지 않는다."""
        shown = {e.chunk.chunk_id for e in self.evidences if e.chunk}
        new = [
            c
            for c in self.agent_pool
            if c.chunk_id in shown and c.chunk_id not in self.agent_registered
        ]
        if new:
            await self.register(new)
            self.agent_registered.update(c.chunk_id for c in new)
            self.agent_trace("agent_register", chunks=len(new), tools=self.tool_count)

    @staticmethod
    def agent_better(previous, latest):
        """더 많은 요구를 답한 초안을 고른다. 같으면 더 많은 원문을 본 나중 초안이다."""
        if previous is None:
            return latest

        def score(answer):
            return len({f for c in answer.claims for f in c.answered_facets}), len(answer.claims)

        return latest if score(latest) >= score(previous) else previous

    async def run_agent(self, plan):
        """run_source의 고정 라운드 자리를 대신한다. 답변·인용 검증·마무리는 기존 메서드다."""
        try:
            return await self.agent_run(plan)
        except Exception as exc:
            # 예외 종류와 코드 위치만 남긴다(메시지·값·본문은 남기지 않는다). 처리는 기존대로다.
            where, frame = None, exc.__traceback__
            while frame is not None:
                if frame.tb_frame.f_globals.get("__name__", "").startswith("app."):
                    where = f"{frame.tb_frame.f_globals['__name__']}:{frame.tb_lineno}"
                frame = frame.tb_next
            self.agent_trace(
                "agent_error",
                error=type(exc).__name__,
                code=exc.code if isinstance(exc, ChatTransportError) else None,
                where=where,
                steps=self.agent_steps,
                model_calls=self.model_calls,
                provider_calls=self.execution.calls,
                tools=self.tool_count,
            )
            raise

    async def agent_run(self, plan):
        ctx = self.execution.context
        self.agent_started = time.monotonic()
        self.agent_active = True
        if self.hcfg.agent_relax_plan_range and (self.source_from, self.source_to) != (
            ctx.episode_from,
            ctx.episode_to,
        ):
            self.agent_wide_range = True
            if ctx.episode_from is None and ctx.episode_to is None and plan.rdb_mode == "NONE":
                # 원문만 읽는 질문에서 계획이 좁힌 범위 밖도 읽으므로 '회차 범위로 걸렀다'는 안내는
                # 붙이지 않는다. 등록 이력을 함께 읽는 질문은 그 조회가 여전히 좁힌 범위를 쓰므로 남긴다.
                self.codes = [c for c in self.codes if c != "SOURCE_RANGE"]
        if not self.tools_available(1 + self.agent_tail_tools()):
            # 등록 조회가 도구 예산을 다 썼으면 실행 실패 대신 기존 부족 안내로 마감한다.
            self.codes.append("SEARCH_LIMIT")
            return await self.unanswered_source_result(plan)
        self.consume("episode_span")
        self.agent_span = await self.handler.search.episode_span(self.scope)
        if not self.agent_span[2]:
            self.codes.append("SEARCH_LIMIT")
            return await self.unanswered_source_result(plan)
        if self.agent_wide_range:
            lo, hi, _ = self.agent_span
            focus = (max(lo, self.source_from or lo), min(hi, self.source_to or hi))
            self.agent_focus = focus if focus[0] <= focus[1] else None
        facets = requested_facets(plan)
        items = bool(self.logical_items)
        if items:
            self.item_draft = empty_item_answer(self.logical_items, facets)
        await self.evidence(
            self.chosen,
            reserve=self.agent_min_tools()
            + len(self.collection_checks)
            + self.current_check_count(),
            revalidate=True,
        )
        self.add_direct_evidence()
        self.agent_ref_no = len(self.evidences)
        self.agent_trace(
            "agent_start",
            span=list(self.agent_span),
            focus=list(self.agent_focus) if self.agent_focus else None,
            items=len(self.logical_items),
            rows=len(self.chosen),
            direct=len(self.evidences),
            tools=self.tool_count,
        )
        await self.agent_seed(plan)
        best = None
        gaps = None
        retry_same = False
        while True:
            pool_before = len(self.agent_pool)
            if not retry_same:
                await self.agent_explore(plan, gaps)
                if not self.evidences:
                    break
                if gaps is not None and len(self.agent_pool) == pool_before:
                    # 재탐색에서 새로 읽은 것이 없으면 같은 자료로 다시 답하지 않는다.
                    break
            retry_same = False
            if not self.models_available():
                self.codes.append("SEARCH_LIMIT")
                break
            await self.agent_register()
            if items:
                # 항목별 답은 근거 등록부(E* → 근거)로 마무리한다. 묶음은 줄지 않으므로 참조가 유지된다.
                self.remember_evidence()
            self.agent_answers += 1
            started = time.monotonic()
            draft = await self.answer(plan)
            self.agent_answer_seconds = time.monotonic() - started
            if items:
                draft = merge_item_answers(
                    self.item_draft, draft, self.logical_items, facets, event_scope=plan.event_scope
                )
                self.item_draft = draft
            self.agent_trace(
                "agent_answer",
                attempt=self.agent_answers,
                coverage=draft.coverage,
                missing=list(draft.missing_facets)
                or sorted({f for i in draft.item_answers for f in i.missing_facets}),
                seconds=round(self.agent_answer_seconds, 1),
                steps=self.agent_steps,
                **self.agent_pool_state(),
            )
            ready = draft.coverage == "COMPLETE" if items else self.source_answer_ready(plan, draft)
            if ready:
                return await self.finish_answer(draft)
            if draft.coverage == "PARTIAL":
                best = draft if items else self.agent_better(best, draft)
                self.preserve_supported_evidence(
                    best, self.evidence_registry.values() if items else self.evidences
                )
                self.fallback_direct_checks = len(
                    {e.direct_ref for e in self.evidences if e.direct_ref}
                )
            if self.agent_answers >= self.hcfg.agent_max_answers:
                break
            if draft.coverage == "NONE" and (
                self.answer_validation_failed or self.agent_two_stage()
            ):
                # 자료가 부족한 것이 아니므로 더 찾지 않고 같은 자료로 한 번 더 답하게 한다.
                # - 인용·참조 검증에 실패해 답 전체를 버린 경우: 같은 방식으로 다시 답한다(검증 기준은
                #   그대로다).
                # - 두 단계 답변이 빈 답으로 끝난 경우(뽑힌 사실이 질문과 이어지지 않았거나 쓴 문장이
                #   사건·조건 검사에서 모두 빠짐): 읽은 원문을 한 단계 답변으로 한 번 더 읽힌다.
                fallback = not self.answer_validation_failed
                needed = self.agent_answer_seconds
                if fallback:
                    needed = max(30.0, needed)
                # 다시 답할 시간이 남아 있지 않으면 서버 마감을 넘기지 않도록 여기서 멈춘다.
                if self.model_time_left() < needed + 10:
                    self.agent_trace(
                        "agent_stop", reason="ANSWER_RETRY_TIME", step=self.agent_steps
                    )
                    self.codes.append("SEARCH_LIMIT")
                    break
                if fallback:
                    self.agent_extract_off = True
                self.agent_trace(
                    "agent_answer_retry",
                    reason="SINGLE_STAGE_FALLBACK" if fallback else "VALIDATION",
                    attempt=self.agent_answers,
                )
                retry_same = True
                continue
            if not self.agent_can_step():
                break
            gaps = self.agent_gaps(plan, draft)
        if best is not None:
            if best.coverage != "COMPLETE":
                self.codes.append("PARTIAL_ANSWER")
            return await self.finish_answer(best)
        return await self.unanswered_source_result(plan)

    def agent_can_verify_registered(self, plan, registered):
        """등록 이력만으로 답한 변화·이력 질문을 원문 탐색으로 보완할지(agent_verify_registered)."""
        return bool(
            self.hcfg.agent
            and self.hcfg.agent_verify_registered
            and plan.disposition == "PROCEED"
            # 계획의 sourceFallback 표시는 같은 질문에서도 실행마다 달라져 조건으로 쓰지 않는다.
            and not plan.registered_current_required
            and plan.source_need == "NONE"
            and plan.rdb_mode in {"HISTORY", "CURRENT_AND_HISTORY"}
            and plan.subjects
            and plan.collection is None
            and plan.character_filter is None
            and plan.answer_shape not in {"COUNT", "DISTINCT"}
            and registered.result is not None
            and registered.result.outcome == "ANSWER"
        )

    # ---- 사실 추출 → 답 작성 ----

    def agent_two_stage(self):
        """탐색의 일반 답변에만 쓴다. 항목별(perItem) 답변은 한 단계 그대로다."""
        return bool(
            self.agent_active
            and self.hcfg.agent_extract
            and not self.logical_items
            and not self.agent_extract_off
        )

    def agent_extract_groups(self, passages, limit):
        """원문 덩어리를 회차·본문 순서대로 묶는다. 묶음 수가 limit를 넘지 않게 묶음 크기를 키운다."""
        sizes = [self.agent_count_tokens(p["text"]) for p in passages]
        size = self.hcfg.agent_extract_group_tokens
        if math.ceil(sum(sizes) / size) > limit:
            size = math.ceil(sum(sizes) / limit) + 1
        groups, current, used = [], [], 0
        for passage, tokens in zip(passages, sizes, strict=True):
            if current and used + tokens > size and len(groups) < limit - 1:
                groups.append(current)
                current, used = [], 0
            current.append(passage)
            used += tokens
        if current:
            groups.append(current)
        return groups

    async def agent_extract_group(self, plan, group):
        """묶음 하나에서 사실 후보를 뽑는다(계량된 호출 1회). 실패한 묶음은 빈 목록으로 두고 계속한다."""
        key = tuple((p["references"][0]["evidenceRef"], len(p["text"])) for p in group)
        if key in self.agent_fact_cache:
            return self.agent_fact_cache[key], "cached"
        ctx = self.execution.context
        body = {
            "question": ctx.question,
            **self.conversation_payload(),
            "requestedParts": [p.model_dump(by_alias=True) for p in plan.requested_parts],
            "eventContext": plan.event_context,
            "subjects": [s.name for s in plan.subjects],
            "passages": [
                {
                    "ref": p["references"][0]["evidenceRef"],
                    "episodeNo": p["sourceEpisodeNo"],
                    "text": p["text"],
                }
                for p in group
            ],
        }
        if not self.fits(EXTRACT_PROMPT, body, response_schema(FactExtraction)):
            return [], "INPUT_LIMIT"
        try:
            result = await self.model(
                FactExtraction,
                EXTRACT_PROMPT,
                body,
                "CHAT_ANSWER",
                EXTRACT_VERSION,
                max_attempts=1,
                output_tokens=self.hcfg.agent_extract_output_tokens,
            )
        except (ModelTimeBudgetExceeded, ProviderFailure) as exc:
            # 한 묶음의 실패(출력 잘림·형식 오류·일시적 provider 오류·시간 부족)는 다른 묶음의 사실로
            # 답하게 둔다. 사용량은 Execution.call이 이미 기록했다.
            return [], type(exc).__name__
        self.agent_fact_cache[key] = result.facts
        return result.facts, "ok"

    @staticmethod
    def agent_locate_fact(fact, group):
        """사실의 인용문을 기존 검증기로 그 추출 호출에 준 묶음(group) 안에서 찾는다.

        ((조각 참조, 원문 그대로의 구절), None) 또는 (None, 사유)를 돌려준다. 인용문은 지정한 원문 덩어리
        안에 글자 그대로, 한 번만 있어야 한다(답변 인용과 같은 기준). 참조 번호만 틀린 경우에 한해 같은
        묶음의 다른 덩어리 중 정확히 한 곳에 있으면 그곳으로 바로잡는다. 그 호출에 보여 주지 않은 다른
        묶음의 참조는 같은 문장이 거기에 있어도 받지 않는다. 읽지 않은 원문에 사실을 귀속하지 않기 위함이다.
        """
        quote = fact.quote.strip()
        if not quote:
            return None, "EMPTY"
        try:
            found = resolve_support_quotes([_Quote(fact.evidence_ref, quote)], group)[0]
            return (found.evidence_refs[0], found.quote), None
        except ValueError as exc:
            reason = str(exc)
        if reason not in {"GROUNDING_QUOTE_NOT_FOUND", "GROUNDING_UNKNOWN_REFERENCE"}:
            return None, reason
        hits = []
        for passage in group:
            anchor = passage["references"][0]["evidenceRef"]
            try:
                hits.append(resolve_support_quotes([_Quote(anchor, quote)], group)[0])
            except ValueError:
                continue
        if len(hits) == 1:
            return (hits[0].evidence_refs[0], hits[0].quote), None
        return None, reason

    async def agent_extract_facts(self, plan, payload):
        """읽은 묶음 전체에서 검증된 사실 목록을 만든다. 묶음별 추출은 병렬로 돌린다."""
        passages = payload["passages"]
        # 답 작성 호출 1회와 그 수정 재시도 1회 몫은 남긴다. 병렬 호출은 각자 Execution.call로 예약하므로
        # 묶음 수를 남은 호출 몫 안으로 미리 정해 예약이 한도를 넘지 않게 한다.
        groups = self.agent_extract_groups(
            passages, max(1, min(self.hcfg.agent_extract_max_groups, self.agent_call_room() - 2))
        )
        started = time.monotonic()
        results = await asyncio.gather(
            *(self.agent_extract_group(plan, group) for group in groups),
            return_exceptions=True,
        )
        for result in results:
            # 예약 거절·실행 한도 같은 전송 오류와 예기치 못한 오류는 기존대로 실행 실패로 올린다.
            # gather가 모든 묶음을 기다린 뒤이므로 남아 도는 호출은 없다.
            if isinstance(result, BaseException):
                raise result
        order = {
            reference["evidenceRef"]: (index, reference["start"])
            for index, passage in enumerate(passages)
            for reference in passage["references"]
        }
        episodes = {
            reference["evidenceRef"]: passage["sourceEpisodeNo"]
            for passage in passages
            for reference in passage["references"]
        }
        checked, dropped, seen, report = [], {}, set(), []
        for group, (raw, status) in zip(groups, results, strict=True):
            kept = 0
            for fact in raw:
                located, reason = self.agent_locate_fact(fact, group)
                if located is None:
                    dropped[reason] = dropped.get(reason, 0) + 1
                    continue
                if located in seen:
                    continue
                seen.add(located)
                kept += 1
                checked.append((order[located[0]], fact.text.strip(), located))
            report.append(
                {
                    "tokens": sum(self.agent_count_tokens(p["text"]) for p in group),
                    "status": status,
                    "raw": len(raw),
                    "kept": kept,
                }
            )
        statuses = {item["status"] for item in report} - {"ok", "cached"}
        if statuses:
            # 읽지 못한 묶음의 원문은 답에 반영되지 않는다. 내부 진단 사유로 남긴다.
            self.codes.append(
                "EVIDENCE_CONTEXT_LIMIT" if statuses == {"INPUT_LIMIT"} else "SEARCH_LIMIT"
            )
        checked.sort(key=lambda item: item[0])
        if len(checked) > FACT_LIMIT:
            # 회차 순서로 앞의 사실만 남는다. 뒤 회차의 사실이 빠졌음을 진단 사유로 남긴다.
            self.metrics.evidence_omissions.labels("agent_facts", "ITEM_LIMIT").inc(
                len(checked) - FACT_LIMIT
            )
            self.codes.append("EVIDENCE_CONTEXT_LIMIT")
        facts = [
            _Fact(f"F{n}", text, located[0], located[1], episodes[located[0]])
            for n, (_, text, located) in enumerate(checked[:FACT_LIMIT], 1)
        ]
        self.agent_trace(
            "agent_extract",
            groups=report,
            facts=len(facts),
            verified=len(checked),
            dropped=dropped,
            seconds=round(time.monotonic() - started, 1),
        )
        return facts

    def agent_compose_payload(self, payload, facts):
        """답 작성 입력: 기존 답변 입력에서 원문 덩어리를 빼고 검증된 사실 목록을 넣는다."""
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"passages", "items", "previousItemAnswers"}
        }
        body["facts"] = [
            {"factRef": f.ref, "episodeNo": f.episode, "text": f.text, "quote": f.quote}
            for f in facts
        ]
        return body

    def agent_grounded_from(self, composed, facts, plan):
        """답 작성 출력(F* 참조)을 기존 답변 모델로 옮긴다. supports는 검증된 사실의 인용문이다.

        요구 항목의 답함/못함 분할은 claim이 실제로 답했다고 표시한 것으로 서버가 계산한다. 같은 요구를
        답하고도 '못 답함'에 함께 적은 출력은 답한 쪽으로 정리한다. 이후 기존 검증이 그대로 적용된다.
        """
        by_ref = {f.ref: f for f in facts}
        requested = requested_facets(plan)
        rows = {r.ref for r in self.chosen}
        cited = set()

        def supports(refs, limit):
            chosen, seen = [], set()
            for ref in refs:
                fact = by_ref.get(ref)
                if fact is None or (fact.evidence_ref, fact.quote) in seen:
                    continue
                # 문장의 첫 인용은 결과 상한까지, 그 뒤 인용은 여유 한도까지만 새 조각을 쓴다.
                room = CITED_CHUNK_SOFT_LIMIT if chosen else RETRIEVAL_SOURCE_LIMIT
                if fact.evidence_ref not in cited and len(cited) >= room:
                    continue
                seen.add((fact.evidence_ref, fact.quote))
                cited.add(fact.evidence_ref)
                chosen.append(EvidenceSupport(evidence_ref=fact.evidence_ref, quote=fact.quote))
                if len(chosen) == limit:
                    break
            if not chosen:
                raise ValueError("Unknown evidence or setting reference")
            return chosen

        claims = []
        for claim in composed.claims:
            quotes = supports(claim.fact_refs, 12)
            answered = [f for f in dict.fromkeys(claim.answered_facets) if f in requested]
            related = [
                f
                for f in dict.fromkeys(claim.related_facets)
                if f in requested and f not in answered
            ]
            if not answered and not related and requested:
                # 요구 표시 없이 쓴 문장은 '관련 맥락'으로만 둔다(답한 것으로 세지 않는다).
                related = [requested[0]]
            claims.append(
                AgentGroundedClaim(
                    text=claim.text,
                    evidence_refs=list(dict.fromkeys(q.evidence_ref for q in quotes)),
                    row_refs=[r for r in dict.fromkeys(claim.row_refs) if r in rows],
                    answered_facets=answered,
                    related_facets=related,
                    event_status=claim.event_status,
                    supports=quotes,
                    event_match=claim.event_match,
                )
            )
        covered = {f for claim in claims for f in claim.answered_facets}
        missing = [f for f in requested if f not in covered]
        explanations, explained = [], set()
        for explanation in composed.missing_explanations:
            if explanation.facet in missing and explanation.facet not in explained:
                explained.add(explanation.facet)
                explanations.append(explanation)
        conflicts = []
        for conflict in composed.conflicts:
            quotes = supports(conflict.fact_refs, 3)
            known = [r for r in dict.fromkeys(conflict.row_refs) if r in rows]
            if known:
                conflicts.append(
                    EvidenceConflict(
                        row_refs=known,
                        evidence_refs=list(dict.fromkeys(q.evidence_ref for q in quotes)),
                        supports=quotes,
                        event_match=conflict.event_match,
                    )
                )
        requests_parts = bool(plan.requested_parts or plan.requested_facets)
        return AgentGroundedAnswer(
            coverage="COMPLETE",
            claims=claims,
            conflicts=conflicts,
            unanswered_row_refs=[
                r for r in dict.fromkeys(composed.unanswered_row_refs) if r in rows
            ],
            missing_facets=missing if requests_parts else [],
            missing_explanations=explanations if requests_parts else [],
        )

    async def agent_extract_answer(self, plan, payload, valid, attempts):
        """두 단계 답변. 기존 검증(valid)을 통과한 답을 돌려주며 이후 처리는 answer()가 한다.

        추출 1회 + 작성 1회를 부를 호출 몫이 없거나 사실 목록이 답 작성 입력에 들어가지 않으면
        None을 돌려주고, answer()가 같은 원문으로 한 단계 답변을 부른다.
        """
        if self.agent_call_room() < 2:
            return None
        facts = await self.agent_extract_facts(plan, payload)
        if not facts:
            return self.empty_source_answer(plan)
        body = self.agent_compose_payload(payload, facts)
        if not self.fits(COMPOSE_PROMPT, body, response_schema(ComposedAnswer)):
            self.agent_trace("agent_compose_skipped", reason="INPUT_LIMIT", facts=len(facts))
            return None
        holder = {}

        def check(composed):
            holder["answer"] = valid(self.agent_grounded_from(composed, facts, plan))

        started = time.monotonic()
        composed = await self.model(
            ComposedAnswer,
            COMPOSE_PROMPT,
            body,
            "CHAT_ANSWER",
            COMPOSE_VERSION,
            check,
            max_attempts=max(1, min(attempts, self.agent_call_room())),
        )
        self.agent_trace(
            "agent_compose",
            facts=len(facts),
            claims=len(composed.claims),
            relevant_facts=len(set(composed.relevant_fact_refs)),
            cited_facts=len({ref for claim in composed.claims for ref in claim.fact_refs}),
            missing=len(holder["answer"].missing_facets),
            seconds=round(time.monotonic() - started, 1),
        )
        return holder["answer"]
