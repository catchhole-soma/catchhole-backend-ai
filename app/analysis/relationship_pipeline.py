"""Lease-bound relation processing, isolated from character/world results.

Only provider/structured-output failures are isolated. Authority, source, quota,
transport-to-Spring and unexpected failures retain the parent job's failure path.
The Spring completion transaction owns publication, never this pipeline.
"""

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.relationship_observations import RelationshipInputTooLargeError
from app.exceptions.failure_classification import is_candidate_comparison_failure

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
        self.context = context

    async def extract_chunk(self, chunk):
        if self.context is not None or self.failure_code or chunk.id in self.extractions:
            return
        try:
            self.extractions[chunk.id] = await self.extractor.extract_from_chunk(
                source_chunk_id=chunk.id, chunk_text=chunk.chunk_text,
                episode_no=self.payload.episode.episode_no,
                episode_title=self.payload.episode.title,
            )
            if sum(len(result.observations) for result in self.extractions.values()) > MAX_EPISODE_OBSERVATIONS:
                self.failure_code = "INPUT_TOO_LARGE"
        except RelationshipInputTooLargeError:
            self.failure_code = "INPUT_TOO_LARGE"
        except Exception as exc:
            if not is_candidate_comparison_failure(exc):
                raise
            # Do not store provider/error text, which can contain the manuscript.
            self.failure_code = "EXTRACTION_FAILED"

    async def finish(self, chunks):
        if self.context is not None and self.context["status"] in _TERMINAL_STATUSES:
            return {"relationshipAnalysisStatus": self.context["status"]}
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
                self.failure_code = "INPUT_TOO_LARGE"
            if self.failure_code is not None:
                return await self._fail()
            context = await self.spring.prepare_relationship_analysis(
                self.payload.analysis_job_id, self.payload.lease_token,
                {"formatVersion": 1, "sourceHash": self.source_hash,
                 "observations": observations, "rejectedObservationCount": rejected_count},
            )
            self._accept_context(context)
        if self.context["status"] in _TERMINAL_STATUSES:
            return {"relationshipAnalysisStatus": self.context["status"]}
        try:
            # Empty extraction is successful and never implies existing edges ended.
            decisions = (await self.comparator.compare(self.context)
                         if self.context["observations"] else [])
        except RelationshipInputTooLargeError:
            self.failure_code = "INPUT_TOO_LARGE"
            return await self._fail()
        except Exception as exc:
            if not is_candidate_comparison_failure(exc):
                raise
            self.failure_code = "COMPARISON_FAILED"
            return await self._fail()
        await self.spring.complete_relationship_analysis(
            self.payload.analysis_job_id, self.payload.lease_token,
            {"formatVersion": 1, "contextToken": self.context["contextToken"],
             "decisions": decisions},
        )
        return {"relationshipAnalysisStatus": "COMPLETED",
                "relationshipObservationCount": len(self.context["observations"]),
                "relationshipDecisionCount": len(decisions)}

    async def _fail(self):
        await self.spring.fail_relationship_analysis(
            self.payload.analysis_job_id, self.payload.lease_token,
            {"formatVersion": 1, "sourceHash": self.source_hash,
             "contextToken": self.context.get("contextToken") if self.context else None,
             "failureCode": self.failure_code},
        )
        return {"relationshipAnalysisStatus": "FAILED"}
