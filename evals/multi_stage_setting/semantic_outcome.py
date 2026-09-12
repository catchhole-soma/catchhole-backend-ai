from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from app.analysis.json_response import parse_json_object
from app.core.config import get_settings
from app.llm.exceptions import LlmOutputTruncatedError, LlmResponseValidationError
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import LlmResponseSchema
from app.llm.responses import LlmTextResponse
from app.usage.metering import _estimate_text_token_upper_bound
from evals.multi_stage_setting.provider_diagnostics import (
    provider_failure_details,
    request_size_details,
    sanitize_provider_details,
)

DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "semantic_outcome_judge.md"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_MAX_INPUT_TOKENS = 64000
DEFAULT_MAX_OUTPUT_TOKENS = 32000
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")
PROMPT_CACHE_KEY = "multi-stage-setting-eval:semantic-outcome:v4"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorldSettingNameContext:
    category: str
    subject_name: str
    scope_name: str | None
    expected_setting_name: str
    actual_setting_name: str
    actual_scope_name: str | None = None
    related_paths: tuple[dict[str, object], ...] = ()
    operation: str | None = None


@dataclass(frozen=True)
class CharacterSettingContext:
    entity_id: str
    fact_type: str
    expected_fact_key: str
    actual_fact_key: str
    schema_pattern: str


@dataclass(frozen=True)
class SemanticOutcomeCase:
    case_id: str
    expected_value: str | None
    actual_value: str | None
    before_value: str | None = None
    source_values: tuple[str, ...] = ()
    required_facts: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    evidence_quotes: tuple[str, ...] = ()
    setting_context: WorldSettingNameContext | None = None
    character_context: CharacterSettingContext | None = None
    scenario_id: str | None = None


class SemanticOutcomeDecision(BaseModel):
    case_id: str = Field(alias="caseId")
    core_meaning_covered: bool = Field(alias="coreMeaningCovered", strict=True)
    required_facts_covered: bool = Field(alias="requiredFactsCovered", strict=True)
    forbidden_facts_absent: bool = Field(alias="forbiddenFactsAbsent", strict=True)
    contradiction: bool = Field(strict=True)
    unsupported_detail: bool = Field(alias="unsupportedDetail", strict=True)
    reason: str
    same_setting: bool | None = Field(default=None, alias="sameSetting", strict=True)
    setting_reason: str | None = Field(default=None, alias="settingReason")
    scope_equivalent: bool | None = Field(default=None, alias="scopeEquivalent", strict=True)
    scope_reason: str | None = Field(default=None, alias="scopeReason")
    value_resolved: bool = Field(default=True, alias="valueResolved", strict=True)

    @property
    def matched(self) -> bool | None:
        if not self.value_resolved:
            return None
        return (
            self.core_meaning_covered
            and self.required_facts_covered
            and self.forbidden_facts_absent
            and not self.contradiction
            and not self.unsupported_detail
        )


class SemanticOutcomeResponse(BaseModel):
    results: list[SemanticOutcomeDecision]


def _response_schema() -> LlmResponseSchema:
    schema = SemanticOutcomeResponse.model_json_schema(by_alias=True)

    def require_fields(node: dict) -> None:
        node.pop("default", None)
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for value in node.values():
            if isinstance(value, dict):
                require_fields(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        require_fields(item)

    require_fields(schema)
    return LlmResponseSchema(name="setting_semantic_outcomes_v4", schema=schema)


@dataclass(frozen=True)
class SemanticOutcomeBatchResult:
    decisions: tuple[SemanticOutcomeDecision, ...]
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


class SemanticOutcomeJudge(Protocol):
    async def judge_many(
        self,
        cases: Sequence[SemanticOutcomeCase],
    ) -> SemanticOutcomeBatchResult: ...


class OpenAISemanticOutcomeJudge:
    def __init__(
        self,
        client: OpenAIResponsesClient | None = None,
        *,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if max_input_tokens < 1:
            raise ValueError("max_input_tokens must be at least 1.")
        if not 1 <= max_output_tokens <= 128000:
            raise ValueError("max_output_tokens must be between 1 and 128000.")
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Unsupported semantic judge reasoning effort.")
        if not model.strip():
            raise ValueError("Semantic judge model must not be empty.")
        if client is None:
            settings = get_settings()
            client = OpenAIResponsesClient(
                api_key=settings.llm_api_key,
                model=model,
                responses_api_url=settings.openai_responses_api_url,
                reasoning_effort=reasoning_effort,
            )
        elif isinstance(client, OpenAIResponsesClient):
            # Share the injected transport, not mutable product model/effort settings.
            client = OpenAIResponsesClient(
                api_key=client.api_key,
                model=model,
                responses_api_url=client.responses_api_url,
                reasoning_effort=reasoning_effort,
                http_client=client.http_client,
            )
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.prompt_path = prompt_path
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens

    async def judge_many(
        self,
        cases: Sequence[SemanticOutcomeCase],
    ) -> SemanticOutcomeBatchResult:
        if not cases:
            return SemanticOutcomeBatchResult(decisions=())
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        response_schema = _response_schema()
        chunks = self._batch_cases(cases, system_prompt, response_schema)
        decisions: list[SemanticOutcomeDecision] = []
        input_tokens = cached_input_tokens = output_tokens = 0
        for chunk in chunks:
            result = await self._judge_chunk(chunk, system_prompt, response_schema)
            decisions.extend(result.decisions)
            input_tokens += result.input_tokens
            cached_input_tokens += result.cached_input_tokens
            output_tokens += result.output_tokens
        return SemanticOutcomeBatchResult(
            decisions=tuple(decisions),
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )

    def _batch_cases(
        self,
        cases: Sequence[SemanticOutcomeCase],
        system_prompt: str,
        response_schema: LlmResponseSchema,
    ) -> list[list[SemanticOutcomeCase]]:
        def input_tokens(items: Sequence[SemanticOutcomeCase]) -> int:
            # Use the same tokenizer, schema accounting, and framing margin as runtime metering.
            return _estimate_text_token_upper_bound(
                system_prompt, _user_prompt(items), self.model, 0, response_schema
            )

        chunks: list[list[SemanticOutcomeCase]] = []
        chunk: list[SemanticOutcomeCase] = []
        for index, case in enumerate(cases):
            if chunk and case.scenario_id != chunk[-1].scenario_id:
                chunks.append(chunk)
                chunk = []
            proposed = [*chunk, case]
            if input_tokens(proposed) <= self.max_input_tokens:
                chunk = proposed
                continue
            # Validate every singleton before any paid request; never truncate or drop a case.
            if not chunk or input_tokens([case]) > self.max_input_tokens:
                raise ValueError(
                    f"Semantic judge case at index {index} exceeds max_input_tokens "
                    f"({self.max_input_tokens}); no cases were sent."
                )
            chunks.append(chunk)
            chunk = [case]
        if chunk:
            chunks.append(chunk)
        return chunks

    async def _request_judgment(
        self,
        cases: Sequence[SemanticOutcomeCase],
        system_prompt: str,
        response_schema: LlmResponseSchema,
    ) -> LlmTextResponse:
        user_prompt = _user_prompt(cases)
        details = sanitize_provider_details({
            **request_size_details(system_prompt, user_prompt, response_schema.schema),
            "model": self.model,
            "purpose": "SEMANTIC_JUDGE",
            "max_output_tokens": self.max_output_tokens,
            "store_responses": getattr(self.client, "store_responses", False),
        })
        print("LLM call started " + json.dumps(details), file=sys.stderr, flush=True)
        started = time.monotonic()
        try:
            response = await self.client.create_text_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                max_output_tokens=self.max_output_tokens,
                prompt_cache_key=PROMPT_CACHE_KEY,
                response_schema=response_schema,
            )
        except (httpx.HTTPError, LlmResponseValidationError) as exc:
            failure = sanitize_provider_details({
                **details,
                **provider_failure_details(exc, model=self.model, prompt_cache_key=PROMPT_CACHE_KEY),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            exc.evaluation_provider_details = failure
            print("LLM call failed " + json.dumps(failure), file=sys.stderr, flush=True)
            raise
        completed = sanitize_provider_details({
            **details,
            "response_id": getattr(response, "raw_response", {}).get("id"),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
        print("LLM call completed " + json.dumps(completed), file=sys.stderr, flush=True)
        return response

    async def _judge_chunk(
        self,
        cases: Sequence[SemanticOutcomeCase],
        system_prompt: str,
        response_schema: LlmResponseSchema,
    ) -> SemanticOutcomeBatchResult:
        try:
            response = await self._request_judgment(cases, system_prompt, response_schema)
        except LlmOutputTruncatedError as exc:
            if len(cases) == 1:
                raise
            logger.warning(
                "Semantic judge output truncated; splitting case_count=%s max_output_tokens=%s",
                len(cases),
                self.max_output_tokens,
            )
            midpoint = len(cases) // 2
            left = await self._judge_chunk(cases[:midpoint], system_prompt, response_schema)
            right = await self._judge_chunk(cases[midpoint:], system_prompt, response_schema)
            return SemanticOutcomeBatchResult(
                decisions=left.decisions + right.decisions,
                input_tokens=(exc.input_token_count or 0) + left.input_tokens + right.input_tokens,
                cached_input_tokens=(
                    (exc.cached_input_token_count or 0)
                    + left.cached_input_tokens
                    + right.cached_input_tokens
                ),
                output_tokens=(exc.output_token_count or 0)
                + left.output_tokens
                + right.output_tokens,
            )
        try:
            parsed = SemanticOutcomeResponse.model_validate(parse_json_object(response.text))
        except ValueError:
            raise ValueError("Semantic outcome judge response is invalid.") from None
        expected_ids = [case.case_id for case in cases]
        decision_by_id: dict[str, SemanticOutcomeDecision] = {}
        for decision in parsed.results:
            if decision.case_id in decision_by_id:
                raise ValueError("Semantic outcome judge returned a duplicate caseId.")
            decision_by_id[decision.case_id] = decision
        if set(decision_by_id) != set(expected_ids):
            raise ValueError("Semantic outcome judge caseIds do not match the request.")
        for case in cases:
            decision = decision_by_id[case.case_id]
            if "value_resolved" not in decision.model_fields_set or not decision.reason.strip():
                raise ValueError("Semantic outcome judge value decision is invalid.")
            required_verdicts = []
            if case.setting_context is not None or case.character_context is not None:
                required_verdicts.append(("same_setting", "setting_reason"))
            if case.setting_context is not None:
                required_verdicts.append(("scope_equivalent", "scope_reason"))
            if case.character_context is not None and (
                decision.scope_equivalent is not None or decision.scope_reason is not None
            ):
                raise ValueError("Semantic outcome judge character scope decision must be null.")
            for verdict, reason in required_verdicts:
                explanation = getattr(decision, reason)
                if (
                    verdict not in decision.model_fields_set
                    or reason not in decision.model_fields_set
                    or not explanation
                    or not explanation.strip()
                ):
                    raise ValueError("Semantic outcome judge setting-path decision is invalid.")
        return SemanticOutcomeBatchResult(
            decisions=tuple(decision_by_id[case_id] for case_id in expected_ids),
            input_tokens=response.input_token_count or 0,
            cached_input_tokens=response.cached_input_token_count or 0,
            output_tokens=response.output_token_count or 0,
        )


def _user_prompt(cases: Sequence[SemanticOutcomeCase]) -> str:
    return json.dumps(
        {
            "cases": [
                {
                    "caseId": case.case_id,
                    "beforeValue": case.before_value,
                    "sourceValues": list(case.source_values),
                    "expectedValue": case.expected_value,
                    "actualValue": case.actual_value,
                    "requiredFacts": list(case.required_facts),
                    "forbiddenFacts": list(case.forbidden_facts),
                    "evidenceQuotes": list(case.evidence_quotes),
                    **(
                        {
                            "settingContext": {
                                "category": case.setting_context.category,
                                "subjectName": case.setting_context.subject_name,
                                "scopeName": case.setting_context.scope_name,
                                "actualScopeName": case.setting_context.actual_scope_name,
                                "expectedSettingName": case.setting_context.expected_setting_name,
                                "actualSettingName": case.setting_context.actual_setting_name,
                                "relatedPaths": list(case.setting_context.related_paths),
                                "operation": case.setting_context.operation,
                            }
                        }
                        if case.setting_context is not None
                        else {}
                    ),
                    **(
                        {
                            "characterContext": {
                                "entityId": case.character_context.entity_id,
                                "factType": case.character_context.fact_type,
                                "expectedFactKey": case.character_context.expected_fact_key,
                                "actualFactKey": case.character_context.actual_fact_key,
                                "schemaPattern": case.character_context.schema_pattern,
                            }
                        }
                        if case.character_context is not None
                        else {}
                    ),
                }
                for case in cases
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
    )
