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
    """A Worker-issued read; v3 judges only ask for VECTOR searches and target context (grid v4)."""

    ref: str
    kind: Literal["COMPARISON_CONTEXT", "CHANGES_EXCEPTIONS", "NEW_CLUE"]
    operation: Literal["BASELINE", "TARGET_CONTEXT", "VECTOR"]
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
    reason: str = Field(min_length=1)


class SegmentCoverage(Contract):
    segment_ref: str
    status: Literal["PLANNED", "NO_CHECK", "UNPROCESSED"]
    reason: str = Field(min_length=1)


class QuestionBatch(Contract):
    questions: list[QuestionDraft]
    segments: list[SegmentCoverage]


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


class BasisQuote(Contract):
    """One exact basis sentence of a decision (the comparison side, never T1).

    An E quote is an absolute span of its evidence source (`source_ref`/`start`/`end` in code points) and is
    published as a citation; a B quote is a substring of a stored setting value, so it has no source position.
    """

    ref: str
    source_ref: str | None
    start: int | None
    end: int | None
    quote: str = Field(min_length=1)


_OUTCOME = Literal["CONFLICT", "SUSPECTED_CONFLICT", "NEEDS_CONTEXT", "NO_CONFLICT"]
_BASIS = Literal["DIRECT_CONTRADICTION", "CHANGE_NOT_FOUND", "AMBIGUOUS_MEANING", "COMPATIBLE", "DB_RAW_DISAGREEMENT"]
_IMPACT = Literal["SEVERE", "CAUTION", "REFERENCE"]


class ValidationDecision(Contract):
    """The published unit shape (unchanged); v3 always publishes empty follow_ups/affected_question_refs."""

    action: Literal["FOLLOW_UP", "DECIDED"]
    outcome: _OUTCOME | None
    basis: _BASIS | None
    reason: str = Field(min_length=1)
    impact: _IMPACT | None
    impact_reason: str | None
    target_spans: list[Span]
    evidence_refs: list[str]
    completion: CompletionAssessment | None
    follow_ups: list[LookupIntent]
    affected_question_refs: list[str]
    disagreements: list[Disagreement]
    # Exact basis sentences checked by the Worker (absent in decisions published before this field).
    basis_quotes: list[BasisQuote] = Field(default_factory=list)
    # The judge-written finding title after the Worker's author-text check (absent before 2026-11-03, read as None).
    headline: str | None = None


# Judge answers (no docstrings: they would become schema descriptions).
class ContextRequest(Contract):
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)


class BasisQuoteDraft(Contract):
    ref: str
    quote: str = Field(min_length=1)


class JudgeOpenAnswer(Contract):
    action: Literal["FOLLOW_UP", "DECIDED"]
    outcome: _OUTCOME | None
    basis: _BASIS | None
    # Required (nullable) by the strict schema; the default only tolerates older scripted answers.
    headline: str | None = None
    reason: str = Field(min_length=1)
    impact: _IMPACT | None
    impact_reason: str | None
    target_spans: list[Span]
    evidence_refs: list[str]
    completion: CompletionAssessment | None
    disagreements: list[Disagreement]
    # Required by the strict schema; the default only tolerates older scripted answers.
    basis_quotes: list[BasisQuoteDraft] = Field(default_factory=list)
    # Absent from the schema when no search can run; the Worker treats a missing list as empty.
    search_queries: list[str] = Field(default_factory=list)
    context_requests: list[ContextRequest]


class JudgeFinalAnswer(Contract):
    action: Literal["DECIDED"]
    outcome: _OUTCOME
    basis: _BASIS
    headline: str | None = None
    reason: str = Field(min_length=1)
    impact: _IMPACT | None
    impact_reason: str | None
    target_spans: list[Span]
    evidence_refs: list[str]
    completion: CompletionAssessment
    disagreements: list[Disagreement]
    basis_quotes: list[BasisQuoteDraft] = Field(default_factory=list)


def decision_from_answer(answer):
    """The published ValidationDecision of a DECIDED judge answer (no follow-ups, no re-opened questions).

    `basis_quotes` stays empty here: the judge fills it from the answer after the Worker checked each quote.
    `headline` is copied as answered; the author-text check (`author_text.sanitize_decision`) normalizes it.
    """
    return ValidationDecision(
        action="DECIDED", outcome=answer.outcome, basis=answer.basis, reason=answer.reason, headline=answer.headline,
        impact=answer.impact, impact_reason=answer.impact_reason, target_spans=list(answer.target_spans),
        evidence_refs=list(answer.evidence_refs), completion=answer.completion, follow_ups=[],
        affected_question_refs=[], disagreements=list(answer.disagreements))


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
    """TYPO's fixed split (TYPO grid v1); SETTING uses `split_setting_segments` (grid v4)."""
    if type(core_chars) is not int or core_chars < 1 or type(context_chars) is not int or context_chars < 0:
        raise ValueError("Invalid segment budget")
    return tuple(Segment(f"TSEG{n + 1}", start, min(len(text), start + core_chars),
                         max(0, start - context_chars), min(len(text), start + core_chars + context_chars))
                 for n, start in enumerate(range(0, len(text), core_chars)))


def setting_cuts(text, unit_chars):
    """SETTING grid v4 cut points (normative, identical in Java; integer arithmetic on code points only).

    n = ceil(L/U) segments of equal target length; cut k is the position right after a U+000A in the window
    ((2k-1)L/2n, (2k+1)L/2n] nearest to t = kL/n (tie: the smaller position), else max(t, lo+1). The windows
    never overlap and hi > lo because L >= n, so cuts strictly increase and no segment is empty.
    """
    length = len(text)
    n = (length + unit_chars - 1) // unit_chars
    cuts = []
    for k in range(1, n):
        t = (k * length) // n
        lo = ((2 * k - 1) * length) // (2 * n)
        hi = ((2 * k + 1) * length) // (2 * n)
        best = None
        for p in range(lo + 1, hi + 1):
            if text[p - 1] == "\n" and (best is None or abs(p - t) < abs(best - t)):
                best = p  # ascending p: an equal distance keeps the smaller position
        cuts.append(best if best is not None else max(t, lo + 1))
    return cuts


def split_setting_segments(text, *, unit_chars, context_chars):
    """SETTING grid v4: TSEGn cores of equal length cut after newlines, each with read-only context."""
    if type(unit_chars) is not int or unit_chars < 1 or type(context_chars) is not int or context_chars < 0:
        raise ValueError("Invalid segment budget")
    length = len(text)
    if not length:
        return ()
    bounds = [0, *setting_cuts(text, unit_chars), length]
    return tuple(Segment(f"TSEG{i}", bounds[i - 1], bounds[i], max(0, bounds[i - 1] - context_chars),
                         min(length, bounds[i] + context_chars))
                 for i in range(1, len(bounds)))


def segment_wire(segment):
    """The server grid's segment row (`{"ref","start","end","contextStart","contextEnd"}`)."""
    return {"ref": segment.ref, "start": segment.start, "end": segment.end,
            "contextStart": segment.context_start, "contextEnd": segment.context_end}


TARGET_REF = "T1"
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,99}")
SEARCH_QUERY_LIMIT = 1000
MAX_SEARCH_QUERIES = 3
# A decision's exact basis sentences (`basis_quotes`): at most 3, each at most 300 code points.
MAX_BASIS_QUOTES = 3
BASIS_QUOTE_LIMIT = 300
# A published headline has at most 80 code points (the prompt asks for about 40); longer ones are dropped to None.
HEADLINE_LIMIT = 80


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


def normalize_batch(batch, target, *, aliases, accept=None, stats=None):
    """Normalize every question span of one planner batch; duplicate spans collapse."""
    questions = []
    for question in batch.questions:
        spans = list(dict.fromkeys(anchor_span(span, target, aliases=aliases, accept=accept, stats=stats)
                                   for span in question.target_spans))
        if spans != question.target_spans:
            question = question.model_copy(update={"target_spans": spans})
        questions.append(question)
    return batch.model_copy(update={"questions": questions})


def validate_context_request(start, end, target_length):
    """A judge's TARGET_CONTEXT request: an exact non-empty [start,end) range of T1."""
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= target_length:
        raise ValueError("INVALID_TARGET_CONTEXT_REQUEST")
    return start, end


def validate_search_query(query):
    """A judge's natural-language VECTOR search sentence: 1 to SEARCH_QUERY_LIMIT characters, not blank."""
    if not isinstance(query, str) or not query.strip() or len(query) > SEARCH_QUERY_LIMIT:
        raise ValueError("INVALID_SEARCH_REQUEST")
    return query


def validate_batch(batch, segment, subjects, target):
    """One PLANNER batch of one segment: spans inside the core (context is read-only), PLANNED iff questions."""
    if len(batch.segments) != 1 or batch.segments[0].segment_ref != segment.ref:
        raise ValueError("SEGMENT_COVERAGE_MISSING")
    status = batch.segments[0].status
    if status == "UNPROCESSED":
        raise ValueError("SEGMENT_COVERAGE_UNPROCESSED")
    if len({q.local_ref for q in batch.questions}) != len(batch.questions):
        raise ValueError("DUPLICATE_QUESTION")
    for question in batch.questions:
        if not set(question.subject_refs) <= set(subjects):
            raise ValueError("UNKNOWN_SUBJECT_REFERENCE")
        for span in question.target_spans:
            validate_span(span, target)
            if not segment.start <= span.start < span.end <= segment.end:
                raise ValueError("SPAN_OUTSIDE_SEGMENT")
    if (status == "PLANNED") != bool(batch.questions):
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
            combined = previous.draft.model_copy(update={"target_spans": spans})
            merged[index] = MergedQuestion(previous.ref, combined, previous.original_refs + (original,))
    return tuple(merged)


class IncompleteInspection(RuntimeError):
    """`code` is the stable category; optional `cause` is the specific safe rule code (GH331).

    `stage` (`PLANNER:TSEG1`, `JUDGE:Q3`) is attached where the failure happened, because planner and judge
    calls run concurrently and one shared "current stage" would be wrong.
    """

    def __init__(self, code, cause=None, *, stage=None):
        self.code = code
        self.cause = safe_code(cause)
        self.stage = stage
        super().__init__(code)


@dataclass(frozen=True)
class InvocationBudget:
    """Caller-supplied per-attempt execution limits of one branch; per-role output caps live in RoleRuntime."""

    model_calls: int
    lookups: int
    seconds: float
    input_tokens: int
    segment_chars: int
    context_chars: int
    search_limit: int

    def __post_init__(self):
        for name in ("model_calls", "lookups", "input_tokens", "segment_chars", "search_limit"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Invalid invocation budget")
        if (type(self.context_chars) is not int or self.context_chars < 0
                or isinstance(self.seconds, bool) or not isinstance(self.seconds, (int, float))
                or not math.isfinite(self.seconds) or self.seconds <= 0):
            raise ValueError("Invalid invocation budget")
        if self.search_limit > 100:
            raise ValueError("Search transport limit is 100")


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
    # Experiment mode: question (or planner segment) -> {code, cause, call}; the run continued past them.
    question_failures: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per question judge flow (calls, forms, searches, cache hits); diagnostics only, never published or logged.
    # A finding the rule guard cleared also keeps the judge's original impact and reason for review (experiment).
    traces: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
