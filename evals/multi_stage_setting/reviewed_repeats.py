"""Hash-bound, independently reviewed character repeats: a scoring view only."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from evals.multi_stage_setting.character_semantics import character_fact_key_spelling_matches
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage1Prediction,
    CharacterStage2Prediction,
    GoldSnapshotV3,
    PredictionBundleV3,
    ScenarioPrediction,
    StrictModel,
)
from evals.multi_stage_setting.matching import Stage1MatchingResult


class SourceChunk(StrictModel):
    chunk_index: int = Field(ge=0, strict=True)
    start_offset: int = Field(ge=0, strict=True)
    end_offset: int = Field(gt=0, strict=True)


class ReviewedRepeatGroup(StrictModel):
    candidate_ids: list[str] = Field(min_length=2)
    continuity_verified: Literal[True]


class CharacterRepeatProvenance(StrictModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_prediction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunks: list[SourceChunk] = Field(min_length=1)
    repeat_groups: list[ReviewedRepeatGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reviewed_groups(self) -> CharacterRepeatProvenance:
        ids = [candidate_id for group in self.repeat_groups for candidate_id in group.candidate_ids]
        if any(not candidate_id.strip() for candidate_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("Reviewed repeat candidate IDs must be nonblank and unique.")
        chunks = sorted(self.chunks, key=lambda chunk: chunk.start_offset)
        if len({chunk.chunk_index for chunk in chunks}) != len(chunks):
            raise ValueError("Repeat provenance chunk indices must be unique.")
        for index, chunk in enumerate(chunks):
            if chunk.start_offset >= chunk.end_offset:
                raise ValueError("Repeat provenance chunk bounds are empty or reversed.")
            if index and chunks[index - 1].end_offset > chunk.start_offset:
                raise ValueError("Repeat provenance chunks overlap.")
            if index and chunks[index - 1].chunk_index >= chunk.chunk_index:
                raise ValueError("Repeat provenance chunk indices contradict source order.")
        return self


class CharacterRepeatProvenanceBundle(StrictModel):
    scenarios: dict[str, CharacterRepeatProvenance]


def scenario_prediction_sha256(prediction: ScenarioPrediction) -> str:
    """Public helper for a sidecar pinned to the complete original scenario payload."""

    return hashlib.sha256(json.dumps(
        prediction.model_dump(mode="json", by_alias=True),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def validate_repeat_provenance(
    provenance: dict[str, Any] | CharacterRepeatProvenanceBundle,
    gold: GoldSnapshotV3,
    predictions: PredictionBundleV3,
) -> CharacterRepeatProvenanceBundle:
    result = CharacterRepeatProvenanceBundle.model_validate(provenance)
    scenarios = {scenario.scenario_id: scenario for scenario in gold.scenarios}
    predicted = {scenario.scenario_id: scenario for scenario in predictions.scenarios}
    for scenario_id, proof in result.scenarios.items():
        if scenario_id not in scenarios or scenario_id not in predicted:
            raise ValueError("Repeat provenance references an unknown scenario.")
        source = scenarios[scenario_id].source_text
        if source is None:
            raise ValueError("Repeat provenance requires the original source text for hash validation.")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != proof.source_sha256:
            raise ValueError("Repeat provenance source hash mismatch.")
        if scenario_prediction_sha256(predicted[scenario_id]) != proof.scenario_prediction_sha256:
            raise ValueError("Repeat provenance prediction hash mismatch.")
        if any(chunk.end_offset > len(source) for chunk in proof.chunks):
            raise ValueError("Repeat provenance chunk bounds exceed the original source.")
        candidate_ids = {candidate.candidate_id for candidate in predicted[scenario_id].stage1}
        if any(candidate_id not in candidate_ids for group in proof.repeat_groups
               for candidate_id in group.candidate_ids):
            raise ValueError("Repeat provenance references an unknown handoff candidate.")
    return result


def reviewed_repeat_diagnostics(
    proof: CharacterRepeatProvenance,
    source_text: str,
    prediction: ScenarioPrediction,
    gold_rows: list[CharacterStage1Gold],
    matching: Stage1MatchingResult,
) -> list[dict[str, Any]]:
    sources = {source.candidate_id: source for source in prediction.stage1}
    decisions = {decision.source_candidate_id: decision for decision in prediction.stage2
                 if isinstance(decision, CharacterStage2Prediction)}
    chunks = {chunk.chunk_index: chunk for chunk in proof.chunks}
    diagnostics = []
    for reviewed in proof.repeat_groups:
        members = [sources[candidate_id] for candidate_id in reviewed.candidate_ids]
        members.sort(key=lambda source: (source.sort_order, source.candidate_id))
        ids = {source.candidate_id for source in members}
        first = members[0]
        reason = None
        gold_ids: list[str] = []
        if any(not isinstance(source, CharacterStage1Prediction)
               or source.candidate_kind != "SETTING" or source.fact_type != "PROFILE"
               or source.match_status != "MATCHED" or not source.entity_ref
               or source.value_type in {None, "UNKNOWN"} or source.value_json is None
               for source in members):
            reason = "NOT_RESOLVED_PROFILE"
        elif any(_fact_payload(source) != _fact_payload(first) for source in members):
            reason = "FACT_PAYLOAD_MISMATCH"
        elif any(source.candidate_id not in decisions
                 or decisions[source.candidate_id].temporal_scope != "PRESENT"
                 for source in members):
            reason = "TEMPORAL_SCOPE_NOT_PRESENT"
        else:
            origins = [_verified_chunk(source, source_text, chunks) for source in members]
            if None in origins:
                reason = "UNVERIFIED_CHUNK_EVIDENCE"
            elif len(set(origins)) != len(origins):
                reason = "SAME_CHUNK_DUPLICATE"
            else:
                slot_gold = [row for row in gold_rows if row.decision == "EXTRACT"
                             and row.entity_ref == first.entity_ref and row.fact_type == "PROFILE"
                             and any(character_fact_key_spelling_matches(key, first.fact_key)
                                     for key in row.accepted_fact_keys)]
                matches = [match for match in matching.matches
                           if match.prediction.candidate_id in ids]
                if (len(slot_gold) != 1 or len(matches) != 1
                        or matches[0].identity_matched is not True
                        or matches[0].gold.gold_id != slot_gold[0].gold_id
                        or "CURRENT" not in slot_gold[0].context_tags
                        or not set(slot_gold[0].context_tags) <= {"CURRENT", "REPEAT"}):
                    reason = "GOLD_FACT_NOT_UNIQUE_CURRENT"
                elif any(
                    isinstance(source, CharacterStage1Prediction)
                    and source.candidate_kind == "SETTING"
                    and source.entity_ref == first.entity_ref
                    and source.fact_type == first.fact_type
                    and character_fact_key_spelling_matches(source.fact_key, first.fact_key)
                    and first.sort_order <= source.sort_order <= members[-1].sort_order
                    and source.candidate_id not in ids
                    and (_fact_payload(source) != _fact_payload(first)
                         or source.match_status != "MATCHED"
                         or _verified_chunk(source, source_text, chunks) is None
                         or source.candidate_id not in decisions
                         or decisions[source.candidate_id].temporal_scope != "PRESENT")
                    for source in prediction.stage1
                ):
                    reason = "INTERVENING_SLOT_CHANGE_OR_UNKNOWN"
                else:
                    gold_ids = list(matches[0].source_gold_ids)
        diagnostics.append({
            "candidateIds": [source.candidate_id for source in members],
            "representativePredictionId": first.candidate_id if reason is None else None,
            "goldIds": gold_ids,
            "status": "GROUPED" if reason is None else "NOT_GROUPED",
            "reason": reason,
        })
    return diagnostics


def _fact_payload(source: CharacterStage1Prediction) -> tuple:
    return (source.entity_ref, source.fact_type, source.fact_key, source.value_type,
            source.display_value,
            json.dumps(source.value_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _verified_chunk(
    source: CharacterStage1Prediction,
    source_text: str,
    chunks: dict[int, SourceChunk],
) -> int | None:
    # Only the saved runtime adapter's documented encoding is accepted. Bounds come
    # from its reviewed saved plan, never from re-running today's chunk splitter.
    chunk_index, position = divmod(source.sort_order, 1_000_000)
    chunk = chunks.get(chunk_index)
    if chunk is None or position == 0 or not source.evidence_spans:
        return None
    for span in source.evidence_spans:
        if (span.start_offset is None or span.end_offset is None
                or not chunk.start_offset <= span.start_offset < span.end_offset <= chunk.end_offset
                or source_text[span.start_offset:span.end_offset] != span.quote):
            return None
    return chunk_index
