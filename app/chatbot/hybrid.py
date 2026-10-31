"""Bounded policy for RDB, linked quotations and current-manuscript retrieval."""

import logging
import math
from dataclasses import dataclass, replace

import tiktoken
from sqlalchemy.exc import SQLAlchemyError

from app.chatbot.answer_recovery import recover_claims
from app.chatbot.client import ChatTransportError
from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.hybrid_grounding import resolve_support_quotes, resolve_support_references
from app.chatbot.hybrid_items import (
    FACET_LABELS,
    answer_evidence_refs,
    apply_event_scope,
    empty_item_answer,
    event_matches,
    group_items,
    merge_item_answers,
    merge_missing_explanations,
    render_item_answers,
    render_missing_explanations,
    request_labels,
    requested_facets,
    select_context_seeds,
    validate_item_answer,
)
from app.chatbot.hybrid_metrics import HybridMetrics, ObservedEmbeddingClient, ObservedTextClient
from app.chatbot.hybrid_prompts import ANSWER_PROMPT, ANSWER_VERSION, PLAN_PROMPT, PLAN_VERSION
from app.chatbot.providers import ChatEmbeddingClient
from app.chatbot.qa import ChatQaHandler, ModelTimeBudgetExceeded, _Session, response_schema
from app.chatbot.qa_prompts import FIELD_VERSION
from app.chatbot.qa_render import (
    AnswerRow,
    current_changed,
    message,
    notices,
    render_answer,
    safe_text,
    setting_label,
)
from app.chatbot.qa_synthesis import logical_groups
from app.chatbot.qa_validation import (
    ChatResponseContractError,
    LookupClarificationRequired,
    ResponseValidationFailure,
)
from app.chatbot.runtime import ProviderFailure, ProviderOutputTruncated
from app.clients.setting_tools_client import SettingToolClientError
from app.retrieval.evidence import compose_evidence
from app.retrieval.search_contracts import SearchScope, SearchScopeUnavailable
from app.schemas.chat_execution import ChatResult, Finish
from app.schemas.chat_hybrid import (
    EvidenceConflict,
    EvidenceSupport,
    GroundedAnswer,
    HybridPlan,
    ItemAnswer,
)
from app.schemas.chat_qa import QuestionPlan
from app.schemas.chat_retrieval import RetrievalReference
from app.schemas.setting_tools import SettingToolQuery

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Evidence:
    ref: str
    text: str
    episode: int
    chunk: object = None
    direct_ref: object = None
    direct_item: object = None


def wire_reference(chunk):
    return RetrievalReference(
        chunk_id=chunk.chunk_id,
        episode_id=chunk.episode_id,
        source_episode_no=chunk.episode_no,
        source_hash=chunk.source_hash,
        source_version=chunk.source_version,
        quote=chunk.text,
        start_offset=chunk.start_offset,
        end_offset=chunk.end_offset,
    )


class _QueryEmbedding:
    def __init__(self, session):
        self.session = session
        self.model = "text-embedding-3-small"
        self.version = session.handler.hybrid.embedding_version
        self.client = ChatEmbeddingClient(
            ObservedEmbeddingClient(session.handler.embedding, session.metrics),
            session.execution,
            model_name=self.model,
        )

    async def create_embedding(self, text):
        session = self.session
        if (
            getattr(session.handler.embedding, "model", None) != self.model
            or session.handler.embedding.version != self.version
        ):
            raise ProviderFailure()
        if session.embedding_calls >= session.handler.hybrid.max_query_embeddings:
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        session.embedding_calls += 1
        response = await self.client.create_embeddings([text], validate=self.validate_response)
        return response.embeddings[0]

    def validate_response(self, response):
        # Validate before the metered invocation settles its measured usage.
        if isinstance(response.input_token_count, int) and response.input_token_count >= 0:
            self.session.metrics.tokens.labels("embedding_input").inc(response.input_token_count)
        vectors = response.embeddings
        if (
            response.model != self.model
            or len(vectors) != 1
            or len(vectors[0]) != 1536
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in vectors[0]
            )
            or not any(vectors[0])
        ):
            raise ValueError("Invalid query embedding response")


class HybridQaHandler(ChatQaHandler):
    def __init__(self, delegate, search, embedding, settings=None, hybrid=None, metrics=None):
        self.metrics = metrics or HybridMetrics()
        super().__init__(
            ObservedTextClient(delegate, self.metrics, (PLAN_VERSION, FIELD_VERSION)), settings
        )
        self.search, self.embedding = search, embedding
        self.hybrid = hybrid or HybridSettings()

    async def __call__(self, execution):
        session = _HybridSession(self, execution)
        try:
            result = await session.run()
        except (SearchScopeUnavailable, SQLAlchemyError):
            session.codes.append("SEARCH_UNAVAILABLE")
            result = message(
                "INSUFFICIENT_EVIDENCE",
                "원문 검색 범위나 저장소를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                session.codes + ["SEARCH_UNAVAILABLE"],
            )
        except ChatTransportError as exc:
            if exc.code in {"SOURCE_CHANGED", "STORAGE_UNAVAILABLE"}:
                session.codes.append(exc.code)
                result = message(
                    "INSUFFICIENT_EVIDENCE",
                    "답변에 사용한 원문을 현재 상태로 검증하지 못해 답변을 보류했습니다.",
                    session.codes + [exc.code],
                )
            else:
                result = Finish(
                    failure=exc.code
                    if exc.code in {"QUOTA_EXHAUSTED", "EXECUTION_LIMIT_EXCEEDED"}
                    else "NETWORK_FAILURE"
                )
        except SettingToolClientError as exc:
            if exc.code == "CONCURRENT_UPDATE" or exc.status_code == 409:
                result = current_changed(session.codes)
            else:
                result = Finish(
                    failure="EXECUTION_LIMIT_EXCEEDED"
                    if exc.status_code == 429
                    else "NETWORK_FAILURE"
                )
        except LookupClarificationRequired:
            result = message(
                "CLARIFICATION",
                "어떤 인물의 어떤 설정을 다시 확인할지 이름과 항목을 함께 알려주세요.",
                session.codes,
            )
        except ModelTimeBudgetExceeded:
            result = Finish(failure="EXECUTION_LIMIT_EXCEEDED")
        except ProviderFailure:
            result = Finish(failure="PROVIDER_FAILURE")
        finally:
            self.metrics.rounds.observe(session.rounds)
        self.metrics.outcomes.labels(result.failure or result.result.outcome).inc()
        known = {
            "SEARCH_UNAVAILABLE",
            "SOURCE_CHANGED",
            "STORAGE_UNAVAILABLE",
            "INDEX_PARTIAL",
            "NO_SEARCH_EVIDENCE",
            "SEARCH_LIMIT",
            "CONTEXT_LIMIT",
            "EVIDENCE_CONTEXT_LIMIT",
            "PARTIAL_ANSWER",
            "SETTING_SOURCE_CONFLICT",
        }
        for code in set(session.codes) & known:
            self.metrics.reasons.labels(code).inc()
        return result


class _HybridSession(_Session):
    def __init__(self, handler, execution):
        super().__init__(handler, execution)
        self.hcfg, self.metrics = handler.hybrid, handler.metrics
        self.model_calls = self.embedding_calls = self.rounds = 0
        self.evidences = []
        self.chosen = []
        self.fallback_direct_checks = 0
        self.logical_items = []
        self.item_draft = None
        self.evidence_registry = {}
        self.item_searches = {}
        self.event_scope = None
        self.render_registered_values = False
        self.collection_conditions = []
        self.has_collection_condition = False
        self.plan = None
        self.current_requirement_confirmed = False
        self.current_requirement_targets = set()
        self.current_requirement_resolved = False
        self.supported_evidence = ()
        self.recovery_evidence = ()
        self.answer_validation_failed = False
        self.ordered_coverage = None
        self.ordered_kept = ()

    def trace(self, stage, **data):
        # No questions, source text, model output, member/work IDs or secrets.
        logger.info(
            "Chat retrieval trace attempt=%s stage=%s data=%s",
            getattr(self.execution.context, "attempt_id", None),
            stage,
            data,
        )

    def before_model_call(self, purpose):
        if self.model_calls >= self.hcfg.max_model_calls:
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        self.model_calls += 1

    def record_model_response(self, purpose, response):
        for name, value in (
            ("text_input", response.input_token_count),
            ("text_cached_input", response.cached_input_token_count),
            ("text_output", response.output_token_count),
        ):
            if isinstance(value, int) and value >= 0:
                self.metrics.tokens.labels(name).inc(value)

    def consume(self, method):
        if self.tool_count >= min(self.cfg.max_tool_calls, self.execution.context.max_tool_calls):
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        self.tool_count += 1
        self.metrics.operations.labels(method).inc()

    async def query(self, request):
        self.metrics.operations.labels("rdb").inc()
        return await super().query(request)

    @property
    def scope(self):
        ctx = self.execution.context
        return SearchScope(
            ctx.member_id, ctx.work_id, self.source_from or 1, self.source_to or 2147483647
        )

    async def search(self, method, *args, **kwargs):
        self.consume(method)
        with self.metrics.latency.labels(method).time():
            result = await getattr(self.handler.search, method)(self.scope, *args, **kwargs)
        self.codes.extend(result.readiness.limitations)
        if result.readiness.partial:
            self.codes.append("INDEX_PARTIAL")
        if result.limited:
            self.codes.append("SEARCH_LIMIT")
        self.metrics.chunks.labels("returned").inc(len(result.chunks))
        self.trace(
            "search",
            method=method,
            round=self.rounds,
            limited=result.limited,
            candidates=[
                {
                    "ref": str(c.chunk_id),
                    "rank": n,
                    "episode": c.episode_no,
                    "index": c.chunk_index,
                    "score": round(c.score, 5),
                }
                for n, c in enumerate(result.chunks, 1)
            ],
        )
        return result.chunks

    async def register(self, chunks):
        if not chunks:
            return
        self.consume("receipt")
        await self.execution.client.retrieval_sources(
            self.execution.dispatch, self.execution.runner, [wire_reference(c) for c in chunks]
        )

    def source_tool_reserve(self, plan):
        # 원문 검색이 필요한 계획은 등록 조회가 도구 예산을 다 쓰지 않도록
        # 검색·이웃·영수증·재검증 몫(라운드당 3 + 마무리 2)을 미리 남긴다.
        if plan.source_need == "NONE":
            return 0
        # 3라운드는 선택적이라 처음 두 라운드 몫만 예약한다.
        rounds = min(self.hcfg.max_rounds, 2)
        return 3 * rounds + 2 + (1 if plan.ordering != "NONE" else 0)

    def row_page_reserve(self, plan, subject, index, *, base):
        """인물별 등록 조회가 남길 도구 수. 집중 조회와 등록 전용 계획은 기존 예약을 유지한다."""
        # 새 schema는 focus 객체를 항상 채우므로 내용(유형·항목)이 비어 있는지로 집중 여부를 본다.
        focused = subject.focus.fact_type is not None or subject.focus.attribute is not None
        if focused or plan.source_need == "NONE":
            return base
        limit = min(self.cfg.max_tool_calls, self.execution.context.max_tool_calls)
        floor = max(base, self.source_tool_reserve(plan))
        remaining = len(plan.subjects) - index
        # 뒤에 남은 인물들의 조회(FIND 1회씩)와 원문 검색 몫을 뺀 나머지를 인물 수로 나눠 쓴다.
        spare = max(0, limit - self.tool_count - floor - (remaining - 1))
        share = max(1, spare // remaining) if spare else 0
        return limit - self.tool_count - share

    def ordering_anchors(self, plan, objects=None):
        """최초/최후 탐색의 기준 표현. 인물명은 필수, 대상·사건 단어는 절반 이상 일치로 요구한다.

        인물명을 빼면 '스킬' 같은 일반 단어의 작품 첫 언급으로 흘러가고, 대상 단어를 모두
        요구하면 활용형 차이로 0건이 된다. 대상 표현은 집중 항목이 있으면 그것, 없으면 인물명을
        뺀 검색어다.
        """
        # 계획은 인물을 최대 3명 허용하므로 모두 필수 표현으로 보존한다.
        required = list(dict.fromkeys(s.name.strip() for s in plan.subjects if s.name.strip()))[:3]
        lowered = {n.casefold() for n in required}
        focus = [s.focus.attribute for s in plan.subjects if s.focus.attribute]
        if objects is None:
            objects = focus or [t for t in plan.search_terms if t.strip().casefold() not in lowered]
        optional = list(
            dict.fromkeys(
                w for term in objects for w in term.split() if w and w.casefold() not in lowered
            )
        )[:4]
        return required, optional

    async def ordered_search(self, plan, found, objects=None):
        """검색어가 등장하는 출처 회차를 처음/마지막부터 읽어 일반 keyword 후보 앞에 둔다."""
        required, optional = self.ordering_anchors(plan, objects)
        anchors = [*required, *optional]
        # 이번 라운드의 남은 비용: 정렬 검색 1 + 이웃 1 + 영수증 1 + 최종 재검증 1
        if not anchors or not self.tools_available(4):
            self.codes.append("SEARCH_LIMIT")
            return found
        ordered = await self.search(
            "keyword_ordered",
            required,
            optional,
            order=plan.ordering,
            limit=self.hcfg.keyword_top_k,
        )
        self.codes.append(f"ORDERED_SEARCH_{plan.ordering}")
        self.ordered_coverage = {
            "order": plan.ordering,
            "anchors": anchors,
            "boundaryEpisode": ordered[0].episode_no if ordered else None,
            "truncated": len(ordered) >= self.hcfg.keyword_top_k,
        }
        # 순서상 앞의 몇 개만 우선 배치한다. 전부 앞에 두면 점수 1.0 근처의 초반 언급이
        # 질문과 더 밀접한 일반 keyword 후보를 근거 상한 밖으로 밀어낸다.
        kept = list(ordered[: self.hcfg.ordered_top_k])
        self.ordered_kept = tuple(kept)
        self.trace(
            "ordered", round=self.rounds, anchors=len(anchors), found=len(ordered), kept=len(kept)
        )
        seen = {c.chunk_id for c in kept}
        return [*kept, *(c for c in found if c.chunk_id not in seen)]

    def extra_round_affordable(self):
        """3라운드는 검색·답변·마무리를 서버 마감 안에 끝낼 시간이 남아 있을 때만 시작한다."""
        last = max(10.0, getattr(self, "last_model_seconds", 0.0))
        affordable = self.model_time_left() > 2 * last + 10.0
        self.trace("extra_round", round=self.rounds + 1, affordable=affordable)
        return affordable

    def episode_ordered_head(self, plan, found):
        """정렬 질문의 의미 검색 후보 중 가장 이른/늦은 ordered_top_k개. 유사도 순서보다 앞에 둔다."""
        head = sorted(
            found,
            key=lambda c: (c.episode_no, c.chunk_index, str(c.chunk_id)),
            reverse=plan.ordering == "LATEST",
        )[: self.hcfg.ordered_top_k]
        self.trace(
            "ordered_vector",
            round=self.rounds,
            found=len(found),
            kept=len(head),
            boundary=head[0].episode_no if head else None,
        )
        return head

    async def focus_neighbors(self):
        """PARTIAL 초안이 인용한 장면의 앞뒤를 더 읽는다. 회차는 맞췄지만 세부가 빠진 경우용."""
        seeds = [e.chunk for e in self.supported_evidence if e.chunk]
        if not seeds or not self.hcfg.focus_radius:
            return ()
        # 이번 라운드의 남은 비용: 이웃 1 + 영수증 1 + 최종 재검증 1 + 최종 영수증 1
        if not self.tools_available(4):
            self.codes.append("SEARCH_LIMIT")
            return ()
        found = await self.search(
            "neighbors", seeds, radius=self.hcfg.focus_radius, limit=self.hcfg.max_neighbors
        )
        self.trace("focus", round=self.rounds, seeds=len(seeds), found=len(found))
        return found

    def tools_available(self, cost, *, direct_checks=True):
        reserved = max(
            self.fallback_direct_checks,
            (len({e.direct_ref for e in self.evidences if e.direct_ref}) if direct_checks else 0),
        )
        reserved += len(self.collection_checks) + self.current_check_count()
        return self.tool_count + cost + reserved <= min(
            self.cfg.max_tool_calls, self.execution.context.max_tool_calls
        )

    def current_check_count(self):
        # Collection read tokens already protect all selected subjects together.
        if self.plan is not None and self.plan.collection is not None:
            return 0
        return len(
            {(row.domain, row.item.subject_id) for row in self.chosen if row.item.kind == "CURRENT"}
        )

    def models_available(self, *, embedding=False):
        # Query embeddings share the execution call ledger with text generation.
        # A search is useful only when at least one answer invocation can follow it.
        return (
            self.model_calls < self.hcfg.max_model_calls
            and self.execution.calls + 1 + int(embedding) <= self.execution.context.max_calls
            and self.has_model_time()
        )

    async def run(self):
        ctx = self.execution.context
        if ctx.operational or not ctx.question or not ctx.question.strip():
            return message("CLARIFICATION", "조회할 대상과 질문을 알려주세요.", self.codes)

        def valid(plan):
            self.validate_focus(plan)
            explicit = self.observed_question_text()
            if any(s.name.strip().casefold() not in explicit for s in plan.subjects):
                raise ChatResponseContractError("UNOBSERVED_SUBJECT_NAME")
            # Keyword search splits on whitespace too: grouping observed words
            # into one entry must not reject the same query as separate entries.
            if any(
                word.casefold() not in explicit
                for term in plan.search_terms
                for word in term.split()
            ):
                raise ChatResponseContractError("UNOBSERVED_SEARCH_TERM")

        plan = await self.model(
            HybridPlan,
            PLAN_PROMPT,
            self.plan_payload(PLAN_PROMPT, response_schema(HybridPlan)),
            "CHAT_INTERPRETATION",
            PLAN_VERSION,
            valid,
        )
        self.plan = plan
        self.trace(
            "plan",
            rdb=plan.rdb_mode,
            source=plan.source_need,
            parts=len(plan.requested_parts),
            event=plan.event_context is not None,
            ordering=plan.ordering,
            registered_current_required=plan.registered_current_required,
        )
        # Old saved planner records keep their earlier rendering contract. New
        # provider schemas always require this explicit presentation decision.
        self.render_registered_values = (
            plan.registered_current_required
            or plan.include_registered_values
            or "include_registered_values" not in plan.model_fields_set
        )
        self.event_scope = plan.event_scope
        if plan.disposition != "PROCEED" or plan.source_need == "NONE":
            # Registered-only synthesis and collection queries share the base handler.
            # Terminal guidance does not execute the tentative lookup. Its
            # filters are meaningful only when the converted intent can query.
            registered = await self.answer_plan(
                QuestionPlan(
                    intent=plan.rdb_mode if plan.disposition == "PROCEED" else plan.disposition,
                    subjects=plan.subjects,
                    include_evidence=plan.include_evidence,
                    episode_from=plan.episode_from,
                    episode_to=plan.episode_to,
                    character_filter=plan.character_filter
                    if plan.disposition == "PROCEED"
                    else None,
                    collection=plan.collection if plan.disposition == "PROCEED" else None,
                    answer_shape=plan.answer_shape,
                )
            )
            if not self.can_supplement_registered(plan, registered):
                return registered
            if not self.models_available() or not self.tools_available(4):
                return registered
            self.chosen = list(self.selected_rows)
            plan = HybridPlan.model_validate(
                {**plan.model_dump(), "source_need": "SCENE", "source_fallback": False}
            )
            self.plan = plan
            self.trace("registered_fallback", reasons=sorted(self.lookup_gaps))
            return await self.run_source(plan, collect=False)
        return await self.run_source(plan)

    def can_supplement_registered(self, plan, registered):
        # Absence is recorded where it is observed. Public warnings/empty text
        # cannot distinguish missing information from a failed or truncated read.
        blocked = {
            "PARTIAL_RESULTS",
            "CONVERSATION_LIMIT",
            "CONTEXT_LIMIT",
            "VALUE_LIMIT",
            "SELECTION_LIMIT",
            "ANSWER_LIMIT",
            "CURRENT_CHANGED",
            "SOURCE_CHANGED",
            "STORAGE_UNAVAILABLE",
            "INVALID_QUOTE",
            "OUT_OF_SCOPE",
            "SOURCE_PURGED",
            "SOURCE_MISSING",
        }
        return bool(
            plan.disposition == "PROCEED"
            and plan.source_fallback
            and not plan.registered_current_required
            and plan.source_need == "NONE"
            and plan.subjects
            and plan.collection is None
            and plan.character_filter is None
            and plan.answer_shape not in {"COUNT", "DISTINCT"}
            and registered.result is not None
            and registered.result.outcome == "INSUFFICIENT_EVIDENCE"
            and self.lookup_gaps
            and not blocked.intersection(self.codes)
        )

    async def run_source(self, plan, *, collect=True):
        """Reuse the same scoped execution, selected rows and remaining budgets."""
        ctx = self.execution.context
        if not self.range_allowed(plan):
            return message(
                "CLARIFICATION",
                "허용된 출처 회차 범위 안에서 질문해 주세요.",
                self.codes + ["OUT_OF_SCOPE"],
            )
        self.include_sources = True
        self.codes.append("NARRATIVE_SOURCE_RANGE")
        if self.source_from is not None or self.source_to is not None:
            self.codes.append("SOURCE_RANGE")
        if collect:
            stop = await self.collect_rows(plan)
            if stop:
                return stop
        if plan.registered_current_required:
            self.current_requirement_confirmed = self.current_requirement_satisfied(plan)
            if not self.current_requirement_confirmed:
                self.codes.append("REGISTERED_CURRENT_UNCONFIRMED")
        if not self.models_available():
            registered = await self.current_without_source(plan)
            if registered is not None:
                return registered
            return message(
                "INSUFFICIENT_EVIDENCE",
                "관련 자료를 읽었지만 이번 처리 한도 안에서 원문 답변까지 확인하지 못했습니다. 질문 범위를 좁혀 주세요.",
                self.codes + ["CONTEXT_LIMIT"],
            )
        if plan.per_item:
            self.logical_items = group_items(
                self.chosen, current_targets=plan.rdb_mode == "CURRENT_AND_HISTORY"
            )
        answer_schema = response_schema(GroundedAnswer)
        self.trim_conversation_to_fit(
            ANSWER_PROMPT, lambda: self.answer_payload(plan), answer_schema
        )
        if (plan.per_item and len(self.logical_items) > 100) or not self.fits(
            ANSWER_PROMPT, self.answer_payload(plan), answer_schema
        ):
            return message(
                "CLARIFICATION",
                "요청한 설정 항목이 많아 각각의 근거를 함께 확인하기 어렵습니다. 조회할 항목이나 유형을 좁혀 주세요.",
                self.codes + ["CONTEXT_LIMIT"],
            )
        if self.logical_items:
            return await self.run_items(plan)
        # A direct lookup also needs a final recheck. Keep room for a minimal
        # search -> receipt -> final revalidation -> final receipt path.
        await self.evidence(
            self.chosen,
            reserve=3 * self.hcfg.max_rounds
            + 2
            + len(self.collection_checks)
            + self.current_check_count(),
            revalidate=True,
        )
        self.add_direct_evidence()
        draft = None
        partial_snapshot = None
        # A linked quote can already explain the cause/process: no query embedding is needed.
        if self.evidences:
            draft = await self.answer(plan)
            if self.source_answer_ready(plan, draft):
                return await self.finish_answer(draft)
            if draft.coverage == "PARTIAL":
                partial_snapshot = (draft, tuple(self.evidences))
                self.preserve_supported_evidence(*partial_snapshot)
                self.fallback_direct_checks = len(
                    {e.direct_ref for e in self.evidences if e.direct_ref}
                )

        terms = list(plan.search_terms) or [ctx.question]
        if plan.per_item:
            # Actual setting names improve searches such as "각 능력은 어떻게 얻었어?";
            # no model-authored key or invented synonym is promoted into the query.
            terms.extend(r.item.display_name for r in self.chosen if r.item.display_name)
        query = " ".join(dict.fromkeys(terms))
        if len(query) > 1000:
            self.codes.append("SEARCH_QUERY_LIMIT")
            query = query[:1000]
        candidates = []
        for round_no in range(1, self.hcfg.max_rounds + 1):
            if not self.tools_available(4) or not self.models_available(embedding=round_no > 1):
                self.codes.append("SEARCH_LIMIT")
                break
            if round_no >= 3 and not self.extra_round_affordable():
                self.codes.append("SEARCH_LIMIT")
                break
            self.rounds = round_no
            if round_no == 1:
                hints = tuple(
                    dict.fromkeys(
                        item.evidence.episode_id
                        for row in self.chosen
                        for _, item in row.evidence
                        if item.evidence.episode_id is not None
                    )
                )
                found = await self.search(
                    "keyword", query, limit=self.hcfg.keyword_top_k, preferred_episode_ids=hints
                )
                if plan.ordering != "NONE":
                    found = await self.ordered_search(plan, found)
            else:
                if self.embedding_calls >= self.hcfg.max_query_embeddings:
                    # 설정으로 라운드 수만 늘리고 임베딩 허용량을 그대로 둔 경우도 실행 오류 없이 멈춘다.
                    self.codes.append("SEARCH_LIMIT")
                    break
                vector_limit = self.hcfg.vector_top_k
                if plan.ordering != "NONE":
                    # 의미 검색은 활용형 차이를 넘어 장면을 찾으므로 정렬 질문은 더 넓게 읽고
                    # 그중 가장 이른/늦은 몇 개를 앞세운다. 임베딩 호출 수는 그대로다.
                    vector_limit = min(vector_limit * 3, 100)
                found = await self.search(
                    "vector",
                    self.expansion_query(plan, draft, query),
                    _QueryEmbedding(self),
                    limit=vector_limit,
                    # A returned candidate may have been omitted by context limits.
                    # Only passages actually exposed to the answer model were read.
                    excluded_chunk_ids=tuple(
                        dict.fromkeys(e.chunk.chunk_id for e in self.evidences if e.chunk)
                    ),
                )
            if (
                round_no > 1
                and plan.requested_parts
                and draft is not None
                and draft.coverage == "NONE"
                and not self.answer_validation_failed
            ):
                # The previous context answered none of the requested parts. Do not
                # let its incomparable lexical scores crowd out the new vector hits.
                candidates = list(found)
            else:
                candidates.extend(found)
            if found and self.hcfg.neighbor_radius and self.tools_available(4):
                candidates.extend(
                    await self.search(
                        "neighbors",
                        found,
                        radius=self.hcfg.neighbor_radius,
                        limit=self.hcfg.max_neighbors,
                    )
                )
            elif found and self.hcfg.neighbor_radius:
                self.codes.append("SEARCH_LIMIT")
            ordered_head = ()
            if plan.ordering != "NONE":
                # 1라운드는 정렬 검색이 남긴 청크를, 이후 라운드는 의미 검색의 가장 이른/늦은 후보를
                # 조합 우선순위로 넘긴다. 점수만으로는 1.0 keyword 후보와 회차 오름차순에 밀린다.
                ordered_head = (
                    self.ordered_kept if round_no == 1 else self.episode_ordered_head(plan, found)
                )
            focus = ()
            if round_no > 1 and partial_snapshot is not None:
                # 앞 라운드가 일부만 답했다면 그 장면의 이어지는 대목을 새 전역 후보보다 먼저 읽는다.
                focus = await self.focus_neighbors()
                candidates.extend(focus)
            previous = self.evidence_identity()
            self.add_search_evidence(
                candidates,
                fresh=found if round_no > 1 and plan.requested_parts else (),
                focus=focus,
                ordered=ordered_head,
            )
            if self.evidence_identity() == previous:
                continue
            await self.register([e.chunk for e in self.evidences if e.chunk])
            draft = await self.answer(plan)
            if self.source_answer_ready(plan, draft):
                return await self.finish_answer(draft)
            # Equal PARTIAL coverage does not establish an improvement. Keep the first
            # supported answer and its E-reference mapping, which recomposition can replace.
            if draft.coverage == "PARTIAL" and partial_snapshot is None:
                partial_snapshot = (draft, tuple(self.evidences))
                self.fallback_direct_checks = len(
                    {e.direct_ref for e in self.evidences if e.direct_ref}
                )
            elif draft.coverage == "PARTIAL" and (
                plan.requested_facets or plan.requested_parts or draft.conflicts
            ):
                partial_snapshot = self.merge_partial_snapshot(partial_snapshot, draft, plan)
            if partial_snapshot is not None:
                self.preserve_supported_evidence(*partial_snapshot)
        if partial_snapshot is not None:
            draft, evidence_snapshot = partial_snapshot
            self.evidences = list(evidence_snapshot)
            if draft.coverage != "COMPLETE":
                self.codes.append("PARTIAL_ANSWER")
            # Finalization still revalidates the restored sources and registers their quotes.
            return await self.finish_answer(draft)
        if self.has_collection_condition and (
            self.uncertain_condition_rows() or not self.confirmed_condition_rows()
        ):
            await self.revalidate_settings()
            return message(
                "INSUFFICIENT_EVIDENCE",
                "현재 등록 자료로 질문의 조건에 해당하는 대상을 확정하지 못해 관련 원문 설명을 보류했습니다.",
                self.codes,
            )
        registered = await self.current_without_source(plan)
        if registered is not None:
            return registered
        if plan.per_item and self.chosen:
            await self.revalidate_settings()
            names = ", ".join(
                dict.fromkeys(safe_text(r.item.display_name or "설정") for r in self.chosen)
            )
            return message(
                "INSUFFICIENT_EVIDENCE",
                f"{names}: 등록된 항목은 확인했지만, 이번에 조회한 원문으로는 요청한 경위나 이유를 확인하지 못했습니다.",
                self.codes + ["NO_SEARCH_EVIDENCE"],
            )
        if plan.requested_parts:
            needs = "·".join(safe_text(p.description) for p in plan.requested_parts)
            text = (
                f"확인한 자료만으로 확정해서 답하기 어려운 내용입니다: {needs}. "
                "해당 사실이 없다고 단정한 것은 아닙니다."
            )
            scope_codes = {
                "SEARCH_LIMIT",
                "SEARCH_QUERY_LIMIT",
                "EVIDENCE_CONTEXT_LIMIT",
                "EVIDENCE_LIMIT",
                "CONTEXT_LIMIT",
            }
            if scope_codes.intersection(self.codes):
                text += (
                    " 관련 자료와 원문을 모두 살펴본 결과는 아니므로 추가 근거가 있을 수 있습니다."
                )
            return message(
                "INSUFFICIENT_EVIDENCE",
                text,
                [
                    c
                    for c in self.codes
                    if c
                    not in scope_codes
                    | {
                        "NARRATIVE_SOURCE_RANGE",
                        "NO_RELATED_SETTING",
                        "NO_SEARCH_EVIDENCE",
                        "HISTORY_IS_NOT_A_STATE_SNAPSHOT",
                    }
                ],
            )
        return message(
            "INSUFFICIENT_EVIDENCE",
            "조회한 원문에서 질문을 뒷받침할 근거를 충분히 찾지 못했습니다. 작품 전체에 해당 사실이 없다는 뜻은 아닙니다.",
            self.codes + ["NO_SEARCH_EVIDENCE"],
        )

    def source_answer_ready(self, plan, answer):
        if answer.coverage == "COMPLETE":
            return True
        # A reported difference is not automatically a missing request. Once all
        # explicit parts are supported, preserve the conflict/partial status and
        # finish instead of searching the same question just to change metadata.
        # This runs after event/current-condition guards, not on the raw response.
        return bool(
            answer.coverage == "PARTIAL"
            and plan.requested_parts
            and not plan.per_item
            and not answer.missing_facets
            and not answer.unanswered_row_refs
            and set(requested_facets(plan))
            <= {facet for claim in answer.claims for facet in claim.answered_facets}
            and (not plan.registered_current_required or self.current_requirement_confirmed)
        )

    async def collect_rows(self, plan):
        if plan.rdb_mode == "NONE":
            return None
        if plan.collection is not None:
            stop = await self.collection_rows(plan.collection, reserve=4)
            if stop:
                return stop
            if plan.collection.predicate:
                self.has_collection_condition = True
                groups = logical_groups(self.rows, by_subject=True)
                decisions = await self.interpret_condition(
                    groups, plan.collection.predicate, reserve_models=1
                )
                by_ref = {d.item_ref: d for d in decisions}
                allowed = {
                    row.ref
                    for ref, rows in groups.items()
                    if ref not in by_ref or by_ref[ref].verdict != "NO_MATCH"
                    for row in rows
                }
                self.collection_conditions = [
                    {
                        "rowRefs": [r.ref for r in rows],
                        "verdict": by_ref[ref].verdict if ref in by_ref else "UNKNOWN",
                    }
                    for ref, rows in groups.items()
                ]
                if any(c["verdict"] == "UNKNOWN" for c in self.collection_conditions):
                    self.codes.append("CONDITION_UNCERTAIN")
                self.rows = [row for row in self.rows if row.ref in allowed]
                self.focused_refs.intersection_update(allowed)
            self.chosen, stop = await self.select_rows(plan, allow_missing=True)
            return stop
        targets = set()
        self.current_requirement_resolved = bool(plan.subjects)
        for index, subject in enumerate(plan.subjects):
            item, stop = await self.resolve_subject(subject, allow_missing=True)
            if stop:
                return stop
            if item is None:
                self.current_requirement_resolved = False
                continue
            targets.add((subject.domain, item.subject_id))
            modes = (
                ["CURRENT", "HISTORY"]
                if plan.rdb_mode == "CURRENT_AND_HISTORY"
                else ["HISTORY" if plan.rdb_mode == "HISTORY" else "CURRENT"]
            )
            if modes == ["CURRENT", "HISTORY"] and len(plan.subjects) > 1:
                # 여러 인물의 현재 속성 비교에 이력까지 읽으면 항목·페이지 조회가 도구 예산을 넘겨
                # '항목을 모두 확인하지 못함'으로 끝났다. 비교는 현재값으로 답하고 이력 생략을 알린다.
                modes = ["CURRENT"]
                if "HISTORY_SKIPPED_FOR_COMPARISON" not in self.codes:
                    self.codes.append("HISTORY_SKIPPED_FOR_COMPARISON")
            gaps_before = set(self.lookup_gaps)
            stop = await self.subject_rows(
                subject,
                item.subject_id,
                modes,
                reserve=self.row_page_reserve(
                    plan,
                    subject,
                    index,
                    base=4 + (len(plan.subjects) if "CURRENT" in modes else 0),
                ),
            )
            if stop:
                if plan.source_need != "NONE" and "FIELDS_INCOMPLETE" in (
                    self.lookup_gaps - gaps_before
                ):
                    # 원문 라운드 예약 때문에 항목 158개 인물의 등록 항목을 다 읽지 못하면 되묻지 않고
                    # 등록 현재값은 미확인으로 두고 원문 답변을 계속한다(45화 황금 가면 실제 사례).
                    self.current_requirement_resolved = False
                    if "REGISTERED_LOOKUP_INCOMPLETE" not in self.codes:
                        self.codes.append("REGISTERED_LOOKUP_INCOMPLETE")
                    continue
                return stop
        self.chosen, stop = await self.select_rows(plan, allow_missing=True)
        if stop:
            return stop
        if {(row.domain, row.item.subject_id) for row in self.chosen} != targets:
            self.codes.append("NO_RELATED_SETTING")
        self.current_requirement_targets = targets
        return None

    def current_requirement_satisfied(self, plan):
        """A manuscript passage cannot satisfy a required registered-current read."""
        if self.incomplete_current_subjects or set(self.codes) & {
            "VALUE_LIMIT",
            "CONTEXT_LIMIT",
            "SELECTION_LIMIT",
            "CURRENT_CHANGED",
            "CONDITION_UNCERTAIN",
        }:
            return False
        if plan.collection is not None:
            return bool(self.collection_checks) and not self.incomplete_subjects
        confirmed = {
            (row.domain, row.item.subject_id)
            for row in self.chosen
            if row.item.kind == "CURRENT" and not row.item.value_truncated
        }
        return self.current_requirement_resolved and self.current_requirement_targets <= confirmed

    async def current_without_source(self, plan):
        """Preserve observed current values when the independent source request failed."""
        # Only current values are retained here. Previously fetched quotations
        # have not passed finish_answer's final source revalidation.
        current_rows = [
            AnswerRow(r.ref, r.item, r.domain) for r in self.chosen if r.item.kind == "CURRENT"
        ]
        if not plan.registered_current_required or not current_rows:
            return None
        if self.has_collection_condition and (
            self.uncertain_condition_rows() or not self.confirmed_condition_rows()
        ):
            return None
        await self.revalidate_settings()
        registered = render_answer(
            current_rows,
            self.codes,
            direct_only=False,
            maximum=self.cfg.max_answer_characters,
        )
        text = registered.result.text + "\n요청하신 원문 내용은 이번 조회에서 확인하지 못했어요."
        if len(text) <= self.cfg.max_answer_characters:
            return Finish(result=registered.result.model_copy(update={"text": text}))
        return None

    async def run_items(self, plan):
        """Search missing information per logical item, sharing the normal execution budget."""
        facets = requested_facets(plan)
        self.item_draft = empty_item_answer(self.logical_items, facets)
        # Do not spend the whole query budget verifying many usage snippets before
        # any acquisition/context search. Every fetched direct source needs a recheck.
        originals = [
            next((r for r in item.rows if r.item.sources), None) for item in self.logical_items
        ]
        originals = [r for r in originals if r is not None][:2]
        direct_rows = [
            AnswerRow(
                row.ref, row.item.model_copy(update={"sources": row.item.sources[:1]}), row.domain
            )
            for row in originals
        ]
        await self.evidence(
            direct_rows,
            reserve=4 + len(self.collection_checks) + self.current_check_count(),
            revalidate=True,
        )
        for original, fetched in zip(originals, direct_rows, strict=True):
            original.evidence.extend(fetched.evidence)
        self.add_direct_evidence()
        self.remember_evidence()
        if self.evidences:
            draft = await self.answer(plan)
            self.item_draft = merge_item_answers(
                self.item_draft, draft, self.logical_items, facets, event_scope=plan.event_scope
            )
            self.preserve_supported_evidence(self.item_draft, self.evidence_registry.values())
            if self.item_draft.coverage == "COMPLETE":
                return await self.finish_answer(self.item_draft)

        for round_no in range(1, self.hcfg.max_rounds + 1):
            if not self.models_available():
                self.codes.append("SEARCH_LIMIT")
                break
            self.rounds = round_no
            used = answer_evidence_refs(self.item_draft)
            self.evidences = [
                self.evidence_registry[ref] for ref in self.evidence_registry if ref in used
            ]
            self.fallback_direct_checks = len(
                {e.direct_ref for e in self.evidences if e.direct_ref}
            )
            missing = {i.item_ref for i in self.item_draft.item_answers if i.missing_facets}
            targets = sorted(
                (i for i in self.logical_items if i.ref in missing),
                key=lambda i: self.item_searches.get(i.ref, 0),
            )
            before = self.evidence_identity()
            found_by_item = []
            for item in targets:
                reserve = 5 if found_by_item and self.hcfg.neighbor_radius else 4
                if len(self.evidences) >= self.hcfg.max_evidence or not self.tools_available(
                    reserve
                ):
                    self.codes.append("SEARCH_LIMIT")
                    break
                row = item.rows[0]
                query = row.item.display_name or row.item.subject_name
                if row.item.scope_name:
                    query = f"{query} {row.item.scope_name}"
                if len(query) > 1000:
                    query = query[:1000]
                    self.codes.append("SEARCH_QUERY_LIMIT")
                previous_attempts = self.item_searches.get(item.ref, 0)
                self.item_searches[item.ref] = previous_attempts + 1
                hints = tuple(
                    dict.fromkeys(
                        ev.evidence.episode_id
                        for r in item.rows
                        for _, ev in r.evidence
                        if ev.evidence.episode_id is not None
                    )
                )
                if previous_attempts and self.embedding_calls < self.hcfg.max_query_embeddings:
                    if not self.models_available(embedding=True):
                        self.codes.append("SEARCH_LIMIT")
                        continue
                    vector_limit = self.hcfg.vector_top_k
                    if plan.ordering != "NONE":
                        vector_limit = min(vector_limit * 3, 100)
                    found = await self.search(
                        "vector",
                        self.expansion_query(
                            plan,
                            self.item_draft,
                            f"{row.item.subject_name} {query}",
                            item_ref=item.ref,
                        ),
                        _QueryEmbedding(self),
                        limit=vector_limit,
                        excluded_chunk_ids=tuple(
                            e.chunk.chunk_id for e in self.evidences if e.chunk
                        ),
                    )
                    if plan.ordering != "NONE" and found:
                        # 항목 문맥 배분은 점수 등급으로 고르므로 가장 이른/늦은 후보를 최상위 등급에 둔다.
                        head = self.episode_ordered_head(plan, found)
                        kept = {c.chunk_id for c in head}
                        top = max(c.score for c in found)
                        found = [
                            *(replace(c, score=top) for c in head),
                            *(c for c in found if c.chunk_id not in kept),
                        ]
                elif not previous_attempts:
                    found = await self.search(
                        "keyword",
                        query,
                        limit=self.hcfg.keyword_top_k,
                        preferred_episode_ids=hints,
                    )
                    if plan.ordering != "NONE":
                        # 항목별 질문도 최초/최후 장면을 항목 표현 기준으로 회차 순서로 읽는다.
                        found = await self.ordered_search(plan, found, objects=[query])
                else:
                    continue
                found_by_item.append((item.ref, found))
            # Allocate context jointly: a common passage can anchor several items.
            # Filling an early item's entire neighborhood first would starve later
            # items even when the store returned their exact acquisition scene.
            seeds = select_context_seeds(
                found_by_item,
                maximum=max(0, self.hcfg.max_evidence - len(self.evidences)),
            )
            candidates = list(seeds)
            if seeds and self.hcfg.neighbor_radius and self.tools_available(4):
                candidates.extend(
                    await self.search(
                        "neighbors",
                        seeds,
                        radius=self.hcfg.neighbor_radius,
                        limit=self.hcfg.max_neighbors,
                    )
                )
            elif seeds and self.hcfg.neighbor_radius:
                self.codes.append("SEARCH_LIMIT")
            if candidates:
                self.add_item_evidence(candidates, context_slots=self.hcfg.max_evidence)
            if self.evidence_identity() == before:
                continue
            await self.register([e.chunk for e in self.evidences if e.chunk])
            draft = await self.answer(plan)
            self.item_draft = merge_item_answers(
                self.item_draft, draft, self.logical_items, facets, event_scope=plan.event_scope
            )
            self.preserve_supported_evidence(self.item_draft, self.evidence_registry.values())
            if self.item_draft.coverage == "COMPLETE":
                return await self.finish_answer(self.item_draft)
        if self.item_draft.coverage != "NONE":
            return await self.finish_answer(self.item_draft)
        registered = await self.current_without_source(plan)
        if registered is not None:
            return registered
        text = "\n".join(
            render_item_answers(
                self.item_draft,
                self.logical_items,
                {},
                blocked_rows=self.uncertain_condition_rows(),
                event_scope=plan.event_scope,
                labels=request_labels(plan),
            )
        )
        if len(text) + 50 > self.cfg.max_answer_characters:
            return message(
                "CLARIFICATION",
                "확인할 항목이나 유형을 좁혀 주세요.",
                self.codes + ["ANSWER_LIMIT"],
            )
        await self.revalidate_settings()
        return message(
            "INSUFFICIENT_EVIDENCE",
            text + "\n작품 전체에 해당 사실이 없다는 뜻은 아닙니다.",
            self.codes + ["NO_SEARCH_EVIDENCE"],
        )

    def remember_evidence(self):
        for evidence in self.evidences:
            self.evidence_registry[evidence.ref] = evidence

    @staticmethod
    def source_identity(evidence):
        # E numbers are local to each composed answer, never stable source identities.
        return (
            evidence.chunk.fingerprint
            if evidence.chunk
            else (evidence.direct_ref, evidence.text, evidence.episode)
        )

    def preserve_supported_evidence(self, answer, evidences):
        used = answer_evidence_refs(answer)
        # This includes every original chunk covering a cross-boundary support.
        self.supported_evidence = tuple(e for e in evidences if e.ref in used)

    def merge_partial_snapshot(self, previous, latest, plan):
        """Combine independent requested facets with a fresh, unambiguous E map."""
        old_answer, old_evidence = previous

        def row_facets(claims):
            result = {}
            for claim in claims:
                for row in claim.row_refs:
                    result.setdefault(row, set()).update(claim.answered_facets)
            return result

        before = row_facets(old_answer.claims)
        current = row_facets(latest.claims)
        requested = set(requested_facets(plan))

        def unanswered_rows(claims):
            retained = row_facets(claims)
            resolved = {
                row
                for row, facets in current.items()
                if requested <= facets
                and requested <= retained.get(row, set())
                and row not in latest.unanswered_row_refs
            }
            # New evidence for another row cannot resolve an earlier row's gap.
            # Nor may replacing overlapping claims silently erase a prior row
            # or a facet that had been answered for that particular row.
            dropped = [
                row
                for row, facets in before.items()
                if row not in retained or facets - retained[row]
            ]
            incomplete = [row for row, facets in retained.items() if not requested <= facets]
            return list(
                dict.fromkeys(
                    [r for r in old_answer.unanswered_row_refs if r not in resolved]
                    + latest.unanswered_row_refs
                    + dropped
                    + incomplete
                )
            )

        old_facets = {f for c in old_answer.claims for f in c.answered_facets}
        new_facets = {f for c in latest.claims for f in c.answered_facets}
        # A conflicting interpretation is not an additive fact. Keep the latest
        # explicit conflict, and never silently combine contradictory narratives.
        if latest.conflicts:
            return latest, tuple(self.evidences)
        row_progress = any(
            facets - before.get(row, set()) for row, facets in current.items()
        ) or any(
            row in current and requested <= current[row] and row not in latest.unanswered_row_refs
            for row in old_answer.unanswered_row_refs
        )
        if old_answer.conflicts or not (new_facets - old_facets or row_progress):
            return previous

        def superseded(claim):
            if claim.row_refs:
                return all(
                    row in current and set(claim.answered_facets) <= current[row]
                    for row in claim.row_refs
                )
            # One new facet must not erase the other independently answered
            # parts of a multi-facet claim. Only a complete replacement wins.
            return (
                bool(claim.answered_facets)
                and set(claim.answered_facets) <= new_facets
                or (plan.event_scope is not None and not claim.answered_facets)
            )

        chosen = [c for c in old_answer.claims if not superseded(c)]
        statements = [(c, old_evidence) for c in chosen] + [
            (c, self.evidences) for c in latest.claims
        ]
        evidence, claims = [], []
        by_identity = {}
        for claim, snapshot in statements:
            refs = []
            remapped = {}
            for ref in claim.evidence_refs:
                source = next(e for e in snapshot if e.ref == ref)
                identity = (
                    source.chunk.fingerprint
                    if source.chunk
                    else (source.direct_ref, source.text, source.episode)
                )
                if identity not in by_identity:
                    new_ref = f"E{len(evidence) + 1}"
                    by_identity[identity] = new_ref
                    evidence.append(
                        _Evidence(
                            new_ref,
                            source.text,
                            source.episode,
                            source.chunk,
                            source.direct_ref,
                            source.direct_item,
                        )
                    )
                remapped[ref] = by_identity[identity]
                refs.append(remapped[ref])
            # A validated support may have anchored an adjacent chunk in the
            # original merged passage. Resolve that quote again in its original
            # snapshot before choosing an anchor in the new citation map.
            original_passages = self.evidence_passages(snapshot) if claim.supports else []
            supports = [
                support.model_copy(
                    update={
                        "evidence_ref": remapped[
                            resolve_support_references([support], original_passages)[0]
                        ]
                    }
                )
                for support in claim.supports
            ]
            claims.append(claim.model_copy(update={"evidence_refs": refs, "supports": supports}))
        if len(evidence) > self.hcfg.max_evidence or len(claims) > 8:
            self.codes.append("EVIDENCE_CONTEXT_LIMIT")
            if old_facets <= new_facets:
                return (
                    latest.model_copy(
                        update={"unanswered_row_refs": unanswered_rows(latest.claims)}
                    ),
                    tuple(self.evidences),
                )
            return previous
        answered = {f for c in claims for f in c.answered_facets}
        missing = [f for f in requested_facets(plan) if f not in answered]
        unanswered = unanswered_rows(claims)
        combined = GroundedAnswer(
            coverage="PARTIAL"
            if missing or unanswered or self.uncertain_condition_rows()
            else "COMPLETE",
            claims=claims,
            conflicts=[],
            missing_facets=missing,
            missing_explanations=merge_missing_explanations(missing, old_answer, latest),
            unanswered_row_refs=unanswered,
        )
        self.fallback_direct_checks = len({e.direct_ref for e in evidence if e.direct_ref})
        return combined, tuple(evidence)

    def evidence_token_count(self, evidences):
        # Count retained passages as they appear in the model payload; overlapping
        # references already retained must not consume the budget twice.
        chunks = [e.chunk for e in evidences if e.chunk]
        merged = compose_evidence(chunks, max_items=100, per_episode=100, max_tokens=10**9)
        encoding = tiktoken.get_encoding("cl100k_base")
        return merged.token_count + sum(
            len(encoding.encode(e.text, disallowed_special=())) for e in evidences if e.direct_ref
        )

    def record_evidence_omission(self, stage, reason, count=1):
        if count:
            self.metrics.evidence_omissions.labels(stage, reason).inc(count)
            self.codes.append("EVIDENCE_CONTEXT_LIMIT")

    def record_composition(self, stage, bundle):
        for omission in bundle.omissions:
            self.record_evidence_omission(stage, omission.reason, omission.count)
        logger.info(
            "Chat evidence composition stage=%s input=%d duplicates=%d selected=%d "
            "tokens=%d omissions=%s",
            stage,
            bundle.input_count,
            bundle.duplicate_count,
            len(bundle.chunks),
            bundle.token_count,
            {o.reason: o.count for o in bundle.omissions},
        )

    def add_item_evidence(self, candidates, *, context_slots=None):
        def quote_identity(chunk):
            return (
                chunk.source_fingerprint,
                chunk.start_offset,
                chunk.end_offset,
                chunk.chunk_hash,
                chunk.text,
            )

        known = {e.chunk.fingerprint for e in self.evidences if e.chunk}
        known_quotes = {quote_identity(e.chunk) for e in self.evidences if e.chunk}
        candidates = [c for c in candidates if quote_identity(c) not in known_quotes]
        if not candidates:
            return
        remaining = self.hcfg.max_evidence - len(self.evidences)
        if remaining < 1:
            self.record_evidence_omission("item_search", "ITEM_LIMIT", len(candidates))
            return
        context_slots = min(context_slots or 1 + 2 * self.hcfg.neighbor_radius, remaining)
        retained = [e.chunk for e in self.evidences if e.chunk]
        direct_tokens = self.evidence_token_count([e for e in self.evidences if e.direct_ref])
        if direct_tokens >= self.hcfg.evidence_tokens:
            self.record_evidence_omission("item_search", "TOKEN_LIMIT", len(candidates))
            return
        bundle = compose_evidence(
            [*retained, *candidates],
            max_items=len(retained) + context_slots,
            max_tokens=self.hcfg.evidence_tokens - direct_tokens,
            per_episode=self.hcfg.per_episode,
            retained_chunk_ids=[c.chunk_id for c in retained],
        )
        for chunk in bundle.chunks:
            if chunk.fingerprint in known:
                continue
            evidence = next(
                (
                    e
                    for e in self.evidence_registry.values()
                    if e.chunk and e.chunk.fingerprint == chunk.fingerprint
                ),
                None,
            )
            if evidence is None:
                evidence = _Evidence(
                    f"E{len(self.evidence_registry) + 1}", chunk.text, chunk.episode_no, chunk=chunk
                )
                self.evidence_registry[evidence.ref] = evidence
            self.evidences.append(evidence)
        self.record_composition("item_search", bundle)

    def add_direct_evidence(self):
        seen = set()
        for row in self.chosen:
            for ref, item in row.evidence:
                for quote in item.evidence.quotes:
                    identity = (ref, quote.text, quote.start_offset, quote.end_offset)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if len(self.evidences) >= self.hcfg.max_evidence:
                        self.record_evidence_omission("direct", "ITEM_LIMIT")
                        self.codes.append("EVIDENCE_LIMIT")
                        continue
                    # Available legacy direct evidence may lack absolute offsets; it retains
                    # its original typed receipt instead of pretending to be a retrieval chunk.
                    candidate = _Evidence(
                        f"E{len(self.evidences) + 1}",
                        quote.text,
                        item.evidence.source_episode_no,
                        direct_ref=ref,
                        direct_item=item,
                    )
                    if (
                        self.evidence_token_count([*self.evidences, candidate])
                        > self.hcfg.evidence_tokens
                    ):
                        self.record_evidence_omission("direct", "TOKEN_LIMIT")
                        continue
                    self.evidences.append(candidate)

    def evidence_identity(self):
        return tuple(
            (e.ref, e.chunk.fingerprint if e.chunk else (e.direct_ref, e.text, e.episode))
            for e in self.evidences
        )

    def expansion_query(self, plan, draft, query, *, item_ref=None):
        if draft is None:
            missing = set(requested_facets(plan))
        elif draft.item_answers:
            missing = {
                facet
                for item in draft.item_answers
                if item_ref is None or item.item_ref == item_ref
                for facet in item.missing_facets
            }
        else:
            missing = set(draft.missing_facets)
        descriptions = [p.description for p in plan.requested_parts if p.ref in missing]
        return " ".join(
            filter(
                None,
                [
                    plan.event_context,
                    *[s.name for s in plan.subjects],
                    *descriptions,
                    query,
                    self.execution.context.question,
                ],
            )
        )[:1000]

    def add_search_evidence(self, candidates, *, fresh=(), focus=(), ordered=()):
        protected = {self.source_identity(e) for e in self.supported_evidence}
        direct = [e for e in self.evidences if e.direct_ref]
        direct.sort(key=lambda e: self.source_identity(e) not in protected)
        protected_chunks = [e.chunk for e in self.supported_evidence if e.chunk]
        available = self.hcfg.max_evidence - len(direct)
        # Leave room for search evidence when many direct quotes did not explain the question.
        if available < 1:
            old_count = len(direct)
            direct = [
                e
                for index, e in enumerate(direct)
                if index < max(0, self.hcfg.max_evidence // 2)
                or self.source_identity(e) in protected
            ]
            if len(direct) < old_count:
                self.record_evidence_omission("search", "ITEM_LIMIT", old_count - len(direct))
                self.codes.append("EVIDENCE_LIMIT")
        # Direct quotes and retrieved passages share one evidence text budget.
        # Keep at least half for new search when linked quotes did not answer it.
        while direct and self.evidence_token_count(direct) > self.hcfg.evidence_tokens // 2:
            removable = next(
                (e for e in reversed(direct) if self.source_identity(e) not in protected), None
            )
            if removable is None:
                break
            direct.remove(removable)
            self.record_evidence_omission("search", "TOKEN_LIMIT")
        available = self.hcfg.max_evidence - len(direct)
        remaining_tokens = self.hcfg.evidence_tokens - self.evidence_token_count(direct)
        if available < 1 or remaining_tokens < 1:
            self.evidences = direct
            self.record_evidence_omission(
                "search", "ITEM_LIMIT" if available < 1 else "TOKEN_LIMIT", len(candidates)
            )
            return
        # A rejected answer is not evidence of irrelevant source material. Retain
        # a bounded share of its grounded quote candidates, without publishing any
        # rejected claim. A valid NONE still yields to fresh vector results.
        recovery = []
        if self.answer_validation_failed:
            prior = self.recovery_evidence or tuple(self.evidences)
            recovery = [e.chunk for e in prior if e.chunk][: max(1, available // 2)]
        priority = [
            *protected_chunks,
            *recovery,
            *focus,
            *ordered,
            *fresh,
        ]
        bundle = compose_evidence(
            [*protected_chunks, *recovery, *candidates],
            max_items=available,
            max_tokens=remaining_tokens,
            # Episode diversity is useful across a work, but a question scoped to
            # one episode should be able to use the shared evidence budget there.
            per_episode=(
                self.hcfg.max_evidence - len(direct)
                if self.source_from is not None and self.source_from == self.source_to
                # 검증된 장면을 더 읽는 라운드는 같은 회차 몫을 늘려 장면을 이어 읽게 한다.
                else min(
                    self.hcfg.per_episode + (max(2, self.hcfg.max_evidence // 3) if focus else 0),
                    self.hcfg.max_evidence - len(direct),
                )
            ),
            # After verified/recovery evidence, prioritize new hits in their own
            # rank order. Old unproven lexical scores must not crowd out vector
            # hits; the common item/token/episode caps still bound this context.
            priority_chunk_ids=[c.chunk_id for c in priority],
            # 요구 항목이 있어도 전수·개수 질문은 회차를 고루 읽어야 서로 다른 인물·종류가 근거에 들어온다.
            episode_round_robin=not bool(self.plan and self.plan.requested_parts)
            or bool(self.plan and self.plan.answer_shape in {"LIST", "COUNT", "DISTINCT"}),
        )
        self.evidences = [replace(e, ref=f"E{index}") for index, e in enumerate(direct, 1)]
        for chunk in bundle.chunks:
            self.evidences.append(
                _Evidence(f"E{len(self.evidences) + 1}", chunk.text, chunk.episode_no, chunk=chunk)
            )
        self.record_composition("search", bundle)
        self.trace(
            "selected",
            round=self.rounds,
            refs=[
                {"ref": e.ref, "chunk": str(e.chunk.chunk_id) if e.chunk else None}
                for e in self.evidences
            ],
            omissions={o.reason: o.count for o in bundle.omissions},
        )

    def evidence_passages(self, evidences):
        """Build the same original citation spans for model input and snapshot remapping."""
        chunks = [e.chunk for e in evidences if e.chunk]
        bundle = compose_evidence(
            chunks,
            max_items=self.hcfg.max_evidence,
            max_tokens=self.hcfg.evidence_tokens,
            per_episode=self.hcfg.max_evidence,
        )
        by_id = {e.chunk.chunk_id: e.ref for e in evidences if e.chunk}
        passages = [
            {
                "text": p.text,
                "sourceEpisodeNo": p.chunks[0].episode_no,
                "references": [
                    {
                        "evidenceRef": by_id[c.chunk_id],
                        "start": c.start_offset - p.start_offset,
                        "end": c.end_offset - p.start_offset,
                        "methods": list(c.methods),
                    }
                    for c in p.chunks
                ],
            }
            for p in bundle.passages
        ]
        passages.extend(
            {
                "text": e.text,
                "sourceEpisodeNo": e.episode,
                "references": [
                    {
                        "evidenceRef": e.ref,
                        "start": 0,
                        "end": len(e.text),
                        "methods": ["DIRECT_SETTING_EVIDENCE"],
                    }
                ],
            }
            for e in evidences
            if e.direct_ref
        )
        return passages

    def answer_payload(self, plan):
        selected = {r.ref for r in self.chosen}
        rows = [
            r
            for r in self.payload(self.execution.context.question, plan)["rows"]
            if r["rowRef"] in selected
        ]
        for row in rows:
            row.pop("settingKey", None)
        return {
            "question": self.execution.context.question,
            **self.conversation_payload(),
            "plan": plan.model_dump(by_alias=True),
            "rows": rows,
            "registeredCurrent": {
                "required": plan.registered_current_required,
                "confirmed": self.current_requirement_confirmed,
            },
            "collectionConditions": [
                {**condition, "rowRefs": [r for r in condition["rowRefs"] if r in selected]}
                for condition in self.collection_conditions
                if set(condition["rowRefs"]) & selected
            ],
            "items": [
                {"itemRef": item.ref, "rowRefs": [r.ref for r in item.rows], "label": item.label}
                for item in self.logical_items
            ],
            "requestedFacets": requested_facets(plan),
            "previousItemAnswers": []
            if self.item_draft is None
            else [i.model_dump(by_alias=True) for i in self.item_draft.item_answers if i.answers],
            "passages": self.evidence_passages(self.evidences),
            **({"searchOrdering": self.ordered_coverage} if self.ordered_coverage else {}),
            "limitations": notices(self.codes, include_conversation_limit=True),
        }

    def uncertain_condition_rows(self):
        return {
            ref
            for condition in self.collection_conditions
            if condition["verdict"] == "UNKNOWN"
            for ref in condition["rowRefs"]
        }

    def confirmed_condition_rows(self):
        return {
            ref
            for condition in self.collection_conditions
            if condition["verdict"] == "MATCH"
            for ref in condition["rowRefs"]
        }

    def apply_condition_guard(self, answer, plan):
        """A past source event cannot certify an uncertain current-state predicate.

        The same answer model does not get authority to promote UNKNOWN candidates.
        Hold their statements until a separate current-condition proof contract exists.
        """
        if not self.has_collection_condition:
            return answer
        blocked = self.uncertain_condition_rows()
        confirmed = self.confirmed_condition_rows()
        facets = requested_facets(plan)
        conflicts = [c for c in answer.conflicts if not set(c.row_refs) & blocked]
        if self.logical_items:
            blocked_items = {
                i.ref for i in self.logical_items if any(r.ref in blocked for r in i.rows)
            }
            results = [
                ItemAnswer(item_ref=item.item_ref, answers=[], missing_facets=list(facets))
                if item.item_ref in blocked_items
                else item
                for item in answer.item_answers
            ]
            supported = bool(conflicts or any(i.answers for i in results))
            complete = all(not i.missing_facets for i in results) and not conflicts
            return GroundedAnswer(
                coverage="COMPLETE" if complete else "PARTIAL" if supported else "NONE",
                claims=[],
                conflicts=conflicts,
                item_answers=results,
            )
        # Without row references a condition-scoped statement cannot be assigned
        # to a confirmed subject, so it cannot be published as a matching result.
        claims = [c for c in answer.claims if c.row_refs and set(c.row_refs) <= confirmed]
        removed_facets = {
            facet
            for claim in answer.claims
            if claim not in claims
            for facet in [*claim.answered_facets, *claim.related_facets]
        }
        covered = {f for c in claims for f in c.answered_facets}
        missing = [f for f in facets if f not in covered]
        supported = bool(claims or conflicts)
        complete = (
            supported
            and not missing
            and not conflicts
            and not blocked
            and not answer.unanswered_row_refs
        )
        return GroundedAnswer(
            coverage="COMPLETE" if complete else "PARTIAL" if supported else "NONE",
            claims=claims,
            conflicts=conflicts,
            missing_facets=missing,
            missing_explanations=merge_missing_explanations(
                [f for f in missing if f not in removed_facets], answer
            ),
            unanswered_row_refs=answer.unanswered_row_refs,
        )

    def empty_source_answer(self, plan):
        return (
            empty_item_answer(self.logical_items, requested_facets(plan))
            if self.logical_items
            else GroundedAnswer(
                coverage="NONE",
                claims=[],
                conflicts=[],
                missing_facets=requested_facets(plan),
                unanswered_row_refs=[r.ref for r in self.chosen] if plan.per_item else [],
            )
        )

    async def answer(self, plan):
        self.answer_validation_failed = False
        self.recovery_evidence = ()
        schema = response_schema(GroundedAnswer)
        protected = {self.source_identity(e) for e in self.supported_evidence}
        if not protected <= {self.source_identity(e) for e in self.evidences}:
            # A new complete answer must not silently replace a supported snapshot
            # after losing part of its source context. The caller keeps the snapshot.
            self.codes.append("EVIDENCE_CONTEXT_LIMIT")
            self.trace("supported_context_unavailable", round=self.rounds)
            return self.empty_source_answer(plan)
        self.trim_conversation_to_fit(ANSWER_PROMPT, lambda: self.answer_payload(plan), schema)
        while self.evidences and not self.fits(ANSWER_PROMPT, self.answer_payload(plan), schema):
            removable = next(
                (
                    index
                    for index in range(len(self.evidences) - 1, -1, -1)
                    if self.source_identity(self.evidences[index]) not in protected
                ),
                None,
            )
            if removable is None:
                self.codes.append("CONTEXT_LIMIT")
                self.trace("supported_context_unavailable", round=self.rounds)
                return self.empty_source_answer(plan)
            removed = self.evidences.pop(removable)
            self.trace(
                "omitted",
                reason="INPUT_LIMIT",
                ref=removed.ref,
                chunk=str(removed.chunk.chunk_id) if removed.chunk else None,
            )
            self.metrics.evidence_omissions.labels("answer", "INPUT_LIMIT").inc()
            self.codes.append("CONTEXT_LIMIT")
        if not self.evidences:
            return self.empty_source_answer(plan)
        e_refs, r_refs = {e.ref for e in self.evidences}, {r.ref for r in self.chosen}
        payload = self.answer_payload(plan)
        self.trace(
            "answer_input",
            round=self.rounds,
            refs=[
                {"ref": e.ref, "chunk": str(e.chunk.chunk_id) if e.chunk else None}
                for e in self.evidences
            ],
        )

        def ground_statement(statement):
            refs = statement.evidence_refs
            supports = statement.supports
            if statement.supports or plan.requested_parts:
                resolved = resolve_support_quotes(statement.supports, payload["passages"])
                refs = list(
                    dict.fromkeys(ref for support in resolved for ref in support.evidence_refs)
                )
                if len(refs) > (3 if isinstance(statement, EvidenceConflict) else 6):
                    raise ValueError("Unknown evidence or setting reference")
                supports = [
                    EvidenceSupport.model_validate(
                        {"evidenceRef": support.evidence_refs[0], "quote": support.quote}
                    )
                    for support in resolved
                ]
            if plan.event_context is not None and statement.event_match is None:
                from app.chatbot.qa_validation import ChatResponseContractError

                raise ChatResponseContractError("EVENT_CONTEXT_REQUIRED")
            return statement.model_copy(update={"evidence_refs": refs, "supports": supports})

        def grounded(answer):
            return answer.model_copy(
                update={
                    "claims": [ground_statement(c) for c in answer.claims],
                    "conflicts": [ground_statement(c) for c in answer.conflicts],
                    "item_answers": [
                        item.model_copy(
                            update={"answers": [ground_statement(a) for a in item.answers]}
                        )
                        for item in answer.item_answers
                    ],
                }
            )

        def validated(answer):
            answer = grounded(answer)
            if (
                not answer_evidence_refs(answer) <= e_refs
                or any(not set(c.row_refs) <= r_refs for c in [*answer.claims, *answer.conflicts])
                or not set(answer.unanswered_row_refs) <= r_refs
            ):
                raise ValueError("Unknown evidence or setting reference")
            if self.logical_items:
                validate_item_answer(answer, self.logical_items, requested_facets(plan))
            elif answer.item_answers:
                raise ValueError("No logical item references were supplied")
            elif plan.per_item:
                covered = {r for c in [*answer.claims, *answer.conflicts] for r in c.row_refs}
                if covered | set(answer.unanswered_row_refs) != r_refs:
                    raise ValueError(
                        "Each requested setting needs an answer or explicit uncertainty"
                    )
            if not self.logical_items and (plan.requested_facets or plan.requested_parts):
                requested = set(requested_facets(plan))
                covered = {f for claim in answer.claims for f in claim.answered_facets}
                missing = set(answer.missing_facets)
                if (
                    any(
                        not c.answered_facets
                        and not c.related_facets
                        and (
                            c.event_status is None
                            or event_matches(c.event_status, plan.event_scope)
                        )
                        for c in answer.claims
                    )
                    or any(not set(c.related_facets) <= requested for c in answer.claims)
                    or covered & missing
                    or covered | missing != requested
                ):
                    from app.chatbot.qa_validation import ChatResponseContractError

                    raise ChatResponseContractError("REQUESTED_PARTS_INCOMPLETE")
                complete = not missing and not answer.conflicts and not answer.unanswered_row_refs
                # Coverage summarizes already validated structure, rather than
                # asking the model to regenerate correct claims for an enum mismatch.
                answer = answer.model_copy(
                    update={
                        "coverage": "COMPLETE"
                        if complete
                        else "PARTIAL"
                        if answer.claims or answer.conflicts
                        else "NONE",
                    }
                )
            return answer

        def valid(answer):
            # A location-checked quote from a rejected response can prioritize a
            # source candidate in recovery. It never makes that response publishable.
            hinted = {self.source_identity(e) for e in self.recovery_evidence}
            for statement in [
                *answer.claims,
                *answer.conflicts,
                *(a for item in answer.item_answers for a in item.answers),
            ]:
                if not statement.supports:
                    continue
                try:
                    refs = resolve_support_references(statement.supports, payload["passages"])
                except ValueError:
                    continue
                hinted.update(self.source_identity(e) for e in self.evidences if e.ref in refs)
            self.recovery_evidence = tuple(
                e for e in self.evidences if self.source_identity(e) in hinted
            )
            try:
                return validated(answer)
            except ValueError:
                recovered = None
                if plan.requested_parts and not plan.per_item and not self.logical_items:
                    recovered = recover_claims(
                        answer,
                        requested_facets(plan),
                        ground_statement,
                        evidence_refs=e_refs,
                        row_refs=r_refs,
                    )
                if recovered is None:
                    raise
                recovered = validated(recovered)
                self.trace(
                    "answer_partial_recovered",
                    round=self.rounds,
                    kept=len(recovered.claims),
                    removed=len(answer.claims) - len(recovered.claims),
                )
                return recovered

        attempts = min(
            self.cfg.max_attempts,
            self.hcfg.max_model_calls - self.model_calls,
            self.execution.context.max_calls - self.execution.calls,
        )
        try:
            if attempts < 1:
                self.codes.append("SEARCH_LIMIT")
                raise ResponseValidationFailure()
            answer = await self.model(
                GroundedAnswer,
                ANSWER_PROMPT,
                payload,
                "CHAT_ANSWER",
                ANSWER_VERSION,
                valid,
                max_attempts=attempts,
            )
        except (ModelTimeBudgetExceeded, ProviderOutputTruncated) as exc:
            # A failed expansion must not erase an earlier validated answer.
            if not self.supported_evidence:
                # 첫 답변이 출력 상한에서 잘리고 복구할 시간이 없으면 실행 실패 대신 부족 안내로 마감한다.
                # 긴 조회(두 인물 CURRENT_AND_HISTORY + 원문)에서 4,500토큰 답이 잘린 실제 사례.
                self.codes.append("ANSWER_LIMIT")
                self.trace(
                    "answer_truncated_unrecovered", reason=type(exc).__name__, round=self.rounds
                )
                return self.empty_source_answer(plan)
            self.codes.append("SEARCH_LIMIT")
            self.trace("answer_expansion_stopped", reason=type(exc).__name__, round=self.rounds)
            return self.empty_source_answer(plan)
        except ResponseValidationFailure:
            # Reject every unverified claim, but keep the search/partial-answer
            # recovery path alive. Transport and provider failures still propagate.
            self.trace("answer_validation_exhausted", round=self.rounds)
            self.answer_validation_failed = True
            return self.empty_source_answer(plan)
        answer = validated(answer)
        answer = apply_event_scope(
            answer, plan.event_scope, requested_facets(plan), event_context=plan.event_context
        )
        answer = self.apply_condition_guard(answer, plan)
        self.trace(
            "answer",
            round=self.rounds,
            coverage=answer.coverage,
            used=sorted(answer_evidence_refs(answer)),
            missing=answer.missing_facets,
        )
        return answer

    async def finish_answer(self, answer):
        used = answer_evidence_refs(answer)
        if self.logical_items:
            self.evidences = [e for ref, e in self.evidence_registry.items() if ref in used]
        evidence = [e for e in self.evidences if e.ref in used]
        if not used or len(evidence) != len(used):
            return message(
                "INSUFFICIENT_EVIDENCE",
                "답변 근거를 검증할 수 없습니다.",
                self.codes + ["INVALID_QUOTE"],
            )
        await self.revalidate_settings()
        chunks = [e.chunk for e in evidence if e.chunk]
        if chunks:
            self.consume("revalidate")
            live = await self.handler.search.revalidate(self.scope, chunks)
            if {c.fingerprint for c in live} != {c.fingerprint for c in chunks}:
                raise ChatTransportError(403, "SOURCE_CHANGED")
            await self.register(chunks)
        checked = set()
        for ev in evidence:
            if ev.direct_ref and ev.direct_ref not in checked:
                checked.add(ev.direct_ref)
                response = await self.query(
                    SettingToolQuery(
                        tool=ev.direct_ref.tool,
                        target_id=ev.direct_ref.target_id,
                        size=self.cfg.page_size,
                        episode_from=self.source_from,
                        episode_to=self.source_to,
                    )
                )
                if response.items.content != [ev.direct_item]:
                    raise ChatTransportError(403, "SOURCE_CHANGED")
        missing_current = (
            self.plan is not None
            and self.plan.registered_current_required
            and not self.current_requirement_confirmed
        )
        if answer.coverage != "COMPLETE" or self.uncertain_condition_rows() or missing_current:
            self.codes.append("PARTIAL_ANSWER")
        self.metrics.chunks.labels("used").inc(len(evidence))
        direct_refs = list(dict.fromkeys(e.direct_ref for e in evidence if e.direct_ref))
        chunk_refs = list(dict.fromkeys(e.chunk.chunk_id for e in evidence if e.chunk))
        public_numbers = {
            e.ref: direct_refs.index(e.direct_ref) + 1
            if e.direct_ref
            else len(direct_refs) + chunk_refs.index(e.chunk.chunk_id) + 1
            for e in evidence
        }
        parts = (
            render_item_answers(
                answer,
                self.logical_items,
                public_numbers,
                blocked_rows=self.uncertain_condition_rows(),
                event_scope=self.event_scope,
                labels=request_labels(self.plan) if self.plan else None,
            )
            if self.logical_items
            else []
        )
        if answer.claims or parts:
            parts.insert(
                0,
                "등록 정보와 원문을 구분해 확인한 내용입니다."
                if any(claim.row_refs for claim in answer.claims)
                else "원문에서 확인한 내용입니다.",
            )
        for claim in answer.claims:
            numbers = ", ".join(
                str(n) for n in dict.fromkeys(public_numbers[r] for r in claim.evidence_refs)
            )
            prefix = (
                {
                    "PLANNED": "계획으로 확인된 내용: ",
                    "UNCERTAIN": "발생 여부가 불확실한 기록: ",
                    "OCCURRED": "발생 사실로 확인된 내용: ",
                }[claim.event_status]
                if claim.event_status and not event_matches(claim.event_status, self.event_scope)
                else ""
            )
            parts.append(f"- {prefix}{safe_text(claim.text)} (근거 {numbers})")
        if not self.logical_items and answer.missing_facets:
            explanations, remaining = render_missing_explanations(answer)
            parts.extend(explanations)
            labels = request_labels(self.plan) if self.plan else FACET_LABELS
            needs = "·".join(labels[f] for f in remaining)
            if remaining and self.plan and self.plan.requested_parts:
                parts.append(f"다만 다음 내용은 확인한 자료만으로 확정하기 어려워요: {needs}.")
            elif remaining:
                prefix = "실제로 이루어진 " if self.event_scope == "ACTUAL" else ""
                parts.append(
                    f"{prefix}{needs} 정보는 이번에 확인한 자료만으로 확정하기 어렵습니다."
                )
        if (
            self.chosen
            and self.render_registered_values
            and (not self.logical_items or self.plan.registered_current_required)
        ):
            # Render real registered values, independently of model-authored prose.
            registered = render_answer(
                [
                    AnswerRow(row.ref, row.item, row.domain)
                    for row in self.chosen
                    if row.ref not in self.uncertain_condition_rows()
                ],
                self.codes,
                direct_only=False,
                maximum=self.cfg.max_answer_characters,
            )
            parts.append("\n" + registered.result.text)
        unanswered = set(answer.unanswered_row_refs)
        for row in self.chosen:
            if row.ref in unanswered:
                parts.append(
                    f"- {safe_text(row.item.subject_name)} · {setting_label(row.item)}: "
                    "이번에 확인한 원문으로는 요청한 경위나 이유를 확인하지 못했습니다."
                )
        for conflict in answer.conflicts:
            labels = ", ".join(
                setting_label(r.item) for r in self.chosen if r.ref in conflict.row_refs
            )
            parts.append(
                f"등록된 {labels} 설정과 원문의 내용이 다르게 나타납니다. 근거에서 두 내용을 함께 확인해 주세요."
            )
            self.codes.append("SETTING_SOURCE_CONFLICT")
        if missing_current:
            parts.append(
                "요청하신 현재 등록 설정은 확인하지 못했어요. 등록된 대상의 이름이나 조회 항목을 확인해 주세요."
            )
        public_codes = list(self.codes)
        if missing_current:
            # The body already explains the exact unmet requirement.
            public_codes = [c for c in public_codes if c != "REGISTERED_CURRENT_UNCONFIRMED"]
        # History truncation is not itself a defect in an answer whose subjects
        # and supporting sources have been resolved again for this question.
        public_codes = [c for c in public_codes if c != "CONVERSATION_LIMIT"]
        if not self.render_registered_values:
            public_codes = [c for c in public_codes if c != "NO_RELATED_SETTING"]
        # Candidate/context limits remain in internal diagnostics. Explain actual
        # unanswered requirements above, instead of a blanket search disclaimer.
        # Index readiness and real source/lookup failures remain public.
        scope_codes = {
            "SEARCH_LIMIT",
            "SEARCH_QUERY_LIMIT",
            "EVIDENCE_CONTEXT_LIMIT",
            "CONTEXT_LIMIT",
        }
        public_codes = [
            c for c in public_codes if c not in scope_codes | {"NARRATIVE_SOURCE_RANGE"}
        ]
        # Missing requested facets/rows already have specific sentences above.
        # A legacy PARTIAL answer without such detail still needs one plain note.
        explained_gap = bool(
            answer.missing_facets
            or answer.unanswered_row_refs
            or answer.conflicts
            or any(item.missing_facets for item in answer.item_answers)
            or missing_current
        )
        if "PARTIAL_ANSWER" in public_codes and not explained_gap:
            parts.append("이번에 확인한 자료로는 질문의 일부만 답할 수 있었어요.")
        public_codes = [c for c in public_codes if c != "PARTIAL_ANSWER"]
        text = "\n".join(parts)
        self.trace(
            "published",
            coverage="PARTIAL" if missing_current else answer.coverage,
            registered_current_confirmed=self.current_requirement_confirmed,
            refs=[
                {"ref": e.ref, "chunk": str(e.chunk.chunk_id) if e.chunk else None}
                for e in evidence
            ],
        )
        if len(text) > self.cfg.max_answer_characters:
            # Never cut away citations while keeping a claim which depends on them.
            return message(
                "INSUFFICIENT_EVIDENCE",
                "검증된 답변과 근거를 답변 길이 한도 안에 담을 수 없습니다. 질문 범위를 좁혀 주세요.",
                self.codes + ["ANSWER_LIMIT"],
            )
        return Finish(
            result=ChatResult(
                outcome="ANSWER",
                text=text,
                sources=direct_refs,
                retrieval_sources=[wire_reference(c) for c in chunks],
                limitations=notices(public_codes),
            )
        )

    async def revalidate_settings(self):
        await self.revalidate_collections()
        if self.current_check_count() and not await self.current_subjects_unchanged(self.chosen):
            raise SettingToolClientError("CONCURRENT_UPDATE")
