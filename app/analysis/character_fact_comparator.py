import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonBatchDecision,
    CharacterFactComparisonBatchResult,
    CharacterFactComparisonDecision,
)
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
    is_explicit_inactive_status,
    validate_character_fact_decision,
    validate_resolved_canonical_fact_key,
    validate_status_active_value,
)
from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.comparison_reason import (
    USER_FACING_REASON_INSTRUCTIONS, mask_display_names,
)
from app.analysis.ordered_context import (
    ORDERED_STATE_INSTRUCTIONS, ordered_provenance_data, ordered_reference_data,
)
from app.schemas.analysis_context import WorkerAnalysisReference
from app.analysis.json_response import compact_error_message, request_validated_model
from app.core.config import get_settings
from app.domain.setting_values import normalize_setting_display_value
from app.exceptions.failure_classification import is_candidate_comparison_failure
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.schemas.worker import (
    WorkerCharacterFactComparisonCandidatePayload,
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchSnapshotEntry,
    WorkerCharacterPriorFactCandidate,
    WorkerCharacterSnapshotEntry,
)

COMPARISON_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_fact_comparison.md"
)
BATCH_COMPARISON_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "llm"
    / "prompts"
    / "character_fact_comparison_batch.md"
)
CHARACTER_FACT_COMPARISON_BATCH_CACHE_KEY = "character-fact-comparison-batch:v3"
ORDERED_CHARACTER_SLOT_INSTRUCTIONS = (
    "현재 활성인 동일 Fact 유형·동일 resolved key가 있으면 새 내용이어도 ADD는 금지입니다. "
    "예를 들어 profile.attribute의 기존 값과 새 후보의 문장이 다르더라도 같은 slot입니다. "
    "의미에 따라 기존 정보를 보존하는 MERGE, 최신값으로 바꾸는 UPDATE, 중복 EXCLUDE 또는 "
    "안전하게 판단할 수 없는 REVIEW_REQUIRED를 명시적으로 선택하세요. UPDATE/MERGE의 "
    "target_ref는 그 시점의 활성 P*/앞선 Q*여야 합니다. ADD를 맞추려고 EXACT/ALIAS 또는 "
    "비-STATUS PATTERN key를 바꾸거나 기존 정보를 임의로 제거하지 마세요. 앞 후보의 "
    "UPDATE/MERGE로 P*가 Q*로 교체됐다면 이전 P*를 다시 사용하지 마세요."
)
logger = logging.getLogger(__name__)
SNAPSHOT_REFERENCE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[PQ][0-9]+(?![A-Za-z0-9])")
CANDIDATE_REFERENCE_PATTERN = re.compile(r"(?<![A-Za-z0-9])C[0-9]+(?![A-Za-z0-9])")
UUID_PATTERN = re.compile(
    r"(?<![A-Fa-f0-9])"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[1-5][A-Fa-f0-9]{3}-"
    r"[89ABab][A-Fa-f0-9]{3}-[A-Fa-f0-9]{12}"
    r"(?![A-Fa-f0-9])"
)
INTERNAL_REASON_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"snapshot|canonical(?:\s+Fact)?|Fact(?:\s+(?:type|key))?|"
    r"ADD|UPDATE|MERGE|REMOVE|HISTORY_ONLY|EXCLUDE|REVIEW_REQUIRED|"
    r"AGE|LEVEL|PROFILE|STAT|SKILL|ITEM|STATUS"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CharacterSnapshotReference:
    reference: str
    entry: WorkerCharacterSnapshotEntry


class OrderedCharacterSlotConflict(ValueError):
    """One unchanged validator rule, with no candidate values in its message."""

    def __init__(self, candidate_ref: str, target: CharacterProjectionEntry) -> None:
        self.candidate_ref = candidate_ref
        self.target_ref = target.reference
        self.target_fact_type = target.fact_type
        self.target_fact_key = target.fact_key
        self.target_source_candidate_ref = target.source_candidate_ref
        super().__init__("CANONICAL_SLOT_ALREADY_EXISTS")


class OrderedCharacterBatchRecoveryError(ComparisonValidationError):
    """In-memory verified independent decisions; never serialize provider payloads."""

    def __init__(self, message: str, retained_decisions: tuple) -> None:
        super().__init__(message)
        self.retained_decisions = retained_decisions


class CharacterFactComparator:
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = COMPARISON_PROMPT_PATH,
        batch_prompt_path: Path = BATCH_COMPARISON_PROMPT_PATH,
        model: str | None = None,
        max_attempts: int | None = None,
        max_output_tokens: int | None = None,
        batch_max_output_tokens: int | None = None,
        batch_max_input_tokens: int | None = None,
        batch_max_candidates: int | None = None,
    ) -> None:
        settings = get_settings()
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.batch_prompt_path = batch_prompt_path
        self.model = model or settings.effective_llm_comparison_model
        self.max_attempts = _resolve_max_attempts(max_attempts)
        self.max_output_tokens = (
            settings.llm_comparison_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        self.batch_max_output_tokens = (
            settings.llm_character_fact_batch_comparison_max_output_tokens
            if batch_max_output_tokens is None
            else batch_max_output_tokens
        )
        self.batch_max_input_tokens = (
            settings.llm_character_fact_batch_comparison_max_input_tokens
            if batch_max_input_tokens is None
            else batch_max_input_tokens
        )
        self.batch_max_candidates = (
            settings.character_fact_comparison_batch_max_candidates
            if batch_max_candidates is None
            else batch_max_candidates
        )

    async def compare(
        self,
        candidate: WorkerCharacterFactComparisonCandidatePayload,
        snapshot_entries: list[WorkerCharacterSnapshotEntry],
        prior_candidates: list[WorkerCharacterPriorFactCandidate] | None = None,
    ) -> tuple[CharacterFactComparisonDecision, dict]:
        validate_status_active_value(
            candidate.canonical_fact_type,
            candidate.value_json,
            field_name="candidate.value_json",
        )
        references = [
            CharacterSnapshotReference(reference=f"P{index}", entry=entry)
            for index, entry in enumerate(snapshot_entries, start=1)
        ]
        exact_target_refs = [
            reference.reference
            for reference in references
            if reference.entry.fact_type == candidate.canonical_fact_type
            and reference.entry.fact_key == candidate.canonical_fact_key
        ]
        if len(exact_target_refs) > 1:
            raise ValueError("Canonical snapshot slot must be unique.")
        exact_target_ref = exact_target_refs[0] if exact_target_refs else None
        explicit_inactive_status = is_explicit_inactive_status(
            candidate.canonical_fact_type,
            candidate.value_json,
        )
        allowed_operations: list[str] = []
        if not explicit_inactive_status:
            if exact_target_ref is None:
                allowed_operations.append("ADD")
            else:
                allowed_operations.extend(["UPDATE", "MERGE"])
        has_current_status = any(reference.entry.fact_type == "STATUS" for reference in references)
        if candidate.canonical_fact_type == "STATUS" and has_current_status:
            allowed_operations.append("REMOVE")
        allowed_operations.extend(["HISTORY_ONLY", "EXCLUDE", "REVIEW_REQUIRED"])
        # DB 식별자는 provider에 노출하지 않고 이번 요청 안에서만 유효한 참조를 사용한다.
        prompt_payload = {
            "candidate": {
                "entity_name": candidate.entity_name,
                "matched_character_name": candidate.matched_character_name,
                "attribute_name": candidate.attribute_name,
                "attribute_value": candidate.attribute_value,
                "value_json": candidate.value_json,
                "value_type": candidate.value_type,
                "confidence": candidate.confidence,
                "canonical_fact_type": candidate.canonical_fact_type,
                "canonical_fact_key": candidate.canonical_fact_key,
                "evidence_spans": [
                    evidence.model_dump(mode="json") for evidence in candidate.evidence_spans
                ],
            },
            "snapshot_entries": [
                {
                    "ref": reference.reference,
                    "fact_type": reference.entry.fact_type,
                    "fact_key": reference.entry.fact_key,
                    "fact_value": reference.entry.fact_value,
                    "value_json": reference.entry.value_json,
                }
                for reference in references
            ],
            # 모델이 의미가 비슷한 다른 STATUS를 UPDATE 대상으로 고르지 않도록
            # exact slot 존재 여부와 이번 요청에서 허용되는 operation을 명시한다.
            "exact_target_ref": exact_target_ref,
            "allowed_operations": allowed_operations,
            "prior_candidates": [
                prior_candidate.model_dump(mode="json")
                for prior_candidate in (prior_candidates or [])
            ],
        }
        decision = await request_validated_model(
            client=self.llm_client,
            response_model=CharacterFactComparisonDecision,
            system_prompt=self.prompt_path.read_text(encoding="utf-8") + "\n\n" + USER_FACING_REASON_INSTRUCTIONS,
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="character-fact-comparison:v10",
            operation_name="Character-fact comparison",
            logger=logger,
            validate_model=lambda comparison_decision: _validate_comparison_decision(
                comparison_decision,
                candidate,
                references,
            ),
            retry_user_prompt_builder=_build_retry_user_prompt,
        )
        decision = _replace_internal_snapshot_references(decision, references)
        decision = _normalize_scalar_proposal(decision, candidate)
        return decision, decision.model_dump(mode="json")

    async def compare_batch(
        self,
        *,
        matched_character_name: str,
        canonical_fact_type: str,
        candidates: list[WorkerCharacterFactComparisonBatchCandidate],
        snapshot_entries: list[
            WorkerCharacterFactComparisonBatchSnapshotEntry | CharacterProjectionEntry
        ],
        ordered_context: bool = False,
        unresolved_references: tuple[WorkerAnalysisReference, ...] = (),
        max_attempts_override: int | None = None,
    ) -> tuple[CharacterFactComparisonBatchResult, dict]:
        """Compare ordered candidates while projecting each accepted decision in memory."""

        if not candidates:
            raise ComparisonValidationError(
                "Character comparison batch must include candidates."
            )
        if len(candidates) > self.batch_max_candidates:
            raise ComparisonValidationError(
                "character_batch_candidate_limit_exceeded"
            )
        initial_entries = _projection_entries(snapshot_entries)
        if not ordered_context and any(entry.origin == "PROVISIONAL" for entry in initial_entries):
            raise ComparisonValidationError("Provisional snapshots require explicit ordered analysis.")
        if unresolved_references and not ordered_context:
            raise ComparisonValidationError("Unresolved references require explicit ordered analysis.")
        try:
            _validate_batch_candidates(candidates, canonical_fact_type)
            if ordered_context:
                CharacterProjectionState(initial_entries)
                for entry in initial_entries:
                    ordered_provenance_data(entry.provenance)
        except (TypeError, ValueError, ComparisonValidationError) as error:
            if ordered_context:
                raise OrderedInputContextError("Character batch source context is invalid.") from error
            raise
        prompt_payload = _batch_prompt_payload(
            matched_character_name,
            canonical_fact_type,
            candidates,
            initial_entries,
        )
        system_prompt = self.batch_prompt_path.read_text(encoding="utf-8") + "\n\n" + USER_FACING_REASON_INSTRUCTIONS
        if ordered_context:
            system_prompt += "\n\n" + ORDERED_STATE_INSTRUCTIONS + "\n\n" + ORDERED_CHARACTER_SLOT_INSTRUCTIONS
            _add_ordered_snapshot_provenance(prompt_payload, initial_entries)
            prompt_payload["unresolved_references"] = ordered_reference_data(unresolved_references)
        user_prompt = json.dumps(prompt_payload, ensure_ascii=False)
        estimated_input_tokens = _estimate_prompt_tokens(system_prompt, user_prompt, self.model)
        if estimated_input_tokens > self.batch_max_input_tokens:
            if ordered_context:
                raise OrderedInputContextError("character_batch_input_limit_exceeded")
            raise ComparisonValidationError("character_batch_input_limit_exceeded")

        attempts = self.max_attempts if max_attempts_override is None else max_attempts_override
        if type(attempts) is not int or not 1 <= attempts <= self.max_attempts:
            raise OrderedInputContextError("Invalid character recovery attempt limit.")
        last_failed_payload = None

        def remember_validation_failure(attempt, error, payload):
            nonlocal last_failed_payload
            last_failed_payload = payload

        try:
            result = await request_validated_model(
                client=self.llm_client,
                response_model=CharacterFactComparisonBatchResult,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                max_output_tokens=self.batch_max_output_tokens,
                max_attempts=attempts,
                prompt_cache_key=(CHARACTER_FACT_COMPARISON_BATCH_CACHE_KEY
                                  + (":ordered-provisional-v1" if ordered_context else "")),
                operation_name="Character-fact batch comparison",
                logger=logger,
                validate_model=lambda comparison_result: _validate_batch_comparison_result(
                    comparison_result,
                    canonical_fact_type,
                    candidates,
                    initial_entries,
                    ordered_context=ordered_context,
                    display_names=(matched_character_name,),
                ),
                retry_user_prompt_builder=lambda original, exc: _build_bounded_batch_retry_user_prompt(
                    original,
                    exc,
                    system_prompt=system_prompt,
                    model=self.model,
                    max_input_tokens=self.batch_max_input_tokens,
                    ordered_context=ordered_context,
                ),
                **({"validation_failure_callback": remember_validation_failure,
                    "validation_error_summary": lambda error: (
                    "ordered_batch=CANONICAL_SLOT_ALREADY_EXISTS"
                    if isinstance(error, OrderedCharacterSlotConflict) else compact_error_message(error)
                )} if ordered_context else {}),
            )
        except ComparisonValidationError as error:
            if not ordered_context or not is_candidate_comparison_failure(error):
                raise
            retained = _independent_ordered_decisions(
                last_failed_payload, canonical_fact_type, candidates, initial_entries,
                display_names=(matched_character_name,),
            )
            raise OrderedCharacterBatchRecoveryError(str(error), retained) from error
        normalized_result = _normalize_batch_comparison_result(
            result,
            canonical_fact_type,
            candidates,
            initial_entries,
        )
        raw = normalized_result.model_dump(mode="json")
        raw["estimated_input_tokens"] = estimated_input_tokens
        return normalized_result, raw

    def batch_fits(
        self,
        *,
        matched_character_name: str,
        canonical_fact_type: str,
        candidates: list[WorkerCharacterFactComparisonBatchCandidate],
        snapshot_entries: list[
            WorkerCharacterFactComparisonBatchSnapshotEntry | CharacterProjectionEntry
        ],
        ordered_context: bool = False,
        unresolved_references: tuple[WorkerAnalysisReference, ...] = (),
    ) -> bool:
        if not candidates or len(candidates) > self.batch_max_candidates:
            return False
        initial_entries = _projection_entries(snapshot_entries)
        if not ordered_context and any(entry.origin == "PROVISIONAL" for entry in initial_entries):
            return False
        if unresolved_references and not ordered_context:
            return False
        prompt_payload = _batch_prompt_payload(
            matched_character_name,
            canonical_fact_type,
            candidates,
            initial_entries,
        )
        system_prompt = self.batch_prompt_path.read_text(encoding="utf-8") + "\n\n" + USER_FACING_REASON_INSTRUCTIONS
        if ordered_context:
            system_prompt += "\n\n" + ORDERED_STATE_INSTRUCTIONS + "\n\n" + ORDERED_CHARACTER_SLOT_INSTRUCTIONS
            _add_ordered_snapshot_provenance(prompt_payload, initial_entries)
            prompt_payload["unresolved_references"] = ordered_reference_data(unresolved_references)
        return _estimate_prompt_tokens(
            system_prompt,
            json.dumps(prompt_payload, ensure_ascii=False),
            self.model,
        ) <= self.batch_max_input_tokens


def _build_retry_user_prompt(original_user_prompt: str, exc: Exception) -> str:
    """검증 실패 이유를 다음 시도의 보정 지시로만 전달한다."""

    payload = json.loads(original_user_prompt)
    payload["validation_feedback"] = {
        "previous_response_rejected": True,
        "reason": compact_error_message(exc),
        "correction": (
            "allowed_operations 중 하나만 선택하세요. UPDATE와 MERGE만 target_ref를 "
            "사용하며 exact_target_ref와 정확히 같아야 합니다. 현재 후보를 snapshot에 "
            "남기지 않고 관련 STATUS를 끝내려면 REMOVE, target_ref=null, 한 개 이상의 "
            "removed_snapshot_refs를 사용하세요. 현재 후보도 지속 상태로 남겨야 하면 "
            "ADD/UPDATE/MERGE와 removed_snapshot_refs를 함께 사용하세요. candidate 또는 "
            "proposed STATUS의 value_json.active가 boolean false이면 ADD/UPDATE/MERGE를 "
            "선택하지 마세요. active가 있으면 문자열이 아닌 JSON boolean이어야 합니다. "
            "proposal은 candidate.value_type을 유지하세요. STRING이면 "
            "proposed_value_json.value에 JSON 문자열을 넣으세요. NUMBER는 JSON 숫자, "
            "BOOLEAN은 JSON boolean을 사용하세요. "
            "판단 이유에는 "
            "내부 key·enum·UUID를 쓰지 말고 사용자가 이해할 수 있는 한국어만 쓰세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_batch_retry_user_prompt(
    original_user_prompt: str, exc: Exception, *, ordered_context: bool = False,
) -> str:
    payload = json.loads(original_user_prompt)
    payload["validation_feedback"] = {
        "previous_response_rejected": True,
        "reason": compact_error_message(exc),
        "correction": (
            "모든 candidate_ref를 입력 순서대로 정확히 한 번 반환하세요. 현재 candidate보다 "
            "앞에서 활성화된 P*/Q*만 target_ref 또는 removed_snapshot_refs로 사용하세요. "
            "EXACT/ALIAS와 비-STATUS PATTERN key는 initial_canonical_fact_key 그대로 반환하고, "
            "STATUS pattern key만 의미가 같은 안정적인 status.* 이름으로 정규화하세요. UPDATE/MERGE는 "
            "현재 활성인 동일 resolved key만 target으로 삼고, REMOVE는 현재 후보를 snapshot에 "
            "남기지 않으면서 관련 STATUS를 한 개 이상 종료할 때만 선택하세요. "
            "각 proposal은 해당 candidate.value_type을 유지하세요. STRING이면 "
            "proposed_value_json.value에 JSON 문자열을 넣으세요. NUMBER는 JSON 숫자, "
            "BOOLEAN은 JSON boolean을 사용하세요."
        ),
    }
    if ordered_context and isinstance(exc, OrderedCharacterSlotConflict):
        payload["validation_feedback"]["reason_code"] = "CANONICAL_SLOT_ALREADY_EXISTS"
        payload["validation_feedback"]["correction"] = (
            ORDERED_CHARACTER_SLOT_INSTRUCTIONS + " 모든 candidate를 입력 순서대로 포함한 JSON 전체를 다시 반환하세요."
        )
        context = _verified_slot_conflict_context(payload, exc)
        if context is not None:
            payload["validation_feedback"]["existing_slot"] = context
    return json.dumps(payload, ensure_ascii=False)


def _verified_slot_conflict_context(payload: dict, error: OrderedCharacterSlotConflict) -> dict | None:
    """Recheck C/P/Q and any displayed key against input, never response values."""
    candidates = payload.get("candidates", [])
    candidate_index = next((index for index, item in enumerate(candidates)
                            if isinstance(item, dict) and item.get("candidate_ref") == error.candidate_ref), None)
    if candidate_index is None or not re.fullmatch(r"C[1-9][0-9]*", error.candidate_ref):
        return None
    candidate = candidates[candidate_index]
    if not isinstance(candidate.get("initial_canonical_fact_key"), str):
        return None
    context = {"candidate_ref": error.candidate_ref,
               "initial_canonical_fact_key": candidate["initial_canonical_fact_key"]}
    if payload.get("canonical_fact_type") != error.target_fact_type:
        return None
    if re.fullmatch(r"P[1-9][0-9]*", error.target_ref):
        target = next((item for item in payload.get("snapshot_entries", [])
                       if isinstance(item, dict) and item.get("ref") == error.target_ref), None)
        if (target is None or target.get("fact_type") != error.target_fact_type
                or target.get("fact_key") != error.target_fact_key):
            return None
        context.update(active_target_ref=error.target_ref, fact_type=target["fact_type"],
                       fact_key=target["fact_key"])
        return context
    if re.fullmatch(r"Q[1-9][0-9]*", error.target_ref):
        target = next((item for item in candidates[:candidate_index] if isinstance(item, dict)
                       and item.get("projected_snapshot_ref") == error.target_ref
                       and item.get("candidate_ref") == error.target_source_candidate_ref), None)
        if target is None:
            return None
        context.update(active_target_ref=error.target_ref,
                       target_source_candidate_ref=target["candidate_ref"],
                       fact_type=payload["canonical_fact_type"])
        # A normalized STATUS key may originate in a prior response. The active Q
        # still identifies that decision, but only an input-owned key is emitted.
        if target.get("initial_canonical_fact_key") == error.target_fact_key:
            context["fact_key"] = target["initial_canonical_fact_key"]
        return context
    return None


def _build_bounded_batch_retry_user_prompt(
    original_user_prompt: str,
    exc: Exception,
    *,
    system_prompt: str,
    model: str,
    max_input_tokens: int,
    ordered_context: bool = False,
) -> str:
    retry_prompt = _build_batch_retry_user_prompt(original_user_prompt, exc, ordered_context=ordered_context)
    if _estimate_prompt_tokens(system_prompt, retry_prompt, model) > max_input_tokens:
        if ordered_context:
            raise OrderedInputContextError("character_batch_input_limit_exceeded")
        raise ComparisonValidationError("character_batch_input_limit_exceeded")
    return retry_prompt


def _resolve_max_attempts(max_attempts: int | None) -> int:
    resolved = get_settings().llm_extraction_max_attempts if max_attempts is None else max_attempts
    if resolved < 1:
        raise ValueError("max_attempts must be at least 1.")
    return resolved


def _validate_comparison_decision(
    decision: CharacterFactComparisonDecision,
    candidate: WorkerCharacterFactComparisonCandidatePayload,
    references: list[CharacterSnapshotReference],
) -> None:
    entries_by_ref = {
        reference.reference: CharacterProjectionEntry(
            reference=reference.reference,
            fact_type=reference.entry.fact_type,
            fact_key=reference.entry.fact_key,
            fact_value=reference.entry.fact_value,
            value_json=reference.entry.value_json,
        )
        for reference in references
    }
    validate_character_fact_decision(
        decision,
        candidate_fact_type=candidate.canonical_fact_type,
        resolved_fact_key=candidate.canonical_fact_key,
        candidate_value_type=candidate.value_type,
        candidate_value_json=candidate.value_json,
        entries_by_ref=entries_by_ref,
    )
    reason_refs = set(SNAPSHOT_REFERENCE_PATTERN.findall(decision.comparison_reason))
    unknown_reason_refs = reason_refs - entries_by_ref.keys()
    if unknown_reason_refs:
        raise ValueError(
            f"Unknown snapshot refs in comparison reason: {sorted(unknown_reason_refs)}"
        )
    _validate_user_facing_reason(decision.comparison_reason, candidate, references)



def _normalize_scalar_proposal(
    decision: CharacterFactComparisonDecision,
    candidate: WorkerCharacterFactComparisonCandidatePayload,
) -> CharacterFactComparisonDecision:
    if decision.proposed_value_json is None:
        return decision
    normalized = normalize_setting_display_value(
        candidate.value_type,
        decision.proposed_value_json,
        decision.proposed_fact_value,
    )
    if normalized == decision.proposed_fact_value:
        return decision
    return decision.model_copy(update={"proposed_fact_value": normalized})


def _validate_user_facing_reason(
    comparison_reason: str,
    candidate: WorkerCharacterFactComparisonCandidatePayload,
    references: list[CharacterSnapshotReference],
) -> None:
    """검토 화면에 그대로 노출되는 설명에서 내부 구현 식별자를 거절한다."""

    _validate_user_facing_reason_values(
        comparison_reason,
        candidate.canonical_fact_key,
        [reference.entry.fact_key for reference in references],
        display_names=(candidate.entity_name, candidate.matched_character_name),
    )


def _validate_user_facing_reason_values(
    comparison_reason: str,
    candidate_fact_key: str,
    snapshot_fact_keys: list[str],
    *,
    display_names: tuple[str, ...] = (),
) -> None:
    if CANDIDATE_REFERENCE_PATTERN.search(comparison_reason):
        raise ValueError("comparison_reason must not expose request-local candidate refs.")
    internal_fact_keys = {candidate_fact_key, *snapshot_fact_keys}
    normalized_reason = comparison_reason.casefold()
    leaked_fact_keys = sorted(
        fact_key for fact_key in internal_fact_keys if fact_key.casefold() in normalized_reason
    )
    if leaked_fact_keys:
        raise ValueError("comparison_reason must not expose internal Fact keys.")
    display_reason = mask_display_names(comparison_reason, display_names)
    if UUID_PATTERN.search(comparison_reason) or INTERNAL_REASON_TERM_PATTERN.search(
        display_reason
    ):
        raise ValueError("comparison_reason must not expose internal implementation terms.")


def _replace_internal_snapshot_references(
    decision: CharacterFactComparisonDecision,
    references: list[CharacterSnapshotReference],
) -> CharacterFactComparisonDecision:
    comparison_reason = decision.comparison_reason
    for reference in sorted(references, key=lambda item: len(item.reference), reverse=True):
        display_value = (reference.entry.fact_value or "").strip()
        replacement = f"현재 '{display_value}' 설정" if display_value else "현재 관련 설정"
        particle_by_input = {
            "을": "을",
            "를": "을",
            "이": "이",
            "가": "이",
            "은": "은",
            "는": "은",
            "과": "과",
            "와": "과",
            "으로": "으로",
            "로": "으로",
        }
        comparison_reason = re.sub(
            # Python의 Unicode \b는 숫자 뒤 한글 조사도 word 문자로 보므로 `P1을`을 놓친다.
            # ASCII ref 토큰의 좌우만 제한해 조사와 붙은 표현은 치환하고 P1/P10은 구분한다.
            rf"(?<![A-Za-z0-9]){re.escape(reference.reference)}"
            rf"(?P<particle>으로|을|를|이|가|은|는|과|와|로)?(?![A-Za-z0-9])",
            lambda match, replacement=replacement, particles=particle_by_input: (
                replacement
                + particles.get(match.group("particle") or "", match.group("particle") or "")
            ),
            comparison_reason,
        )
    if comparison_reason == decision.comparison_reason:
        return decision
    return decision.model_copy(update={"comparison_reason": comparison_reason})


def _projection_entries(
    entries: list[WorkerCharacterFactComparisonBatchSnapshotEntry | CharacterProjectionEntry],
) -> list[CharacterProjectionEntry]:
    return [
        entry
        if isinstance(entry, CharacterProjectionEntry)
        else CharacterProjectionEntry(
            reference=entry.snapshot_ref,
            origin=entry.origin,
            source_candidate_ref=entry.source_candidate_ref,
            dependency_candidate_refs=tuple(entry.dependency_candidate_refs),
            provenance=entry.provenance,
            fact_type=entry.fact_type,
            fact_key=entry.fact_key,
            fact_value=entry.fact_value,
            value_json=entry.value_json,
        )
        for entry in entries
    ]


def _validate_batch_candidates(
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    canonical_fact_type: str,
) -> None:
    refs = [candidate.candidate_ref for candidate in candidates]
    projected_refs = [candidate.projected_snapshot_ref for candidate in candidates]
    if len(refs) != len(set(refs)):
        raise ValueError("Character comparison batch candidate refs must be unique.")
    if len(projected_refs) != len(set(projected_refs)):
        raise ValueError("Character comparison projected snapshot refs must be unique.")
    candidate_indexes = [int(reference[1:]) for reference in refs]
    projected_indexes = [int(reference[1:]) for reference in projected_refs]
    if candidate_indexes != projected_indexes:
        raise ValueError("Each Cn candidate must own the corresponding Qn slot.")
    if candidate_indexes != sorted(candidate_indexes):
        raise ValueError("Character comparison candidates must follow local ref chronology.")
    for candidate in candidates:
        validate_status_active_value(
            canonical_fact_type,
            candidate.value_json,
            field_name=f"candidate[{candidate.candidate_ref}].value_json",
        )


def _add_ordered_snapshot_provenance(payload: dict, entries: list[CharacterProjectionEntry]) -> None:
    for item, entry in zip(payload["snapshot_entries"], entries, strict=True):
        try:
            item.update(ordered_provenance_data(entry.provenance))
        except ComparisonValidationError as error:
            raise OrderedInputContextError("Character snapshot provenance is invalid.") from error


def _batch_prompt_payload(
    matched_character_name: str,
    canonical_fact_type: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    snapshot_entries: list[CharacterProjectionEntry],
) -> dict:
    return {
        "matched_character_name": matched_character_name,
        "canonical_fact_type": canonical_fact_type,
        "candidates": [
            {
                "candidate_ref": candidate.candidate_ref,
                "projected_snapshot_ref": candidate.projected_snapshot_ref,
                "source_episode_no": candidate.source_episode_no,
                "raw_fact_key": candidate.raw_fact_key,
                "initial_canonical_fact_key": candidate.initial_canonical_fact_key,
                "canonical_key_resolution": candidate.canonical_key_resolution,
                "attribute_value": candidate.attribute_value,
                "value_json": candidate.value_json,
                "value_type": candidate.value_type,
                "confidence": candidate.confidence,
                "evidence_spans": [
                    evidence.model_dump(mode="json")
                    for evidence in candidate.evidence_spans
                ],
            }
            for candidate in candidates
        ],
        "snapshot_entries": [
            {
                "ref": entry.reference,
                "origin": entry.origin,
                "source_candidate_ref": entry.source_candidate_ref,
                "fact_type": entry.fact_type,
                "fact_key": entry.fact_key,
                "fact_value": entry.fact_value,
                "value_json": entry.value_json,
            }
            for entry in snapshot_entries
        ],
    }


def _validate_batch_comparison_result(
    result: CharacterFactComparisonBatchResult,
    canonical_fact_type: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    initial_entries: list[CharacterProjectionEntry],
    *,
    ordered_context: bool = False,
    display_names: tuple[str, ...] = (),
) -> None:
    expected_refs = [candidate.candidate_ref for candidate in candidates]
    actual_refs = [decision.candidate_ref for decision in result.decisions]
    if actual_refs != expected_refs:
        raise ValueError(
            "Batch decisions must cover every candidate exactly once in input order."
        )

    state = CharacterProjectionState(initial_entries)
    for candidate, decision in zip(candidates, result.decisions, strict=True):
        validate_resolved_canonical_fact_key(
            initial_fact_key=candidate.initial_canonical_fact_key,
            resolved_fact_key=decision.resolved_canonical_fact_key,
            canonical_key_resolution=candidate.canonical_key_resolution,
            fact_type=canonical_fact_type,
        )
        active_entries = state.entries
        active_refs = {entry.reference for entry in active_entries}
        reason_refs = set(SNAPSHOT_REFERENCE_PATTERN.findall(decision.comparison_reason))
        unknown_reason_refs = reason_refs - active_refs
        if unknown_reason_refs:
            raise ValueError(
                "Unknown or future snapshot refs in comparison reason: "
                f"{sorted(unknown_reason_refs)}"
            )
        _validate_user_facing_reason_values(
            decision.comparison_reason,
            decision.resolved_canonical_fact_key,
            [entry.fact_key for entry in active_entries],
            display_names=display_names,
        )
        try:
            state.apply(
                candidate_ref=candidate.candidate_ref,
                projected_snapshot_ref=candidate.projected_snapshot_ref,
                fact_type=canonical_fact_type,
                resolved_fact_key=decision.resolved_canonical_fact_key,
                value_type=candidate.value_type,
                candidate_value_json=candidate.value_json,
                decision=decision,
            )
        except ValueError as error:
            # Preserve every validator and all other errors. Only this exact
            # domain-output rule gets input-owned ordered retry context.
            if (ordered_context and type(error) is ValueError
                    and error.args == ("ADD is invalid when the canonical Fact slot already exists.",)):
                target_ref = state.exact_target_ref(canonical_fact_type, decision.resolved_canonical_fact_key)
                target = state.entries_by_ref.get(target_ref)
                if target is not None:
                    raise OrderedCharacterSlotConflict(candidate.candidate_ref, target) from error
            raise


def _normalize_batch_comparison_result(
    result: CharacterFactComparisonBatchResult,
    canonical_fact_type: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    initial_entries: list[CharacterProjectionEntry],
) -> CharacterFactComparisonBatchResult:
    state = CharacterProjectionState(initial_entries)
    normalized_decisions: list[CharacterFactComparisonBatchDecision] = []
    for candidate, decision in zip(candidates, result.decisions, strict=True):
        active_entries = state.entries
        normalized = _normalize_scalar_proposal(decision, candidate)
        normalized = _replace_projection_references(normalized, active_entries)
        state.apply(
            candidate_ref=candidate.candidate_ref,
            projected_snapshot_ref=candidate.projected_snapshot_ref,
            fact_type=canonical_fact_type,
            resolved_fact_key=normalized.resolved_canonical_fact_key,
            value_type=candidate.value_type,
            candidate_value_json=candidate.value_json,
            decision=normalized,
        )
        normalized_decisions.append(normalized)
    return CharacterFactComparisonBatchResult(decisions=normalized_decisions)


def _independent_ordered_decisions(
    payload: dict | None,
    canonical_fact_type: str,
    candidates: list[WorkerCharacterFactComparisonBatchCandidate],
    initial_entries: list[CharacterProjectionEntry],
    *,
    display_names: tuple[str, ...] = (),
) -> tuple[CharacterFactComparisonBatchDecision | None, ...]:
    """Revalidate only the final response, skipping uncertain writes and their suffix.

    Fixed non-STATUS keys bound each failed candidate's possible effects to that
    key. STATUS can rename a key or remove other statuses, so after a failed
    STATUS every later candidate must be reconsidered. Missing/duplicate/order
    errors make all decisions untrusted. Provider values stay in memory only.
    """
    empty = (None,) * len(candidates)
    rows = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != len(candidates):
        return empty
    if any(not isinstance(row, dict) or row.get("candidate_ref") != candidate.candidate_ref
           for row, candidate in zip(rows, candidates, strict=True)):
        return empty
    state = CharacterProjectionState(initial_entries)
    blocked_keys: set[str] = set()
    uncertain_status = False
    retained = []
    for row, candidate in zip(rows, candidates, strict=True):
        if uncertain_status or candidate.initial_canonical_fact_key in blocked_keys:
            retained.append(None)
            continue
        try:
            result = CharacterFactComparisonBatchResult(decisions=[
                CharacterFactComparisonBatchDecision.model_validate(row),
            ])
            _validate_batch_comparison_result(
                result, canonical_fact_type, [candidate], state.entries, ordered_context=True,
                display_names=display_names,
            )
            normalized = _normalize_batch_comparison_result(
                result, canonical_fact_type, [candidate], state.entries,
            ).decisions[0]
            state.apply(
                candidate_ref=candidate.candidate_ref,
                projected_snapshot_ref=candidate.projected_snapshot_ref,
                fact_type=canonical_fact_type,
                resolved_fact_key=normalized.resolved_canonical_fact_key,
                value_type=candidate.value_type,
                candidate_value_json=candidate.value_json,
                decision=normalized,
            )
        except (TypeError, ValueError):
            blocked_keys.add(candidate.initial_canonical_fact_key)
            uncertain_status = canonical_fact_type == "STATUS"
            retained.append(None)
        else:
            retained.append(normalized)
    return tuple(retained)


def _replace_projection_references(
    decision: CharacterFactComparisonBatchDecision,
    entries: list[CharacterProjectionEntry],
) -> CharacterFactComparisonBatchDecision:
    references = [
        CharacterSnapshotReference(
            reference=entry.reference,
            entry=WorkerCharacterSnapshotEntry(
                fact_type=entry.fact_type,
                fact_key=entry.fact_key,
                fact_value=entry.fact_value,
                value_json=entry.value_json,
            ),
        )
        for entry in entries
    ]
    return _replace_internal_snapshot_references(decision, references)


def _estimate_prompt_tokens(system_prompt: str, user_prompt: str, model: str) -> int:
    try:
        encoding = (
            tiktoken.get_encoding("o200k_base")
            if model.startswith("gpt-5.6")
            else tiktoken.encoding_for_model(model)
        )
        content_tokens = len(encoding.encode(system_prompt, disallowed_special=())) + len(
            encoding.encode(user_prompt, disallowed_special=())
        )
        return int(content_tokens * 1.10) + 256
    except Exception:  # noqa: BLE001 - a byte bound keeps splitting deterministic.
        return len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8")) + 512
