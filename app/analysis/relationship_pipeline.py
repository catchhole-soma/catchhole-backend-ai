"""Lease-bound relation processing, isolated from character/world results.

Only provider/structured-output failures are isolated. Authority, source, quota,
transport-to-Spring and unexpected failures retain the parent job's failure path.
The Spring completion transaction owns publication, never this pipeline.
"""

import re

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.relationship_diagnostics import annotate_relationship_failure
from app.analysis.relationship_grouped_comparator import FAILURE_REASONS
from app.analysis.relationship_observations import RelationshipInputTooLargeError
from app.exceptions.failure_classification import (
    failure_diagnostics,
    is_candidate_comparison_failure,
)
from app.monitoring.worker_tracing import worker_span
from app.schemas.worker import failure_check

MAX_EPISODE_OBSERVATIONS = 384
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "APPLIED"})


class RelationshipAnalysisPipeline:
    def __init__(self, *, spring_client, payload, extractor, comparator):
        self.spring = spring_client
        self.payload = payload
        self.extractor = extractor
        self.comparator = comparator
        self.source_hash = payload.frozen_source().content_hash
        if not isinstance(self.source_hash, str) or not self.source_hash:
            raise OrderedInputContextError("Relationship analysis requires a frozen source hash.")
        self.context = None
        self.extractions = {}
        self.failure_code = None
        self.failure_exception = None
        self.failure_stage = "RELATIONSHIP_EXTRACTION"

    def _record_failure(self, code, error=None, *, stage="RELATIONSHIP_EXTRACTION", checks=()):
        # Ordinary extraction still isolates optional relationship failures; only
        # the dedicated worker re-raises this typed cause after the safe fail API.
        self.failure_code = code
        self.failure_stage = stage
        if code == "INPUT_TOO_LARGE":
            error = annotate_relationship_failure(
                error or RelationshipInputTooLargeError("Relationship input exceeds its budget."),
                stage=stage, reason="RELATIONSHIP_INPUT_TOO_LARGE",
                checks=checks or getattr(getattr(error, "failure_diagnostics", None), "checks", ()),
            )
        self.failure_exception = error

    async def load(self):
        context = await self.spring.get_relationship_analysis(
            self.payload.analysis_job_id, self.payload.lease_token,
        )
        if context is not None:
            self._accept_context(context)

    def _accept_context(self, context):
        if (not isinstance(context, dict)
                or type(context.get("formatVersion")) is not int
                or context["formatVersion"] != 1
                or context.get("sourceHash") != self.source_hash
                or type(context.get("sourceEpisodeNo")) is not int
                or context["sourceEpisodeNo"] != self.payload.episode.episode_no
                or context.get("status") not in {"PREPARED", *_TERMINAL_STATUSES}
                or (context["status"] == "PREPARED" and (
                    not isinstance(context.get("contextToken"), str) or not context["contextToken"].strip()
                    or not isinstance(context.get("observations"), list)
                ))):
            raise OrderedInputContextError("Invalid frozen relationship context.")
        contract = getattr(self.payload, "relationship_analysis_contract", None) or {}
        partial = context.get("supportsPartialResults", False)
        advertised = contract.get("supportsPartialResults", False)
        if (type(partial) is not bool or type(advertised) is not bool or partial != advertised):
            raise OrderedInputContextError("Invalid relationship partial-result capability.")
        if partial and context["status"] in {"COMPLETED", "APPLIED"}:
            self._failed_observation_count(context)
        self.context = context
        if context["status"] == "FAILED":
            # The saved context does not carry the original diagnostics. Never
            # invent an extraction/comparison cause when resuming a failed row.
            self.failure_stage = "RELATIONSHIP_RESUME"
            self.failure_exception = annotate_relationship_failure(
                ComparisonValidationError("Previously failed relationship analysis."),
                stage=self.failure_stage, reason="RELATIONSHIP_PREVIOUS_FAILURE",
            )

    @staticmethod
    def _failed_observation_count(context):
        failures = context.get("failures")
        if not isinstance(failures, list) or len(failures) > MAX_EPISODE_OBSERVATIONS:
            raise OrderedInputContextError("Invalid frozen relationship failures.")
        seen = set()
        expected = {row.get("ref") for row in context.get("observations", [])}
        for failure in failures:
            if (not isinstance(failure, dict) or failure.get("reasonCode") not in FAILURE_REASONS
                    or not isinstance(failure.get("observationRefs"), list)
                    or not failure["observationRefs"]):
                raise OrderedInputContextError("Invalid frozen relationship failures.")
            for ref in failure["observationRefs"]:
                if (not isinstance(ref, str) or re.fullmatch(r"O[1-9][0-9]*", ref) is None
                        or len(ref) > 12 or ref in seen or ref not in expected):
                    raise OrderedInputContextError("Invalid frozen relationship failures.")
                seen.add(ref)
        if len(seen) > MAX_EPISODE_OBSERVATIONS:
            raise OrderedInputContextError("Invalid frozen relationship failures.")
        return len(seen)

    def _terminal_summary(self):
        status = self.context["status"]
        if self.context.get("supportsPartialResults") is True and status in {"COMPLETED", "APPLIED"}:
            count = self._failed_observation_count(self.context)
            if count:
                return {"relationshipAnalysisStatus": "PARTIAL", "relationshipFailureCount": count,
                        "relationshipFailedGroupCount": len(self.context["failures"])}
        return {"relationshipAnalysisStatus": status}

    async def extract_chunk(self, chunk):
        if self.context is not None or self.failure_code or chunk.id in self.extractions:
            return
        try:
            self.extractions[chunk.id] = await self.extractor.extract_from_chunk(
                source_chunk_id=chunk.id, chunk_text=chunk.chunk_text,
                episode_no=self.payload.episode.episode_no,
                episode_title=self.payload.episode.title,
            )
            count = sum(len(result.observations) for result in self.extractions.values())
            if count > MAX_EPISODE_OBSERVATIONS:
                self._record_failure("INPUT_TOO_LARGE", checks=[failure_check("observationCount", "MAX", MAX_EPISODE_OBSERVATIONS, count)])
        except RelationshipInputTooLargeError as exc:
            self._record_failure("INPUT_TOO_LARGE", exc)
        except Exception as exc:
            if not is_candidate_comparison_failure(exc):
                raise
            # Do not store provider/error text, which can contain the manuscript.
            self._record_failure("EXTRACTION_FAILED", exc)

    async def finish(self, chunks):
        with worker_span("analysis.relationship.compare") as span:
            summary = await self._finish(chunks)
            span.set_attributes({
                "analysis.relationship.failed_observation_count": summary.get("relationshipFailureCount", 0),
                "analysis.relationship.failed_group_count": summary.get("relationshipFailedGroupCount", 0),
                "analysis.relationship.decision_count": summary.get("relationshipDecisionCount", 0),
            })
            if summary["relationshipAnalysisStatus"] in {"FAILED", "PARTIAL"}:
                span.set_outcome("partial" if summary["relationshipAnalysisStatus"] == "PARTIAL" else "failure", error=True)
            return summary

    async def _finish(self, chunks):
        if self.context is not None and self.context["status"] in _TERMINAL_STATUSES:
            return self._terminal_summary()
        if self.context is None:
            for chunk in chunks:
                await self.extract_chunk(chunk)
            observations = []
            rejected_count = 0
            for chunk in chunks:
                result = self.extractions.get(chunk.id)
                if result is None:
                    continue
                rejected_count += len(result.rejections)
                for observation in result.observations:
                    observations.append({
                        "ref": f"O{len(observations) + 1}",
                        "source": observation.source.model_dump(),
                        "target": observation.target.model_dump(),
                        "summary": observation.summary,
                        "evidence": [{
                            "startOffset": chunk.start_offset + span.start_offset,
                            "endOffset": chunk.start_offset + span.end_offset,
                            "quote": span.quote,
                        } for span in observation.evidence_spans],
                    })
            if len(observations) > MAX_EPISODE_OBSERVATIONS:
                self._record_failure("INPUT_TOO_LARGE", checks=[failure_check("observationCount", "MAX", MAX_EPISODE_OBSERVATIONS, len(observations))])
            if self.failure_code is not None:
                return await self._fail()
            context = await self.spring.prepare_relationship_analysis(
                self.payload.analysis_job_id, self.payload.lease_token,
                {"formatVersion": 1, "sourceHash": self.source_hash,
                 "observations": observations, "rejectedObservationCount": rejected_count},
            )
            self._accept_context(context)
        if self.context["status"] in _TERMINAL_STATUSES:
            return self._terminal_summary()
        self.failure_stage = "RELATIONSHIP_COMPARE"
        partial = self.context.get("supportsPartialResults") is True
        try:
            # Empty extraction is successful and never implies existing edges ended.
            if partial:
                outcome = await self.comparator.compare_partial(self.context)
                decisions, failures = outcome.decisions, outcome.failures
            else:
                decisions = (await self.comparator.compare(self.context)
                             if self.context["observations"] else [])
                failures = []
        except RelationshipInputTooLargeError as exc:
            self._record_failure("INPUT_TOO_LARGE", exc, stage="RELATIONSHIP_COMPARE")
            return await self._fail()
        except Exception as exc:
            if not is_candidate_comparison_failure(exc):
                raise
            self._record_failure("COMPARISON_FAILED", exc, stage="RELATIONSHIP_COMPARE")
            return await self._fail()
        completion = {"formatVersion": 1, "contextToken": self.context["contextToken"],
                      "decisions": decisions}
        if partial:
            completion["failures"] = failures
        await self.spring.complete_relationship_analysis(
            self.payload.analysis_job_id, self.payload.lease_token,
            completion,
        )
        summary = {"relationshipAnalysisStatus": "PARTIAL" if failures else "COMPLETED",
                   "relationshipObservationCount": len(self.context["observations"]),
                   "relationshipDecisionCount": len(decisions)}
        if partial:
            summary["relationshipFailureCount"] = sum(len(row["observationRefs"]) for row in failures)
            summary["relationshipFailedGroupCount"] = len(failures)
        return summary

    async def _fail(self):
        if self.failure_code == "INPUT_TOO_LARGE" and self.failure_exception is None:
            self._record_failure("INPUT_TOO_LARGE", stage=self.failure_stage)
        diagnostic = (failure_diagnostics(self.failure_exception, stage=self.failure_stage)
                      if self.failure_exception is not None else None)
        await self.spring.fail_relationship_analysis(
            self.payload.analysis_job_id, self.payload.lease_token,
            {"formatVersion": 1, "sourceHash": self.source_hash,
             "contextToken": self.context.get("contextToken") if self.context else None,
             "failureCode": self.failure_code,
             **({"failureDiagnostics": diagnostic.model_dump(mode="json", by_alias=True, exclude_none=True)}
                if diagnostic is not None else {})},
        )
        return {"relationshipAnalysisStatus": "FAILED"}
