"""Frozen SETTING inspection (grid v4): plan every segment in parallel, then judge questions four at a time.

No extraction writes. In an execution job each PLANNER stage, the MERGED_PLAN and each decided question are
published to Java as soon as they are valid; without a checkpoint the same code runs as a callable.

With an index fill (owner decision 2026-11-02) the gaps of the frozen RAG scope (prior episodes, current setting
books) are filled while planning runs; judging waits for the fill, then reads readiness once.
"""

import asyncio
import logging
from collections import Counter

from app.validation.contracts import (
    BudgetState,
    IncompleteInspection,
    InspectionResult,
    ValidationDecision,
)
from app.validation.corpus import NOT_READY_CODE, ValidationCorpus
from app.validation.failures import (
    QUESTIONS_INCOMPLETE_CODE,
    continues_after_question_failure,
    is_question_level,
)
from app.validation.index_fill import FILL_FAILED_CODE, IndexFillOutcome
from app.validation.judging import JUDGE_CONCURRENCY, SettingJudge, SettingsCache
from app.validation.planning import SettingPlanner, staged
from app.validation.projection import source_catalogue
from app.validation.search import indexed_sources, vector_ready

logger = logging.getLogger(__name__)
# GH344: the one readiness read before planning may never spend the inspection's seconds budget.
VECTOR_READINESS_TIMEOUT_SECONDS = 5.0
# Judging waits at most this long (and never beyond the run's seconds) for the intake index fill.
INDEX_FILL_TIMEOUT_SECONDS = 1200.0
_DISCARDING_FAILURES = frozenset({"INPUT_CHANGED", "SOURCE_CHANGED", "SOURCE_PURGED"})


class SettingValidationService:
    def __init__(self, data_client, model_client, budget, roles, *, search=None, embedding_client=None,
                 checkpoint=None, experiment=False, judge_concurrency=JUDGE_CONCURRENCY, index_fill=None):
        """`roles` maps PLANNER and JUDGE to their RoleRuntime. `experiment=True` records question-level
        failures and continues (see app.validation.failures); it never publishes, so it needs no checkpoint.
        `index_fill` (`app.validation.index_fill.ValidationIndexFill`) fills the frozen RAG gaps before judging."""
        if experiment and checkpoint is not None:
            raise ValueError("Experiment mode never publishes to an execution checkpoint")
        if type(judge_concurrency) is not int or judge_concurrency < 1:
            raise ValueError("Judge concurrency must be a positive integer")
        self.data, self.model, self.limits, self.roles = data_client, model_client, budget, dict(roles)
        self.search, self.embedding_client = search, embedding_client
        self.checkpoint, self.experiment, self.judge_concurrency = checkpoint, experiment, judge_concurrency
        self.index_fill = index_fill

    async def _search_ready(self, frozen, budget, stats):
        """Whether the frozen episode index can serve VECTOR searches for this job (GH344, one read).

        Without RAG sources (prior episodes, current setting books) nothing needs an index. Without frozen RAG or
        a query embedding client, or if the read fails or times out (capped at VECTOR_READINESS_TIMEOUT_SECONDS),
        search is not offered and any required change search ends that question INCOMPLETE (VECTOR_NOT_READY,
        or VECTOR_INDEX_FILL_FAILED when the intake fill failed).
        """
        episodes = len(indexed_sources(frozen.sources))
        job = frozen.context.job_id or "-"
        if not episodes:
            stats["VECTOR_OFFERED"] = 1
            logger.info("vector search offered=True episodes=0 job=%s", job)
            return False
        if self.search is None or self.embedding_client is None:
            stats["VECTOR_OFFERED"] = 0
            logger.info("vector search offered=False search=%s embedding=%s job=%s", self.search is not None,
                        self.embedding_client is not None, job)
            return False
        timeout = min(budget.remaining(), VECTOR_READINESS_TIMEOUT_SECONDS)
        try:
            readiness = await asyncio.wait_for(self.search.readiness(frozen), timeout)
        except Exception as exc:  # noqa: BLE001 - DB/driver text may carry URLs or credentials; type only.
            stats["VECTOR_OFFERED"] = 0
            logger.warning("vector search readiness unavailable error=%s job=%s; search not offered",
                           type(exc).__name__, job)
            return False
        offered = vector_ready(readiness, self.embedding_client) and readiness.source_count == episodes
        stats["VECTOR_OFFERED"] = int(offered)
        logger.info("vector search offered=%s sources=%d ready=%d chunks=%d scope=%d job=%s", offered,
                    readiness.source_count, readiness.vector_ready_sources, readiness.vector_chunks, episodes, job)
        return offered

    def _start_fill(self, frozen):
        if self.index_fill is None or not indexed_sources(frozen.sources):
            return None
        return asyncio.create_task(self.index_fill.fill(frozen, self.data), name="validation-index-fill")

    async def _finish_fill(self, task, budget, stats):
        """The fill outcome before judging. A run-level failure is raised; anything else ends only the fill."""
        if task is None:
            return None
        timeout = min(budget.remaining(), INDEX_FILL_TIMEOUT_SECONDS)
        try:
            outcome = await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            outcome = IndexFillOutcome("FAILED", "INDEX_FILL_TIMEOUT")
        except IncompleteInspection:
            raise
        except Exception as exc:  # noqa: BLE001 - an unexpected fill error never stops judging; type only.
            logger.warning("validation index fill failed error=%s", type(exc).__name__)
            outcome = IndexFillOutcome("FAILED", "INDEX_FILL_UNEXPECTED")
        stats[f"INDEX_FILL_{outcome.status}"] = 1
        stats["INDEX_FILL_EMBEDDED_CHUNKS"] = int(outcome.counts.get("embeddedChunks", 0))
        return outcome

    @staticmethod
    def _unavailable(outcome):
        """What a needed search fails with when the index is not offered after the fill."""
        if outcome is not None and outcome.failed:
            return FILL_FAILED_CODE, outcome.cause
        if outcome is not None and outcome.status == "UNAVAILABLE":
            return NOT_READY_CODE, outcome.cause
        return NOT_READY_CODE, None

    async def inspect(self):
        budget, stats = BudgetState(self.limits), Counter()
        questions, coverage, decisions, bundles, failures, traces = (), [], {}, {}, {}, {}
        receipts = []
        stage = "INPUT"
        fill_task = None

        async def read(action, *args):
            budget.consume("lookup")
            try:
                return await asyncio.wait_for(action(*args), budget.remaining())
            except TimeoutError:
                raise IncompleteInspection("BUDGET_EXHAUSTED") from None

        try:
            frozen = await read(self.data.read_input)
            if self.checkpoint is not None:
                self.checkpoint.bind_sources(frozen)
            target_read = await read(self.data.read_source, frozen.target)
            target = target_read.text
            if target_read.start != 0 or target_read.end != target_read.total_code_points or not target:
                raise IncompleteInspection("TARGET_NOT_COMPLETE")
            subjects = await read(self.data.read_subjects)
            catalogue = (frozen.model_source_catalogue if frozen.model_source_catalogue is not None
                         else source_catalogue(frozen.sources))
            # The intake fill runs while planning; nothing before judging reads the index.
            fill_task = self._start_fill(frozen)

            stage = "PLANNER"
            planner = SettingPlanner(self.model, budget, self.roles["PLANNER"], checkpoint=self.checkpoint,
                                     stats=stats)
            segments = planner.segments(target)
            questions, coverage = await planner.plan(target, segments, subjects, catalogue,
                                                     failures=failures if self.experiment else None)
            stage = "MERGED_PLAN"
            checks = {}
            if self.checkpoint is not None:
                await planner.check_grid(segments)
                checks = await self.checkpoint.merge(questions)
            for ref, check in checks.items():
                if check.get("outcome") is not None:
                    decisions[ref] = ValidationDecision.model_validate(check["decision"])
                    bundles[ref] = check.get("bundle", {})
            queue = [question for question in questions if question.ref not in decisions]

            stage = "INDEX_FILL"
            fill = await self._finish_fill(fill_task, budget, stats)
            available = await self._search_ready(frozen, budget, stats)
            corpus = ValidationCorpus(frozen, budget, search=self.search, embedding_client=self.embedding_client,
                                      available=available, stats=stats, unavailable=self._unavailable(fill))
            receipts = corpus.receipts

            stage = "JUDGE"
            settings = SettingsCache(lambda refs: read(self.data.read_settings, refs))
            # One batched SETTINGS read for every question subject before judging (<= 100 subjects per read).
            await settings.rows([ref for question in queue for ref in question.draft.subject_refs])
            judge = SettingJudge(self.model, budget, self.roles["JUDGE"], corpus=corpus, settings=settings,
                                 target=target, source_catalogue=catalogue,
                                 segment_refs=[segment.ref for segment in segments], stats=stats,
                                 subjects=subjects, target_episode_no=frozen.target.metadata.get("episodeNo"))
            stopped = await self._judge_all(judge, queue, checks, decisions, bundles, failures, traces)
            if stopped is not None:
                raise stopped
            return self._result(questions, decisions, receipts, coverage, bundles, stats, failures, traces)
        except IncompleteInspection as exc:
            failure = staged(exc, stage)
            diagnostics = self._diagnostics(failure, stats, failures, traces)
            if failure.code in _DISCARDING_FAILURES:
                return InspectionResult("INCOMPLETE", failure_code=failure.code, **diagnostics)
            return InspectionResult("INCOMPLETE", questions, self._ordered(questions, decisions), tuple(receipts),
                                    tuple(coverage), failure.code, self._ordered(questions, bundles), **diagnostics)
        except (ValueError, KeyError, TypeError) as exc:
            failure = staged(exc, stage)
            return InspectionResult("INCOMPLETE", questions, self._ordered(questions, decisions), tuple(receipts),
                                    tuple(coverage), failure.code, self._ordered(questions, bundles),
                                    **self._diagnostics(failure, stats, failures, traces))
        finally:
            if fill_task is not None and not fill_task.done():
                fill_task.cancel()
                await asyncio.gather(fill_task, return_exceptions=True)

    async def _judge_all(self, judge, queue, checks, decisions, bundles, failures, traces):
        """Dispatch up to `judge_concurrency` judges; return the failure that stopped dispatching, if any.

        After a stopping failure no new judge starts, while the ones already running finish and publish.
        """
        slots = asyncio.Semaphore(self.judge_concurrency)
        stop = {"failure": None, "escaped": None}

        async def one(question):
            qref = question.ref
            trace = traces.setdefault(qref, [])
            try:
                revision = checks.get(qref, {}).get("revision", 1)
                decision, bundle = await judge.judge(question, revision, trace)
                if self.checkpoint is not None:
                    await self.checkpoint.decide(qref, decision, bundle)
                decisions[qref], bundles[qref] = decision, bundle
            except (IncompleteInspection, ValueError, KeyError, TypeError, TimeoutError) as exc:
                if isinstance(exc, TimeoutError):
                    exc = IncompleteInspection("BUDGET_EXHAUSTED")
                failure = staged(exc, f"JUDGE:{qref}")
                call = trace[-1]["call"] if trace else 0
                if trace:
                    trace[-1].update(error=failure.code, cause=failure.cause)
                if is_question_level(failure.code) and continues_after_question_failure(self.experiment):
                    failures[qref] = {"code": failure.code, "cause": failure.cause, "call": call}
                    if self.checkpoint is None:
                        return
                    try:
                        # Decision ③: only this question is published INCOMPLETE; the others keep being judged.
                        await self.checkpoint.fail(qref, question.draft, failure.code, failure.cause, call)
                        return
                    except (IncompleteInspection, ValueError, KeyError, TypeError) as published:
                        failure = staged(published, f"JUDGE:{qref}")  # lease/HTTP/conflict: a run-level stop
                if stop["failure"] is None:
                    stop["failure"] = failure
            except asyncio.CancelledError as exc:
                if asyncio.current_task().cancelling():
                    raise
                # Raised from inside (not a cancellation of this judge): a TaskGroup would drop it silently,
                # so it stops dispatching and is re-raised after the running judges finish.
                stop["escaped"] = stop["escaped"] or exc
            finally:
                slots.release()

        try:
            async with asyncio.TaskGroup() as group:
                for question in queue:
                    await slots.acquire()
                    if stop["failure"] is not None or stop["escaped"] is not None:
                        slots.release()
                        break
                    group.create_task(one(question), name=f"validation-judge-{question.ref}")
        except BaseExceptionGroup as group_error:
            # Only unexpected errors escape `one`; surface the first one as before parallel judging.
            raise group_error.exceptions[0] from None
        if stop["escaped"] is not None:
            raise stop["escaped"]
        return stop["failure"]

    @staticmethod
    def _ordered(questions, values):
        order = {question.ref: index for index, question in enumerate(questions)}
        return dict(sorted(values.items(), key=lambda item: order.get(item[0], len(order))))

    def _result(self, questions, decisions, receipts, coverage, bundles, stats, failures, traces):
        if {question.ref for question in questions} - set(decisions) - set(failures):
            raise IncompleteInspection("JUDGING_INCOMPLETE", stage="JUDGE")  # never report a silent gap as done
        if failures and not self.experiment:
            # Decision ③: every question was decided or published INCOMPLETE. The run still is not a clean success.
            logger.warning("setting inspection finished with incomplete questions count=%d codes=%s normalizations=%s",
                           len(failures), dict(sorted(Counter(row["code"] for row in failures.values()).items())),
                           dict(sorted(stats.items())))
            return InspectionResult("INCOMPLETE", questions, self._ordered(questions, decisions), tuple(receipts),
                                    tuple(coverage), QUESTIONS_INCOMPLETE_CODE, self._ordered(questions, bundles),
                                    failure_stage="JUDGE", normalizations=dict(stats),
                                    question_failures=self._ordered(questions, failures), traces=dict(traces))
        if failures:
            order = {f"PLANNER:{row['segment_ref']}": index for index, row in enumerate(coverage)}
            order.update({question.ref: len(order) + index for index, question in enumerate(questions)})
            ref = min(failures, key=lambda key: order.get(key, len(order)))
            first = failures[ref] | {"ref": ref}
            logger.warning("setting inspection finished with question failures count=%d normalizations=%s",
                           len(failures), dict(sorted(stats.items())))
            return InspectionResult("INCOMPLETE", questions, self._ordered(questions, decisions), tuple(receipts),
                                    tuple(coverage), first["code"], self._ordered(questions, bundles),
                                    failure_cause=first["cause"], failure_stage=first["ref"],
                                    normalizations=dict(stats), question_failures=dict(failures),
                                    traces=dict(traces))
        logger.info("setting inspection completed questions=%d decisions=%d normalizations=%s",
                    len(questions), len(decisions), dict(sorted(stats.items())))
        return InspectionResult("COMPLETED", questions, self._ordered(questions, decisions), tuple(receipts),
                                tuple(coverage), evidence_bundles=self._ordered(questions, bundles),
                                normalizations=dict(stats), traces=dict(traces))

    @staticmethod
    def _diagnostics(failure, stats, failures, traces):
        logger.warning("setting inspection incomplete code=%s cause=%s stage=%s normalizations=%s",
                       failure.code, failure.cause, failure.stage, dict(sorted(stats.items())))
        return {"failure_cause": failure.cause if failure.cause != failure.code else None,
                "failure_stage": failure.stage, "normalizations": dict(stats),
                "question_failures": dict(failures), "traces": dict(traces)}
