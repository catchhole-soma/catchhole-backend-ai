"""SETTING grid v4 planning: PLANNER per segment in parallel, then MERGED_PLAN in segment order."""

import asyncio
from dataclasses import asdict

from pydantic import ValidationError

from app.validation.contracts import (
    TARGET_REF,
    IncompleteInspection,
    QuestionBatch,
    merge_questions,
    normalize_batch,
    safe_code,
    segment_wire,
    split_setting_segments,
    validate_batch,
)
from app.validation.failures import is_question_level
from app.validation.models import call_role
from app.validation.schemas import PLANNER_SCHEMA_NAME, planner_schema

SUPPORTED_SETTING_GRID_VERSION = 4


def contract_failure(exc, stage=None):
    """The collapsed INVALID_INSPECTION_CONTRACT category with its specific safe cause (GH331)."""
    if isinstance(exc, ValidationError):
        cause = "CONTRACT_MODEL_INVALID"
    elif isinstance(exc, ValueError):
        cause = safe_code(exc.args[0] if exc.args else None, "CONTRACT_VALUE_INVALID")
    else:
        cause = "CONTRACT_KEY_MISSING" if isinstance(exc, KeyError) else "CONTRACT_TYPE_INVALID"
    return IncompleteInspection("INVALID_INSPECTION_CONTRACT", cause, stage=stage)


def staged(exc, stage):
    """Attach the stage a failure happened in; contract errors become INVALID_INSPECTION_CONTRACT."""
    if isinstance(exc, IncompleteInspection):
        if exc.stage is None:
            exc.stage = stage
        return exc
    return contract_failure(exc, stage)


class SettingPlanner:
    """Plans questions for one frozen target. `checkpoint` is None outside an execution job."""

    def __init__(self, model, budget, runtime, *, checkpoint=None, stats=None):
        self.model, self.budget, self.runtime = model, budget, runtime
        self.checkpoint, self.stats = checkpoint, stats
        self.batches = {}  # segment ref -> validated batch (diagnostics; the published stage is the authority)

    def segments(self, target):
        limits = self.budget.limits
        return split_setting_segments(target, unit_chars=limits.segment_chars, context_chars=limits.context_chars)

    async def plan(self, target, segments, subjects, source_catalogue, *, failures=None):
        """Return `(questions, coverage)`. With `failures` (experiment mode) a failed segment is recorded there
        and the remaining segments still merge; otherwise the first failure in segment order is raised after
        every segment has finished (each successful one is already published)."""
        subject_refs = [row["subject_ref"] for row in subjects]
        schema = planner_schema(segment_refs=[segment.ref for segment in segments], subject_refs=subject_refs)
        aliases = frozenset(segment.ref for segment in segments)
        results = await asyncio.gather(*(self._segment(segment, target, aliases, subjects, source_catalogue, schema)
                                         for segment in segments), return_exceptions=True)
        drafts, coverage = [], []
        for segment, result in zip(segments, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, (IncompleteInspection, ValueError, KeyError, TypeError)):
                    raise result
                failure = staged(result, f"PLANNER:{segment.ref}")
                if failures is None or not is_question_level(failure.code):
                    raise failure
                failures[f"PLANNER:{segment.ref}"] = {"code": failure.code, "cause": failure.cause, "call": 1}
                coverage.append({"role": "PLANNER", "segment_ref": segment.ref, "status": "UNPROCESSED",
                                 "reason": failure.code})
                continue
            coverage.append({"role": "PLANNER", **result.segments[0].model_dump()})
            drafts.extend((question, f"PLANNER:{segment.ref}") for question in result.questions)
        return merge_questions(drafts), coverage

    async def _segment(self, segment, target, aliases, subjects, source_catalogue, schema):
        saved = self.checkpoint.stage("PLANNER", segment.ref) if self.checkpoint else None
        if saved is not None:
            batch = QuestionBatch.model_validate(saved)
        else:
            payload = {
                "segment": asdict(segment),
                "text": target[segment.context_start:segment.context_end],
                "text_start": segment.context_start,
                # GH331: segments are TSEGn; spans always name the target T1.
                "target_source_ref": TARGET_REF,
                "source_catalogue": source_catalogue,
                "subject_catalogue": subjects,
            }
            batch = await call_role(self.model, "PLANNER", payload, QuestionBatch, self.budget, runtime=self.runtime,
                                    schema=schema, schema_name=PLANNER_SCHEMA_NAME)

        def inside(start, end):
            return segment.start <= start and end <= segment.end

        # Normalized before validation and before the durable save the Backend checks; spans stay in the core.
        batch = normalize_batch(batch, target, aliases=aliases, accept=inside, stats=self.stats)
        validate_batch(batch, segment, {row["subject_ref"] for row in subjects}, target)
        if self.checkpoint and saved is None:
            await self.checkpoint.save_stage("PLANNER", segment.ref, batch.model_dump())
        self.batches[segment.ref] = batch
        return batch

    async def check_grid(self, segments):
        """After the PLANNER publications the server grid exists; its v4 segments must equal the local split."""
        grid = await self.checkpoint.setting_grid()
        if not isinstance(grid, dict) or grid.get("gridVersion") != SUPPORTED_SETTING_GRID_VERSION:
            raise IncompleteInspection("GRID_VERSION_UNSUPPORTED", stage="MERGED_PLAN")
        if grid.get("segments") != [segment_wire(segment) for segment in segments]:
            raise IncompleteInspection("GRID_SEGMENTS_MISMATCH", stage="MERGED_PLAN")
