"""Review remaining STATUS slots without inventing facts or rewriting observations."""

import json
import logging
import re
from pathlib import Path
from typing import Literal

import tiktoken
from pydantic import BaseModel, ConfigDict, Field

from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchDecision
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
    is_explicit_inactive_status,
    validate_resolved_canonical_fact_key,
)
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.json_response import request_validated_model, safe_validation_error_summary
from app.domain.enums import CharacterFactComparisonOperation as Operation
from app.domain.enums import CharacterFactTemporalScope as TemporalScope
from app.llm.protocols import TextGenerationClient
from app.schemas.worker import WorkerCharacterFactComparisonBatchCandidate

PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm/prompts/character_status_lifecycle.md"
STATUS_LIFECYCLE_CACHE_KEY = "character-status-lifecycle:v2"
logger = logging.getLogger(__name__)
_LOCAL_REF = re.compile(r"(?<![A-Za-z0-9])[CPQ][1-9][0-9]*(?![A-Za-z0-9])")
_UUID = re.compile(r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}")
_INTERNAL_TERM = re.compile(
    r"(?<![A-Za-z0-9_])(?:snapshot|canonical|Fact|"
    r"ADD|UPDATE|MERGE|REMOVE|HISTORY_ONLY|EXCLUDE|REVIEW_REQUIRED|"
    r"AGE|LEVEL|PROFILE|STAT|SKILL|ITEM|STATUS|KEEP|END|UNKNOWN)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


class _LifecycleValidationError(ValueError):
    pass


class _LifecycleCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_ref: str = Field(pattern=r"^[PQ][1-9][0-9]*$")
    verdict: Literal["KEEP", "END", "UNKNOWN"]
    carrier_candidate_ref: str | None
    reason: str = Field(min_length=1, max_length=500)


class _LifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    checks: list[_LifecycleCheck]


async def reconcile_status_lifecycle(
    *,
    llm_client: TextGenerationClient,
    model: str,
    max_output_tokens: int,
    max_attempts: int,
    max_input_tokens: int,
    matched_character_name: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    initial_entries: list[CharacterProjectionEntry],
    decisions: list[CharacterFactComparisonBatchDecision],
) -> list[CharacterFactComparisonBatchDecision]:
    """Reconcile a complete Spring STATUS batch after all provider fallbacks.

    The caller supplies its existing metered comparison client and runs this after
    segment/singleton comparison has finished. Errors must not bypass this review.
    """
    if min(max_output_tokens, max_attempts, max_input_tokens) < 1:
        raise ValueError("STATUS lifecycle limits must be positive.")
    try:
        state, before = _replay(candidates, initial_entries, decisions)
    except ValueError:
        raise ComparisonValidationError("STATUS_LIFECYCLE_INITIAL_PROJECTION_INVALID") from None
    remaining = [entry for entry in state.entries if entry.fact_type == "STATUS"]
    if not remaining:
        return decisions
    eligible = {
        entry.reference: [
            candidate.candidate_ref
            for candidate, decision in zip(candidates, decisions, strict=True)
            if entry.reference in before[candidate.candidate_ref]
            and entry.reference != candidate.projected_snapshot_ref
            and decision.temporal_scope == TemporalScope.PRESENT
            and is_explicit_inactive_status("STATUS", candidate.value_json)
            and decision.operation != Operation.REVIEW_REQUIRED
            and any(span.quote.strip() for span in candidate.evidence_spans)
        ]
        for entry in remaining
    }
    payload = {
        "matched_character_name": matched_character_name,
        "candidates": [
            {
                "candidate_ref": candidate.candidate_ref,
                "projected_snapshot_ref": candidate.projected_snapshot_ref,
                "source_episode_no": candidate.source_episode_no,
                "fact_key": candidate.initial_canonical_fact_key,
                "attribute_value": candidate.attribute_value,
                "value_json": candidate.value_json,
                "evidence_spans": [span.model_dump(mode="json")
                                   for span in candidate.evidence_spans],
                "temporal_scope": decision.temporal_scope.value,
            }
            for candidate, decision in zip(candidates, decisions, strict=True)
        ],
        "remaining_statuses": [
            {
                "snapshot_ref": entry.reference,
                "fact_key": entry.fact_key,
                "fact_value": entry.fact_value,
                "value_json": entry.value_json,
                "origin": entry.origin,
                "source_candidate_ref": entry.source_candidate_ref,
                "eligible_carrier_candidate_refs": eligible[entry.reference],
            }
            for entry in remaining
        ],
    }
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_prompt = json.dumps(payload, ensure_ascii=False)
    _check_input_limit(system_prompt, user_prompt, model, max_input_tokens)

    def validate(response: _LifecycleResponse) -> None:
        _apply_checks(response, candidates, initial_entries, decisions, remaining, eligible)

    def retry_prompt(original: str, error: Exception) -> str:
        retry_payload = json.loads(original)
        retry_payload["validation_feedback"] = {
            "reason_code": (
                str(error) if isinstance(error, _LifecycleValidationError)
                else safe_validation_error_summary(error)
            ),
            "correction": (
                "남아 있는 상태 참조를 정확히 한 번씩 검토하세요. 종료는 해당 상태의 "
                "eligible_carrier_candidate_refs에 있는 현재 관찰과 그 관찰의 기존 근거로만 "
                "판단하세요. 나머지는 carrier를 null로 두며 설명에 내부 참조나 key를 쓰지 마세요."
            ),
        }
        prompt = json.dumps(retry_payload, ensure_ascii=False)
        _check_input_limit(system_prompt, prompt, model, max_input_tokens)
        return prompt

    response = await request_validated_model(
        client=llm_client,
        response_model=_LifecycleResponse,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        max_output_tokens=max_output_tokens,
        max_attempts=max_attempts,
        prompt_cache_key=STATUS_LIFECYCLE_CACHE_KEY,
        operation_name="Character STATUS lifecycle comparison",
        logger=logger,
        validate_model=validate,
        retry_user_prompt_builder=retry_prompt,
    )
    return _apply_checks(response, candidates, initial_entries, decisions, remaining, eligible)


def _replay(
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    initial_entries: list[CharacterProjectionEntry],
    decisions: list[CharacterFactComparisonBatchDecision],
) -> tuple[CharacterProjectionState, dict[str, set[str]]]:
    refs = [candidate.candidate_ref for candidate in candidates]
    if len(refs) != len(set(refs)) or refs != [decision.candidate_ref for decision in decisions]:
        raise _LifecycleValidationError("STATUS_LIFECYCLE_CANDIDATE_COVERAGE")
    state = CharacterProjectionState(initial_entries)
    before: dict[str, set[str]] = {}
    for candidate, decision in zip(candidates, decisions, strict=True):
        # Revalidate copies as callers can construct model_copy(update=...) values.
        decision = CharacterFactComparisonBatchDecision.model_validate(decision.model_dump())
        if not candidate.initial_canonical_fact_key.startswith("status."):
            raise _LifecycleValidationError("STATUS_LIFECYCLE_NON_STATUS_CANDIDATE")
        if candidate.projected_snapshot_ref != f"Q{candidate.candidate_ref[1:]}":
            raise _LifecycleValidationError("STATUS_LIFECYCLE_PROJECTED_REF_INVALID")
        before[candidate.candidate_ref] = set(state.entries_by_ref)
        validate_resolved_canonical_fact_key(
            initial_fact_key=candidate.initial_canonical_fact_key,
            resolved_fact_key=decision.resolved_canonical_fact_key,
            canonical_key_resolution=candidate.canonical_key_resolution,
            fact_type="STATUS",
            existing_status_keys={entry.fact_key for entry in state.entries
                                  if entry.fact_type == "STATUS"},
        )
        state.apply(
            candidate_ref=candidate.candidate_ref,
            projected_snapshot_ref=candidate.projected_snapshot_ref,
            fact_type="STATUS",
            resolved_fact_key=decision.resolved_canonical_fact_key,
            value_type=candidate.value_type,
            candidate_value_json=candidate.value_json,
            decision=decision,
        )
    return state, before


def _apply_checks(
    response: _LifecycleResponse,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    initial_entries: list[CharacterProjectionEntry],
    decisions: list[CharacterFactComparisonBatchDecision],
    remaining: list[CharacterProjectionEntry],
    eligible: dict[str, list[str]],
) -> list[CharacterFactComparisonBatchDecision]:
    refs = [check.snapshot_ref for check in response.checks]
    expected = {entry.reference for entry in remaining}
    if len(refs) != len(set(refs)) or set(refs) != expected:
        raise _LifecycleValidationError("STATUS_LIFECYCLE_REMAINING_COVERAGE")
    keys = {entry.fact_key for entry in initial_entries}
    keys.update(decision.resolved_canonical_fact_key for decision in decisions)
    checks = {check.snapshot_ref: check for check in response.checks}
    amendments: dict[str, list[_LifecycleCheck]] = {}
    for entry in remaining:
        check = checks[entry.reference]
        _validate_reason(check.reason, keys)
        if check.verdict != "END":
            if check.carrier_candidate_ref is not None:
                raise _LifecycleValidationError("STATUS_LIFECYCLE_NON_END_CARRIER")
            continue
        if check.carrier_candidate_ref not in eligible[entry.reference]:
            raise _LifecycleValidationError("STATUS_LIFECYCLE_CARRIER_NOT_ELIGIBLE")
        amendments.setdefault(check.carrier_candidate_ref, []).append(check)
    if not amendments:
        return decisions
    amended: list[CharacterFactComparisonBatchDecision] = []
    for decision in decisions:
        additions = amendments.get(decision.candidate_ref)
        if not additions:
            amended.append(decision)
            continue
        payload = decision.model_dump()
        payload["removed_snapshot_refs"] = list(dict.fromkeys([
            *decision.removed_snapshot_refs,
            *(check.snapshot_ref for check in additions),
        ]))
        if decision.operation in {Operation.HISTORY_ONLY, Operation.EXCLUDE}:
            payload["operation"] = Operation.REMOVE
        explanations = list(dict.fromkeys(check.reason.strip() for check in additions))
        payload["comparison_reason"] = (
            f"{decision.comparison_reason} 상태 변화 확인: {' '.join(explanations)}"
        )
        try:
            amended.append(CharacterFactComparisonBatchDecision.model_validate(payload))
        except ValueError:
            raise _LifecycleValidationError("STATUS_LIFECYCLE_AMENDMENT_INVALID") from None
    try:
        final, _ = _replay(candidates, initial_entries, amended)
    except ValueError:
        raise _LifecycleValidationError("STATUS_LIFECYCLE_REPLAY_INVALID") from None
    ended_refs = {check.snapshot_ref for check in response.checks if check.verdict == "END"}
    if ended_refs & final.entries_by_ref.keys():
        raise _LifecycleValidationError("STATUS_LIFECYCLE_END_NOT_APPLIED")
    return amended


def _validate_reason(reason: str, fact_keys: set[str]) -> None:
    if (
        not reason.strip() or reason != reason.strip()
        or not re.search(r"[가-힣]", reason)
        or _LOCAL_REF.search(reason) or _UUID.search(reason) or _INTERNAL_TERM.search(reason)
        or any(key.casefold() in reason.casefold() for key in fact_keys)
    ):
        raise _LifecycleValidationError("STATUS_LIFECYCLE_REASON_INVALID")


def _check_input_limit(system_prompt: str, user_prompt: str, model: str, limit: int) -> None:
    try:
        encoding = (tiktoken.get_encoding("o200k_base") if model.startswith("gpt-5.6")
                    else tiktoken.encoding_for_model(model))
        tokens = sum(len(encoding.encode(text, disallowed_special=()))
                     for text in (system_prompt, user_prompt))
        estimate = int(tokens * 1.10) + 256
    except Exception:  # noqa: BLE001 - use the same conservative fallback as comparison.
        estimate = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8")) + 512
    if estimate > limit:
        raise ComparisonValidationError("STATUS_LIFECYCLE_INPUT_LIMIT_EXCEEDED")
