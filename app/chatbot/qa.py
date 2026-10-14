"""Bounded RDB QA. LLMs classify/select; code owns queries, scope and quotations."""

import json
import re
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from app.chatbot.client import ChatTransportError
from app.chatbot.providers import ChatTextClient
from app.chatbot.qa_config import ChatQaSettings
from app.chatbot.qa_lookup import FocusedLookup
from app.chatbot.qa_collection import WorkLookup
from app.chatbot.qa_synthesis import RegisteredSynthesis
from app.chatbot.qa_prompts import PLAN_PROMPT, PLAN_VERSION, SELECT_PROMPT, SELECT_VERSION
from app.chatbot.qa_render import AnswerRow, message, render_answer, safe_text
from app.chatbot.runtime import Execution, ProviderFailure
from app.clients.setting_tools_client import SettingToolClientError
from app.llm.protocols import LlmResponseSchema, TextGenerationClient
from app.schemas.chat_execution import Finish
from app.schemas.chat_qa import AnswerSelection, QuestionPlan
from app.schemas.setting_tools import SettingToolQuery, SettingToolResponse
from app.usage.metering import _estimate_text_token_upper_bound


def response_schema(model: type[BaseModel]) -> LlmResponseSchema:
    schema = model.model_json_schema(by_alias=True)

    def normalize(node):
        if isinstance(node, dict):
            # Local Pydantic validation enforces these string constraints too.
            for key in ("title", "minLength", "maxLength"):
                node.pop(key, None)
            node.pop("default", None)
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(schema)
    return LlmResponseSchema(model.__name__, schema)


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ChatQaHandler:
    def __init__(self, delegate: TextGenerationClient, settings: ChatQaSettings | None = None):
        self.delegate = delegate
        self.settings = settings or ChatQaSettings()

    async def __call__(self, execution: Execution) -> Finish:
        try:
            return await _Session(self, execution).run()
        except SettingToolClientError as exc:
            # A query failure is never translated to an empty setting list.
            return Finish(
                failure="EXECUTION_LIMIT_EXCEEDED" if exc.status_code == 429 else "NETWORK_FAILURE"
            )
        except ChatTransportError as exc:
            return Finish(
                failure=exc.code
                if exc.code in {"QUOTA_EXHAUSTED", "EXECUTION_LIMIT_EXCEEDED"}
                else "NETWORK_FAILURE"
            )
        except ProviderFailure:
            return Finish(failure="PROVIDER_FAILURE")


class _Session(FocusedLookup, WorkLookup, RegisteredSynthesis):
    schema = staticmethod(response_schema)

    def __init__(self, handler: ChatQaHandler, execution: Execution):
        self.handler, self.execution, self.cfg = handler, execution, handler.settings
        self.tools = execution.tools()
        self.tool_count = 0
        self.codes = []
        self.rows: list[AnswerRow] = []
        self.focused_refs: set[str] = set()
        self.snapshot_versions = {}
        self.last_collection = None
        self.collection_checks = []
        self.incomplete_subjects = set()
        self.include_sources = False
        self.source_from = execution.context.episode_from
        self.source_to = execution.context.episode_to
        self.conversation = [t.model_dump() for t in execution.context.conversation]
        if execution.context.conversation_truncated:
            self.codes.append("CONVERSATION_LIMIT")

    def conversation_payload(self):
        return {"conversation": self.conversation} if self.conversation else {}

    def observed_question_text(self):
        # History supplies vocabulary for reference resolution, never evidence or authority.
        return "\n".join(
            [self.execution.context.question or ""]
            + [text for turn in self.conversation for text in turn.values()]
        ).casefold()

    def plan_payload(self, prompt, schema):
        ctx = self.execution.context
        return self.trim_conversation_to_fit(
            prompt,
            lambda: {
                "question": ctx.question,
                "sourceRange": {"from": ctx.episode_from, "to": ctx.episode_to},
                **self.conversation_payload(),
                "conversationTruncated": "CONVERSATION_LIMIT" in self.codes,
            },
            schema,
        )

    def trim_conversation_to_fit(self, prompt, payload: Callable[[], dict], schema):
        # Fresh rows and quotes take precedence over reference-resolution history.
        while True:
            current = payload()
            if self.fits(prompt, current, schema) or not self.conversation:
                return current
            self.conversation.pop(0)
            if "CONVERSATION_LIMIT" not in self.codes:
                self.codes.append("CONVERSATION_LIMIT")

    def before_model_call(self, purpose):
        pass

    def record_model_response(self, purpose, response):
        pass

    def fits(self, prompt: str, payload: dict, schema: LlmResponseSchema) -> bool:
        text = encode(payload)
        return (
            len(prompt) + len(text) <= self.cfg.max_context_characters
            and _estimate_text_token_upper_bound(prompt, text, self.cfg.model, 0, schema)
            <= self.cfg.max_input_tokens
        )

    async def model(
        self,
        cls,
        prompt,
        payload,
        purpose,
        version,
        validate: Callable | None = None,
        *,
        max_attempts=None,
    ):
        schema = response_schema(cls)
        if not self.fits(prompt, payload, schema):
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        client = ChatTextClient(
            self.handler.delegate, self.execution, purpose=purpose, default_model=self.cfg.model
        )
        for attempt in range(
            self.cfg.max_attempts
            if max_attempts is None
            else min(self.cfg.max_attempts, max_attempts)
        ):
            # Each schema retry is a separate metered provider invocation. No transport retry.
            retry_prompt = prompt + (
                "\n이전 출력이 계약 검증에 실패했습니다. schema와 제공된 참조만 사용하세요."
                if attempt
                else ""
            )
            if not self.fits(retry_prompt, payload, schema):
                raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
            self.before_model_call(purpose)
            response = await client.create_text_response(
                retry_prompt,
                encode(payload),
                max_output_tokens=self.cfg.max_output_tokens,
                prompt_cache_key=version,
                response_schema=schema,
            )
            self.record_model_response(purpose, response)
            try:
                parsed = cls.model_validate_json(response.text)
                if validate:
                    validate(parsed)
                return parsed
            except (ValidationError, ValueError):
                pass
        raise ProviderFailure()

    async def query(self, request: SettingToolQuery) -> SettingToolResponse:
        if self.tool_count >= min(self.cfg.max_tool_calls, self.execution.context.max_tool_calls):
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        self.tool_count += 1
        response = await self.tools.query(request)
        if request.tool.value.endswith("_COLLECTION"):
            meta = response.collection
            if (
                meta is None
                or meta.record_count != response.items.total_elements
                or (request.read_token is not None and meta.read_token != request.read_token)
            ):
                raise SettingToolClientError("CONTRACT_ERROR")
        # A general capability restriction is not a warning about every character answer.
        response_codes = [c for c in response.limitations if c != "SETTING_BOOK_UNSUPPORTED"]
        self.codes.extend(
            c
            for c in response_codes
            if self.include_sources
            or c not in {"SOURCE_REFERENCE_LIMIT", "WORLD_SOURCE_SCAN_LIMIT"}
        )
        if response.partial and not response_codes:
            self.codes.append("PARTIAL_RESULTS")
        return response

    async def pages(
        self,
        tool: str,
        *,
        target=None,
        query=None,
        fact_type=None,
        setting_key=None,
        scope_name=None,
        value_equals=None,
        world_category=None,
        source_range=False,
        exhaustive=False,
        reserve=0,
    ):
        items = []
        complete = True
        collection = tool.endswith("_COLLECTION")
        read_token = None
        self.last_collection = None
        page_limit = 10001 if exhaustive else self.cfg.max_pages
        for page in range(page_limit):
            if (
                exhaustive
                and self.tool_count
                >= min(self.cfg.max_tool_calls, self.execution.context.max_tool_calls) - reserve
            ):
                complete = False
                break
            history = tool.endswith("_HISTORY")
            fields = tool.endswith("_FIELDS")
            result = await self.query(
                SettingToolQuery(
                    tool=tool,
                    target_id=target,
                    query=query,
                    fact_type=fact_type,
                    setting_key=setting_key,
                    scope_name=scope_name,
                    value_equals=value_equals,
                    world_category=world_category,
                    read_token=read_token,
                    page=page,
                    size=self.cfg.page_size,
                    episode_from=self.source_from if history or source_range else None,
                    episode_to=self.source_to if history or source_range else None,
                )
            )
            if collection:
                self.last_collection = result.collection
                read_token = result.collection.read_token
                if page == 0:
                    self.collection_checks.append(
                        (
                            SettingToolQuery(
                                tool=tool,
                                fact_type=fact_type,
                                world_category=world_category,
                                setting_key=setting_key,
                                scope_name=scope_name,
                            ),
                            result.collection,
                        )
                    )
                if result.partial:
                    complete = False
            if result.partial and (tool.startswith("FIND_") or fields):
                complete = False
            if result.items.has_next and not result.items.content:
                raise SettingToolClientError("CONTRACT_ERROR")
            for item in result.items.content:
                expected = (
                    "FIELD"
                    if fields
                    else "HISTORY"
                    if history
                    else "CURRENT"
                    if tool.endswith("_CURRENT") or collection
                    else "CHARACTER"
                    if tool == "FIND_CHARACTERS"
                    else "WORLD_SETTING"
                )
                if item.kind != expected or (target is not None and item.subject_id != target):
                    raise SettingToolClientError("CONTRACT_ERROR")
                if (
                    fact_type is not None
                    and not tool.startswith("FIND_")
                    and item.category != fact_type
                ):
                    raise SettingToolClientError("CONTRACT_ERROR")
                if (
                    setting_key is not None
                    and not tool.startswith("FIND_")
                    and (item.setting_key != setting_key or item.scope_name != scope_name)
                ):
                    raise SettingToolClientError("CONTRACT_ERROR")
                if fields and (not item.setting_key or not item.category):
                    raise SettingToolClientError("CONTRACT_ERROR")
                if history and (
                    item.id is None
                    or self.source_from is not None
                    and (
                        item.source_episode_no is None or item.source_episode_no < self.source_from
                    )
                    or self.source_to is not None
                    and (item.source_episode_no is None or item.source_episode_no > self.source_to)
                ):
                    raise SettingToolClientError("CONTRACT_ERROR")
                if expected == "CURRENT":
                    if item.history_only is True or item.contributes_to_current is not True:
                        raise SettingToolClientError("CONTRACT_ERROR")
                    version = self.snapshot_versions.get(item.subject_id)
                    if version is not None and item.setting_version != version:
                        raise SettingToolClientError("CONCURRENT_UPDATE")
                    self.snapshot_versions[item.subject_id] = item.setting_version
                    if item.value_truncated:
                        complete = False
                if exhaustive or len(items) < self.cfg.max_items:
                    items.append(item)
                else:
                    complete = False
            if not result.items.has_next:
                break
            if not exhaustive and (
                page + 1 == self.cfg.max_pages or len(items) >= self.cfg.max_items
            ):
                complete = False
                break
        if not complete:
            self.codes.append("PARTIAL_RESULTS")
        return items, complete

    def range_allowed(self, plan: QuestionPlan) -> bool:
        # Conversation can resolve names, but cannot authorize an old episode filter.
        # Accept bounds repeated from this request's trusted UI range, or numbers
        # attached to an episode expression in the current question (3~5화, 3화부터).
        question = self.execution.context.question or ""
        episodes = {
            int(value)
            for match in re.finditer(
                r"(?<![\d.])(?:제\s*)?(\d+)\s*(?:(?:[~～–—-]|부터|에서)\s*(?:제\s*)?(\d+)\s*)?(?:화|회차)"
                r"|\bepisodes?\s+(\d+)(?:\s*(?:[~–—-]|to|through)\s*(\d+))?",
                question,
                re.IGNORECASE,
            )
            for value in match.groups()
            if value is not None
        }
        if any(
            bound is not None and bound != trusted and bound not in episodes
            for bound, trusted in (
                (plan.episode_from, self.source_from),
                (plan.episode_to, self.source_to),
            )
        ):
            return False
        if plan.episode_from is not None:
            if self.source_from is not None and plan.episode_from < self.source_from:
                return False
            self.source_from = plan.episode_from
        if plan.episode_to is not None:
            if self.source_to is not None and plan.episode_to > self.source_to:
                return False
            self.source_to = plan.episode_to
        return (
            self.source_from is None or self.source_to is None or self.source_from <= self.source_to
        )

    def payload(self, question: str, plan: QuestionPlan, *, rows=None):
        return {
            "question": question,
            **self.conversation_payload(),
            "conversationTruncated": "CONVERSATION_LIMIT" in self.codes,
            "plan": plan.model_dump(by_alias=True),
            "maxSelectedRows": self.cfg.max_selected_rows,
            "partial": any(c in self.codes for c in {"PARTIAL_RESULTS", "CONTEXT_LIMIT"}),
            "rows": [
                {
                    "rowRef": row.ref,
                    "domain": row.domain,
                    **row.item.model_dump(
                        by_alias=True,
                        include={
                            "kind",
                            "subject_name",
                            "category",
                            "scope_name",
                            "setting_key",
                            "display_name",
                            "value",
                            "structured_value_json",
                            "value_truncated",
                            "contributes_to_current",
                            "operation",
                            "history_only",
                            "source_episode_no",
                            "origin",
                        },
                    ),
                }
                for row in (self.rows if rows is None else rows)
            ],
        }

    async def run(self) -> Finish:
        context = self.execution.context
        if context.operational or not context.question or not context.question.strip():
            return message(
                "CLARIFICATION", "조회할 인물이나 세계관 항목과 질문을 알려주세요.", self.codes
            )

        def validate_plan(plan):
            self.validate_focus(plan)
            if any(
                s.name.strip().casefold() not in self.observed_question_text()
                for s in plan.subjects
            ):
                raise ValueError("Only names explicitly present in the question may be searched")

        plan = await self.model(
            QuestionPlan,
            PLAN_PROMPT,
            self.plan_payload(PLAN_PROMPT, response_schema(QuestionPlan)),
            "CHAT_INTERPRETATION",
            PLAN_VERSION,
            validate_plan,
        )
        return await self.answer_plan(plan)

    async def answer_plan(self, plan: QuestionPlan) -> Finish:
        """Reuse bounded RDB behavior after a trusted, validated planner has run."""
        self.include_sources = plan.include_evidence or plan.intent == "DIRECT_EVIDENCE"
        if plan.intent in {"NEEDS_SEARCH", "PAST_STATE", "CLARIFICATION", "UNSUPPORTED"}:
            outcome, text, code = {
                "NEEDS_SEARCH": (
                    "INSUFFICIENT_EVIDENCE",
                    "현재 등록 설정만으로 장면이나 사건의 이유를 확인할 수 없습니다.",
                    "SEMANTIC_SEARCH_REQUIRED",
                ),
                "PAST_STATE": (
                    "INSUFFICIENT_EVIDENCE",
                    "그 회차 당시의 상태를 복원할 수 없습니다. 출처 회차별 변경 이력은 조회할 수 있습니다.",
                    "PAST_STATE_UNSUPPORTED",
                ),
                "CLARIFICATION": (
                    "CLARIFICATION",
                    "어떤 인물이나 설정을 뜻하는지 조금 더 구체적으로 알려주세요.",
                    "NEEDS_CLARIFICATION",
                ),
                "UNSUPPORTED": (
                    "INSUFFICIENT_EVIDENCE",
                    "이 요청은 등록 설정과 직접 근거의 읽기 전용 조회 범위를 벗어납니다.",
                    "UNSUPPORTED_REQUEST",
                ),
            }[plan.intent]
            return message(outcome, text, self.codes + [code])
        if not self.range_allowed(plan):
            return message(
                "CLARIFICATION",
                "허용된 출처 회차 범위 안에서 질문해 주세요.",
                self.codes + ["OUT_OF_SCOPE"],
            )
        if self.source_from is not None or self.source_to is not None:
            self.codes.append("SOURCE_RANGE")
        if plan.intent == "CHARACTER_LIST":
            return await self.character_list(plan)
        if plan.collection is not None:
            return await self.answer_collection(plan)
        targets = []
        for subject in plan.subjects:
            find = "FIND_CHARACTERS" if subject.domain == "CHARACTER" else "FIND_WORLD_SETTINGS"
            found, complete = await self.pages(find, query=subject.name.strip())
            if not complete or len(found) > 1:
                names = ", ".join(safe_text(i.subject_name) for i in found[:5])
                return message(
                    "CLARIFICATION",
                    f"'{safe_text(subject.name)}'의 대상을 하나로 정할 수 없습니다. 이름이나 대상을 더 구체적으로 알려주세요. 조회된 후보: {names or '조회 범위 확인 필요'}",
                    self.codes,
                )
            if not found:
                return message(
                    "INSUFFICIENT_EVIDENCE",
                    f"등록 정보에서 '{safe_text(subject.name)}' 대상을 찾지 못했습니다. 작품에 존재하지 않는다는 뜻은 아닙니다.",
                    self.codes + ["NO_RELATED_SETTING"],
                )
            if found[0].id is None or found[0].id != found[0].subject_id:
                raise SettingToolClientError("CONTRACT_ERROR")
            targets.append((subject, found[0].subject_id))
        synthesis = (
            plan.answer_shape in {"SUMMARY", "COUNT", "LIST"} and plan.intent != "DIRECT_EVIDENCE"
        )
        current_checks = len(targets) if synthesis and plan.intent != "HISTORY" else 0
        for subject, target in targets:
            modes = (
                ["CURRENT", "HISTORY"]
                if plan.intent == "CURRENT_AND_HISTORY"
                else ["HISTORY" if plan.intent == "HISTORY" else "CURRENT"]
            )
            stop = await self.subject_rows(subject, target, modes, reserve=current_checks)
            if stop:
                return stop
        chosen, stop = await self.select_rows(plan)
        if stop:
            return stop
        if {row.item.subject_id for row in chosen} != {target for _, target in targets}:
            self.codes.append("NO_RELATED_SETTING")
        if plan.include_evidence or plan.intent == "DIRECT_EVIDENCE":
            await self.evidence(chosen, reserve=current_checks)
        if synthesis:
            result = await self.registered_answer(plan, chosen)
            if current_checks and not await self.current_subjects_unchanged(chosen):
                return message(
                    "INSUFFICIENT_EVIDENCE",
                    "답변을 준비하는 동안 현재 설정이 변경됐어요. 다시 질문하면 최신 설정으로 확인할게요.",
                    self.codes + ["CURRENT_CHANGED"],
                )
            return result
        return render_answer(
            chosen,
            self.codes,
            direct_only=plan.intent == "DIRECT_EVIDENCE",
            maximum=self.cfg.max_answer_characters,
        )

    async def current_subjects_unchanged(self, rows):
        """Recheck named current subjects after synthesis without sending IDs to the model."""
        subjects = {
            (row.domain, row.item.subject_id): row.item
            for row in rows
            if row.item.kind == "CURRENT"
        }
        for (domain, target), original in subjects.items():
            try:
                result = await self.query(
                    SettingToolQuery(tool=f"{domain}_CURRENT", target_id=target, size=1)
                )
            except SettingToolClientError as exc:
                if exc.status_code in {404, 409} or exc.code == "CONCURRENT_UPDATE":
                    return False
                raise
            if not result.items.content:
                return False
            current = result.items.content[0]
            if (
                current.kind != "CURRENT"
                or current.subject_id != target
                or current.subject_name != original.subject_name
                or current.setting_version is None
                or current.setting_version != original.setting_version
            ):
                return False
        return True

    async def select_rows(self, plan, *, allow_missing=False):
        question = self.execution.context.question
        focused = [row for row in self.rows if row.ref in self.focused_refs]
        candidates = [row for row in self.rows if row.ref not in self.focused_refs]
        if focused and not candidates:
            return list(self.rows), None
        schema = response_schema(AnswerSelection)

        def payload():
            return self.payload(question, plan, rows=candidates)

        self.trim_conversation_to_fit(SELECT_PROMPT, payload, schema)
        while candidates and not self.fits(SELECT_PROMPT, payload(), schema):
            candidates.pop()
            self.codes.append("CONTEXT_LIMIT")
        if not candidates:
            if focused:
                return focused, None
            if allow_missing:
                self.codes.append("NO_RELATED_SETTING")
                return [], None
            return [], message(
                "INSUFFICIENT_EVIDENCE",
                "조회한 등록 정보에서 답변할 항목을 확인하지 못했습니다.",
                self.codes + ["NO_RELATED_SETTING"],
            )
        by_ref = {row.ref: row for row in candidates}

        def validate_selection(selection):
            if len(selection.row_refs) > self.cfg.max_selected_rows or any(
                ref not in by_ref for ref in selection.row_refs
            ):
                raise ValueError("Unknown or excessive row references")

        selection = await self.model(
            AnswerSelection,
            SELECT_PROMPT,
            payload(),
            "CHAT_ANSWER",
            SELECT_VERSION,
            validate_selection,
        )
        if selection.outcome != "ANSWER":
            text = (
                "어떤 설정 항목이나 범위를 뜻하는지 더 구체적으로 알려주세요."
                if selection.outcome == "CLARIFICATION"
                else "조회한 등록 정보만으로 질문에 답할 수 없습니다. 작품 전체에서 사실이 없다는 뜻은 아닙니다."
            )
            if selection.outcome == "INSUFFICIENT_EVIDENCE":
                self.codes.append("NO_RELATED_SETTING")
                if focused:
                    return focused, None
            if allow_missing and selection.outcome == "INSUFFICIENT_EVIDENCE":
                return [], None
            return [], message(selection.outcome, text, self.codes)
        # Preserve server order rather than allowing model reordering of historical events.
        chosen = [row for row in self.rows if row.ref in {*selection.row_refs, *self.focused_refs}]
        if len(selection.row_refs) == self.cfg.max_selected_rows and len(selection.row_refs) < len(
            candidates
        ):
            self.codes.append("SELECTION_LIMIT")
        return chosen, None

    async def evidence(self, rows: list[AnswerRow], *, reserve=0, revalidate=False):
        cache = {}
        for row in rows:
            if row.item.sources_truncated:
                self.codes.append("SOURCE_REFERENCE_LIMIT")
            if not row.item.sources:
                self.codes.append("NO_SOURCE")
            for ref in row.item.sources:
                if ref not in cache:
                    final_checks = len(cache) + 1 if revalidate else 0
                    if (
                        len(cache) >= self.cfg.max_evidence_calls
                        or self.tool_count + 1 + reserve + final_checks
                        > min(self.cfg.max_tool_calls, self.execution.context.max_tool_calls)
                    ):
                        self.codes.append("EVIDENCE_LIMIT")
                        continue
                    result = await self.query(
                        SettingToolQuery(
                            tool=ref.tool,
                            target_id=ref.target_id,
                            size=self.cfg.page_size,
                            episode_from=self.source_from,
                            episode_to=self.source_to,
                        )
                    )
                    if len(result.items.content) != 1:
                        raise SettingToolClientError("CONTRACT_ERROR")
                    item = result.items.content[0]
                    if item.id != ref.target_id or item.kind != "EVIDENCE" or item.evidence is None:
                        raise SettingToolClientError("CONTRACT_ERROR")
                    cache[ref] = item
                item = cache[ref]
                if item.subject_id != row.item.subject_id:
                    raise SettingToolClientError("CONTRACT_ERROR")
                ev = item.evidence
                if ev.status != "AVAILABLE":
                    self.codes.append(ev.status)
                    continue
                if not ev.quotes or ev.matches_current_source is not True:
                    self.codes.append("INVALID_QUOTE")
                    continue
                if (
                    self.source_from is not None
                    and (ev.source_episode_no is None or ev.source_episode_no < self.source_from)
                    or self.source_to is not None
                    and (ev.source_episode_no is None or ev.source_episode_no > self.source_to)
                ):
                    raise SettingToolClientError("CONTRACT_ERROR")
                if row.item.kind == "CURRENT" and (
                    item.contributes_to_current is not True
                    or item.history_only is True
                    or (item.category, item.scope_name, item.setting_key)
                    != (row.item.category, row.item.scope_name, row.item.setting_key)
                ):
                    self.codes.append("STALE_CURRENT_SOURCE")
                    continue
                row.evidence.append((ref, item))
