"""Independent read-only typo checking over one authenticated frozen manuscript."""

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, ValidationError

from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmInputTokenCountError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import LlmCallOptions, LlmResponseSchema
from app.usage.metering import MeteredTextGenerationClient
from app.validation.contracts import (
    BudgetState,
    Contract,
    IncompleteInspection,
    InvocationBudget,
    SegmentCoverage,
    Span,
    TypoCheckInput,
    TypoCheckResult,
    TypoSuggestion,
    anchor_span,
    split_segments,
    validate_span,
)
from app.validation.data import ValidationDataClient

logger = logging.getLogger(__name__)


class ClassifiedTypoSuggestion(TypoSuggestion):
    """GH309-local additive classification; the shared suggestion stays unchanged."""

    category: Literal["SPELLING", "SPACING", "TYPO"]
    reason: str = Field(min_length=1)


class ClassifiedTypoCheckResult(TypoCheckResult):
    suggestions: list[ClassifiedTypoSuggestion]


class _TypoBatch(Contract):
    segments: list[SegmentCoverage]
    suggestions: list[ClassifiedTypoSuggestion]


_SCHEMA = LlmResponseSchema("typo_check_v1", _TypoBatch.model_json_schema())
_PROMPT = (Path(__file__).resolve().parents[1] / "llm/prompts/typo_check.md").read_text()
_OPTIONS = LlmCallOptions(reasoning_effort="low")
_MODEL = "gpt-6.1-terra"


async def _call(state, operation, *args, kind=None, **kwargs):
    if kind is not None:
        state.consume(kind)
    try:
        async with asyncio.timeout(state.remaining()):
            return await operation(*args, **kwargs)
    except TimeoutError:
        raise IncompleteInspection("BUDGET_EXHAUSTED") from None
    except (IncompleteInspection, LlmResponseValidationError, LlmInputTokenCountError,
            httpx.HTTPError):
        raise
    except Exception:  # noqa: BLE001 - external I/O failures must not expose arbitrary details
        raise IncompleteInspection("UNEXPECTED_ERROR") from None


def _anchored(suggestions, segment, text, aliases, stats):
    """GH331 S2: re-anchor each model span on its own exact quote inside the owning segment."""
    def owned(start, end):
        return segment.start <= start < segment.end and end <= segment.context_end
    result = []
    for suggestion in suggestions:
        span = anchor_span(suggestion.span, text, aliases=aliases, accept=owned, stats=stats)
        result.append(suggestion if span is suggestion.span else suggestion.model_copy(update={"span": span}))
    return result


def _validated_batch(response, segment, text, accepted, aliases=frozenset(), stats=None):
    try:
        batch = _TypoBatch.model_validate_json(response.text, strict=True)
    except ValidationError:
        raise IncompleteInspection("TYPO_RESPONSE_INVALID") from None
    batch = batch.model_copy(update={"suggestions": _anchored(batch.suggestions, segment, text, aliases, stats)})
    probe = ClassifiedTypoCheckResult(target_hash="", status="COMPLETED", segments=batch.segments,
                                      suggestions=batch.suggestions, failure_code=None)
    try:
        probe.require_coverage([segment.ref])
    except ValueError:
        raise IncompleteInspection("TYPO_COVERAGE_INVALID") from None
    if (batch.segments[0].status == "PLANNED") != bool(batch.suggestions):
        raise IncompleteInspection("TYPO_COVERAGE_INVALID")
    ordered = sorted(batch.suggestions, key=lambda suggestion: suggestion.span.start)
    for index, suggestion in enumerate(ordered):
        span = suggestion.span
        try:
            validate_span(span, text)
        except ValueError:
            raise IncompleteInspection("INVALID_EXACT_SPAN") from None
        if not segment.start <= span.start < segment.end or span.end > segment.context_end:
            raise IncompleteInspection("TYPO_SPAN_OUTSIDE_SEGMENT")
        if not suggestion.reason.strip() or suggestion.replacement == span.quote:
            raise IncompleteInspection("TYPO_SUGGESTION_INVALID")
        if (index and ordered[index - 1].span.end > span.start
                or any(previous.span.start < span.end and previous.span.end > span.start
                       for previous in accepted)):
            raise IncompleteInspection("TYPO_SUGGESTIONS_OVERLAP")
    return batch.segments[0], ordered


async def _inspect(llm, state, prompt):
    request = {"system_prompt": _PROMPT, "user_prompt": prompt, "model": _MODEL,
               "response_schema": _SCHEMA, "call_options": _OPTIONS}
    if state.model_calls >= state.limits.model_calls:
        raise IncompleteInspection("BUDGET_EXHAUSTED")
    try:
        count = await _call(state, llm.count_input_tokens, **request)
        if type(count) is not int or count < 0:
            raise IncompleteInspection("INPUT_TOKEN_COUNT_FAILED")
        if count > state.limits.input_tokens:
            raise IncompleteInspection("INPUT_BUDGET_EXCEEDED")
        return await _call(state, llm.create_text_response, **request, kind="model",
                           max_output_tokens=state.limits.output_tokens)
    except LlmInputTokenCountError:
        raise IncompleteInspection("INPUT_TOKEN_COUNT_FAILED") from None
    except LlmOutputTruncatedError:
        raise IncompleteInspection("LLM_OUTPUT_TRUNCATED") from None
    except LlmIncompleteResponseError:
        raise IncompleteInspection("LLM_INCOMPLETE_RESPONSE") from None
    except LlmResponseValidationError:
        raise IncompleteInspection("TYPO_RESPONSE_INVALID") from None
    except httpx.HTTPStatusError as exc:
        code = "LLM_PROVIDER_REJECTED" if exc.response.status_code < 500 else "LLM_PROVIDER_ERROR"
        raise IncompleteInspection(code) from None
    except httpx.HTTPError:
        raise IncompleteInspection("LLM_PROVIDER_ERROR") from None


async def check_typos(
    data: ValidationDataClient, llm: OpenAIResponsesClient | MeteredTextGenerationClient,
    *, budget: InvocationBudget, checkpoint=None,
) -> ClassifiedTypoCheckResult:
    """Return suggestions only; orchestration, billing and persistence are caller-owned."""
    state = BudgetState(budget)
    try:
        # Closed injection boundary: unknown/nested wrappers may hide retries or double metering.
        if not (type(llm) is OpenAIResponsesClient or (
            type(llm) is MeteredTextGenerationClient and llm.max_retries == 0
            and type(llm.delegate) is OpenAIResponsesClient
        )):
            raise IncompleteInspection("LLM_CLIENT_CONFIGURATION_INVALID")
        frozen = await _call(state, data.read_input, kind="lookup")
        if checkpoint:
            checkpoint.bind_sources(frozen)
        source = await _call(state, data.read_source, frozen.target, kind="lookup")
    except IncompleteInspection as exc:
        return ClassifiedTypoCheckResult(target_hash=data.context.target_hash, status="INCOMPLETE",
                                         segments=[], suggestions=[], failure_code=exc.code)
    segments = split_segments(source.text, core_chars=budget.segment_chars,
                              context_chars=budget.context_chars)
    check_input = TypoCheckInput(target_hash=frozen.context.target_hash, spans=[
        Span(source_ref="T1", start=segment.start, end=segment.end,
             quote=source.text[segment.start:segment.end]) for segment in segments
    ])
    coverage = [SegmentCoverage(segment_ref=segment.ref, status="UNPROCESSED", reason="NOT_PROCESSED")
                for segment in segments]
    suggestions, failure_code = [], None
    aliases, stats = frozenset(segment.ref for segment in segments), Counter()
    for index, (segment, core) in enumerate(zip(segments, check_input.spans, strict=True)):
        context = Span(source_ref="T1", start=segment.context_start, end=segment.context_end,
                       quote=source.text[segment.context_start:segment.context_end])
        prompt = json.dumps({"segment_ref": segment.ref, "core": core.model_dump(),
                             "context": context.model_dump()}, ensure_ascii=False)
        try:
            saved = checkpoint.stage("TYPO_SEGMENT", segment.ref) if checkpoint else None
            if saved is None:
                response = await _inspect(llm, state, prompt)
            else:
                from app.llm.responses import LlmTextResponse
                response = LlmTextResponse(text=json.dumps({"segments": saved["segments"],
                    "suggestions": saved["suggestions"]}), input_token_count=0, output_token_count=0)
            checked, found = _validated_batch(response, segment, source.text, suggestions, aliases, stats)
            if checkpoint and saved is None:
                await checkpoint.typo(segment.ref, {"segments": [checked.model_dump()],
                    "suggestions": [row.model_dump() for row in found], "failure_code": None})
            coverage[index] = checked
            suggestions.extend(found)
        except IncompleteInspection as exc:
            failure_code = failure_code or exc.code
            coverage[index] = SegmentCoverage(segment_ref=segment.ref, status="UNPROCESSED",
                                              reason=exc.code)
            if exc.code in {"BUDGET_EXHAUSTED", "LLM_PROVIDER_REJECTED", "UNEXPECTED_ERROR"}:
                for pending in range(index + 1, len(segments)):
                    coverage[pending] = SegmentCoverage(segment_ref=segments[pending].ref,
                                                       status="UNPROCESSED", reason=exc.code)
                break
    try:
        await _call(state, data.validate, kind="lookup")
        state.remaining()
    except IncompleteInspection as exc:
        # A prior success is not valid after source loss/change or failed revalidation.
        failure_code, suggestions = exc.code, []
        coverage = [SegmentCoverage(segment_ref=segment.ref, status="UNPROCESSED", reason=exc.code)
                    for segment in segments]
    result = ClassifiedTypoCheckResult(
        target_hash=check_input.target_hash, status="INCOMPLETE" if failure_code else "COMPLETED",
        segments=coverage, suggestions=suggestions, failure_code=failure_code,
    )
    result.require_coverage([segment.ref for segment in segments])
    if failure_code:
        logger.warning("typo inspection incomplete code=%s normalizations=%s", failure_code, dict(sorted(stats.items())))
    else:
        logger.info("typo inspection completed suggestions=%d normalizations=%s", len(suggestions),
                    dict(sorted(stats.items())))
    return result
