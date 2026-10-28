"""Callable GH306 inspection. No Worker claims, extraction writes or result persistence."""

import asyncio
import json
import re
from dataclasses import asdict
from hashlib import sha256

from app.validation.clues import collect_clues
from app.validation.contracts import (
    BudgetState,
    IncompleteInspection,
    InspectionResult,
    LookupIntent,
    LookupReceipt,
    QuestionBatch,
    RelatedSelection,
    ValidationDecision,
    merge_questions,
    split_segments,
    validate_batch,
    validate_span,
)
from app.validation.corpus import ValidationCorpus
from app.validation.models import call_role


def _model_state(value, sources=()):
    """Preserve values, history, conditions and times; exclude server IDs/storage metadata."""
    if isinstance(value, dict):
        result = {
            key: _model_state(item, sources)
            for key, item in value.items()
            if not re.search(r"(?:Id|Ids|Hash|S3Key|S3Version|Token)$", key)
            and key
            not in {
                "actualCharacterId",
                "actualWorldSettingId",
                "provisionalSubjectKey",
                "subjectRef",
                "sourceKey",
                "sourceVersion",
                "sourceHash",
            }
        }
        source_id = (
            value.get("sourceEpisodeId")
            or value.get("episodeId")
            or value.get("sourceSettingBookRevisionId")
            or value.get("revisionId")
        )
        source_hash = value.get("sourceHash") or value.get("contentHash")
        source_version = value.get("sourceVersion", value.get("contentS3Version"))
        for source in sources:
            if (
                source_id in {source.metadata.get("episodeId"), source.metadata.get("revisionId")}
                and source_id is not None
                and source_hash == source.content_hash
                and (source_version is None or source_version == source.source_version)
                and value.get("availability") != "UNAVAILABLE"
                and value.get("sourceAvailability") != "UNAVAILABLE"
            ):
                result["source_ref"] = source.ref
                break
        return result
    if isinstance(value, list):
        return [_model_state(item, sources) for item in value]
    return value


def _settings(entries, sources=()):
    subjects, result = {}, []
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get("state"), dict):
            raise IncompleteInspection("INVALID_BASELINE_CONTRACT")
        state = row["state"]
        identity = row.get("subjectRef") or "world:" + str(
            state.get("targetWorldSettingId") or row["entryRef"]
        )
        subjects.setdefault(identity, f"S{len(subjects) + 1}")
        result.append(
            {
                "entry_ref": row["entryRef"],
                "subject_ref": subjects[identity],
                "domain": row["domain"],
                "name": state.get("name") or state.get("subjectName"),
                "aliases": state.get("aliases", []),
                "state": _model_state(state, sources),
                "author_correction": state.get("explicitAuthorCorrection"),
            }
        )
    return result


class SettingValidationService:
    def __init__(self, data_client, model_client, budget, *, search=None, embedding_client=None):
        self.data, self.model, self.limits = data_client, model_client, budget
        self.search, self.embedding_client = search, embedding_client

    async def inspect(self):
        budget = BudgetState(self.limits)
        questions, decisions, receipts, coverage, bundles = (), {}, [], [], {}
        try:

            async def read(action, *args):
                budget.consume("lookup")
                return await asyncio.wait_for(action(*args), budget.remaining())

            frozen = await read(self.data.read_input)
            target_read = await read(self.data.read_source, frozen.target)
            target = target_read.text
            if (
                target_read.start != 0
                or target_read.end != target_read.total_code_points
                or not target
            ):
                raise IncompleteInspection("TARGET_NOT_COMPLETE")
            entries, offset = [], 0
            while True:
                page, next_offset, more = await read(
                    self.data.read_entries_page, offset, self.limits.catalogue_page_size
                )
                if next_offset != offset + len(page) or more and not page:
                    raise IncompleteInspection("INCOMPLETE_CATALOGUE")
                entries.extend(page)
                offset = next_offset
                if not more:
                    break
            if len(entries) != frozen.entry_count or len(
                {row["entryRef"] for row in entries}
            ) != len(entries):
                raise IncompleteInspection("INCOMPLETE_CATALOGUE")
            settings = _settings(entries, frozen.sources)
            source_catalogue = [
                dict(
                    source_ref=source.ref,
                    source_kind=source.kind,
                    **{
                        key: source.metadata[key]
                        for key in (
                            "episodeNo",
                            "revisionNo",
                            "referenceTimeKind",
                            "referenceEpisodeNo",
                        )
                        if key in source.metadata
                    },
                )
                for source in frozen.sources
            ]
            subjects = {row["subject_ref"] for row in settings}
            catalogue = [
                {key: row[key] for key in ("entry_ref", "subject_ref", "domain", "name", "aliases")}
                | {"paths": list(row["state"].get("slots", row["state"].get("propertiesJson", {})))}
                for row in settings
            ]
            segments = split_segments(
                target,
                core_chars=self.limits.segment_chars,
                context_chars=self.limits.context_chars,
            )
            clues = collect_clues(
                target, [name for row in catalogue for name in [row["name"], *row["aliases"]]]
            )
            coverage = [
                {
                    "role": role,
                    "segment_ref": segment.ref,
                    "status": "UNPROCESSED",
                    "reason": "NOT_RUN",
                }
                for segment in segments
                for role in ("PLANNER", "COVERAGE")
            ]
            corpus = ValidationCorpus(
                self.data,
                frozen,
                budget,
                search=self.search,
                embedding_client=self.embedding_client,
            )
            book_rules, book_origins, book_coverage = [], {}, {}
            books = [source for source in frozen.sources if source.kind == "SETTING_BOOK"]
            for source in books:
                book_coverage[source.ref] = len(coverage)
                coverage.append(
                    {
                        "role": "BOOK_SOURCE",
                        "source_ref": source.ref,
                        "status": "UNPROCESSED",
                        "reason": "NOT_READ",
                        "source_read_status": "NOT_READ",
                        "planned_checks": None,
                        "completed_checks": 0,
                    }
                )
            for source in books:
                raw = await read(self.data.read_source, source)
                if raw.start != 0 or raw.end != raw.total_code_points:
                    raise IncompleteInspection("BOOK_NOT_COMPLETE")
                count = (len(raw.text) + self.limits.segment_chars - 1) // self.limits.segment_chars
                source_coverage = coverage[book_coverage[source.ref]]
                source_coverage.update(
                    source_read_status="READY",
                    planned_checks=count * len(segments),
                    reason="BOOK_REVIEW_PENDING",
                )
                # Every raw-book segment needs a high review. Do not allocate/omit excess work.
                if (len(book_rules) + count) * len(segments) + 2 * len(
                    segments
                ) > self.limits.model_calls:
                    raise IncompleteInspection("BUDGET_EXHAUSTED")
                for index, book_segment in enumerate(
                    split_segments(
                        raw.text,
                        core_chars=self.limits.segment_chars,
                        context_chars=self.limits.context_chars,
                    )
                ):
                    budget.remaining()
                    start, end = book_segment.context_start, book_segment.context_end
                    ref = corpus.register(source, start, end, raw.text[start:end], "BOOK_REVIEW")
                    book_rules.append(
                        {
                            **corpus.evidence[ref],
                            "evidence_ref": ref,
                            "ref": f"{source.ref}SEG{index + 1}",
                            "core_start": book_segment.start,
                            "core_end": book_segment.end,
                        }
                    )
                if not count:
                    source_coverage.update(status="PROCESSED", reason="EMPTY_SOURCE_READ")
            drafts = []
            for segment in segments:
                common = {
                    "segment": asdict(segment),
                    "text": target[segment.context_start : segment.context_end],
                    "text_start": segment.context_start,
                    "source_catalogue": source_catalogue,
                    # Identification only. Selection below pages independently over all settings.
                    "catalogue": [
                        {key: row[key] for key in ("entry_ref", "subject_ref", "name", "aliases")}
                        for row in catalogue
                    ],
                }
                first = await call_role(self.model, "PLANNER", common, QuestionBatch, budget)
                validate_batch(first, [segment], subjects, [], target)
                coverage = self._record_coverage(coverage, "PLANNER", first)
                drafts.extend((question, f"PLANNER:{segment.ref}") for question in first.questions)
                selected = set()
                for index in range(0, len(catalogue), self.limits.catalogue_page_size):
                    page = catalogue[index : index + self.limits.catalogue_page_size]
                    selection = await call_role(
                        self.model,
                        "RELATED_SELECTION",
                        {
                            **common,
                            "catalogue": page,
                        },
                        RelatedSelection,
                        budget,
                    )
                    if len(selection.entries) != len(page) or {
                        entry.entry_ref for entry in selection.entries
                    } != {row["entry_ref"] for row in page}:
                        raise IncompleteInspection("RELATED_SELECTION_INCOMPLETE")
                    selected.update(
                        entry.entry_ref for entry in selection.entries if entry.selected
                    )
                    coverage.append(
                        {
                            "role": "RELATED_SELECTION",
                            "segment_ref": segment.ref,
                            "selections": [entry.model_dump() for entry in selection.entries],
                        }
                    )
                relevant = [row for row in settings if row["entry_ref"] in selected]
                relevant_clues = [
                    clue for clue in clues if segment.start <= clue["span"]["start"] < segment.end
                ]
                second = await call_role(
                    self.model,
                    "COVERAGE",
                    {
                        **common,
                        "settings": relevant,
                        "mechanical_clues": relevant_clues,
                    },
                    QuestionBatch,
                    budget,
                )
                validate_batch(second, [segment], subjects, selected, target)
                if (
                    len(second.clue_checks) != len(relevant_clues)
                    or {row.entry_ref for row in second.clue_checks}
                    != {row["ref"] for row in relevant_clues}
                    or any(row.status == "UNPROCESSED" for row in second.clue_checks)
                ):
                    raise IncompleteInspection("CLUE_COVERAGE_INCOMPLETE")
                coverage = self._record_coverage(coverage, "COVERAGE", second)
                drafts.extend(
                    (question, f"COVERAGE:{segment.ref}") for question in second.questions
                )
                # Fixed BOOK raw is inspected independently even when DB/planner have no questions.
                for rule in book_rules:
                    coverage_index = len(coverage)
                    coverage.append(
                        {
                            "role": "BOOK_COVERAGE",
                            "segment_ref": segment.ref,
                            "rule_ref": rule["ref"],
                            "source_ref": rule["source_ref"],
                            "status": "UNPROCESSED",
                            "reason": "NOT_RUN",
                        }
                    )
                    batch = await call_role(
                        self.model,
                        "COVERAGE",
                        {
                            **common,
                            "settings": [],
                            "raw_rules": [rule],
                            "mechanical_clues": [],
                        },
                        QuestionBatch,
                        budget,
                    )
                    validate_batch(batch, [segment], subjects, [rule["ref"]], target)
                    coverage[coverage_index].update(
                        status=batch.segments[0].status,
                        reason=batch.segments[0].reason,
                        rules=[row.model_dump() for row in batch.rules],
                    )
                    source_coverage = coverage[book_coverage[rule["source_ref"]]]
                    source_coverage["completed_checks"] += 1
                    if source_coverage["completed_checks"] == source_coverage["planned_checks"]:
                        source_coverage.update(
                            status="PROCESSED", reason="INDEPENDENT_BOOK_REVIEW_COMPLETE"
                        )
                    origin = f"BOOK_COVERAGE:{segment.ref}:{rule['ref']}"
                    book_origins[origin] = rule["evidence_ref"]
                    drafts.extend((question, origin) for question in batch.questions)
                    if batch.questions:
                        for record in coverage:
                            if (
                                record.get("role") == "COVERAGE"
                                and record.get("segment_ref") == segment.ref
                            ):
                                record.update(
                                    status="PLANNED", reason="INDEPENDENT_BOOK_QUESTION_ADDED"
                                )
            questions = merge_questions(drafts)
            queue = list(questions)
            by_ref = {question.ref: question for question in questions}
            pending = {
                question.ref: [(intent, False) for intent in question.draft.lookup_intents]
                for question in questions
            }
            own_receipts = {question.ref: [] for question in questions}
            revisions = {question.ref: 1 for question in questions}
            target_context_reads = {question.ref: [] for question in questions}
            baseline_selections = {
                question.ref: {
                    row["entry_ref"]
                    for row in settings
                    if row["subject_ref"] in question.draft.subject_refs
                }
                for question in questions
            }
            seen = {}
            while queue:
                question = queue.pop(0)
                qref, draft = question.ref, question.draft
                relevant = [
                    row for row in settings if row["entry_ref"] in baseline_selections[qref]
                ]
                if not own_receipts[qref]:
                    for operation in ("BASELINE", "TARGET_CONTEXT"):
                        intent = LookupIntent(
                            ref=f"{qref}-{operation}",
                            kind="COMPARISON_CONTEXT",
                            operation=operation,
                            purpose="같은 대상·시점·조건의 고정 비교 자료 확인",
                        )
                        receipt = LookupReceipt(
                            f"L{len(receipts) + 1}",
                            intent,
                            "SUCCEEDED",
                            tuple(row["entry_ref"] for row in relevant)
                            if operation == "BASELINE"
                            else ("T1",),
                        )
                        receipts.append(receipt)
                        own_receipts[qref].append(receipt)
                    book_refs = tuple(
                        dict.fromkeys(
                            ref
                            for origin, ref in book_origins.items()
                            if any(
                                original.startswith(origin + ":")
                                for original in question.original_refs
                            )
                        )
                    )
                    if book_refs:
                        intent = LookupIntent(
                            ref=f"{qref}-BOOK",
                            kind="COMPARISON_CONTEXT",
                            operation="DIRECT",
                            purpose="독립 점검에서 실제 읽은 고정 설정집 근거",
                        )
                        receipt = LookupReceipt(
                            f"L{len(receipts) + 1}",
                            intent,
                            "SUCCEEDED",
                            book_refs,
                            readiness={"executed_methods": ("BOOK_REVIEW",)},
                        )
                        receipts.append(receipt)
                        own_receipts[qref].append(receipt)
                while pending[qref]:
                    intent, from_follow_up = pending[qref].pop(0)
                    signature = (qref, intent.model_dump_json(exclude={"ref"}))
                    if signature in seen:
                        if from_follow_up:
                            raise IncompleteInspection("FOLLOW_UP_NO_PROGRESS")
                        original = seen[signature]
                        reused = LookupReceipt(
                            f"L{len(receipts) + 1}",
                            intent,
                            "SUCCEEDED",
                            original.evidence_refs,
                            original.limited,
                            reused_from=original.ref,
                            readiness=original.readiness,
                        )
                        receipts.append(reused)
                        own_receipts[qref].append(reused)
                        continue
                    try:
                        if intent.operation == "BASELINE":
                            if not set(intent.evidence_refs) <= {
                                row["entry_ref"] for row in settings
                            }:
                                raise IncompleteInspection("UNKNOWN_BASELINE_REFERENCE")
                            baseline_selections[qref].update(intent.evidence_refs)
                            relevant = [
                                row
                                for row in settings
                                if row["entry_ref"] in baseline_selections[qref]
                            ]
                            refs, limited = tuple(row["entry_ref"] for row in relevant), False
                        elif intent.operation == "TARGET_CONTEXT":
                            start = 0 if intent.start is None else intent.start
                            end = len(target) if intent.end is None else intent.end
                            if intent.source_ref not in {None, "T1"} or not 0 <= start < end <= len(
                                target
                            ):
                                raise IncompleteInspection("INVALID_TARGET_CONTEXT_REQUEST")
                            target_context_reads[qref].append(
                                {
                                    "source_ref": "T1",
                                    "start": start,
                                    "end": end,
                                    "quote": target[start:end],
                                }
                            )
                            refs, limited = ("T1",), False
                        else:
                            refs, limited = await corpus.lookup(intent)
                    except IncompleteInspection as exc:
                        receipt = LookupReceipt(
                            f"L{len(receipts) + 1}",
                            intent,
                            "FAILED",
                            failure_code=exc.code,
                            readiness=dict(corpus.last_readiness)
                            if corpus.last_readiness
                            else None,
                        )
                        receipts.append(receipt)
                        own_receipts[qref].append(receipt)
                        raise
                    receipt = LookupReceipt(
                        f"L{len(receipts) + 1}",
                        intent,
                        "SUCCEEDED",
                        refs,
                        limited,
                        readiness=dict(corpus.last_readiness)
                        if intent.operation not in {"BASELINE", "TARGET_CONTEXT"}
                        and corpus.last_readiness
                        else None,
                    )
                    receipts.append(receipt)
                    own_receipts[qref].append(receipt)
                    seen[signature] = receipt
                evidence_refs = {
                    ref for receipt in own_receipts[qref] for ref in receipt.evidence_refs
                }
                evidence = [row for ref, row in corpus.evidence.items() if ref in evidence_refs]
                payload = {
                    "question_ref": qref,
                    "check_revision": revisions[qref],
                    "question": draft.model_dump(),
                    "target_context": [
                        {
                            "start": max(0, span.start - self.limits.context_chars),
                            "text": target[
                                max(0, span.start - self.limits.context_chars) : min(
                                    len(target), span.end + self.limits.context_chars
                                )
                            ],
                        }
                        for span in draft.target_spans
                    ],
                    "settings": relevant,
                    "evidence": evidence,
                    "receipts": [
                        asdict(receipt) | {"intent": receipt.intent.model_dump()}
                        for receipt in own_receipts[qref]
                    ],
                    "question_refs": list(by_ref),
                    "additional_target_context": target_context_reads[qref],
                    "source_catalogue": source_catalogue,
                }
                # Different obligations/receipts invalidate earlier decisions, even on unchanged manuscript.
                payload["input_digest"] = sha256(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                bundles[qref] = payload
                judgment = await call_role(self.model, "JUDGE", payload, ValidationDecision, budget)
                if judgment.action == "FOLLOW_UP":
                    if (
                        not judgment.follow_ups
                        or judgment.outcome is not None
                        or judgment.completion is not None
                    ):
                        raise IncompleteInspection("INVALID_FOLLOW_UP")
                    affected = set(judgment.affected_question_refs) | {qref}
                    if not affected <= set(by_ref):
                        raise IncompleteInspection("UNKNOWN_QUESTION_REFERENCE")
                    for ref in affected:
                        decisions.pop(ref, None)
                        bundles.pop(ref, None)
                        revisions[ref] += 1
                        pending[ref].extend((intent, True) for intent in judgment.follow_ups)
                        if by_ref[ref] not in queue:
                            queue.append(by_ref[ref])
                    continue
                judgment = self._validate_decision(
                    judgment, question, relevant, evidence, own_receipts[qref], target
                )
                decisions[qref] = judgment
            await read(self.data.validate)
            await corpus.revalidate()
            # Re-read target too: unchanged DB metadata does not authenticate changed storage bytes.
            final_target = await read(self.data.read_source, frozen.target)
            if final_target.text != target:
                raise IncompleteInspection("INPUT_CHANGED")
            await read(self.data.validate)
            return InspectionResult(
                "COMPLETED",
                questions,
                decisions,
                tuple(receipts),
                tuple(coverage),
                evidence_bundles=bundles,
            )
        except IncompleteInspection as exc:
            if exc.code in {"INPUT_CHANGED", "SOURCE_CHANGED", "SOURCE_PURGED"}:
                return InspectionResult("INCOMPLETE", failure_code=exc.code)
            return InspectionResult(
                "INCOMPLETE",
                questions,
                decisions,
                tuple(receipts),
                tuple(coverage),
                exc.code,
                bundles,
            )
        except (ValueError, KeyError, TypeError):
            return InspectionResult(
                "INCOMPLETE",
                questions,
                decisions,
                tuple(receipts),
                tuple(coverage),
                "INVALID_INSPECTION_CONTRACT",
            )
        except TimeoutError:
            return InspectionResult(
                "INCOMPLETE",
                questions,
                decisions,
                tuple(receipts),
                tuple(coverage),
                "BUDGET_EXHAUSTED",
            )

    @staticmethod
    def _record_coverage(coverage, role, batch):
        values = {value.segment_ref: value for value in batch.segments}
        return [
            dict(
                role=role,
                **values[row["segment_ref"]].model_dump(),
                rules=[rule.model_dump() for rule in batch.rules],
            )
            if row.get("role") == role and row.get("segment_ref") in values
            else row
            for row in coverage
        ]

    @staticmethod
    def _validate_decision(value, question, settings, evidence, receipts, target):
        fail = lambda: IncompleteInspection("INVALID_DECISION")
        if (
            value.follow_ups
            or value.affected_question_refs
            or value.completion is None
            or value.outcome is None
            or value.basis is None
        ):
            raise fail()
        if set(value.target_spans) != set(question.draft.target_spans):
            raise fail()
        for span in value.target_spans:
            validate_span(span, target)
        facts = {row["entry_ref"]: row for row in settings}
        actual = {row["ref"]: row for row in evidence}
        known = {"T1"} | set(facts) | set(actual)
        real_receipts = {receipt.ref: receipt for receipt in receipts}
        if not set(value.evidence_refs) <= known:
            raise fail()
        for assessment in (
            value.completion.comparison_context,
            value.completion.changes_exceptions,
            value.completion.no_pending_clues,
            value.completion.required_reads,
        ):
            if (
                not assessment.evidence_refs
                or not set(assessment.evidence_refs) <= known
                or not assessment.receipt_refs
                or not set(assessment.receipt_refs) <= set(real_receipts)
            ):
                raise fail()
            if any(real_receipts[ref].status != "SUCCEEDED" for ref in assessment.receipt_refs):
                raise fail()
        if not set(real_receipts) <= set(value.completion.required_reads.receipt_refs):
            raise fail()
        if "T1" not in value.completion.comparison_context.evidence_refs:
            raise fail()
        if value.outcome == "SUSPECTED_CONFLICT" and not any(
            real_receipts[ref].intent.kind == "CHANGES_EXCEPTIONS"
            and real_receipts[ref].intent.operation in {"KEYWORD", "VECTOR"}
            for ref in value.completion.changes_exceptions.receipt_refs
        ):
            raise fail()  # CHANGE_NOT_FOUND needs an actual successful change/exception search.
        if value.basis == "DB_RAW_DISAGREEMENT" and not value.disagreements:
            raise fail()
        if value.outcome != "NO_CONFLICT" and (not value.impact or not value.impact_reason):
            raise fail()
        if value.outcome == "NO_CONFLICT" and (
            value.impact is not None or value.impact_reason is not None
        ):
            raise fail()
        expected_basis = {
            "CONFLICT": "DIRECT_CONTRADICTION",
            "SUSPECTED_CONFLICT": "CHANGE_NOT_FOUND",
            "NEEDS_CONTEXT": "AMBIGUOUS_MEANING",
            "NO_CONFLICT": "COMPATIBLE",
        }
        if value.basis not in {expected_basis[value.outcome], "DB_RAW_DISAGREEMENT"}:
            raise fail()
        if value.outcome in {"CONFLICT", "SUSPECTED_CONFLICT"} and not (
            set(value.evidence_refs) & (set(facts) | set(actual))
        ):
            raise fail()  # same-target-only contradictions are outside this inspection
        unresolved = []
        for disagreement in value.disagreements:
            if disagreement.fact_ref not in facts or disagreement.evidence_ref not in actual:
                raise fail()
            correction = facts[disagreement.fact_ref].get("author_correction")
            scope = question.draft
            applicable = (
                isinstance(correction, dict)
                and correction.get("kind") == "AUTHOR_CORRECTION"
                and disagreement.correction_ref == correction.get("ref")
                and correction.get("subject_ref") in scope.subject_refs
                and correction.get("property_or_rule") == scope.property_or_rule
                and correction.get("time_context") == scope.time_context
                and correction.get("conditions") == scope.conditions
            )
            if not applicable:
                unresolved.append(disagreement)
        if unresolved:
            refs = list(
                dict.fromkeys(
                    value.evidence_refs
                    + [ref for row in unresolved for ref in (row.fact_ref, row.evidence_ref)]
                )
            )
            return value.model_copy(
                update={
                    "outcome": "NEEDS_CONTEXT",
                    "basis": "DB_RAW_DISAGREEMENT",
                    "reason": "비교 기준과 원문이 달라 적용 범위를 확정할 수 없습니다. "
                    + " ".join(row.reason for row in unresolved),
                    "impact": value.impact or "REFERENCE",
                    "impact_reason": value.impact_reason or "비교 기준 확인 필요",
                    "evidence_refs": refs,
                }
            )
        return value
