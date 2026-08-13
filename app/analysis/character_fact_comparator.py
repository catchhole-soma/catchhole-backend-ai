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
    ) -> None:
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model or get_settings().effective_llm_comparison_model
        self.max_attempts = _resolve_max_attempts(max_attempts)

    async def compare(
        self,
        candidate: WorkerCharacterFactComparisonCandidatePayload,
        snapshot_entries: list[WorkerCharacterSnapshotEntry],
        prior_candidates: list[WorkerCharacterPriorFactCandidate] | None = None,
    ) -> tuple[CharacterFactComparisonDecision, dict]:
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
        allowed_operations = (
            ["UPDATE", "MERGE", "HISTORY_ONLY", "EXCLUDE", "REVIEW_REQUIRED"]
            if exact_target_ref is not None
            else ["ADD", "HISTORY_ONLY", "EXCLUDE", "REVIEW_REQUIRED"]
        )
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
            max_output_tokens=2000,
            max_attempts=self.max_attempts,
            prompt_cache_key="character-fact-comparison:v4",
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
        return decision, decision.model_dump(mode="json")


def _build_retry_user_prompt(original_user_prompt: str, exc: Exception) -> str:
    """검증 실패 이유를 다음 시도의 보정 지시로만 전달한다."""

    payload = json.loads(original_user_prompt)
    payload["validation_feedback"] = {
        "previous_response_rejected": True,
        "reason": compact_error_message(exc),
        "correction": (
            "allowed_operations 중 하나만 선택하세요. UPDATE 또는 MERGE는 "
            "exact_target_ref가 null이 아닐 때만 사용할 수 있고 target_ref는 "
            "exact_target_ref와 정확히 같아야 합니다. 의미가 비슷하지만 key가 다른 "
            "STATUS를 대체하려면 ADD와 removed_snapshot_refs를 사용하세요."
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
        }
    ):
        raise ValueError(
            "Snapshot removal requires an explicit PRESENT STATUS addition or replacement."
        )


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
            lambda match: replacement
            + particle_by_input.get(match.group("particle") or "", match.group("particle") or ""),
            comparison_reason,
        )
    if comparison_reason == decision.comparison_reason:
        return decision
    return decision.model_copy(update={"comparison_reason": comparison_reason})
