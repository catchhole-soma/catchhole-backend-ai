"""SETTING judge state machine (grid v4): DB current values first, RAG only when the DB cannot decide.

Per question, at most MAX_JUDGE_CALLS calls:

1. OPEN: the draft, ±JUDGE_TARGET_CONTEXT_CHARS of target context and compact current rows of the question's
   subjects. The answer is DECIDED, or FOLLOW_UP with up to MAX_SEARCH_QUERIES natural-language VECTOR searches
   and/or T1 context ranges.
2. After a FOLLOW_UP the Worker runs those reads and asks again in FINAL form (DECIDED only). A FOLLOW_UP with at
   least one search also gets one Worker VECTOR search built from the question's own target text
   (`target_search_query`), on top of the judge's queries, so earlier scenes phrased like the target are found.
3. An answer without sufficient DB evidence and a validated B quote must have a successful VECTOR receipt;
   otherwise the Worker searches once and asks FINAL. This also applies after context-only FOLLOW_UP and
   clearing an unquoted finding. Search failures remain incomplete; an empty scope uses NOT_APPLICABLE.
4. A DECIDED SUSPECTED_CONFLICT from a non-final-limit call without a change search gets one Worker VECTOR
   change search (`change_search_query`: check_intent + target sentence), then the next call is FINAL. The shared three-call limit still applies, so accepting SUSPECTED_CONFLICT requires that receipt.

Judge rule guard (2026-11-03): a CONFLICT/SUSPECTED_CONFLICT is a finding only when a specific earlier statement
contradicts the target, and that statement is what the judge copies into `basis_quotes`. An accepted decision with
one of those outcomes but no valid basis quote (and no DB/raw disagreement) is normalized to NO_CONFLICT
(`clear_unquoted_finding`): absent evidence is not a contradiction. It is counted, never a question failure.
"""

import asyncio
import json
import re
from dataclasses import asdict
from hashlib import sha256

from app.validation.author_text import author_labels, sanitize_decision
from app.validation.contracts import (
    BASIS_QUOTE_LIMIT,
    MAX_BASIS_QUOTES,
    MAX_SEARCH_QUERIES,
    SEARCH_QUERY_LIMIT,
    TARGET_REF,
    BasisQuote,
    IncompleteInspection,
    JudgeFinalAnswer,
    JudgeOpenAnswer,
    LookupIntent,
    anchor_span,
    decision_from_answer,
    validate_context_request,
    validate_search_query,
)
from app.validation.data import MAX_SETTING_SUBJECTS
from app.validation.models import call_role
from app.validation.projection import (
    bundle_settings,
    compact_setting_row,
    entry_order,
    setting_value_leaves,
)
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
# 2026-11-03: marks the one search the Worker adds to every FOLLOW_UP that asks for searches.
WORKER_DB_GAP_SEARCH_PURPOSE = "Worker가 DB 비교 근거 부족 시 요구하는 이전 원문 검색"
WORKER_TARGET_SEARCH_PURPOSE = "Worker가 대상 문장과 앞뒤 원고로 만든 이전 장면 의미 검색"
# Code points of target text taken before and after each span for that search (cut back to whole lines).
TARGET_QUERY_CONTEXT_CHARS = 100
FINDING_OUTCOMES = frozenset({"CONFLICT", "SUSPECTED_CONFLICT"})
# Author sentence of a finding the guard cleared (polite style of author_text.REASON_FALLBACKS).
UNQUOTED_FINDING_REASON = "이전 기록에 이번 원고와 직접 어긋나는 문장이 없어 새로 나온 내용으로 봐요."
_SEARCH_QUERY_LIMIT = SEARCH_QUERY_LIMIT
_DECISION_FIELDS = ("outcome", "basis", "headline", "impact", "impact_reason", "completion")


def is_change_search(receipt):
    """A successful CHANGES_EXCEPTIONS VECTOR read: what SUSPECTED_CONFLICT/CHANGE_NOT_FOUND needs."""
    return (receipt.status == "SUCCEEDED" and receipt.intent.kind == "CHANGES_EXCEPTIONS"
            and receipt.intent.operation == "VECTOR")


def _one_line(text):
    return " ".join(text.split())


def _span_quotes(draft):
    spans = sorted(draft.target_spans, key=lambda span: (span.start, span.end))
    return list(dict.fromkeys(quote for quote in (_one_line(span.quote) for span in spans) if quote))


def change_search_query(draft):
    """Deterministic change-search query: the question's check_intent, then its target sentences in target order.

    Whitespace runs become one space, duplicates are dropped and the result is cut to SEARCH_QUERY_LIMIT; '' if
    nothing is left. (Before 2026-11-03 it concatenated subject_mentions and property_or_rule, which read like
    keywords and missed earlier scenes written in the target's own words.)
    """
    parts = dict.fromkeys(part for part in (_one_line(draft.check_intent), *_span_quotes(draft)) if part)
    return " ".join(parts)[:_SEARCH_QUERY_LIMIT].strip()


def target_search_query(draft, target):
    """The Worker's VECTOR query from the question's own target text (a semantic query, never a keyword scan).

    Each span is widened by up to TARGET_QUERY_CONTEXT_CHARS code points on both sides; a widening that ends
    mid-line is cut back to the nearest line break inside the added context (the span itself is never cut).
    Overlapping windows merge, windows join in target order with whitespace runs collapsed to one space. If that
    exceeds SEARCH_QUERY_LIMIT, the span quotes alone are used; the result is cut to the limit ('' if empty).
    Questions on the same span therefore build the same query and share one search per run.
    """
    windows = []
    length = len(target)
    for span in sorted(draft.target_spans, key=lambda item: (item.start, item.end)):
        start, end = max(0, min(span.start, length)), max(0, min(span.end, length))
        low, high = max(0, start - TARGET_QUERY_CONTEXT_CHARS), min(length, end + TARGET_QUERY_CONTEXT_CHARS)
        if low > 0 and target[low - 1] != "\n":
            cut = target.find("\n", low, start)
            low = cut + 1 if cut >= 0 else low
        if high < length and target[high] != "\n":
            cut = target.rfind("\n", end, high)
            high = cut if cut >= 0 else high
        if windows and low <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], high))
        else:
            windows.append((low, high))
    query = " ".join(part for part in (_one_line(target[low:high]) for low, high in windows) if part)
    if len(query) > _SEARCH_QUERY_LIMIT:
        query = " ".join(_span_quotes(draft))
    return query[:_SEARCH_QUERY_LIMIT].strip()


def clear_unquoted_finding(answer, quotes, stats=None):
    """`(answer, cleared_outcome)`: the judge-rule guard on an accepted DECIDED answer (2026-11-03).

    CONFLICT/SUSPECTED_CONFLICT need a specific earlier statement that contradicts the target, copied as a basis
    quote. With no checked quote (`quotes` is the output of `normalize_basis_quotes`) and no DB/raw
    `disagreements` (those keep their own NEEDS_CONTEXT rule), the answer becomes NO_CONFLICT/COMPATIBLE without an
    impact and with UNQUOTED_FINDING_REASON, counted as `<OUTCOME>_QUOTE_MISSING_CLEARED`. Never a question failure.
    """
    if answer.outcome not in FINDING_OUTCOMES or quotes or answer.disagreements:
        return answer, None
    if stats is not None:
        stats[f"{answer.outcome}_QUOTE_MISSING_CLEARED"] += 1
    cleared = answer.model_copy(update={"outcome": "NO_CONFLICT", "basis": "COMPATIBLE", "impact": None,
                                        "impact_reason": None, "reason": UNQUOTED_FINDING_REASON, "headline": None})
    return cleared, answer.outcome


_BASIS_FOR_OUTCOME = {"CONFLICT": "DIRECT_CONTRADICTION", "SUSPECTED_CONFLICT": "CHANGE_NOT_FOUND",
                      "NEEDS_CONTEXT": "AMBIGUOUS_MEANING", "NO_CONFLICT": "COMPATIBLE"}


def drop_invalid_disagreements(answer, settings, evidence, stats=None):
    """Keep only DB/raw disagreements that pair a stored setting row (B) with an earlier-source evidence (E).

    `disagreements` is for "the stored value and an earlier episode disagree"; judges sometimes put the target
    contradiction itself there (e.g. fact B vs T1). That used to fail the whole question
    (INVALID_DECISION_DISAGREEMENT_REFERENCE) and hid a real finding, so such rows are dropped and counted instead.
    When nothing is left a DB_RAW_DISAGREEMENT basis falls back to the outcome's own basis.
    """
    if not answer.disagreements:
        return answer
    facts = {row["entry_ref"] for row in settings}
    actual = {row["ref"] for row in evidence if row.get("source_ref") != TARGET_REF}
    kept = [row for row in answer.disagreements if row.fact_ref in facts and row.evidence_ref in actual]
    if len(kept) == len(answer.disagreements):
        return answer
    if stats is not None:
        stats["INVALID_DISAGREEMENTS_DROPPED"] += len(answer.disagreements) - len(kept)
    update = {"disagreements": kept}
    if not kept and answer.basis == "DB_RAW_DISAGREEMENT" and answer.outcome in _BASIS_FOR_OUTCOME:
        update["basis"] = _BASIS_FOR_OUTCOME[answer.outcome]
    return answer.model_copy(update=update)


def receipt_wire(receipt):
    return asdict(receipt) | {"intent": receipt.intent.model_dump()}


_EVIDENCE_REF = re.compile(r"E[1-9][0-9]*")
_ENTRY_REF = re.compile(r"B[1-9][0-9]*")


def _evidence_quote(ref, quote, row):
    """The absolute span of `quote` inside one evidence chunk, or None (first occurrence; CRLF chunks retried)."""
    chunk = row.get("quote") if row is not None else None
    if not isinstance(chunk, str) or row.get("source_ref") == TARGET_REF or type(row.get("start")) is not int:
        return None
    index = chunk.find(quote)
    if index < 0 and "\n" in quote and "\r\n" in chunk:
        crlf = quote.replace("\r\n", "\n").replace("\n", "\r\n")
        index = chunk.find(crlf)
        quote = crlf
    if index < 0 or len(quote) > BASIS_QUOTE_LIMIT:
        return None
    start = row["start"] + index
    return BasisQuote(ref=ref, source_ref=row["source_ref"], start=start, end=start + len(quote), quote=quote)


def _setting_quote(ref, quote, row):
    """A B quote is a substring of one stored setting value of that row; it has no source position."""
    if row is None or not any(quote in leaf for leaf in setting_value_leaves(row)):
        return None
    return BasisQuote(ref=ref, source_ref=None, start=None, end=None, quote=quote)


def normalize_basis_quotes(drafts, evidence, settings, stats=None):
    """Checked basis sentences of one decision, in model order, at most MAX_BASIS_QUOTES.

    `E#` must be an exact substring of that evidence row's chunk and becomes an absolute code point span of its
    source (`start = chunk start + index`); `B#` must be a substring of one setting value leaf of that row. Anything
    else (unknown/T1 ref, not found, too long, duplicate, over the limit) is dropped and counted as
    `BASIS_QUOTES_DROPPED`: a missing basis sentence is never a question failure.
    """
    by_evidence = {row["ref"]: row for row in evidence}
    by_setting = {row["entry_ref"]: row for row in settings}
    kept, seen, dropped = [], set(), 0
    for draft in drafts:
        ref, quote = draft.ref, draft.quote.strip()
        item = None
        if quote and len(quote) <= BASIS_QUOTE_LIMIT and len(kept) < MAX_BASIS_QUOTES:
            if _EVIDENCE_REF.fullmatch(ref):
                item = _evidence_quote(ref, quote, by_evidence.get(ref))
            elif _ENTRY_REF.fullmatch(ref):
                item = _setting_quote(ref, quote, by_setting.get(ref))
        if item is None or (item.ref, item.quote) in seen:
            dropped += 1
            continue
        seen.add((item.ref, item.quote))
        kept.append(item)
    if dropped and stats is not None:
        stats["BASIS_QUOTES_DROPPED"] += dropped
    return kept


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
                 stats, subjects=None, target_episode_no=None):
        self.model, self.budget, self.runtime = model, budget, runtime
        self.corpus, self.settings, self.target = corpus, settings, target
        self.source_catalogue, self.segment_refs, self.stats = source_catalogue, frozenset(segment_refs), stats
        # SUBJECTS catalogue rows: S refs in author text become subject names (None: every S ref is "대상").
        self.subjects = subjects
        # The target's episode number, so the author sentence can say "31화" (model payload only, not the bundle).
        self.target_episode_no = target_episode_no if type(target_episode_no) is int else None
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
            # target_episode_no is a model-only input: the bundle (and its input_digest) stays the Backend's shape.
            payload = {**bundle, "form": form, "target_episode_no": self.target_episode_no,
                       "settings": [compact_setting_row(row) for row in rows]}
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
                row.update(await self._follow_up(qref, draft, answer, own, extra_context))
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
            answer = drop_invalid_disagreements(answer, rows, evidence, self.stats)
            quotes = normalize_basis_quotes(answer.basis_quotes, evidence, rows, self.stats)
            # The rule guard runs before validation: a finding without a contradicting quote is NO_CONFLICT, not a
            # question failure (e.g. a CONFLICT that cites no external evidence at all).
            original = answer
            answer, cleared = clear_unquoted_finding(answer, quotes, self.stats)
            decision = validate_decision(decision_from_answer(answer), question, rows, evidence, own, self.target,
                                         self.stats)
            # A broad/absent DB statement cannot close a question without reading earlier sources.
            # The model assesses semantic sufficiency; the Worker also requires a real, checked DB quote.
            # Context-only FOLLOW_UP and a cleared unquoted finding must pass the same gate.
            searched = any(receipt.status == "SUCCEEDED" and receipt.intent.operation == "VECTOR"
                           for receipt in own)
            db_supported = answer.db_evidence_sufficient and any(_ENTRY_REF.fullmatch(q.ref) for q in quotes)
            if not searched and not db_supported:
                if calls >= MAX_JUDGE_CALLS:
                    raise _invalid("INVALID_DECISION_REQUIRED_SEARCH_MISSING")
                query = change_search_query(draft)
                if not query:
                    raise _invalid("INVALID_DECISION_REQUIRED_SEARCH_QUERY_MISSING")
                before = self.stats["RAG_CACHE_HITS"]
                own.append(await corpus.vector(LookupIntent(
                    ref=f"{qref}-DB_GAP_VECTOR", kind="COMPARISON_CONTEXT", operation="VECTOR",
                    purpose=WORKER_DB_GAP_SEARCH_PURPOSE, query=query)))
                self.stats["DB_GAP_SEARCH_INSERTED"] += 1
                row.update(db_gap_search=True, cache_hits=self.stats["RAG_CACHE_HITS"] - before)
                form = "FINAL"
                continue
            if cleared is not None:
                # In-memory diagnostics for reviewing the guard (the experiment writes them; never logged/published).
                row.update(cleared=cleared, cleared_impact=original.impact, cleared_reason=original.reason)
            decision = decision.model_copy(update={"basis_quotes": quotes})
            labels = author_labels(evidence=evidence, source_catalogue=self.source_catalogue, settings=rows,
                                   subjects=self.subjects)
            decision = sanitize_decision(decision, labels, self.stats)
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
            if answer.basis_quotes:
                ignored["basis_quotes"] = []
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
            if answer.outcome == "NO_CONFLICT" and answer.headline is not None:
                # No finding, no title (Backend rejects a NO_CONFLICT headline).
                self.stats["NO_CONFLICT_HEADLINE_CLEARED"] += 1
                updates["headline"] = None
        return answer.model_copy(update=updates) if updates else answer

    async def _follow_up(self, qref, draft, answer, own, extra_context):
        """Run the judge's reads; with at least one search, also the Worker's target-text search (after them).

        That search is extra to the judge's MAX_SEARCH_QUERIES (at most 3 + 1 per follow-up). It spends a lookup
        and a query embedding like any search unless the same query already ran in this run (then reused).
        """
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
        worker = 0
        target_query = target_search_query(draft, self.target) if queries else ""
        if target_query:
            own.append(await self.corpus.vector(LookupIntent(
                ref=f"{qref}-TARGET_VECTOR", kind="COMPARISON_CONTEXT", operation="VECTOR",
                purpose=WORKER_TARGET_SEARCH_PURPOSE, query=target_query)))
            self.stats["TARGET_SEARCH_INSERTED"] += 1
            worker = 1
        self.stats["FOLLOW_UPS"] += 1
        return {"searches": len(queries), "worker_searches": worker, "context_requests": len(ranges)}

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
    if set(value.target_spans) != set(question.draft.target_spans):
        # The judged spans are always the question's own spans; an echoed span the judge mis-copied is replaced
        # instead of failing the question (31화 planted run: Q66 INVALID_EXACT_SPAN).
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
    if value.outcome == "NO_CONFLICT" and value.basis not in {"COMPATIBLE", "DB_RAW_DISAGREEMENT"}:
        # NO_CONFLICT has a single meaning; a "not found" basis code on it (e.g. CHANGE_NOT_FOUND) is a
        # labelling slip, not a different decision. Fill it instead of failing the question.
        value = value.model_copy(update={"basis": "COMPATIBLE"})
        if stats is not None:
            stats["NO_CONFLICT_BASIS_NORMALIZED"] += 1
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
            "outcome": "NEEDS_CONTEXT", "basis": "DB_RAW_DISAGREEMENT", "headline": None,
            "reason": "비교 기준과 원문이 달라 적용 범위를 확정할 수 없습니다. " + " ".join(row.reason for row in unresolved),
            "impact": value.impact or "REFERENCE", "impact_reason": value.impact_reason or "비교 기준 확인 필요",
            "evidence_refs": refs,
        })
    return value
