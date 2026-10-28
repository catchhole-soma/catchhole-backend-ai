"""Validation-owned contracts; model references never grant source/work access."""

from dataclasses import dataclass, field
import math
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Span(Contract):
    source_ref: str
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1)


class LookupIntent(Contract):
    ref: str
    kind: Literal["COMPARISON_CONTEXT", "CHANGES_EXCEPTIONS", "NEW_CLUE"]
    operation: Literal["BASELINE", "TARGET_CONTEXT", "DIRECT", "KEYWORD", "VECTOR", "NEIGHBORS"]
    purpose: str = Field(min_length=1)
    query: str | None = None
    source_ref: str | None = None
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, gt=0, strict=True)
    evidence_refs: list[str] = Field(default_factory=list)


class QuestionDraft(Contract):
    local_ref: str = Field(min_length=1)
    target_spans: list[Span] = Field(min_length=1)
    subject_refs: list[str]
    subject_mentions: list[str]
    property_or_rule: str = Field(min_length=1)
    time_context: str = Field(min_length=1)
    conditions: list[str]
    assertion_mode: Literal["NARRATION", "DIALOGUE", "THOUGHT", "HYPOTHETICAL", "UNKNOWN"]
    check_intent: str = Field(min_length=1)
    lookup_intents: list[LookupIntent]
    reason: str = Field(min_length=1)


class SegmentCoverage(Contract):
    segment_ref: str
    status: Literal["PLANNED", "NO_CHECK", "UNPROCESSED"]
    reason: str = Field(min_length=1)


class RuleCoverage(Contract):
    entry_ref: str
    status: Literal["CHECKED", "NOT_APPLICABLE", "UNPROCESSED"]
    reason: str = Field(min_length=1)


class QuestionBatch(Contract):
    questions: list[QuestionDraft]
    segments: list[SegmentCoverage]
    rules: list[RuleCoverage]
    clue_checks: list[RuleCoverage] = Field(default_factory=list)


class EntrySelection(Contract):
    entry_ref: str
    selected: bool = Field(strict=True)
    reason: str = Field(min_length=1)


class RelatedSelection(Contract):
    entries: list[EntrySelection]


class Assessment(Contract):
    reason: str = Field(min_length=1)
    evidence_refs: list[str]
    receipt_refs: list[str]


class CompletionAssessment(Contract):
    comparison_context: Assessment
    changes_exceptions: Assessment
    no_pending_clues: Assessment
    required_reads: Assessment


class Disagreement(Contract):
    fact_ref: str
    evidence_ref: str
    reason: str = Field(min_length=1)
    correction_ref: str | None = None


class ValidationDecision(Contract):
    action: Literal["FOLLOW_UP", "DECIDED"]
    outcome: Literal["CONFLICT", "SUSPECTED_CONFLICT", "NEEDS_CONTEXT", "NO_CONFLICT"] | None
    basis: Literal["DIRECT_CONTRADICTION", "CHANGE_NOT_FOUND", "AMBIGUOUS_MEANING",
                   "COMPATIBLE", "DB_RAW_DISAGREEMENT"] | None
    reason: str = Field(min_length=1)
    impact: Literal["SEVERE", "CAUTION", "REFERENCE"] | None
    impact_reason: str | None
    target_spans: list[Span]
    evidence_refs: list[str]
    completion: CompletionAssessment | None
    follow_ups: list[LookupIntent]
    affected_question_refs: list[str]
    disagreements: list[Disagreement]


class TypoCheckInput(Contract):
    """GH309 reuses the frozen target and coverage contract; no checker is wired here."""

    target_hash: str
    spans: list[Span]


class TypoSuggestion(Contract):
    span: Span
    replacement: str
    reason: str


class TypoCheckResult(Contract):
    target_hash: str
    status: Literal["COMPLETED", "INCOMPLETE"]
    segments: list[SegmentCoverage]
    suggestions: list[TypoSuggestion]
    failure_code: str | None

    def require_coverage(self, expected_refs):
        if (len(self.segments) != len(expected_refs)
                or {segment.segment_ref for segment in self.segments} != set(expected_refs)
                or self.status == "COMPLETED" and (self.failure_code is not None
                    or any(segment.status == "UNPROCESSED" for segment in self.segments))):
            raise ValueError("TYPO_COVERAGE_INCOMPLETE")


@dataclass(frozen=True)
class Segment:
    ref: str
    start: int
    end: int
    context_start: int
    context_end: int


def split_segments(text, *, core_chars, context_chars):
    if type(core_chars) is not int or core_chars < 1 or type(context_chars) is not int or context_chars < 0:
        raise ValueError("Invalid segment budget")
    return tuple(Segment(f"TSEG{n + 1}", start, min(len(text), start + core_chars),
                         max(0, start - context_chars), min(len(text), start + core_chars + context_chars))
                 for n, start in enumerate(range(0, len(text), core_chars)))


def validate_span(span, text, *, source_ref="T1"):
    if span.source_ref != source_ref or not 0 <= span.start < span.end <= len(text) or text[span.start:span.end] != span.quote:
        raise ValueError("INVALID_EXACT_SPAN")
    return True


def validate_batch(batch, segments, subjects, entries, target):
    expected = {s.ref for s in segments}
    if len(batch.segments) != len(expected) or {s.segment_ref for s in batch.segments} != expected:
        raise ValueError("SEGMENT_COVERAGE_MISSING")
    if any(s.status == "UNPROCESSED" for s in batch.segments):
        raise ValueError("SEGMENT_COVERAGE_UNPROCESSED")
    if len(batch.rules) != len(entries) or {r.entry_ref for r in batch.rules} != set(entries):
        raise ValueError("RULE_COVERAGE_MISSING")
    if any(r.status == "UNPROCESSED" for r in batch.rules):
        raise ValueError("RULE_COVERAGE_UNPROCESSED")
    if len({q.local_ref for q in batch.questions}) != len(batch.questions):
        raise ValueError("DUPLICATE_QUESTION")
    for question in batch.questions:
        if not set(question.subject_refs) <= set(subjects):
            raise ValueError("UNKNOWN_SUBJECT_REFERENCE")
        for span in question.target_spans:
            validate_span(span, target)
            if not any(s.context_start <= span.start < span.end <= s.context_end for s in segments):
                raise ValueError("SPAN_OUTSIDE_SEGMENT")
    for segment in segments:
        has_question = any(any(span.start < segment.end and span.end > segment.start
                               for span in q.target_spans) for q in batch.questions)
        status = next(s.status for s in batch.segments if s.segment_ref == segment.ref)
        if (status == "PLANNED") != has_question:
            raise ValueError("SEGMENT_QUESTION_COVERAGE_MISMATCH")


@dataclass(frozen=True)
class MergedQuestion:
    ref: str
    draft: QuestionDraft
    original_refs: tuple[str, ...]


def merge_questions(drafts):
    merged = []
    keys = {}
    for draft, origin in drafts:
        key = (tuple(sorted(draft.subject_refs)), draft.property_or_rule, draft.time_context,
               tuple(sorted(draft.conditions)), draft.assertion_mode)
        # Unresolved identities need an actual identity proof; textual resemblance is insufficient.
        index = keys.get(key) if draft.subject_refs else None
        original = f"{origin}:{draft.local_ref}"
        if index is None:
            index = len(merged)
            keys[key] = index
            merged.append(MergedQuestion(f"Q{index + 1}", draft, (original,)))
        else:
            previous = merged[index]
            spans = list(dict.fromkeys(previous.draft.target_spans + draft.target_spans))
            intents = list(previous.draft.lookup_intents)
            intents.extend(intent for intent in draft.lookup_intents if intent not in intents)
            combined = previous.draft.model_copy(update={"target_spans": spans, "lookup_intents": intents})
            merged[index] = MergedQuestion(previous.ref, combined, previous.original_refs + (original,))
    return tuple(merged)


class IncompleteInspection(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InvocationBudget:
    """Caller-supplied execution limits; no production quality/cost limits are implied."""

    model_calls: int
    lookups: int
    seconds: float
    input_tokens: int
    output_tokens: int
    segment_chars: int
    context_chars: int
    catalogue_page_size: int
    search_limit: int

    def __post_init__(self):
        for name in ("model_calls", "lookups", "input_tokens", "output_tokens", "segment_chars",
                     "catalogue_page_size", "search_limit"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Invalid invocation budget")
        if type(self.context_chars) is not int or self.context_chars < 0 or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("Invalid invocation budget")
        if self.search_limit > 100 or self.catalogue_page_size > 100:
            raise ValueError("Page/search transport limit is 100")


@dataclass
class BudgetState:
    limits: InvocationBudget
    started: float = field(default_factory=monotonic)
    model_calls: int = 0
    lookups: int = 0

    def remaining(self):
        seconds = self.limits.seconds - (monotonic() - self.started)
        if seconds <= 0:
            raise IncompleteInspection("BUDGET_EXHAUSTED")
        return seconds

    def consume(self, kind):
        self.remaining()
        field_name = "model_calls" if kind == "model" else "lookups"
        if getattr(self, field_name) >= getattr(self.limits, field_name):
            raise IncompleteInspection("BUDGET_EXHAUSTED")
        setattr(self, field_name, getattr(self, field_name) + 1)


@dataclass(frozen=True)
class LookupReceipt:
    ref: str
    intent: LookupIntent
    status: str
    evidence_refs: tuple[str, ...] = ()
    limited: bool = False
    failure_code: str | None = None
    reused_from: str | None = None
    readiness: dict[str, Any] | None = None


@dataclass(frozen=True)
class InspectionResult:
    status: Literal["COMPLETED", "INCOMPLETE"]
    questions: tuple[MergedQuestion, ...] = ()
    decisions: dict[str, ValidationDecision] = field(default_factory=dict)
    receipts: tuple[LookupReceipt, ...] = ()
    coverage: tuple[dict[str, Any], ...] = ()
    failure_code: str | None = None
    evidence_bundles: dict[str, dict[str, Any]] = field(default_factory=dict)
