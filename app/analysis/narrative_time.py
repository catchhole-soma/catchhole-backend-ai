"""Located narrative observations and their request-local prompt projection."""

from copy import deepcopy
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NarrativeTimeEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, alias="startOffset", ge=0)
    end_offset: int | None = Field(default=None, alias="endOffset", ge=0)
    source_chunk_id: UUID | None = Field(default=None, alias="sourceChunkId")


class NarrativeTimePoint(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    description: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_spans: list[NarrativeTimeEvidence] = Field(default_factory=list, alias="evidenceSpans")
    context_ref: str | None = Field(default=None, alias="contextRef", pattern=r"^T[1-9][0-9]*$")

    def storage_data(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude={"context_ref"})

    def located(self) -> bool:
        return bool(self.description and self.evidence_spans) and all(
            span.source_chunk_id is not None and span.start_offset is not None
            and span.end_offset is not None and span.end_offset > span.start_offset
            for span in self.evidence_spans
        )


class NarrativeTime(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    fact_time: NarrativeTimePoint | None = Field(default=None, alias="factTime")
    reference_time: NarrativeTimePoint | None = Field(default=None, alias="referenceTime")

    def storage_data(self) -> dict:
        return {"factTime": None if self.fact_time is None else self.fact_time.storage_data(),
                "referenceTime": None if self.reference_time is None else self.reference_time.storage_data()}


def ground_extraction_narrative_time(result, *, chunk, time_context=()):
    """Resolve T refs before value-evidence sorting; prior spans keep their own chunk."""
    from app.analysis.evidence_span_resolver import resolve_evidence_span_offsets
    from app.analysis.schemas import ExtractedEvidenceSpan

    def ground(point):
        if point is None:
            return None
        if point.context_ref is not None:
            index = int(point.context_ref[1:]) - 1
            if index >= len(time_context) or not time_context[index].located():
                raise ValueError("NARRATIVE_TIME_CONTEXT_REF_INVALID")
            return time_context[index].model_copy(deep=True)
        if not point.description or not point.evidence_spans:
            return None
        spans = []
        for span in point.evidence_spans:
            located_ranges = set()
            if span.start_offset is not None and span.end_offset is not None:
                for base in (chunk.start_offset, 0):
                    start, end = span.start_offset - base, span.end_offset - base
                    if 0 <= start < end <= len(chunk.chunk_text) and chunk.chunk_text[start:end] == span.quote:
                        located_ranges.add((start + chunk.start_offset, end + chunk.start_offset))
            if len(located_ranges) == 1:
                start, end = located_ranges.pop()
                resolved = ExtractedEvidenceSpan(quote=span.quote, start_offset=start, end_offset=end)
            else:
                resolved = resolve_evidence_span_offsets(
                    ExtractedEvidenceSpan(quote=span.quote), chunk.chunk_text, chunk.start_offset,
                )
            # Persist only located points; Java's frozen capture requires exact spans.
            if resolved.start_offset is None or resolved.end_offset is None:
                return None
            spans.append(NarrativeTimeEvidence(**resolved.model_dump(), source_chunk_id=chunk.id))
        return NarrativeTimePoint(description=point.description, evidence_spans=spans)

    candidates = []
    for candidate in result.candidates:
        time = candidate.narrative_time
        candidates.append(candidate.model_copy(update={"narrative_time": None if time is None else NarrativeTime(
            fact_time=ground(time.fact_time), reference_time=ground(time.reference_time),
        )}))
    contexts = [ground(point) for point in result.time_context]
    return result.model_copy(update={"candidates": candidates, "time_context": [point for point in contexts if point and point.located()]})


def time_context_prompt_data(time_context) -> list[dict]:
    return narrative_prompt_data([{"ref": f"T{index}", **point.storage_data()}
                                 for index, point in enumerate(time_context, 1)])


def narrative_prompt_data(value: Any) -> Any:
    """Keep all semantic/evidence rows; replace backend identities with local refs."""
    identities: dict[tuple[str, str], str] = {}
    prefixes = {"sourceCandidateId": "S", "sourceAnalysisJobId": "J", "sourceChunkId": "T", "factId": "H"}
    omitted = {"storageBinding", "contentHash", "sourceHash", "inputStateHash", "projectionStateHash", "contextToken",
               "episodeId", "settingBookId", "revisionId", "workId", "characterId", "provisionalSubjectKey", "selectionNames"}

    def project(item):
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json", by_alias=True)
        if isinstance(item, list):
            return [project(row) for row in item]
        if not isinstance(item, dict):
            return item
        projected = {}
        for key, child in item.items():
            if key in omitted:
                continue
            if key in prefixes:
                if child is not None:
                    identity = (key, str(child))
                    if identity not in identities:
                        identities[identity] = prefixes[key] + str(1 + sum(existing[0] == key for existing in identities))
                    projected[{"factId": "historyRef", "sourceChunkId": "sourceChunkRef",
                               "sourceCandidateId": "sourceRef", "sourceAnalysisJobId": "sourceJobRef"}[key]] = identities[identity]
                continue
            # Future backend identity fields must never silently reach the model.
            if key.endswith("Id") or key.endswith("_id"):
                continue
            projected[key] = project(child)
        return projected

    return project(value)


def validate_narrative_time_choice(decision, candidate) -> None:
    if not decision.advance_current_narrative_time:
        return
    time = candidate.narrative_time
    if (str(decision.temporal_scope) != "PRESENT" or str(decision.operation) == "REVIEW_REQUIRED"
            or time is None or time.reference_time is None or not time.reference_time.located()):
        raise ValueError("NARRATIVE_TIME_ADVANCE_REQUIRES_LOCATED_PRESENT_OBSERVATION")


def project_advisory_narrative_time(context, candidate, decision, *, fact_type=None, previous_entries=()):
    if context is None and candidate.narrative_time is None:
        return context
    projected = deepcopy(context) if context is not None else {}
    if decision.advance_current_narrative_time:
        projected["currentNarrativeTime"] = {
            **candidate.narrative_time.reference_time.storage_data(),
            "sourceCandidateRef": candidate.candidate_ref,
        }
    operation = str(decision.operation)
    if operation == "REVIEW_REQUIRED":
        return projected
    slots = projected.setdefault("currentSlots", [])
    history = projected.setdefault("historyFacts", [])
    by_ref = {entry.reference: entry for entry in previous_entries}
    ended = [by_ref[ref] for ref in decision.removed_snapshot_refs if ref in by_ref]
    if operation == "UPDATE" and decision.target_ref in by_ref:
        ended.append(by_ref[decision.target_ref])
    for entry in ended:
        prior = next((row for row in slots if row.get("factType") == entry.fact_type and row.get("factKey") == entry.fact_key), None)
        history.append({"factType": entry.fact_type, "factKey": entry.fact_key,
                        "factValue": entry.fact_value, "valueJson": entry.value_json,
                        "operation": "ENDED_BY_PRIOR_DECISION", "endedByCandidateRef": candidate.candidate_ref,
                        "supportingSources": [] if prior is None else prior.get("supportingSources", [])})
        slots[:] = [row for row in slots if not (row.get("factType") == entry.fact_type and row.get("factKey") == entry.fact_key)]
    time = None if candidate.narrative_time is None else candidate.narrative_time.storage_data()
    source = {"sourceCandidateRef": candidate.candidate_ref, "quoteRelation": "SOURCE_CONTEXT_ONLY",
              "source": None if candidate.source is None else candidate.source.prompt_data(),
              "evidenceSpans": [span.model_dump(mode="json", by_alias=True) for span in candidate.evidence_spans],
              "narrativeTime": time}
    if operation in {"ADD", "UPDATE", "MERGE"}:
        prior = next((row for row in slots if row.get("factType") == fact_type and row.get("factKey") == decision.resolved_canonical_fact_key), None)
        supporting = [*(prior.get("supportingSources", []) if prior and operation == "MERGE" else []), source]
        slots[:] = [row for row in slots if not (row.get("factType") == fact_type and row.get("factKey") == decision.resolved_canonical_fact_key)]
        slots.append({"factType": fact_type, "factKey": decision.resolved_canonical_fact_key,
                      "factValue": decision.proposed_fact_value, "valueJson": decision.proposed_value_json,
                      "supportingSources": supporting})
    elif operation in {"REMOVE", "HISTORY_ONLY"}:
        history.append({"factType": fact_type, "factKey": decision.resolved_canonical_fact_key,
                        "factValue": candidate.attribute_value, "valueJson": candidate.value_json,
                        "operation": operation, "temporalScope": str(decision.temporal_scope),
                        "sourceCandidateRef": candidate.candidate_ref, "narrativeTime": time,
                        "supportingSource": source})
    return projected
