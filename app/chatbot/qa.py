"""Bounded RDB QA. LLMs classify/select; code owns queries, scope and quotations."""

import json
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from app.chatbot.client import ChatTransportError
from app.chatbot.providers import ChatTextClient
from app.chatbot.qa_config import ChatQaSettings
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


class _Session:
    def __init__(self, handler: ChatQaHandler, execution: Execution):
        self.handler, self.execution, self.cfg = handler, execution, handler.settings
        self.tools = execution.tools()
        self.tool_count = 0
        self.codes = ["SETTING_BOOK_UNSUPPORTED"]
        self.rows: list[AnswerRow] = []
        self.source_from = execution.context.episode_from
        self.source_to = execution.context.episode_to

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

    async def model(self, cls, prompt, payload, purpose, version, validate: Callable | None = None):
        schema = response_schema(cls)
        if not self.fits(prompt, payload, schema):
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        client = ChatTextClient(
            self.handler.delegate, self.execution, purpose=purpose, default_model=self.cfg.model
        )
        for attempt in range(self.cfg.max_attempts):
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
        self.codes.extend(response.limitations)
        if response.partial:
            self.codes.append("PARTIAL_RESULTS")
        return response

    async def pages(self, tool: str, *, target=None, query=None):
        items = []
        complete = True
        version = None
        for page in range(self.cfg.max_pages):
            history = tool.endswith("_HISTORY")
            result = await self.query(
                SettingToolQuery(
                    tool=tool,
                    target_id=target,
                    query=query,
                    page=page,
                    size=self.cfg.page_size,
                    episode_from=self.source_from if history else None,
                    episode_to=self.source_to if history else None,
                )
            )
            if result.partial and tool.startswith("FIND_"):
                complete = False
            for item in result.items.content:
                expected = (
                    "HISTORY"
                    if history
                    else "CURRENT"
                    if tool.endswith("_CURRENT")
                    else "CHARACTER"
                    if tool == "FIND_CHARACTERS"
                    else "WORLD_SETTING"
                )
                if item.kind != expected or (target is not None and item.subject_id != target):
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
                    if version is not None and item.setting_version != version:
                        raise SettingToolClientError("CONCURRENT_UPDATE")
                    version = item.setting_version
                if len(items) < self.cfg.max_items:
                    items.append(item)
                else:
                    complete = False
            if not result.items.has_next:
                break
            if page + 1 == self.cfg.max_pages or len(items) >= self.cfg.max_items:
                complete = False
                break
        if not complete:
            self.codes.append("PARTIAL_RESULTS")
        return items, complete

    def range_allowed(self, plan: QuestionPlan) -> bool:
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

    def payload(self, question: str, plan: QuestionPlan):
        return {
            "question": question,
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
                for row in self.rows
            ],
        }

    async def run(self) -> Finish:
        context = self.execution.context
        if context.operational or not context.question or not context.question.strip():
            return message(
                "CLARIFICATION", "조회할 인물이나 세계관 항목과 질문을 알려주세요.", self.codes
            )
        question = context.question

        def validate_plan(plan):
            if any(s.name.strip().casefold() not in question.casefold() for s in plan.subjects):
                raise ValueError("Only names explicitly present in the question may be searched")

        plan = await self.model(
            QuestionPlan,
            PLAN_PROMPT,
            {
                "question": question,
                "sourceRange": {"from": context.episode_from, "to": context.episode_to},
            },
            "CHAT_INTERPRETATION",
            PLAN_VERSION,
            validate_plan,
        )
        return await self.answer_plan(plan)

    async def answer_plan(self, plan: QuestionPlan) -> Finish:
        """Reuse bounded RDB behavior after a trusted, validated planner has run."""
        question = self.execution.context.question
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
                    "인물·세계관의 이름과 조회할 항목을 구체적으로 알려주세요. 한 번에 최대 3개 대상을 조회합니다.",
                    "UNSUPPORTED_REQUEST",
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
            targets.append((subject.domain, found[0].subject_id))
        for domain, target in targets:
            modes = (
                ["CURRENT", "HISTORY"]
                if plan.intent == "CURRENT_AND_HISTORY"
                else ["HISTORY" if plan.intent == "HISTORY" else "CURRENT"]
            )
            for mode in modes:
                items, _ = await self.pages(f"{domain}_{mode}", target=target)
                if mode == "HISTORY":
                    self.codes.append("HISTORY_IS_NOT_A_STATE_SNAPSHOT")
                for item in items:
                    if len(self.rows) >= self.cfg.max_items:
                        self.codes.append("PARTIAL_RESULTS")
                        break
                    self.rows.append(AnswerRow(f"R{len(self.rows) + 1}", item, domain))
        schema = response_schema(AnswerSelection)
        while self.rows and not self.fits(SELECT_PROMPT, self.payload(question, plan), schema):
            self.rows.pop()
            self.codes.append("CONTEXT_LIMIT")
        if not self.rows:
            return message(
                "INSUFFICIENT_EVIDENCE",
                "조회한 등록 정보에서 답변할 항목을 확인하지 못했습니다.",
                self.codes + ["NO_RELATED_SETTING"],
            )
        by_ref = {row.ref: row for row in self.rows}

        def validate_selection(selection):
            if len(selection.row_refs) > self.cfg.max_selected_rows or any(
                ref not in by_ref for ref in selection.row_refs
            ):
                raise ValueError("Unknown or excessive row references")

        selection = await self.model(
            AnswerSelection,
            SELECT_PROMPT,
            self.payload(question, plan),
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
            return message(selection.outcome, text, self.codes)
        # Preserve server order rather than allowing model reordering of historical events.
        chosen = [row for row in self.rows if row.ref in selection.row_refs]
        if len(chosen) == self.cfg.max_selected_rows and len(chosen) < len(self.rows):
            self.codes.append("SELECTION_LIMIT")
        if {row.item.subject_id for row in chosen} != {target for _, target in targets}:
            self.codes.append("NO_RELATED_SETTING")
        if plan.include_evidence or plan.intent == "DIRECT_EVIDENCE":
            await self.evidence(chosen)
        return render_answer(
            chosen,
            self.codes,
            direct_only=plan.intent == "DIRECT_EVIDENCE",
            maximum=self.cfg.max_answer_characters,
        )

    async def evidence(self, rows: list[AnswerRow]):
        cache = {}
        for row in rows:
            if row.item.sources_truncated:
                self.codes.append("SOURCE_REFERENCE_LIMIT")
            if not row.item.sources:
                self.codes.append("NO_SOURCE")
            for ref in row.item.sources:
                if ref not in cache:
                    if len(cache) >= self.cfg.max_evidence_calls:
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
