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
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.json_response import compact_error_message, request_validated_model
from app.core.config import get_settings
from app.domain.setting_values import normalize_setting_display_value
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
CHARACTER_FACT_COMPARISON_BATCH_CACHE_KEY = "character-fact-comparison-batch:v2"
logger = logging.getLogger(__name__)
SNAPSHOT_REFERENCE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[PQ][0-9]+(?![A-Za-z0-9])")
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
            system_prompt=self.prompt_path.read_text(encoding="utf-8"),
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="character-fact-comparison:v9",
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
        _validate_batch_candidates(candidates, canonical_fact_type)
        prompt_payload = _batch_prompt_payload(
            matched_character_name,
            canonical_fact_type,
            candidates,
            initial_entries,
        )
        system_prompt = self.batch_prompt_path.read_text(encoding="utf-8")
        user_prompt = json.dumps(prompt_payload, ensure_ascii=False)
        estimated_input_tokens = _estimate_prompt_tokens(system_prompt, user_prompt, self.model)
        if estimated_input_tokens > self.batch_max_input_tokens:
            raise ComparisonValidationError("character_batch_input_limit_exceeded")

        result = await request_validated_model(
            client=self.llm_client,
            response_model=CharacterFactComparisonBatchResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=self.batch_max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key=CHARACTER_FACT_COMPARISON_BATCH_CACHE_KEY,
            operation_name="Character-fact batch comparison",
            logger=logger,
            validate_model=lambda comparison_result: _validate_batch_comparison_result(
                comparison_result,
                canonical_fact_type,
                candidates,
                initial_entries,
            ),
            retry_user_prompt_builder=_build_batch_retry_user_prompt,
        )
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
    ) -> bool:
        if not candidates or len(candidates) > self.batch_max_candidates:
            return False
        initial_entries = _projection_entries(snapshot_entries)
        prompt_payload = _batch_prompt_payload(
            matched_character_name,
            canonical_fact_type,
            candidates,
            initial_entries,
        )
        return _estimate_prompt_tokens(
            self.batch_prompt_path.read_text(encoding="utf-8"),
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
            "판단 이유에는 "
            "내부 key·enum·UUID를 쓰지 말고 사용자가 이해할 수 있는 한국어만 쓰세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_batch_retry_user_prompt(original_user_prompt: str, exc: Exception) -> str:
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
            "남기지 않으면서 관련 STATUS를 한 개 이상 종료할 때만 선택하세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


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
    )


def _validate_user_facing_reason_values(
    comparison_reason: str,
    candidate_fact_key: str,
    snapshot_fact_keys: list[str],
) -> None:
    internal_fact_keys = {candidate_fact_key, *snapshot_fact_keys}
    normalized_reason = comparison_reason.casefold()
    leaked_fact_keys = sorted(
        fact_key for fact_key in internal_fact_keys if fact_key.casefold() in normalized_reason
    )
    if leaked_fact_keys:
        raise ValueError("comparison_reason must not expose internal Fact keys.")
    if UUID_PATTERN.search(comparison_reason) or INTERNAL_REASON_TERM_PATTERN.search(
        comparison_reason
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
        )
        state.apply(
            candidate_ref=candidate.candidate_ref,
            projected_snapshot_ref=candidate.projected_snapshot_ref,
            fact_type=canonical_fact_type,
            resolved_fact_key=decision.resolved_canonical_fact_key,
            value_type=candidate.value_type,
            candidate_value_json=candidate.value_json,
            decision=decision,
        )


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
