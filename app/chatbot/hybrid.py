"""Bounded policy for RDB, linked quotations and current-manuscript retrieval."""

from dataclasses import dataclass
import math

from sqlalchemy.exc import SQLAlchemyError

from app.chatbot.client import ChatTransportError
from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.hybrid_metrics import HybridMetrics
from app.chatbot.hybrid_prompts import ANSWER_PROMPT, ANSWER_VERSION, PLAN_PROMPT, PLAN_VERSION
from app.chatbot.providers import ChatEmbeddingClient
from app.chatbot.qa import ChatQaHandler, _Session, response_schema
from app.chatbot.qa_prompts import SELECT_PROMPT, SELECT_VERSION
from app.chatbot.qa_render import AnswerRow, message, notices, render_answer, safe_text
from app.chatbot.runtime import ProviderFailure
from app.clients.setting_tools_client import SettingToolClientError
from app.retrieval.evidence import compose_evidence
from app.retrieval.search_contracts import SearchScope, SearchScopeUnavailable
from app.schemas.chat_execution import ChatResult, Finish
from app.schemas.chat_hybrid import GroundedAnswer, HybridPlan
from app.schemas.chat_qa import AnswerSelection, QuestionPlan
from app.schemas.chat_retrieval import RetrievalReference
from app.schemas.setting_tools import SettingToolQuery


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
            session.handler.embedding, session.execution, model_name=self.model
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
        session.metrics.operations.labels("embedding").inc()
        response = await self.client.create_embeddings([text])
        if isinstance(response.input_token_count, int) and response.input_token_count >= 0:
            session.metrics.tokens.labels("embedding_input").inc(response.input_token_count)
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
            raise ProviderFailure()
        return vectors[0]


class HybridQaHandler(ChatQaHandler):
    def __init__(self, delegate, search, embedding, settings=None, hybrid=None, metrics=None):
        super().__init__(delegate, settings)
        self.search, self.embedding = search, embedding
        self.hybrid = hybrid or HybridSettings()
        self.metrics = metrics or HybridMetrics()

    async def __call__(self, execution):
        session = _HybridSession(self, execution)
        try:
            result = await session.run()
        except (SearchScopeUnavailable, SQLAlchemyError):
            result = message(
                "INSUFFICIENT_EVIDENCE",
                "원문 검색 범위나 저장소를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                session.codes + ["SEARCH_UNAVAILABLE"],
            )
        except ChatTransportError as exc:
            if exc.code in {"SOURCE_CHANGED", "STORAGE_UNAVAILABLE"}:
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
            result = Finish(
                failure="EXECUTION_LIMIT_EXCEEDED" if exc.status_code == 429 else "NETWORK_FAILURE"
            )
        except ProviderFailure:
            result = Finish(failure="PROVIDER_FAILURE")
        finally:
            self.metrics.rounds.observe(session.rounds)
        self.metrics.outcomes.labels(result.failure or result.result.outcome).inc()
        return result


class _HybridSession(_Session):
    def __init__(self, handler, execution):
        super().__init__(handler, execution)
        self.hcfg, self.metrics = handler.hybrid, handler.metrics
        self.model_calls = self.embedding_calls = self.rounds = 0
        self.evidences = []
        self.chosen = []

    def before_model_call(self, purpose):
        if self.model_calls >= self.hcfg.max_model_calls:
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        self.model_calls += 1
        self.metrics.model_calls.labels(purpose).inc()

    def record_model_response(self, purpose, response):
        for name, value in (
            ("text_input", response.input_token_count),
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
        return result.chunks

    async def register(self, chunks):
        if not chunks:
            return
        self.consume("receipt")
        await self.execution.client.retrieval_sources(
            self.execution.dispatch, self.execution.runner, [wire_reference(c) for c in chunks]
        )

    async def run(self):
        ctx = self.execution.context
        if ctx.operational or not ctx.question or not ctx.question.strip():
            return message("CLARIFICATION", "조회할 대상과 질문을 알려주세요.", self.codes)

        def valid(plan):
            explicit = ctx.question.casefold()
            if any(s.name.strip().casefold() not in explicit for s in plan.subjects) or any(
                t.strip().casefold() not in explicit for t in plan.search_terms
            ):
                raise ValueError("Only explicit question names and terms are accepted")

        plan = await self.model(
            HybridPlan,
            PLAN_PROMPT,
            {
                "question": ctx.question,
                "sourceRange": {"from": ctx.episode_from, "to": ctx.episode_to},
            },
            "CHAT_INTERPRETATION",
            PLAN_VERSION,
            valid,
        )
        if plan.disposition != "PROCEED" or plan.source_need == "NONE":
            # Current/history answers retain the existing extractive renderer and query limits.
            return await self.answer_plan(
                QuestionPlan(
                    intent=plan.rdb_mode if plan.disposition == "PROCEED" else plan.disposition,
                    subjects=plan.subjects,
                    include_evidence=plan.include_evidence,
                    episode_from=plan.episode_from,
                    episode_to=plan.episode_to,
                )
            )
        if not self.range_allowed(plan):
            return message(
                "CLARIFICATION",
                "허용된 출처 회차 범위 안에서 질문해 주세요.",
                self.codes + ["OUT_OF_SCOPE"],
            )
        self.codes.append("NARRATIVE_SOURCE_RANGE")
        if self.source_from is not None or self.source_to is not None:
            self.codes.append("SOURCE_RANGE")
        stop = await self.collect_rows(plan)
        if stop:
            return stop
        await self.evidence(self.chosen)
        self.add_direct_evidence()
        draft = None
        # A linked quote can already explain the cause/process: no query embedding is needed.
        if self.evidences:
            draft = await self.answer(plan)
            if draft.coverage == "COMPLETE":
                return await self.finish_answer(draft)

        query = " ".join(plan.search_terms) or ctx.question[:1000]
        if len(ctx.question) > 1000 and not plan.search_terms:
            self.codes.append("SEARCH_QUERY_LIMIT")
        candidates = []
        for round_no in range(1, self.hcfg.max_rounds + 1):
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
            else:
                if self.hcfg.max_query_embeddings == 0:
                    self.codes.append("SEARCH_LIMIT")
                    break
                found = await self.search(
                    "vector",
                    ctx.question[:1000],
                    _QueryEmbedding(self),
                    limit=self.hcfg.vector_top_k,
                )
            candidates.extend(found)
            if found and self.hcfg.neighbor_radius:
                candidates.extend(
                    await self.search(
                        "neighbors",
                        found,
                        radius=self.hcfg.neighbor_radius,
                        limit=self.hcfg.max_neighbors,
                    )
                )
            previous = self.evidence_identity()
            self.add_search_evidence(candidates)
            if self.evidence_identity() == previous:
                continue
            await self.register([e.chunk for e in self.evidences if e.chunk])
            draft = await self.answer(plan)
            if draft.coverage == "COMPLETE":
                return await self.finish_answer(draft)
        if draft and draft.coverage != "NONE":
            self.codes.append("PARTIAL_ANSWER")
            return await self.finish_answer(draft)
        return message(
            "INSUFFICIENT_EVIDENCE",
            "조회한 원문에서 질문을 뒷받침할 근거를 충분히 찾지 못했습니다. 작품 전체에 해당 사실이 없다는 뜻은 아닙니다.",
            self.codes + ["NO_SEARCH_EVIDENCE"],
        )

    async def collect_rows(self, plan):
        if plan.rdb_mode == "NONE":
            return None
        for subject in plan.subjects:
            tool = "FIND_CHARACTERS" if subject.domain == "CHARACTER" else "FIND_WORLD_SETTINGS"
            found, complete = await self.pages(tool, query=subject.name.strip())
            if not complete or len(found) > 1:
                return message(
                    "CLARIFICATION",
                    f"'{safe_text(subject.name)}' 대상을 하나로 정할 수 없습니다. 이름이나 대상을 구체적으로 알려주세요.",
                    self.codes,
                )
            if not found:
                self.codes.append("NO_RELATED_SETTING")
                continue  # An unregistered character can still occur in manuscript text.
            item = found[0]
            if item.id is None or item.id != item.subject_id:
                raise SettingToolClientError("CONTRACT_ERROR")
            modes = (
                ["CURRENT", "HISTORY"]
                if plan.rdb_mode == "CURRENT_AND_HISTORY"
                else ["HISTORY" if plan.rdb_mode == "HISTORY" else "CURRENT"]
            )
            for mode in modes:
                items, _ = await self.pages(f"{subject.domain}_{mode}", target=item.subject_id)
                if mode == "HISTORY":
                    self.codes.append("HISTORY_IS_NOT_A_STATE_SNAPSHOT")
                for value in items:
                    if len(self.rows) >= self.cfg.max_items:
                        self.codes.append("PARTIAL_RESULTS")
                        break
                    self.rows.append(AnswerRow(f"R{len(self.rows) + 1}", value, subject.domain))
        schema = response_schema(AnswerSelection)
        while self.rows and not self.fits(
            SELECT_PROMPT, self.payload(self.execution.context.question, plan), schema
        ):
            self.rows.pop()
            self.codes.append("CONTEXT_LIMIT")
        if not self.rows:
            return None

        def valid(selection):
            refs = {r.ref for r in self.rows}
            if (
                len(selection.row_refs) > self.cfg.max_selected_rows
                or not set(selection.row_refs) <= refs
            ):
                raise ValueError("Unknown/excessive RDB row references")

        selection = await self.model(
            AnswerSelection,
            SELECT_PROMPT,
            self.payload(self.execution.context.question, plan),
            "CHAT_ANSWER",
            SELECT_VERSION,
            valid,
        )
        if selection.outcome == "CLARIFICATION":
            return message(
                "CLARIFICATION", "어떤 설정 항목이나 범위인지 구체적으로 알려주세요.", self.codes
            )
        self.chosen = [r for r in self.rows if r.ref in selection.row_refs]
        if len(self.chosen) == self.cfg.max_selected_rows and len(self.chosen) < len(self.rows):
            self.codes.append("SELECTION_LIMIT")
        return None

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
                        self.codes.append("EVIDENCE_LIMIT")
                        continue
                    # Available legacy direct evidence may lack absolute offsets; it retains
                    # its original typed receipt instead of pretending to be a retrieval chunk.
                    self.evidences.append(
                        _Evidence(
                            f"E{len(self.evidences) + 1}",
                            quote.text,
                            item.evidence.source_episode_no,
                            direct_ref=ref,
                            direct_item=item,
                        )
                    )

    def evidence_identity(self):
        return tuple(
            (e.ref, e.chunk.fingerprint if e.chunk else (e.direct_ref, e.text, e.episode))
            for e in self.evidences
        )

    def add_search_evidence(self, candidates):
        direct = [e for e in self.evidences if e.direct_ref]
        available = self.hcfg.max_evidence - len(direct)
        # Leave room for search evidence when many direct quotes did not explain the question.
        if available < 1:
            direct = direct[: max(0, self.hcfg.max_evidence // 2)]
            available = self.hcfg.max_evidence - len(direct)
            self.codes.append("EVIDENCE_LIMIT")
        bundle = compose_evidence(
            candidates,
            max_items=available,
            max_tokens=self.hcfg.evidence_tokens,
            per_episode=min(self.hcfg.per_episode, available),
        )
        self.evidences = [*direct]
        for chunk in bundle.chunks:
            self.evidences.append(
                _Evidence(f"E{len(self.evidences) + 1}", chunk.text, chunk.episode_no, chunk=chunk)
            )
        if bundle.limited:
            self.codes.append("EVIDENCE_CONTEXT_LIMIT")

    def answer_payload(self, plan):
        selected = {r.ref for r in self.chosen}
        rows = [
            r
            for r in self.payload(self.execution.context.question, plan)["rows"]
            if r["rowRef"] in selected
        ]
        chunks = [e.chunk for e in self.evidences if e.chunk]
        bundle = compose_evidence(
            chunks,
            max_items=self.hcfg.max_evidence,
            max_tokens=self.hcfg.evidence_tokens,
            per_episode=self.hcfg.max_evidence,
        )
        by_id = {e.chunk.chunk_id: e.ref for e in self.evidences if e.chunk}
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
            for e in self.evidences
            if e.direct_ref
        )
        return {
            "question": self.execution.context.question,
            "plan": plan.model_dump(by_alias=True),
            "rows": rows,
            "passages": passages,
            "limitations": notices(self.codes),
        }

    async def answer(self, plan):
        schema = response_schema(GroundedAnswer)
        while self.evidences and not self.fits(ANSWER_PROMPT, self.answer_payload(plan), schema):
            self.evidences.pop()
            self.codes.append("CONTEXT_LIMIT")
        if not self.evidences:
            return GroundedAnswer(coverage="NONE", claims=[], conflicts=[])
        e_refs, r_refs = {e.ref for e in self.evidences}, {r.ref for r in self.chosen}

        def valid(answer):
            if any(
                not set(c.evidence_refs) <= e_refs for c in [*answer.claims, *answer.conflicts]
            ) or any(not set(c.row_refs) <= r_refs for c in answer.conflicts):
                raise ValueError("Unknown evidence or setting reference")

        return await self.model(
            GroundedAnswer,
            ANSWER_PROMPT,
            self.answer_payload(plan),
            "CHAT_ANSWER",
            ANSWER_VERSION,
            valid,
        )

    async def finish_answer(self, answer):
        used = {ref for claim in [*answer.claims, *answer.conflicts] for ref in claim.evidence_refs}
        evidence = [e for e in self.evidences if e.ref in used]
        if not used or len(evidence) != len(used):
            return message(
                "INSUFFICIENT_EVIDENCE",
                "답변 근거를 검증할 수 없습니다.",
                self.codes + ["INVALID_QUOTE"],
            )
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
        if answer.coverage != "COMPLETE":
            self.codes.append("PARTIAL_ANSWER")
        self.metrics.chunks.labels("used").inc(len(evidence))
        parts = ["확인한 원문 근거에 따른 답변입니다."]
        for claim in answer.claims:
            parts.append(f"\n- {safe_text(claim.text)} [{', '.join(claim.evidence_refs)}]")
        if self.chosen:
            # Render real registered values, independently of model-authored prose.
            for row in self.chosen:
                registered = render_answer(
                    [AnswerRow(row.ref, row.item, row.domain)],
                    self.codes,
                    direct_only=False,
                    maximum=self.cfg.max_answer_characters,
                )
                parts.append(f"\n\n[{row.ref}] " + registered.result.text)
        for conflict in answer.conflicts:
            parts.append(
                f"\n등록 정보 [{', '.join(conflict.row_refs)}]와 원문 [{', '.join(conflict.evidence_refs)}]가 다르게 나타납니다. 두 내용을 함께 확인해 주세요."
            )
            self.codes.append("SETTING_SOURCE_CONFLICT")
        for ev in evidence:
            parts.append(f"\n[{ev.ref}] 출처 {ev.episode}화: “{safe_text(ev.text)}”")
        text = "".join(parts)
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
                sources=list(dict.fromkeys(e.direct_ref for e in evidence if e.direct_ref)),
                retrieval_sources=[wire_reference(c) for c in chunks],
                limitations=notices(self.codes),
            )
        )
