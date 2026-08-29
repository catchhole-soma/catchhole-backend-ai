import json
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.analysis.json_response import compact_error_message, request_validated_model
from app.analysis.world_setting_schemas import (
    WorldSettingComparisonDecision,
    WorldSettingSubjectSelection,
)
from app.core.config import get_settings
from app.domain.enums import (
    WorldSettingComparisonReviewReason,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingSubject,
)

SUBJECT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "world_setting_subject_resolution.md"
)
COMPARISON_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "world_setting_comparison.md"
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubjectReference:
    reference: str
    world_setting_id: UUID
    subject_name: str


@dataclass(frozen=True)
class ComparisonTargetReference:
    reference: str
    target: WorkerWorldSettingComparisonTarget


@dataclass(frozen=True)
class ScopeAmbiguityMatch:
    target_ref: str
    scope_name: str
    property_name: str


class WorldSettingSubjectResolver:
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = SUBJECT_PROMPT_PATH,
        model: str | None = None,
        max_attempts: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        settings = get_settings()
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model or settings.effective_llm_subject_resolution_model
        self.max_attempts = _resolve_max_attempts(max_attempts)
        self.max_output_tokens = (
            settings.llm_subject_resolution_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )

    async def select_subjects(
        self,
        candidate: WorkerWorldSettingCandidatePayload,
        subjects: list[WorkerWorldSettingSubject],
    ) -> list[SubjectReference]:
        references = [
            SubjectReference(
                reference=f"S{index}",
                world_setting_id=subject.world_setting_id,
                subject_name=subject.subject_name,
            )
            for index, subject in enumerate(subjects, start=1)
        ]
        if not references:
            return []

        payload = await request_validated_model(
            client=self.llm_client,
            response_model=WorldSettingSubjectSelection,
            system_prompt=self.prompt_path.read_text(encoding="utf-8"),
            user_prompt=json.dumps(
                {
                    "candidate": {
                        "category": candidate.category,
                        "subject_name": candidate.subject_name,
                    },
                    "subjects": [
                        {
                            "ref": subject_reference.reference,
                            "subject_name": subject_reference.subject_name,
                        }
                        for subject_reference in references
                    ],
                },
                ensure_ascii=False,
            ),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="world-setting-subject-resolution:v1",
            operation_name="World-setting subject resolution",
            logger=logger,
            validate_model=lambda selection: _validate_subject_refs(
                selection,
                {subject_reference.reference for subject_reference in references},
            ),
        )
        references_by_key = {
            subject_reference.reference: subject_reference for subject_reference in references
        }
        return [references_by_key[ref] for ref in payload.selected_subject_refs]


class WorldSettingComparator:
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
        candidate: WorkerWorldSettingCandidatePayload,
        targets: list[WorkerWorldSettingComparisonTarget],
    ) -> tuple[WorldSettingComparisonDecision, dict]:
        references = [
            ComparisonTargetReference(reference=f"T{index}", target=target)
            for index, target in enumerate(targets, start=1)
        ]
        raw_payload = {
            "candidate": {
                "category": candidate.category,
                "subject_name": candidate.subject_name,
                "scope_name": candidate.scope_name,
                "setting_name": candidate.setting_name,
                "extracted_value": candidate.extracted_value,
                "extracted_values": _source_values(candidate),
                "evidence_spans": [
                    evidence.model_dump(mode="json") for evidence in candidate.evidence_spans
                ],
            },
            "targets": [
                {
                    "ref": target_reference.reference,
                    "subject_name": target_reference.target.subject_name,
                    "properties": [
                        property.model_dump(mode="json")
                        for property in target_reference.target.properties
                    ],
                }
                for target_reference in references
            ],
        }
        decision = await request_validated_model(
            client=self.llm_client,
            response_model=WorldSettingComparisonDecision,
            system_prompt=self.prompt_path.read_text(encoding="utf-8"),
            user_prompt=json.dumps(raw_payload, ensure_ascii=False),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="world-setting-comparison:v9",
            operation_name="World-setting comparison",
            logger=logger,
            validate_model=lambda comparison_decision: _validate_comparison_decision(
                comparison_decision,
                candidate,
                references,
            ),
            retry_user_prompt_builder=_build_retry_user_prompt,
        )
        # scope 없는 후보를 다른 scope의 동명 속성에 연결한 응답은 실패로 재시도하지
        # 않고 사용자가 범위를 선택하는 정상 검토 결과로 바꾼다.
        decision = _normalize_scope_ambiguity(decision, candidate, references)
        # LLM이 판단할 필요가 없는 불변 필드는 원본 후보에서 복원한다. 단일
        # ADD/EXCLUDE/REVIEW_REQUIRED 값을 문장만 다듬어 반환했다는 이유로 같은 비교를
        # 반복하지 않게 한다.
        decision = _normalize_deterministic_fields(decision, candidate)
        decision = _replace_internal_target_references(decision, references)
        return decision, decision.model_dump(mode="json")


def _resolve_max_attempts(max_attempts: int | None) -> int:
    resolved = get_settings().llm_extraction_max_attempts if max_attempts is None else max_attempts
    if resolved < 1:
        raise ValueError("max_attempts must be at least 1.")
    return resolved


def _build_retry_user_prompt(original_user_prompt: str, exc: Exception) -> str:
    """남은 관계형 검증 오류를 다음 비교 시도의 보정 지시로 전달한다."""

    payload = json.loads(original_user_prompt)
    payload["validation_feedback"] = {
        "previous_response_rejected": True,
        "reason": compact_error_message(exc),
        "correction": (
            "입력에 실제 존재하는 target ref와 속성 경로만 사용하세요. "
            "UPDATE와 MERGE는 선택한 기존 속성의 범위명과 설정명을 그대로 유지하고, "
            "ADD는 기존 속성을 비교 대상으로 지정하지 마세요. 후보 범위가 없고 다른 "
            "범위의 동명 속성만 관련될 수 있으면 REVIEW_REQUIRED와 SCOPE_UNRESOLVED를 "
            "사용하세요. JSON 전체를 다시 반환하세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _normalize_deterministic_fields(
    decision: WorldSettingComparisonDecision,
    candidate: WorkerWorldSettingCandidatePayload,
) -> WorldSettingComparisonDecision:
    """비교 판단과 무관하게 원본에서 결정되는 출력 필드를 복원한다."""

    source_values = _source_values(candidate)
    updates: dict[str, object] = {}
    if len(source_values) == 1:
        updates["consolidation_status"] = WorldSettingConsolidationStatus.SINGLE
    # 실제 복수 원문 값이 충돌할 때만 원문 목록을 보존한다. 단일 후보에서 모델이
    # CONFLICT를 잘못 반환한 경우에는 SINGLE로 정규화하되 MERGE 최종값은 유지한다.
    if (
        len(source_values) > 1
        and decision.consolidation_status == WorldSettingConsolidationStatus.CONFLICT
    ):
        updates["proposed_value"] = candidate.extracted_value
    if decision.operation in {
        WorldSettingOperation.ADD,
        WorldSettingOperation.EXCLUDE,
        WorldSettingOperation.REVIEW_REQUIRED,
    }:
        updates["proposed_scope_name"] = candidate.scope_name
        updates["proposed_setting_name"] = candidate.setting_name
        if len(source_values) == 1:
            updates["proposed_value"] = source_values[0]
    return decision.model_copy(update=updates)


def _validate_subject_refs(
    selection: WorldSettingSubjectSelection,
    allowed_refs: set[str],
) -> None:
    unknown_refs = set(selection.selected_subject_refs) - allowed_refs
    if unknown_refs:
        raise ValueError(f"Unknown subject refs: {sorted(unknown_refs)}")


def _validate_comparison_decision(
    decision: WorldSettingComparisonDecision,
    candidate: WorkerWorldSettingCandidatePayload,
    references: list[ComparisonTargetReference],
) -> None:
    source_values = _source_values(candidate)
    if len(source_values) > 1 and decision.consolidation_status == "SINGLE":
        raise ValueError("Multiple extracted values must use MERGED or CONFLICT status.")

    references_by_key = {
        target_reference.reference: target_reference.target for target_reference in references
    }
    if decision.target_ref is not None and decision.target_ref not in references_by_key:
        raise ValueError(f"Unknown comparison target_ref: {decision.target_ref}")
    if decision.operation == WorldSettingOperation.REVIEW_REQUIRED:
        if not _is_scope_ambiguity_match(decision, candidate, references_by_key):
            raise ValueError(
                "SCOPE_UNRESOLVED must match a same-name property under a different scope."
            )
        return
    if _is_scope_ambiguity_match(decision, candidate, references_by_key):
        # 구버전 또는 비결정적인 모델이 UPDATE/MERGE/EXCLUDE로 반환해도 실제 concrete
        # operation으로 통과시키지 않고 compare()에서 REVIEW_REQUIRED로 정규화한다.
        return
    if decision.operation in {WorldSettingOperation.ADD, WorldSettingOperation.EXCLUDE}:
        # 범위명·설정명·단일 추출값은 compare()가 후보 원본으로 정규화한다.
        # 여기서는 LLM이 판단한 operation과 비교 대상 관계만 검증한다.
        pass
    if decision.operation == WorldSettingOperation.ADD:
        return
    if decision.operation == WorldSettingOperation.EXCLUDE:
        if decision.matched_property_name is None:
            return
        if decision.matched_scope_name != candidate.scope_name:
            raise ValueError("A matched property must use the extracted scope name.")
        target = references_by_key[decision.target_ref]
        if not _has_property(
            target,
            decision.matched_scope_name,
            decision.matched_property_name,
        ):
            raise ValueError("The matched property path does not exist in the selected target.")
        return
    if decision.matched_scope_name != candidate.scope_name:
        raise ValueError("UPDATE and MERGE must match the extracted scope name.")
    target = references_by_key[decision.target_ref]
    if not _has_property(
        target,
        decision.matched_scope_name,
        decision.matched_property_name,
    ):
        raise ValueError("The matched property path does not exist in the selected target.")
    if decision.proposed_scope_name != decision.matched_scope_name:
        raise ValueError("UPDATE and MERGE must preserve the stored scope name.")
    if decision.proposed_setting_name != decision.matched_property_name:
        raise ValueError("UPDATE and MERGE must preserve the stored property name.")


def _normalize_scope_ambiguity(
    decision: WorldSettingComparisonDecision,
    candidate: WorkerWorldSettingCandidatePayload,
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonDecision:
    match = _find_scope_ambiguity_match(decision, candidate, references)
    if match is None:
        return decision
    matched_path = f"{match.scope_name} › {match.property_name}"
    return decision.model_copy(
        update={
            "operation": WorldSettingOperation.REVIEW_REQUIRED,
            "review_reason": WorldSettingComparisonReviewReason.SCOPE_UNRESOLVED,
            "target_ref": match.target_ref,
            "matched_scope_name": match.scope_name,
            "matched_property_name": match.property_name,
            "proposed_scope_name": candidate.scope_name,
            "proposed_setting_name": candidate.setting_name,
            "comparison_reason": (
                f"후보에는 범위가 없지만 기존 '{matched_path}' 설정과 관련될 수 있어 "
                "적용 범위 확인이 필요합니다."
            ),
        }
    )


def _find_scope_ambiguity_match(
    decision: WorldSettingComparisonDecision,
    candidate: WorkerWorldSettingCandidatePayload,
    references: list[ComparisonTargetReference],
) -> ScopeAmbiguityMatch | None:
    """모델의 operation과 무관하게 범위가 빠진 동명 후보를 찾는다."""

    if candidate.scope_name is not None:
        return None

    candidate_name = _normalized_name(candidate.setting_name)
    scoped_matches: list[ScopeAmbiguityMatch] = []
    for target_reference in references:
        for property in target_reference.target.properties:
            if _normalized_name(property.setting_name) != candidate_name:
                continue
            # 후보와 동일한 root 경로가 하나라도 있으면 scope가 빠진 것이 아니다.
            if property.scope_name is None:
                return None
            scoped_matches.append(
                ScopeAmbiguityMatch(
                    target_ref=target_reference.reference,
                    scope_name=property.scope_name,
                    property_name=property.setting_name,
                )
            )

    if not scoped_matches:
        return None

    # 모델이 실제 scoped 경로를 골랐다면 그 선택을 보존한다. ADD처럼 경로를 전혀
    # 반환하지 않은 경우에는 같은 target, 그마저 없으면 입력 순서의 첫 경로를 쓴다.
    for match in scoped_matches:
        if (
            decision.target_ref == match.target_ref
            and decision.matched_scope_name == match.scope_name
            and _normalized_name(decision.matched_property_name or "")
            == _normalized_name(match.property_name)
        ):
            return match
    if decision.target_ref is not None:
        for match in scoped_matches:
            if match.target_ref == decision.target_ref:
                return match
    return scoped_matches[0]


def _is_scope_ambiguity_match(
    decision: WorldSettingComparisonDecision,
    candidate: WorkerWorldSettingCandidatePayload,
    references_by_key: dict[str, WorkerWorldSettingComparisonTarget],
) -> bool:
    if (
        candidate.scope_name is not None
        or decision.target_ref is None
        or decision.matched_scope_name is None
        or decision.matched_property_name is None
        or _normalized_name(decision.matched_property_name)
        != _normalized_name(candidate.setting_name)
    ):
        return False
    target = references_by_key.get(decision.target_ref)
    if target is None:
        return False
    # 같은 root 경로가 이미 있으면 그 경로를 우선 비교해야 하므로 scope 미확정이 아니다.
    if _has_property(target, candidate.scope_name, candidate.setting_name):
        return False
    return _has_property(
        target,
        decision.matched_scope_name,
        decision.matched_property_name,
    )


def _has_property(
    target: WorkerWorldSettingComparisonTarget,
    scope_name: str | None,
    setting_name: str,
) -> bool:
    return any(
        property.scope_name == scope_name and property.setting_name == setting_name
        for property in target.properties
    )


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def _replace_internal_target_references(
    decision: WorldSettingComparisonDecision,
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonDecision:
    comparison_reason = decision.comparison_reason
    for target_reference in references:
        comparison_reason = comparison_reason.replace(
            target_reference.reference,
            f"기존 '{target_reference.target.subject_name}' 설정",
        )
    if comparison_reason == decision.comparison_reason:
        return decision
    return decision.model_copy(update={"comparison_reason": comparison_reason})


def _source_values(candidate: WorkerWorldSettingCandidatePayload) -> list[str]:
    values = [value.strip() for value in candidate.extracted_value.splitlines() if value.strip()]
    return values or [candidate.extracted_value]
