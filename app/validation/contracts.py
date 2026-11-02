"""Validation-owned contracts; model references never grant source/work access."""

import math
import re
from dataclasses import dataclass, field
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


# GH349 SUBJECT_RESOLUTION stage payload (no docstrings: they would become schema descriptions).
# One non-literal mention in the segment text and the catalogue subject it refers to, or null.
class MentionResolution(Contract):
    mention: str = Field(min_length=1)
    subject_ref: str | None
    reason: str = Field(min_length=1)


class SubjectResolution(Contract):
    mentions: list[MentionResolution]


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


TARGET_REF = "T1"
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,99}")


def safe_code(value, fallback=None):
    """Return a Backend-safe fixed code (`[A-Z][A-Z0-9_]{0,99}`); never free text."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else fallback


def validate_span(span, text, *, source_ref=TARGET_REF):
    if span.source_ref != source_ref or not 0 <= span.start < span.end <= len(text) or text[span.start:span.end] != span.quote:
        raise ValueError("INVALID_EXACT_SPAN")
    return True


def _occurrences(text, quote):
    index = text.find(quote)
    while index >= 0:
        yield index
        index = text.find(quote, index + 1)


def anchor_span(span, text, *, aliases=frozenset(), accept=None, stats=None):
    """Re-anchor one model span on the frozen target before the strict exact check (GH331).

    A known target segment alias (`TSEGn`) names the same frozen target, so it becomes `T1`;
    any other reference is left for `validate_span` to reject. Offsets are recomputed only from
    the model's own quote: an exact span is kept as is, otherwise the nearest exact occurrence
    (within `accept`, if given) to the model start wins. An absent quote or an equidistant tie
    stays unchanged and is therefore rejected. Quotes are never edited or invented.
    """
    updates = {}
    if span.source_ref != TARGET_REF and span.source_ref in aliases:
        updates["source_ref"] = TARGET_REF
    if not (0 <= span.start < span.end <= len(text) and text[span.start:span.end] == span.quote):
        length = len(span.quote)
        hits = [hit for hit in _occurrences(text, span.quote) if accept is None or accept(hit, hit + length)]
        if hits:
            distance = min(abs(hit - span.start) for hit in hits)
            nearest = [hit for hit in hits if abs(hit - span.start) == distance]
            if len(nearest) == 1:
                updates.update(start=nearest[0], end=nearest[0] + length)
    if not updates:
        return span
    if stats is not None:
        if "source_ref" in updates:
            stats["TARGET_REF_ALIAS"] += 1
        if "start" in updates:
            stats["SPAN_REANCHORED"] += 1
    return span.model_copy(update=updates)


def normalize_intent(intent, aliases, stats=None):
    """TARGET_CONTEXT may name the target by a segment alias; every other ref stays as given."""
    if intent.operation == "TARGET_CONTEXT" and intent.source_ref != TARGET_REF and intent.source_ref in aliases:
        if stats is not None:
            stats["TARGET_REF_ALIAS"] += 1
        return intent.model_copy(update={"source_ref": TARGET_REF})
    return intent


def normalize_batch(batch, target, *, aliases, accept=None, stats=None):
    """Normalize every question span/intent of one planner/coverage batch; duplicates collapse."""
    questions = []
    for question in batch.questions:
        spans = list(dict.fromkeys(anchor_span(span, target, aliases=aliases, accept=accept, stats=stats)
                                   for span in question.target_spans))
        intents = [normalize_intent(intent, aliases, stats) for intent in question.lookup_intents]
        if spans != question.target_spans or intents != question.lookup_intents:
            question = question.model_copy(update={"target_spans": spans, "lookup_intents": intents})
        questions.append(question)
    return batch.model_copy(update={"questions": questions})


@dataclass(frozen=True)
class IntentRefs:
    """Actually readable references for lookup intents at the time a plan/follow-up is accepted."""

    entry_refs: frozenset = frozenset()
    source_refs: frozenset = frozenset()
    evidence_refs: frozenset = frozenset()
    target_length: int = 0
    # VECTOR is offered only when this Worker can execute it (frozen RAG configured or no episodes).
    search_operations: frozenset = frozenset({"KEYWORD", "VECTOR"})


def validate_intents(intents, refs):
    """Reject unreadable intents before they become durable obligations (GH331 S6)."""
    for intent in intents:
        operation = intent.operation
        if operation == "BASELINE" and not set(intent.evidence_refs) <= refs.entry_refs:
            raise ValueError("UNKNOWN_BASELINE_REFERENCE")
        if operation == "DIRECT" and intent.source_ref not in refs.source_refs:
            raise ValueError("UNKNOWN_SOURCE_REFERENCE")
        if operation == "NEIGHBORS" and (not intent.evidence_refs
                                         or not set(intent.evidence_refs) <= refs.evidence_refs):
            raise ValueError("UNKNOWN_EVIDENCE_REFERENCE")
        if operation == "TARGET_CONTEXT":
            start = 0 if intent.start is None else intent.start
            end = refs.target_length if intent.end is None else intent.end
            if intent.source_ref not in {None, TARGET_REF} or not 0 <= start < end <= refs.target_length:
                raise ValueError("INVALID_TARGET_CONTEXT_REQUEST")
        if operation in {"KEYWORD", "VECTOR"} and (not intent.query or not intent.query.strip()
                                                    or len(intent.query) > 1000):
            raise ValueError("INVALID_SEARCH_REQUEST")
        if operation in {"KEYWORD", "VECTOR"} and operation not in refs.search_operations:
            raise ValueError("VECTOR_NOT_READY")


def validate_batch(batch, segments, subjects, entries, target, *, intent_refs=None):
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
        if intent_refs is not None:
            validate_intents(question.lookup_intents, intent_refs)
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
    """`code` is the stable category; optional `cause` is the specific safe rule code (GH331)."""

    def __init__(self, code, cause=None):
        self.code = code
        self.cause = safe_code(cause)
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
    # GH331 diagnostics: the specific safe cause behind failure_code, the stage it happened in,
    # and how often deterministic normalization fired. Never raw text, messages or IDs.
    failure_cause: str | None = None
    failure_stage: str | None = None
    normalizations: dict[str, int] = field(default_factory=dict)
