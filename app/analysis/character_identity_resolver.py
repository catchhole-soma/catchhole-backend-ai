"""Resolve conflicting episode name claims without treating uncertainty as a new person."""

import json
import logging
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.character_name_resolver import (
    UNKNOWN_ENTITY_NAME,
    KnownCharacter,
    is_usable_subject_resolution_name,
    normalize_character_name,
    normalize_known_characters,
    resolve_candidate_character,
)
from app.analysis.evidence_span_resolver import resolve_evidence_span_offsets
from app.analysis.exceptions import LlmExtractionError
from app.analysis.json_response import parse_json_object, safe_validation_error_summary
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.domain.enums import SettingCandidateMatchStatus
from app.llm.protocols import LlmResponseSchema, TextGenerationClient

PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_identity_resolution.md"
)
IDENTITY_RESOLUTION_CACHE_KEY = "character-identity-resolution:v3"
MAX_ATTEMPTS = 2
logger = logging.getLogger(__name__)
_SAFE_IDENTITY_ERROR_CODES = frozenset({
    "IDENTITY_PAIR_COVERAGE",
    "IDENTITY_RELATION_EVIDENCE_REQUIRED",
    "IDENTITY_EVIDENCE_NOT_VERBATIM",
    "IDENTITY_EXISTING_CHARACTERS_MUST_NOT_MERGE",
    "IDENTITY_RELATION_CONTRADICTION",
})


@dataclass(frozen=True)
class CharacterIdentityResolutionResult:
    candidates: list[ExtractedSettingCandidate]
    call_count: int = 0
    renamed_candidate_count: int = 0
    unresolved_candidate_count: int = 0


class _IdentityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["current_episode", "previous_episode"]
    quote: str = Field(min_length=1, max_length=2000)


class _IdentityPairDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_ref: str = Field(pattern=r"^R[1-9][0-9]*$")
    relation: Literal["SAME_PERSON", "DISTINCT_PERSON", "UNRESOLVED"]
    evidence: list[_IdentityEvidence]


class _IdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[_IdentityPairDecision]


async def reconcile_episode_names(
    *,
    llm_client: TextGenerationClient,
    model: str,
    max_output_tokens: int,
    context_episode_text: str,
    previous_episode_text: str | None,
    candidates: list[ExtractedSettingCandidate],
    known_characters: list[KnownCharacter],
) -> CharacterIdentityResolutionResult:
    """Use the caller's metered subject client; preserve facts even when identity is unresolved."""

    known_names = list(dict.fromkeys(character.name for character in known_characters))
    normalized_known = normalize_known_characters(known_characters)
    new_indexes: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        name = candidate.entity_name
        if name in known_names or not is_usable_subject_resolution_name(name):
            continue
        if resolve_candidate_character(candidate, normalized_known).match_status == (
            SettingCandidateMatchStatus.MATCHED
        ):
            continue
        new_indexes.setdefault(name, []).append(index)
    if not new_indexes:
        return CharacterIdentityResolutionResult(candidates=candidates)

    new_names_by_ref = {f"N{index}": name for index, name in enumerate(new_indexes, start=1)}
    known_names_by_ref = {f"K{index}": name for index, name in enumerate(known_names, start=1)}
    names_by_ref = {**known_names_by_ref, **new_names_by_ref}
    chunk_refs = {
        chunk_id: f"H{index}"
        for index, chunk_id in enumerate(
            dict.fromkeys(candidate.source_chunk_id for candidate in candidates), start=1,
        )
    }
    occurrences = {
        ref: [
            {
                "occurrence_ref": f"O{index + 1}",
                "chunk_ref": chunk_refs[candidates[index].source_chunk_id],
                "raw_mention": candidates[index].raw_entity_mention,
                "evidence_spans": [
                    span.model_dump(mode="json") for span in candidates[index].evidence_spans
                ],
            }
            for index in new_indexes[name]
        ]
        for ref, name in new_names_by_ref.items()
    }
    context = {"current_episode": context_episode_text, "previous_episode": previous_episode_text}
    pairs = _conflicting_pairs(names_by_ref, occurrences, context)
    if not pairs:
        return CharacterIdentityResolutionResult(candidates=candidates)
    payload = {
        "known_names": [{"ref": ref, "name": name} for ref, name in known_names_by_ref.items()],
        "new_names": [
            {"ref": ref, "name": name, "occurrences": occurrences[ref]}
            for ref, name in new_names_by_ref.items()
        ],
        "pairs": [
            {"pair_ref": ref, "left_ref": left, "right_ref": right}
            for ref, (left, right) in pairs.items()
        ],
        "identity_context": context,
    }
    # Source chronology chooses a display name only after SAME_PERSON is proven.
    # It is never a signal that an earlier character is the narrator or subject.
    first_occurrence = {
        ref: min(
            (span.start_offset is None, span.start_offset or 0, index)
            for index in new_indexes[name]
            for span in candidates[index].evidence_spans
        )
        for ref, name in new_names_by_ref.items()
    }
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    schema = LlmResponseSchema(
        name="character_identity_resolution", schema=_IdentityResponse.model_json_schema(),
    )
    current_payload = payload
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Quota, lease, transport and truncation failures propagate. Every retry
        # keeps the same metered boundary and cannot silently approve two roots.
        response = await llm_client.create_text_response(
            system_prompt=system_prompt,
            user_prompt=json.dumps(current_payload, ensure_ascii=False),
            model=model,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=IDENTITY_RESOLUTION_CACHE_KEY,
            response_schema=schema,
        )
        try:
            result = _IdentityResponse.model_validate(parse_json_object(response.text))
            replacements = _resolve_pair_decisions(
                result, pairs, names_by_ref, context, first_occurrence,
            )
        except (TypeError, ValueError) as exc:
            summary = _identity_error_summary(exc)
            if attempt == MAX_ATTEMPTS:
                raise LlmExtractionError(
                    f"Character identity resolution failed after {attempt} attempts: {summary}"
                ) from None
            logger.warning("Character identity resolution retry. attempt=%s error=%s", attempt, summary)
            current_payload = {
                **payload,
                "validation_feedback": {
                    "previous_response_rejected": True,
                    "reason": summary,
                    "correction": (
                        "모든 R pair를 정확히 한 번 반환하세요. SAME_PERSON과 DISTINCT_PERSON 모두 "
                        "지정 회차의 실제 인용으로 입증해야 합니다. 이름 부분 일치는 증명이 아닙니다. "
                        "입증할 수 없으면 UNRESOLVED로 남기세요. 동일인 연결과 별개 인물 판단이 "
                        "서로 모순되거나 두 기존 K 인물을 합치면 안 됩니다."
                    ),
                },
            }
            continue

        by_index = {
            index: replacements[ref]
            for ref, name in new_names_by_ref.items()
            for index in new_indexes[name]
            if replacements[ref] != name
        }
        unresolved_count = sum(name == UNKNOWN_ENTITY_NAME for name in by_index.values())
        return CharacterIdentityResolutionResult(
            candidates=[
                candidate.model_copy(update={"entity_name": by_index[index]})
                if index in by_index else candidate
                for index, candidate in enumerate(candidates)
            ],
            call_count=attempt,
            renamed_candidate_count=len(by_index) - unresolved_count,
            unresolved_candidate_count=unresolved_count,
        )
    raise AssertionError("Identity resolution must return or fail within its retry bound.")


def _conflicting_pairs(
    names: dict[str, str],
    occurrences: dict[str, list[dict]],
    context: dict[str, str | None],
) -> dict[str, tuple[str, str]]:
    normalized = {ref: _normalize_name(name) for ref, name in names.items()}
    lines = [
        _normalize_name(line)
        for text in context.values() if text
        for line in text.splitlines() if line.strip()
    ]
    pairs = {}
    for left, right in combinations(names, 2):
        if left.startswith("K") and right.startswith("K"):
            continue
        left_name, right_name = normalized[left], normalized[right]
        overlap = left_name in right_name or right_name in left_name
        raw_bridge = any(
            is_usable_subject_resolution_name(item["raw_mention"])
            and other_name in _normalize_name(item["raw_mention"])
            for ref, other_name in ((left, right_name), (right, left_name))
            for item in occurrences.get(ref, [])
        )
        # A name may be unrelated lexically to its nickname. Co-occurrence is
        # another reason to ask, never a reason to merge without source proof.
        source_bridge = any(left_name in line and right_name in line for line in lines)
        if overlap or raw_bridge or source_bridge:
            pairs[f"R{len(pairs) + 1}"] = (left, right)
    return pairs


def _normalize_name(value: str) -> str:
    return "".join(normalize_character_name(value).split())


def _identity_error_summary(exc: Exception) -> str:
    # Only exact, locally generated codes may become provider correction text.
    # Pydantic inputs or other exception messages must remain sanitized.
    if type(exc) is ValueError and str(exc) in _SAFE_IDENTITY_ERROR_CODES:
        return str(exc)
    return safe_validation_error_summary(exc)


def _resolve_pair_decisions(
    response: _IdentityResponse,
    pairs: dict[str, tuple[str, str]],
    names: dict[str, str],
    context: dict[str, str | None],
    first_occurrence: dict[str, tuple[bool, int, int]],
) -> dict[str, str]:
    refs = [item.pair_ref for item in response.decisions]
    if len(refs) != len(set(refs)) or set(refs) != set(pairs):
        raise ValueError("IDENTITY_PAIR_COVERAGE")
    roots = {ref: ref for ref in names}

    def find(ref: str) -> str:
        while roots[ref] != ref:
            ref = roots[ref]
        return ref

    for item in response.decisions:
        if item.relation != "UNRESOLVED" and not item.evidence:
            raise ValueError("IDENTITY_RELATION_EVIDENCE_REQUIRED")
        for evidence in item.evidence:
            source_text = context[evidence.source]
            if (
                not evidence.quote.strip() or not source_text
                or resolve_evidence_span_offsets(
                    ExtractedEvidenceSpan(quote=evidence.quote), source_text, 0,
                ).start_offset is None
            ):
                raise ValueError("IDENTITY_EVIDENCE_NOT_VERBATIM")
        if item.relation == "SAME_PERSON":
            left, right = pairs[item.pair_ref]
            roots[find(right)] = find(left)

    components: dict[str, list[str]] = {}
    for ref in names:
        components.setdefault(find(ref), []).append(ref)
    for refs_in_component in components.values():
        if sum(ref.startswith("K") for ref in refs_in_component) > 1:
            raise ValueError("IDENTITY_EXISTING_CHARACTERS_MUST_NOT_MERGE")

    unresolved_roots = set()
    for item in response.decisions:
        left, right = pairs[item.pair_ref]
        left_root, right_root = find(left), find(right)
        if item.relation == "DISTINCT_PERSON" and left_root == right_root:
            raise ValueError("IDENTITY_RELATION_CONTRADICTION")
        if item.relation == "UNRESOLVED" and left_root != right_root:
            for root in (left_root, right_root):
                # Known identities and new aliases proven to belong to them stay
                # fixed; only the still unanchored new identity needs review.
                if not any(ref.startswith("K") for ref in components[root]):
                    unresolved_roots.add(root)

    replacements = {}
    for root, refs_in_component in components.items():
        if root in unresolved_roots:
            representative = UNKNOWN_ENTITY_NAME
        else:
            known_ref = next((ref for ref in refs_in_component if ref.startswith("K")), None)
            representative = names[known_ref] if known_ref else names[
                min(refs_in_component, key=lambda ref: first_occurrence[ref])
            ]
        for ref in refs_in_component:
            if ref.startswith("N"):
                replacements[ref] = representative
    return replacements
