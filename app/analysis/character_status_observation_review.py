"""Complete STATUS observations against current source before candidate persistence."""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import tiktoken
from pydantic import BaseModel, ConfigDict, Field

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.exceptions import LlmExtractionError
from app.analysis.json_response import request_validated_model, safe_validation_error_summary
from app.analysis.schemas import (
    ExtractedCharacterSettingCandidate,
    ExtractedEvidenceSpan,
    ExtractedSettingCandidate,
)
from app.analysis.status_evidence import StatusEvidenceDocument
from app.domain.setting_values import normalize_setting_display_value
from app.llm.protocols import LlmResponseSchema, TextGenerationClient

PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm/prompts/character_status_observation_review.md"
STATUS_OBSERVATION_REVIEW_CACHE_KEY = "status-observation-review:v9"
_REVIEW_VOTE_COUNT = 3
logger = logging.getLogger(__name__)


class _ReviewError(ValueError):
    """Only fixed application error codes may be constructed here."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _DraftReview(_StrictModel):
    draft_ref: str
    classification: Literal["STATE_OBSERVATION", "ONE_OFF_TREATMENT_PROCESS"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str]


class _EndObservation(_StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1)


class _TargetReview(_StrictModel):
    target_ref: str
    end_state: Literal["CONTINUES", "ENDED", "UNKNOWN"]
    reason: str = Field(min_length=1, max_length=500)
    end_observations: list[_EndObservation]


class _ReviewResponse(_StrictModel):
    draft_reviews: list[_DraftReview]
    target_reviews: list[_TargetReview]


@dataclass
class _Target:
    target_ref: str
    entity_name: str
    fact_key: str
    value_type: str | None
    active_at_episode_start: bool
    active_fact_value: str | None
    draft_refs: list[str]
    prior_observation_refs: list[str] = field(default_factory=list)
    prior_active: bool | None = None
    raw_entity_mention: str | None = None
    _prior_value_json: dict[str, Any] | None = None
    _producer_group: tuple[UUID, int] | None = None


def _is_noncurrent(candidate: ExtractedSettingCandidate) -> bool:
    return candidate._status_observation_kind in {"PAST", "HYPOTHETICAL"}


def _validated_producer_group(
    candidate: ExtractedSettingCandidate,
    source_chunk_id: UUID,
    identities: dict[tuple[UUID, int], tuple[str, str | None, str, str]],
) -> tuple[UUID, int] | None:
    group = candidate._status_observation_group
    if group is None:
        return None
    if (type(group) is not tuple or len(group) != 2 or type(group[0]) is not UUID
            or type(group[1]) is not int or group[1] < 0):
        raise _ReviewError("STATUS_REVIEW_INVALID_PRODUCER_GROUP")
    if group[0] != source_chunk_id:
        raise _ReviewError("STATUS_REVIEW_PRODUCER_GROUP_SOURCE_MISMATCH")
    identity = (candidate.entity_name, candidate.raw_entity_mention,
                candidate.attribute_name, candidate.value_type)
    if identities.setdefault(group, identity) != identity:
        raise _ReviewError("STATUS_REVIEW_PRODUCER_GROUP_IDENTITY_CONFLICT")
    return group


def _prepare(
    candidates: list[ExtractedSettingCandidate],
    known_characters: tuple[KnownCharacter, ...],
    source_chunk_id: UUID,
    document: StatusEvidenceDocument,
    prior_status_observations: tuple[ExtractedSettingCandidate, ...],
    status_value_types: dict[str, str] | None,
) -> tuple[list[_Target], dict[str, ExtractedSettingCandidate], list[dict[str, Any]], list[dict[str, Any]]]:
    targets: dict[tuple[str, str, str | None], _Target] = {}
    name_counts = Counter(character.name for character in known_characters)
    trusted_names = {name for name, count in name_counts.items() if count == 1}
    # The worker supplies only independently resolved, unambiguous prior subjects.
    trusted_names.update(observation.entity_name for observation in prior_status_observations
                         if not _is_noncurrent(observation))

    def target_for(name: str, key: str, scope: str | None = None) -> _Target:
        identity = (name, key, scope)
        if identity not in targets:
            value_type = ("JSON" if status_value_types is None else
                          status_value_types.get(key, status_value_types.get("status.*")))
            targets[identity] = _Target(f"T{len(targets) + 1}", name, key, value_type, False, None, [])
        return targets[identity]

    for index, character in enumerate(known_characters, 1):
        for status in character.active_statuses:
            if not status.fact_key.startswith("status."):
                raise _ReviewError("STATUS_REVIEW_INVALID_ACTIVE_TARGET")
            target = target_for(character.name, status.fact_key,
                                None if name_counts[character.name] == 1 else f"K{index}")
            target.active_at_episode_start = True
            target.active_fact_value = status.fact_value
    prior_payloads = []
    for index, observation in enumerate(prior_status_observations, 1):
        if (observation.candidate_kind != "SETTING"
                or not (observation.attribute_name or "").startswith("status.")):
            raise _ReviewError("STATUS_REVIEW_INVALID_PRIOR_OBSERVATION")
        if _is_noncurrent(observation):
            continue
        target = target_for(observation.entity_name, observation.attribute_name)
        if status_value_types is None:
            target.value_type = observation.value_type
        target._prior_value_json = observation.value_json
        if observation.raw_entity_mention is not None:
            target.raw_entity_mention = observation.raw_entity_mention
        ref = f"H{index}"
        target.prior_observation_refs.append(ref)
        active = observation.value_json.get("active")
        if type(active) is bool:
            target.prior_active = active
        prior_payloads.append({
            "observation_ref": ref,
            "target_ref": target.target_ref,
            "source_order": index,
            "entity_name": observation.entity_name,
            "raw_entity_mention": observation.raw_entity_mention,
            "attribute_name": observation.attribute_name,
            "attribute_value": observation.attribute_value,
            "value_json": observation.value_json,
            "evidence_spans": [span.model_dump(mode="json") for span in observation.evidence_spans],
        })
    evidence_refs = {
        (unit.start_offset, unit.end_offset, unit.text): unit.ref for unit in document.units
    }
    drafts: dict[str, ExtractedSettingCandidate] = {}
    payloads: list[dict[str, Any]] = []
    producer_identities: dict[tuple[UUID, int], tuple[str, str | None, str, str]] = {}
    for index, candidate in enumerate(candidates, 1):
        if (candidate.candidate_kind != "SETTING"
                or not (candidate.attribute_name or "").startswith("status.")
                or candidate.source_chunk_id != source_chunk_id):
            raise _ReviewError("STATUS_REVIEW_INVALID_DRAFT")
        producer_group = _validated_producer_group(candidate, source_chunk_id, producer_identities)
        refs = []
        for span in candidate.evidence_spans:
            ref = evidence_refs.get((span.start_offset, span.end_offset, span.quote))
            if ref is None:
                raise _ReviewError("STATUS_REVIEW_DRAFT_EVIDENCE_OUTSIDE_DOCUMENT")
            refs.append(ref)
        draft_ref = f"D{index}"
        target = None
        if not _is_noncurrent(candidate):
            trusted_subject = candidate.entity_name in trusted_names
            scope = None if trusted_subject else (
                f"G{producer_group[1]}" if producer_group is not None else draft_ref
            )
            target = target_for(candidate.entity_name, candidate.attribute_name, scope)
            if not trusted_subject:
                target._producer_group = producer_group
            if target.draft_refs and target.value_type != candidate.value_type:
                raise _ReviewError("STATUS_REVIEW_TARGET_TYPE_CONFLICT")
            if status_value_types is None:
                target.value_type = candidate.value_type
            if target.raw_entity_mention is None:
                target.raw_entity_mention = candidate.raw_entity_mention
            target.draft_refs.append(draft_ref)
        drafts[draft_ref] = candidate
        payloads.append({
            "draft_ref": draft_ref,
            "target_ref": target.target_ref if target is not None else None,
            "observation_kind": candidate._status_observation_kind,
            "entity_name": candidate.entity_name,
            "raw_entity_mention": candidate.raw_entity_mention,
            "attribute_name": candidate.attribute_name,
            "attribute_value": candidate.attribute_value,
            "value_json": candidate.value_json,
            "evidence_refs": refs,
        })
    return list(targets.values()), drafts, payloads, prior_payloads


def _preserved_state_refs(
    targets: list[_Target], drafts: dict[str, ExtractedSettingCandidate],
) -> set[str]:
    """Keep observations of an established state outside process classification."""
    preserved: set[str] = set()
    for target in targets:
        observations = [(ref, drafts[ref]) for ref in target.draft_refs]
        group_ends: dict[tuple[UUID, int], list[int]] = {}
        for _, candidate in observations:
            group = candidate._status_observation_group
            if (not _is_noncurrent(candidate) and group is not None
                    and candidate.value_json.get("active") is False):
                group_ends.setdefault(group, []).append(
                    min(span.start_offset for span in candidate.evidence_spans)
                )
        for ref, candidate in observations:
            if _is_noncurrent(candidate) or candidate.value_json.get("active") is False:
                continue
            # Only already resolved target links or a validated producer group
            # establish continuity; a matching name/key alone is insufficient.
            established = target.active_at_episode_start or target.prior_active is True
            before_group_end = any(
                _anchor(candidate) < end_start
                for end_start in group_ends.get(candidate._status_observation_group, ())
            )
            if established or before_group_end:
                preserved.add(ref)
    return preserved


def _editable_drafts(
    drafts: dict[str, ExtractedSettingCandidate], targets: list[_Target],
) -> dict[str, ExtractedSettingCandidate]:
    preserved_state = _preserved_state_refs(targets, drafts)
    return {ref: candidate for ref, candidate in drafts.items()
            if not _is_noncurrent(candidate) and candidate.value_json.get("active") is not False
            and ref not in preserved_state}


def _response_schema(targets: list[_Target], drafts: dict[str, ExtractedSettingCandidate],
                     document: StatusEvidenceDocument) -> LlmResponseSchema:
    editable = _editable_drafts(drafts, targets)
    schema = _ReviewResponse.model_json_schema()
    definitions = schema["$defs"]
    definitions["CurrentEvidenceRef"] = {"type": "string", "enum": list(document.references)}
    definitions["TargetRef"] = {"type": "string", "enum": [t.target_ref for t in targets]}
    definitions["_TargetReview"]["properties"]["target_ref"] = {"$ref": "#/$defs/TargetRef"}
    if editable:
        definitions["DraftRef"] = {"type": "string", "enum": list(editable)}
        definitions["_DraftReview"]["properties"]["draft_ref"] = {"$ref": "#/$defs/DraftRef"}
    for definition in ("_DraftReview", "_EndObservation"):
        definitions[definition]["properties"]["evidence_refs"]["items"] = {
            "$ref": "#/$defs/CurrentEvidenceRef"
        }
    for field_name, size in (("draft_reviews", len(editable)), ("target_reviews", len(targets))):
        schema["properties"][field_name].update(minItems=size, maxItems=size)
    return LlmResponseSchema(name="character_status_observation_review", schema=schema, strict=True)


class _SchemaClient:
    def __init__(self, client: TextGenerationClient, schema: LlmResponseSchema,
                 max_input_tokens: int) -> None:
        self.client, self.schema, self.max_input_tokens = client, schema, max_input_tokens

    async def create_text_response(self, **kwargs):
        text = kwargs["system_prompt"] + kwargs["user_prompt"] + json.dumps(self.schema.schema)
        try:
            tokens = len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))
        except (ValueError, OSError):
            tokens = len(text.encode("utf-8"))
        if int(tokens * 1.1) + 256 > self.max_input_tokens:
            raise LlmExtractionError("STATUS_REVIEW_INPUT_LIMIT_EXCEEDED")
        return await self.client.create_text_response(**kwargs, response_schema=self.schema)


async def review_status_observations(
    *,
    llm_client: TextGenerationClient,
    model: str,
    max_output_tokens: int,
    truncation_retry_max_output_tokens: int,
    max_attempts: int,
    source_chunk_id: UUID,
    chunk_text: str,
    evidence_document: StatusEvidenceDocument,
    status_candidates: list[ExtractedSettingCandidate],
    known_characters: tuple[KnownCharacter, ...],
    narrative_context: dict[str, str | None] | None = None,
    max_input_tokens: int = 64000,
    prior_status_observations: tuple[ExtractedSettingCandidate, ...] = (),
    status_value_types: dict[str, str] | None = None,
) -> list[ExtractedSettingCandidate]:
    if min(max_output_tokens, truncation_retry_max_output_tokens, max_attempts, max_input_tokens) < 1:
        raise _ReviewError("STATUS_REVIEW_INVALID_LIMITS")
    if chunk_text != evidence_document.chunk_text:
        raise _ReviewError("STATUS_REVIEW_DOCUMENT_MISMATCH")
    targets, drafts, draft_payloads, prior_payloads = _prepare(
        status_candidates, known_characters, source_chunk_id, evidence_document,
        prior_status_observations, status_value_types,
    )
    if not targets or not evidence_document.references:
        return list(status_candidates)
    editable = _editable_drafts(drafts, targets)
    preserved_state = _preserved_state_refs(targets, drafts)
    payload = {
        "targets": [{key: value for key, value in vars(target).items() if not key.startswith("_")}
                    for target in targets],
        "draft_observations": [row for row in draft_payloads if row["draft_ref"] in editable],
        "preserved_end_observations": [row for row in draft_payloads
                                       if drafts[row["draft_ref"]].value_json.get("active") is False
                                       and not _is_noncurrent(drafts[row["draft_ref"]])],
        "preserved_state_observations": [row for row in draft_payloads
                                         if row["draft_ref"] in preserved_state],
        "preserved_noncurrent_observations": [row for row in draft_payloads
                                              if _is_noncurrent(drafts[row["draft_ref"]])],
        "prior_observations": prior_payloads,
        "current_evidence": json.loads(evidence_document.render()),
        "narrative_context": narrative_context,
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    def apply(response: _ReviewResponse) -> list[ExtractedSettingCandidate]:
        return _apply(response, targets, drafts, evidence_document, source_chunk_id)

    def feedback(original: str, error: Exception) -> str:
        data = json.loads(original)
        data["validation_feedback"] = {
            "reason_code": str(error) if isinstance(error, _ReviewError)
            else safe_validation_error_summary(error),
            "correction": "모든 T와 draft_observations의 D만 정확히 한 번씩 검토하세요. "
                          "preserved_end_observations, preserved_state_observations, "
                          "preserved_noncurrent_observations의 D는 "
                          "분류하지 않고 현재 E 근거의 후속 종료만 반환하세요.",
        }
        return json.dumps(data, ensure_ascii=False)

    client = _SchemaClient(llm_client, _response_schema(targets, drafts, evidence_document), max_input_tokens)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    votes = []
    for _ in range(_REVIEW_VOTE_COUNT):
        # Every vote starts from the same input/cap. Only malformed output is retried
        # within that vote; no previous vote or retry feedback crosses this boundary.
        votes.append(await request_validated_model(
            client=client,
            response_model=_ReviewResponse,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            max_attempts=max_attempts,
            prompt_cache_key=STATUS_OBSERVATION_REVIEW_CACHE_KEY,
            operation_name="Character STATUS observation extraction",
            logger=logger,
            validate_model=apply,
            retry_user_prompt_builder=feedback,
            truncation_retry_max_output_tokens=truncation_retry_max_output_tokens,
        ))
    try:
        # Separate valid votes can jointly choose incompatible deletions/endings.
        # Reject that chunk rather than drawing extra votes or dropping constraints.
        return apply(_majority_review(votes, targets, drafts, evidence_document))
    except _ReviewError as exc:
        raise LlmExtractionError(f"STATUS_REVIEW_CONSENSUS_INVALID: {exc}") from None


def _majority_review(
    votes: list[_ReviewResponse], targets: list[_Target],
    drafts: dict[str, ExtractedSettingCandidate], document: StatusEvidenceDocument,
) -> _ReviewResponse:
    if len(votes) != _REVIEW_VOTE_COUNT:
        raise _ReviewError("STATUS_REVIEW_INCOMPLETE_VOTES")
    drafts_by_vote = [{row.draft_ref: row for row in vote.draft_reviews} for vote in votes]
    targets_by_vote = [{row.target_ref: row for row in vote.target_reviews} for vote in votes]
    targets_by_ref = {target.target_ref: target for target in targets}
    draft_reviews = []
    for ref in drafts_by_vote[0]:
        rows = [vote[ref] for vote in drafts_by_vote]
        treatment_votes = sum(row.classification == "ONE_OFF_TREATMENT_PROCESS" for row in rows)
        classification = "ONE_OFF_TREATMENT_PROCESS" if treatment_votes >= 2 else "STATE_OBSERVATION"
        draft_reviews.append(next(row for row in rows if row.classification == classification))
    target_reviews = []
    for ref in targets_by_vote[0]:
        rows = [vote[ref] for vote in targets_by_vote]
        if sum(row.end_state == "ENDED" for row in rows) >= 2:
            endings = [row for row in rows if row.end_state == "ENDED"]
            # Read-only active=false observations never enter classification. Use
            # the same original-anchor/no-op definition as final application.
            existing_anchors = _existing_end_anchors(
                [drafts[draft_ref] for draft_ref in targets_by_ref[ref].draft_refs]
            )
            actual_new = [any(_end_spans_and_anchor(end, document)[1] not in existing_anchors
                              for end in row.end_observations) for row in endings]
            proposed_endings = [row for row, adds in zip(endings, actual_new, strict=True) if adds]
            # Agreeing that a target ended is distinct from proposing a new row:
            # an empty or duplicate-only ENDED vote preserves an existing row.
            if len(proposed_endings) >= 2:
                target_reviews.append(proposed_endings[0])
            else:
                target_reviews.append(next(row for row, adds in zip(endings, actual_new, strict=True)
                                           if not adds))
            continue
        elif sum(row.end_state == "CONTINUES" for row in rows) >= 2:
            end_state = "CONTINUES"
        else:
            end_state = "UNKNOWN"
        # Select one entire actual vote in fixed order: do not union evidence,
        # synthesize a summary, or borrow an explanation from the minority.
        target_reviews.append(next(row for row in rows if row.end_state == end_state))
    return _ReviewResponse(draft_reviews=draft_reviews, target_reviews=target_reviews)


def _coverage(actual: list[str], expected: set[str], code: str) -> None:
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise _ReviewError(code)


def _spans(refs: list[str], document: StatusEvidenceDocument) -> list[ExtractedEvidenceSpan]:
    if len(refs) != len(set(refs)):
        raise _ReviewError("STATUS_REVIEW_DUPLICATE_EVIDENCE_REF")
    by_ref = {unit.ref: unit for unit in document.units}
    if any(ref not in by_ref for ref in refs):
        raise _ReviewError("STATUS_REVIEW_UNKNOWN_EVIDENCE_REF")
    return [ExtractedEvidenceSpan(quote=by_ref[ref].text,
                                 start_offset=by_ref[ref].start_offset,
                                 end_offset=by_ref[ref].end_offset) for ref in refs]


def _anchor(candidate: ExtractedSettingCandidate) -> int:
    return max(span.start_offset for span in candidate.evidence_spans)


def _existing_end_anchors(observations: list[ExtractedSettingCandidate]) -> set[int]:
    return {_anchor(candidate) for candidate in observations
            if candidate.value_json.get("active") is False}


def _end_spans_and_anchor(
    end: _EndObservation, document: StatusEvidenceDocument,
) -> tuple[list[ExtractedEvidenceSpan], int]:
    spans = _spans(end.evidence_refs, document)
    return spans, max(span.start_offset for span in spans)


def _apply(response: _ReviewResponse, targets: list[_Target],
           drafts: dict[str, ExtractedSettingCandidate], document: StatusEvidenceDocument,
           source_chunk_id: UUID) -> list[ExtractedSettingCandidate]:
    editable = _editable_drafts(drafts, targets)
    _coverage([r.draft_ref for r in response.draft_reviews], set(editable), "STATUS_REVIEW_DRAFT_COVERAGE")
    _coverage([r.target_ref for r in response.target_reviews], {t.target_ref for t in targets},
              "STATUS_REVIEW_TARGET_COVERAGE")
    retained = {ref: candidate for ref, candidate in drafts.items() if ref not in editable}
    for review in response.draft_reviews:
        _spans(review.evidence_refs, document)
        candidate = drafts[review.draft_ref]
        if review.classification == "ONE_OFF_TREATMENT_PROCESS":
            if not review.evidence_refs:
                raise _ReviewError("STATUS_REVIEW_PROCESS_EVIDENCE_REQUIRED")
        else:
            retained[review.draft_ref] = candidate
    # Keep original order and payloads; review order cannot reorder source observations.
    output = [candidate for ref, candidate in drafts.items() if ref in retained]
    reviews = {r.target_ref: r for r in response.target_reviews}
    for target in targets:
        review = reviews[target.target_ref]
        observations = [retained[ref] for ref in target.draft_refs if ref in retained]
        onsets = [candidate for candidate in observations if candidate.value_json.get("active") is not False]
        latest_onset = max((_anchor(candidate) for candidate in onsets), default=-1)
        # An advisory ENDED with no addition only preserves existing observations.
        # Their temporal interpretation, including a later recurrence or recollection,
        # remains the downstream comparator's responsibility.
        existing_end = (any(candidate.value_json.get("active") is False for candidate in observations)
                        or target.prior_active is False)
        if review.end_state != "ENDED":
            if review.end_observations:
                raise _ReviewError("STATUS_REVIEW_NON_END_OBSERVATIONS")
            continue
        if not review.end_observations and not existing_end:
            raise _ReviewError("STATUS_REVIEW_END_EVIDENCE_REQUIRED")
        if not (target.active_at_episode_start or target.prior_observation_refs or observations):
            raise _ReviewError("STATUS_REVIEW_END_WITHOUT_STATE")
        existing_end_anchors = _existing_end_anchors(observations)
        new_anchors: set[int] = set()
        for end in review.end_observations:
            if not end.summary.strip() or end.summary != end.summary.strip():
                raise _ReviewError("STATUS_REVIEW_INVALID_SUMMARY")
            spans, anchor = _end_spans_and_anchor(end, document)
            if anchor <= latest_onset:
                raise _ReviewError("STATUS_REVIEW_END_BEFORE_LATEST_ONSET")
            if anchor in new_anchors:
                raise _ReviewError("STATUS_REVIEW_DUPLICATE_END")
            new_anchors.add(anchor)
            if anchor in existing_end_anchors:
                # Extra cause refs do not create a second ending at the same source event.
                continue
            if target.value_type not in {"STRING", "NUMBER", "BOOLEAN", "JSON", "UNKNOWN"}:
                raise _ReviewError("STATUS_REVIEW_END_SCHEMA_TYPE_UNAVAILABLE")
            value = {"name": target.fact_key.removeprefix("status."), "active": False}
            if target.value_type in {"STRING", "UNKNOWN"}:
                value["value"] = end.summary
            elif target.value_type in {"NUMBER", "BOOLEAN"}:
                # Scalar STATUS retains its schema's value; the new boolean is only lifecycle metadata.
                source = max(observations, key=_anchor) if observations else None
                source_value = source.value_json if source is not None else target._prior_value_json
                if source_value is not None and "value" in source_value:
                    scalar_value = source_value["value"]
                elif target.active_fact_value is not None:
                    try:
                        scalar_value = json.loads(target.active_fact_value)
                    except (ValueError, TypeError):
                        raise _ReviewError("STATUS_REVIEW_SCALAR_END_VALUE_MISSING") from None
                else:
                    raise _ReviewError("STATUS_REVIEW_SCALAR_END_VALUE_MISSING")
                value["value"] = scalar_value
                value["description"] = end.summary
            try:
                display = normalize_setting_display_value(target.value_type, value, end.summary)
            except ValueError:
                raise _ReviewError("STATUS_REVIEW_SCALAR_END_VALUE_INVALID") from None
            candidate = ExtractedCharacterSettingCandidate(
                candidate_kind="SETTING",
                source_chunk_id=source_chunk_id,
                entity_name=target.entity_name,
                raw_entity_mention=target.raw_entity_mention,
                attribute_name=target.fact_key,
                attribute_value=display,
                value_type=target.value_type,
                value_json=value,
                evidence_spans=spans,
                confidence=None,
            )
            candidate._status_observation_kind = "END"
            candidate._status_observation_group = target._producer_group
            # An already selected identical ending does not create a duplicate source row.
            if not any(old.value_json.get("active") is False
                       and old.evidence_spans == candidate.evidence_spans for old in observations):
                output.append(candidate)
    return sorted(output, key=_anchor)
