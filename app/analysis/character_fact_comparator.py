import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.analysis.character_fact_comparison_schemas import (
    CharacterFactComparisonDecision,
)
from app.analysis.json_response import compact_error_message, request_validated_model
from app.core.config import get_settings
from app.domain.enums import (
    CharacterFactComparisonOperation,
    CharacterFactTemporalScope,
)
from app.domain.setting_values import normalize_setting_display_value
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.schemas.worker import (
    WorkerCharacterFactComparisonCandidatePayload,
    WorkerCharacterPriorFactCandidate,
    WorkerCharacterSnapshotEntry,
)

COMPARISON_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_fact_comparison.md"
)
logger = logging.getLogger(__name__)
SNAPSHOT_REFERENCE_PATTERN = re.compile(r"(?<![A-Za-z0-9])P[0-9]+(?![A-Za-z0-9])")
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
        model: str | None = None,
        max_attempts: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        settings = get_settings()
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model or settings.effective_llm_comparison_model
        self.max_attempts = _resolve_max_attempts(max_attempts)
        self.max_output_tokens = (
            settings.llm_comparison_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )

    async def compare(
        self,
        candidate: WorkerCharacterFactComparisonCandidatePayload,
        snapshot_entries: list[WorkerCharacterSnapshotEntry],
        prior_candidates: list[WorkerCharacterPriorFactCandidate] | None = None,
    ) -> tuple[CharacterFactComparisonDecision, dict]:
        _validate_status_active_value(
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
        explicit_inactive_status = _is_explicit_inactive_status(candidate)
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
            prompt_cache_key="character-fact-comparison:v8",
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
    _validate_status_active_value(
        candidate.canonical_fact_type,
        candidate.value_json,
        field_name="candidate.value_json",
    )
    entries_by_ref = {reference.reference: reference.entry for reference in references}
    exact_slot_refs = {
        reference.reference
        for reference in references
        if reference.entry.fact_type == candidate.canonical_fact_type
        and reference.entry.fact_key == candidate.canonical_fact_key
    }
    requested_refs = set(decision.removed_snapshot_refs)
    if decision.target_ref is not None:
        requested_refs.add(decision.target_ref)
    unknown_refs = requested_refs - entries_by_ref.keys()
    if unknown_refs:
        raise ValueError(f"Unknown snapshot refs: {sorted(unknown_refs)}")
    reason_refs = set(SNAPSHOT_REFERENCE_PATTERN.findall(decision.comparison_reason))
    unknown_reason_refs = reason_refs - entries_by_ref.keys()
    if unknown_reason_refs:
        raise ValueError(
            f"Unknown snapshot refs in comparison reason: {sorted(unknown_reason_refs)}"
        )
    _validate_user_facing_reason(decision.comparison_reason, candidate, references)

    if decision.target_ref is not None:
        target = entries_by_ref[decision.target_ref]
        if (
            target.fact_type != candidate.canonical_fact_type
            or target.fact_key != candidate.canonical_fact_key
        ):
            raise ValueError("UPDATE and MERGE must target the candidate's canonical Fact key.")
        if decision.target_ref in decision.removed_snapshot_refs:
            raise ValueError("The comparison target must not also be removed.")

    if decision.operation == CharacterFactComparisonOperation.ADD and exact_slot_refs:
        raise ValueError("ADD is invalid when the canonical Fact slot already exists.")

    applies_to_snapshot = decision.operation in {
        CharacterFactComparisonOperation.ADD,
        CharacterFactComparisonOperation.UPDATE,
        CharacterFactComparisonOperation.MERGE,
    }
    if applies_to_snapshot:
        _validate_status_active_value(
            candidate.canonical_fact_type,
            decision.proposed_value_json,
            field_name="proposed_value_json",
        )
        if _is_explicit_inactive_status(candidate) or _has_explicit_inactive_status_value(
            candidate.canonical_fact_type,
            decision.proposed_value_json,
        ):
            raise ValueError("An explicitly inactive STATUS value must not enter the snapshot.")

    if decision.operation == CharacterFactComparisonOperation.REMOVE:
        if candidate.canonical_fact_type != "STATUS":
            raise ValueError("REMOVE is only allowed for a canonical STATUS slot.")

    for removed_ref in decision.removed_snapshot_refs:
        if entries_by_ref[removed_ref].fact_type != "STATUS":
            raise ValueError("Only STATUS snapshot entries may be removed in the MVP.")

    if decision.removed_snapshot_refs and (
        candidate.canonical_fact_type != "STATUS"
        or decision.temporal_scope != CharacterFactTemporalScope.PRESENT
        or decision.operation
        not in {
            CharacterFactComparisonOperation.ADD,
            CharacterFactComparisonOperation.UPDATE,
            CharacterFactComparisonOperation.MERGE,
            CharacterFactComparisonOperation.REMOVE,
        }
    ):
        raise ValueError("Snapshot removal requires a PRESENT STATUS transition.")

    if applies_to_snapshot:
        normalize_setting_display_value(
            candidate.value_type,
            decision.proposed_value_json,
            decision.proposed_fact_value,
        )


def _is_explicit_inactive_status(
    candidate: WorkerCharacterFactComparisonCandidatePayload,
) -> bool:
    return _has_explicit_inactive_status_value(
        candidate.canonical_fact_type,
        candidate.value_json,
    )


def _has_explicit_inactive_status_value(
    canonical_fact_type: str,
    value_json: object,
) -> bool:
    return (
        canonical_fact_type == "STATUS"
        and isinstance(value_json, dict)
        and value_json.get("active") is False
    )


def _validate_status_active_value(
    canonical_fact_type: str,
    value_json: object,
    *,
    field_name: str,
) -> None:
    """STATUS의 active는 오직 JSON boolean만 허용한다.

    문자열 ``"false"``는 truthy 값으로 다뤄질 수 있어 종료 후보가 현재 snapshot에
    들어가는 우회를 만든다. 실제 값은 오류 메시지나 로그에 노출하지 않는다.
    """

    if (
        canonical_fact_type == "STATUS"
        and isinstance(value_json, dict)
        and "active" in value_json
        and type(value_json["active"]) is not bool
    ):
        raise ValueError(f"{field_name}.active must be a JSON boolean for STATUS.")


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

    internal_fact_keys = {
        candidate.canonical_fact_key,
        *(reference.entry.fact_key for reference in references),
    }
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
