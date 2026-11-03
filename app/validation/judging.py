"""SETTING judge state machine (grid v4): DB current values first, RAG only when the DB cannot decide.

Per question, at most MAX_JUDGE_CALLS calls:

1. OPEN: the draft, ±JUDGE_TARGET_CONTEXT_CHARS of target context and compact current rows of the question's
   subjects. The answer is DECIDED, or FOLLOW_UP with up to MAX_SEARCH_QUERIES natural-language VECTOR searches
   and/or T1 context ranges.
2. After a FOLLOW_UP the Worker runs those reads and asks again in FINAL form (DECIDED only).
3. A DECIDED SUSPECTED_CONFLICT from a non-final-limit call without a change search gets one Worker VECTOR
   change search, then the next call is FINAL. A third call is only reachable that way, so a final
   SUSPECTED_CONFLICT always carries that receipt.
"""

import asyncio
import json
from dataclasses import asdict
from hashlib import sha256

from app.validation.contracts import (
    MAX_SEARCH_QUERIES,
    TARGET_REF,
    IncompleteInspection,
    JudgeFinalAnswer,
    JudgeOpenAnswer,
    LookupIntent,
    anchor_span,
    decision_from_answer,
    validate_context_request,
    validate_search_query,
    validate_span,
)
from app.validation.data import MAX_SETTING_SUBJECTS
from app.validation.models import call_role
from app.validation.projection import bundle_settings, compact_setting_row, entry_order
from app.validation.schemas import JUDGE_FINAL_SCHEMA_NAME, JUDGE_OPEN_SCHEMA_NAME, judge_schema

# Deliberately separate from the planner's contextChars (1,000): the judge sees ±300 code points per span.
JUDGE_TARGET_CONTEXT_CHARS = 300
MAX_JUDGE_CALLS = 3
JUDGE_CONCURRENCY = 4
BASELINE_PURPOSE = "같은 대상·시점·조건의 고정 비교 자료 확인"
JUDGE_SEARCH_PURPOSE = "판정이 요청한 의미 검색"
JUDGE_CONTEXT_PURPOSE = "판정이 요청한 대상 원고 문맥"
# GH341: marks the single change search the Worker itself issues before accepting SUSPECTED_CONFLICT.
WORKER_CHANGE_SEARCH_PURPOSE = "Worker가 충돌 의심 판정 전에 요구하는 변화·예외 검색"
_SEARCH_QUERY_LIMIT = 1000
_DECISION_FIELDS = ("outcome", "basis", "impact", "impact_reason", "completion")


def is_change_search(receipt):
    """A successful CHANGES_EXCEPTIONS VECTOR read: what SUSPECTED_CONFLICT/CHANGE_NOT_FOUND needs."""
    return (receipt.status == "SUCCEEDED" and receipt.intent.kind == "CHANGES_EXCEPTIONS"
            and receipt.intent.operation == "VECTOR")


def change_search_query(draft):
    """Deterministic query: unique non-empty subject mentions, then the property; '' if none."""
    parts = dict.fromkeys(text.strip() for text in (*draft.subject_mentions, draft.property_or_rule))
    return " ".join(part for part in parts if part)[:_SEARCH_QUERY_LIMIT].strip()


def receipt_wire(receipt):
    return asdict(receipt) | {"intent": receipt.intent.model_dump()}


def _invalid(cause, code="INVALID_DECISION"):
    return IncompleteInspection(code, cause)


class SettingsCache:
    """Per-run subject -> current rows. SETTINGS reads are batched (<= 100 subjects) and de-duplicated in flight."""

    def __init__(self, fetch):
        self._fetch = fetch
        self._rows, self._pending = {}, {}

    async def rows(self, subject_refs):
        refs = list(dict.fromkeys(subject_refs))
        missing = [ref for ref in refs if ref not in self._rows and ref not in self._pending]
        for start in range(0, len(missing), MAX_SETTING_SUBJECTS):
            chunk = missing[start:start + MAX_SETTING_SUBJECTS]
            task = asyncio.ensure_future(self._read(chunk))
            for ref in chunk:
                self._pending[ref] = task
        waits = list(dict.fromkeys(self._pending[ref] for ref in refs if ref in self._pending))
        for task in waits:
            await asyncio.shield(task)
        return sorted((row for ref in refs for row in self._rows.get(ref, ())), key=lambda row: entry_order(row["entry_ref"]))

    async def _read(self, chunk):
        try:
            rows = await self._fetch(chunk)
            found = {ref: [] for ref in chunk}
            for row in rows:
                found[row["subject_ref"]].append(row)
            self._rows.update(found)
        finally:
            for ref in chunk:
                self._pending.pop(ref, None)


class SettingJudge:
    """Judges one question at a time; many run concurrently over one corpus, settings cache and budget."""

    def __init__(self, model, budget, runtime, *, corpus, settings, target, source_catalogue, segment_refs,
                 stats):
        self.model, self.budget, self.runtime = model, budget, runtime
        self.corpus, self.settings, self.target = corpus, settings, target
        self.source_catalogue, self.segment_refs, self.stats = source_catalogue, frozenset(segment_refs), stats
        self.search_offered = corpus.search_offered
        # One schema per form for the whole report keeps the system+schema prefix provider-cacheable.
        self.schemas = {
            "OPEN": judge_schema(entry_refs=(), final=False, search_available=self.search_offered),
            "FINAL": judge_schema(entry_refs=(), final=True, search_available=self.search_offered),
        }

    async def judge(self, question, revision, trace):
        """Return `(decision, bundle)`; `trace` receives one row per call (and the failure, if any)."""
        qref, draft = question.ref, question.draft
        rows = await self.settings.rows(draft.subject_refs)
        corpus = self.corpus
        own = [
            corpus.mint(LookupIntent(ref=f"{qref}-BASELINE", kind="COMPARISON_CONTEXT", operation="BASELINE",
                                     purpose=BASELINE_PURPOSE), "SUCCEEDED", [row["entry_ref"] for row in rows]),
            corpus.mint(LookupIntent(ref=f"{qref}-TARGET_CONTEXT", kind="COMPARISON_CONTEXT",
                                     operation="TARGET_CONTEXT", purpose=BASELINE_PURPOSE), "SUCCEEDED", [TARGET_REF]),
        ]
        extra_context, form, calls = [], "OPEN", 0
        while True:
            calls += 1
            bundle = self._bundle(question, revision, rows, own, extra_context)
            payload = {**bundle, "form": form, "settings": [compact_setting_row(row) for row in rows]}
            answer_type = JudgeOpenAnswer if form == "OPEN" else JudgeFinalAnswer
            row = {"call": calls, "form": form}
            trace.append(row)
            answer = await call_role(self.model, "JUDGE", payload, answer_type, self.budget, runtime=self.runtime,
                                     schema=self.schemas[form],
                                     schema_name=JUDGE_OPEN_SCHEMA_NAME if form == "OPEN" else JUDGE_FINAL_SCHEMA_NAME)
            answer = self._prepare(answer)
            row.update(action=answer.action, outcome=answer.outcome)
            if answer.action == "FOLLOW_UP":
                before = self.stats["RAG_CACHE_HITS"]
                row.update(await self._follow_up(qref, answer, own, extra_context))
                row["cache_hits"] = self.stats["RAG_CACHE_HITS"] - before
                form = "FINAL"
                continue
            if (answer.outcome == "SUSPECTED_CONFLICT" and calls < MAX_JUDGE_CALLS
                    and not any(is_change_search(receipt) for receipt in own)):
                query = change_search_query(draft)
                if not query:
                    raise _invalid("INVALID_DECISION_CHANGE_SEARCH_MISSING")
                before = self.stats["RAG_CACHE_HITS"]
                own.append(await corpus.vector(LookupIntent(
                    ref=f"{qref}-CHANGES_EXCEPTIONS", kind="CHANGES_EXCEPTIONS", operation="VECTOR",
                    purpose=WORKER_CHANGE_SEARCH_PURPOSE, query=query)))
                self.stats["CHANGE_SEARCH_INSERTED"] += 1
                row.update(change_search=True, cache_hits=self.stats["RAG_CACHE_HITS"] - before)
                form = "FINAL"
                continue
            evidence = self._evidence(own)
            decision = validate_decision(decision_from_answer(answer), question, rows, evidence, own, self.target,
                                         self.stats)
            row["decided"] = decision.outcome
            return decision, bundle

    def _prepare(self, answer):
        """Deterministic, meaning-preserving cleanup of one judge answer (GH331 S2/S4/S5)."""
        updates = {}
        spans = list(dict.fromkeys(anchor_span(span, self.target, aliases=self.segment_refs, stats=self.stats)
                                   for span in answer.target_spans))
        if spans != answer.target_spans:
            updates["target_spans"] = spans
        if answer.action == "FOLLOW_UP":
            # A follow-up is not a decision: its outcome/impact/completion are never stored or used.
            ignored = {name: None for name in _DECISION_FIELDS if getattr(answer, name) is not None}
            if ignored:
                self.stats["FOLLOW_UP_DECISION_FIELDS_IGNORED"] += 1
                updates.update(ignored)
        else:
            if getattr(answer, "search_queries", None) or getattr(answer, "context_requests", None):
                # A decision is final for this call: read requests attached to it are never executed.
                self.stats["DECIDED_REQUESTS_IGNORED"] += 1
                updates.update(search_queries=[], context_requests=[])
            if answer.outcome == "NO_CONFLICT" and (answer.impact is not None or answer.impact_reason is not None):
                # NO_CONFLICT is never a finding, so an impact grade has no meaning (Backend rejects it).
                self.stats["NO_CONFLICT_IMPACT_CLEARED"] += 1
                updates.update(impact=None, impact_reason=None)
        return answer.model_copy(update=updates) if updates else answer

    async def _follow_up(self, qref, answer, own, extra_context):
        queries, requests = list(answer.search_queries), list(answer.context_requests)
        if not queries and not requests:
            raise _invalid("INVALID_FOLLOW_UP_EMPTY", "INVALID_FOLLOW_UP")
        if len(queries) > MAX_SEARCH_QUERIES:
            raise _invalid("INVALID_FOLLOW_UP_TOO_MANY_SEARCHES", "INVALID_FOLLOW_UP")
        try:
            for query in queries:
                validate_search_query(query)
            ranges = list(dict.fromkeys(validate_context_request(row.start, row.end, len(self.target))
                                        for row in requests))
        except ValueError as exc:
            raise _invalid(exc.args[0], "INVALID_FOLLOW_UP") from None
        if queries and not self.search_offered:
            raise IncompleteInspection(*self.corpus.unavailable)
        for index, (start, end) in enumerate(ranges, 1):
            own.append(self.corpus.mint(LookupIntent(
                ref=f"{qref}-TARGET_CONTEXT-{index}", kind="COMPARISON_CONTEXT", operation="TARGET_CONTEXT",
                purpose=JUDGE_CONTEXT_PURPOSE, source_ref=TARGET_REF, start=start, end=end), "SUCCEEDED", [TARGET_REF]))
            extra_context.append({"source_ref": TARGET_REF, "start": start, "end": end,
                                  "text": self.target[start:end]})
        for index, query in enumerate(queries, 1):
            own.append(await self.corpus.vector(LookupIntent(
                ref=f"{qref}-VECTOR-{index}", kind="COMPARISON_CONTEXT", operation="VECTOR",
                purpose=JUDGE_SEARCH_PURPOSE, query=query)))
        self.stats["FOLLOW_UPS"] += 1
        return {"searches": len(queries), "context_requests": len(ranges)}

    def _evidence(self, own):
        refs = {ref for receipt in own for ref in receipt.evidence_refs}
        return [row for ref, row in self.corpus.evidence.items() if ref in refs]

    def _bundle(self, question, revision, rows, own, extra_context):
        draft, target = question.draft, self.target
        bundle = {
            "question_ref": question.ref,
            "check_revision": revision,
            "question": draft.model_dump(),
            "target_context": [
                {"start": max(0, span.start - JUDGE_TARGET_CONTEXT_CHARS),
                 "text": target[max(0, span.start - JUDGE_TARGET_CONTEXT_CHARS):
                                min(len(target), span.end + JUDGE_TARGET_CONTEXT_CHARS)]}
                for span in draft.target_spans],
            "additional_target_context": [dict(row) for row in extra_context],
            # GH349 Amendment 2: the published bundle carries refs only; the model reads compact rows.
            "settings": bundle_settings(rows),
            "evidence": self._evidence(own),
            "receipts": [receipt_wire(receipt) for receipt in own],
            "source_catalogue": self.source_catalogue,
        }
        # Different receipts/evidence invalidate earlier decisions, even on an unchanged manuscript.
        bundle["input_digest"] = sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return bundle


def validate_decision(value, question, settings, evidence, receipts, target, stats=None):
    """Worker mirror of the Backend's decision rules; deterministic fills are counted in `stats`."""
    def fail(cause):
        return _invalid("INVALID_DECISION_" + cause)

    if value.follow_ups or value.affected_question_refs:
        raise fail("HAS_FOLLOW_UPS")
    if value.completion is None:
        raise fail("COMPLETION_MISSING")
    if value.outcome is None or value.basis is None:
        raise fail("OUTCOME_MISSING")
    for span in value.target_spans:
        validate_span(span, target)  # an echoed span must still be real target text
    if set(value.target_spans) != set(question.draft.target_spans):
        # GH331: the judged spans are the question's own spans (Backend requires exactly those).
        value = value.model_copy(update={"target_spans": list(question.draft.target_spans)})
        if stats is not None:
            stats["TARGET_SPANS_REPLACED"] += 1
    facts = {row["entry_ref"]: row for row in settings}
    actual = {row["ref"]: row for row in evidence}
    known = {TARGET_REF} | set(facts) | set(actual)
    real_receipts = {receipt.ref: receipt for receipt in receipts}
    if not set(value.evidence_refs) <= known:
        raise fail("UNKNOWN_EVIDENCE_REFERENCE")
    reads = value.completion.required_reads
    if not set(reads.receipt_refs) <= set(real_receipts) or not set(reads.evidence_refs) <= known:
        raise fail("REQUIRED_READS")  # a cited receipt/evidence must really exist
    if set(reads.receipt_refs) != set(real_receipts) or not reads.evidence_refs:
        # GH331: "all required reads succeeded" is a Worker fact, not a model enumeration.
        filled = reads.model_copy(update={
            "receipt_refs": [receipt.ref for receipt in receipts],
            "evidence_refs": reads.evidence_refs or list(dict.fromkeys(
                [TARGET_REF, *(ref for receipt in receipts for ref in receipt.evidence_refs if ref in known)])),
        })
        value = value.model_copy(update={"completion": value.completion.model_copy(update={"required_reads": filled})})
        if stats is not None:
            stats["REQUIRED_READS_COMPLETED"] += 1
    changes = value.completion.changes_exceptions
    searches = [receipt for receipt in receipts if is_change_search(receipt)]
    if value.outcome == "SUSPECTED_CONFLICT" and searches and not any(
            ref in real_receipts and is_change_search(real_receipts[ref]) for ref in changes.receipt_refs):
        # GH341: "a successful change search exists" is a Worker fact, like required_reads.
        filled = changes.model_copy(update={
            "receipt_refs": list(dict.fromkeys([*changes.receipt_refs, *(row.ref for row in searches)])),
            "evidence_refs": list(dict.fromkeys([*changes.evidence_refs, *(
                ref for row in searches for ref in row.evidence_refs if ref in known)])) or [TARGET_REF],
        })
        value = value.model_copy(update={"completion": value.completion.model_copy(update={"changes_exceptions": filled})})
        if stats is not None:
            stats["CHANGE_SEARCH_RECEIPT_FILLED"] += 1
    comparison = value.completion.comparison_context
    if TARGET_REF not in comparison.evidence_refs:
        # Every judge call receives the T1 target context, so "T1 was compared" is a Worker fact like required_reads.
        filled = comparison.model_copy(update={"evidence_refs": [TARGET_REF, *comparison.evidence_refs]})
        value = value.model_copy(update={"completion": value.completion.model_copy(update={"comparison_context": filled})})
        if stats is not None:
            stats["COMPARISON_CONTEXT_TARGET_FILLED"] += 1
    for field_name in ("comparison_context", "changes_exceptions", "no_pending_clues", "required_reads"):
        assessment = getattr(value.completion, field_name)
        if (not assessment.evidence_refs or not set(assessment.evidence_refs) <= known
                or not assessment.receipt_refs or not set(assessment.receipt_refs) <= set(real_receipts)
                or any(real_receipts[ref].status != "SUCCEEDED" for ref in assessment.receipt_refs)):
            raise fail(field_name.upper())
    if not set(real_receipts) <= set(value.completion.required_reads.receipt_refs):
        raise fail("REQUIRED_READS")
    if TARGET_REF not in value.completion.comparison_context.evidence_refs:
        raise fail("COMPARISON_CONTEXT_TARGET")
    if value.outcome == "SUSPECTED_CONFLICT" and not any(
            is_change_search(real_receipts[ref]) for ref in value.completion.changes_exceptions.receipt_refs):
        raise fail("CHANGE_SEARCH_MISSING")  # CHANGE_NOT_FOUND needs an actual successful VECTOR change search.
    if value.basis == "DB_RAW_DISAGREEMENT" and not value.disagreements:
        raise fail("DISAGREEMENT_MISSING")
    if value.outcome != "NO_CONFLICT" and (not value.impact or not value.impact_reason):
        raise fail("IMPACT_MISSING")
    if value.outcome == "NO_CONFLICT" and (value.impact is not None or value.impact_reason is not None):
        raise fail("NO_CONFLICT_IMPACT")
    expected_basis = {"CONFLICT": "DIRECT_CONTRADICTION", "SUSPECTED_CONFLICT": "CHANGE_NOT_FOUND",
                      "NEEDS_CONTEXT": "AMBIGUOUS_MEANING", "NO_CONFLICT": "COMPATIBLE"}
    if value.basis not in {expected_basis[value.outcome], "DB_RAW_DISAGREEMENT"}:
        raise fail("BASIS_MISMATCH")
    if value.outcome in {"CONFLICT", "SUSPECTED_CONFLICT"} and not set(value.evidence_refs) & (set(facts) | set(actual)):
        raise fail("EXTERNAL_EVIDENCE_MISSING")  # same-target-only contradictions are out of scope
    unresolved = []
    for disagreement in value.disagreements:
        if disagreement.fact_ref not in facts or disagreement.evidence_ref not in actual:
            raise fail("DISAGREEMENT_REFERENCE")
        correction = facts[disagreement.fact_ref].get("author_correction")
        scope = question.draft
        applicable = (isinstance(correction, dict) and correction.get("kind") == "AUTHOR_CORRECTION"
                      and disagreement.correction_ref == correction.get("ref")
                      and correction.get("subject_ref") in scope.subject_refs
                      and correction.get("property_or_rule") == scope.property_or_rule
                      and correction.get("time_context") == scope.time_context
                      and correction.get("conditions") == scope.conditions)
        if not applicable:
            unresolved.append(disagreement)
    if unresolved:
        refs = list(dict.fromkeys(value.evidence_refs + [ref for row in unresolved
                                                         for ref in (row.fact_ref, row.evidence_ref)]))
        return value.model_copy(update={
            "outcome": "NEEDS_CONTEXT", "basis": "DB_RAW_DISAGREEMENT",
            "reason": "비교 기준과 원문이 달라 적용 범위를 확정할 수 없습니다. " + " ".join(row.reason for row in unresolved),
            "impact": value.impact or "REFERENCE", "impact_reason": value.impact_reason or "비교 기준 확인 필요",
            "evidence_refs": refs,
        })
    return value
