import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.analysis.json_response import compact_error_message, request_validated_model
from app.analysis.world_setting_schemas import (
    WorldSettingComparisonBatchDecision,
    WorldSettingComparisonBatchResult,
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
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingSubject,
)

SUBJECT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "world_setting_subject_resolution.md"
)
COMPARISON_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "world_setting_comparison.md"
)
BATCH_COMPARISON_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "llm"
    / "prompts"
    / "world_setting_comparison_batch.md"
)
logger = logging.getLogger(__name__)
LOCAL_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[CST]\d+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
UUID_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])"
)
INTERNAL_REASON_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:ADD|UPDATE|MERGE|EXCLUDE|REVIEW_REQUIRED|"
    r"SINGLE|MERGED|CONFLICT|SCOPE_UNRESOLVED|BATCH_LIMIT_EXCEEDED|"
    r"RACE|FACTION|LOCATION|MONSTER|POWER_SYSTEM|WORLD_RULE_HISTORY|IMPORTANT_ITEM|"
    r"NEW|EXISTING|AMBIGUOUS|PROCESSING|COMPLETED|FAILED|"
    r"WORLD_SETTING_SUBJECT_RESOLUTION_STALE|"
    r"BATCH_RESOLVED_TARGET_COVERAGE_INVALID|"
    r"sourceCandidateRefs|source_candidate_refs|"
    r"candidateRef|candidate_ref|canonicalSubjectKey|canonical_subject_key|category|"
    r"consolidationStatus|consolidation_status|operation|reviewReason|review_reason|"
    r"targetRef|target_ref|matchedScopeName|matched_scope_name|matchedPropertyName|"
    r"matched_property_name|proposedScopeName|proposed_scope_name|proposedSettingName|"
    r"proposed_setting_name|proposedValue|proposed_value|comparisonReason|"
    r"comparison_reason|existingRootPropertyNamesToMove|"
    r"existing_root_property_names_to_move|UUID|version|key)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


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
        batch_prompt_path: Path = BATCH_COMPARISON_PROMPT_PATH,
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

    async def compare_batch(
        self,
        category: str,
        candidates: list[WorkerWorldSettingComparisonBatchCandidate],
        targets: list[WorkerWorldSettingComparisonTarget],
    ) -> tuple[WorldSettingComparisonBatchResult, dict]:
        if not candidates:
            raise ValueError("World-setting comparison batch must include candidates.")
        references = [
            ComparisonTargetReference(reference=f"T{index}", target=target)
            for index, target in enumerate(targets, start=1)
        ]
        raw_payload = {
            "category": category,
            "candidates": [
                {
                    "ref": candidate.candidate_ref,
                    "subject_name": candidate.subject_name,
                    "scope_name": candidate.scope_name,
                    "setting_name": candidate.setting_name,
                    "extracted_value": candidate.extracted_value,
                    "extracted_values": _source_values(candidate),
                    "evidence_spans": [
                        evidence.model_dump(mode="json")
                        for evidence in candidate.evidence_spans
                    ],
                }
                for candidate in candidates
            ],
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
        result = await request_validated_model(
            client=self.llm_client,
            response_model=WorldSettingComparisonBatchResult,
            system_prompt=self.batch_prompt_path.read_text(encoding="utf-8"),
            user_prompt=json.dumps(raw_payload, ensure_ascii=False),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="world-setting-comparison-batch:v4",
            operation_name="World-setting batch comparison",
            logger=logger,
            validate_model=lambda comparison_result: _validate_batch_comparison_result(
                comparison_result,
                candidates,
                references,
            ),
            retry_user_prompt_builder=_build_batch_retry_user_prompt,
        )
        normalized_result = _project_batch_comparison_result(
            result,
            candidates,
            references,
        )
        return normalized_result, normalized_result.model_dump(mode="json")


def _resolve_max_attempts(max_attempts: int | None) -> int:
    resolved = get_settings().llm_extraction_max_attempts if max_attempts is None else max_attempts
    if resolved < 1:
        raise ValueError("max_attempts must be at least 1.")
    return resolved


def _build_batch_retry_user_prompt(original_user_prompt: str, exc: Exception) -> str:
    payload = json.loads(original_user_prompt)
    payload["validation_feedback"] = {
        "previous_response_rejected": True,
        "reason": compact_error_message(exc),
        "correction": (
            "모든 candidate ref를 decisions 전체에서 정확히 한 번 사용하세요. "
            "같은 canonical 속성을 보완하는 후보만 한 decision으로 묶고, 독립 속성은 "
            "분리하세요. UPDATE와 MERGE는 실제 기존 경로를 유지하고 ADD만 새 canonical "
            "경로를 제안할 수 있습니다. 기존 canonical 주체의 cluster라면 ADD와 EXCLUDE도 "
            "그 주체의 target_ref를 유지하세요. 2차가 새 non-null 범위를 만들 때는 최종 "
            "형제 속성이 둘 이상이어야 합니다. 기존 root 속성을 형제로 옮길 때만 "
            "existing_root_property_names_to_move에 실제 root 속성명을 적고, 범위명과 "
            "설정명은 같게 만들지 마세요. ADD와 이동 목적지는 기존 exact 경로나 root와 "
            "scope 구조를 충돌시키지 말고, decisions 사이에서 같은 최종 경로를 중복 "
            "제안하지 마세요. JSON 전체를 다시 반환하세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _validate_batch_comparison_result(
    result: WorldSettingComparisonBatchResult,
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
) -> None:
    candidates_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
    allowed_refs = set(candidates_by_ref)
    seen_refs: set[str] = set()
    references_by_key = {
        target_reference.reference: target_reference.target for target_reference in references
    }
    for decision in result.decisions:
        source_refs = set(decision.source_candidate_refs)
        unknown_refs = source_refs - allowed_refs
        if unknown_refs:
            raise ValueError(f"Unknown source candidate refs: {sorted(unknown_refs)}")
        duplicated_refs = seen_refs & source_refs
        if duplicated_refs:
            raise ValueError(f"Duplicated source candidate refs: {sorted(duplicated_refs)}")
        seen_refs.update(source_refs)
        sources = [candidates_by_ref[ref] for ref in decision.source_candidate_refs]
        if len(sources) > 1 and decision.consolidation_status == "SINGLE":
            raise ValueError("A multi-source decision must use MERGED or CONFLICT status.")
        normalized_scopes = {
            None
            if source.scope_name is None
            else _backend_duplicate_key(source.scope_name)
            for source in sources
        }
        if len(normalized_scopes) != 1:
            raise ValueError("A decision must not mix different explicit source scopes.")
        if decision.target_ref is not None and decision.target_ref not in references_by_key:
            raise ValueError(f"Unknown comparison target_ref: {decision.target_ref}")
        _validate_user_facing_reason(decision.comparison_reason, references)
        if len(references_by_key) == 1 and decision.target_ref is None:
            raise ValueError(
                "Every decision in an existing canonical-subject cluster must preserve "
                "its target_ref."
            )

        if decision.operation == WorldSettingOperation.REVIEW_REQUIRED:
            if len(sources) != 1 or not _is_scope_ambiguity_match(
                decision,
                sources[0],
                references_by_key,
            ):
                raise ValueError("SCOPE_UNRESOLVED must identify one ambiguous source candidate.")
            continue
        if len(sources) == 1 and _is_scope_ambiguity_match(
            decision,
            sources[0],
            references_by_key,
        ):
            continue
        if decision.operation == WorldSettingOperation.ADD:
            if decision.matched_scope_name is not None or decision.matched_property_name is not None:
                raise ValueError("ADD must not include a matched property path.")
            if decision.target_ref is not None:
                target = references_by_key[decision.target_ref]
                if _has_property(
                    target,
                    decision.proposed_scope_name,
                    decision.proposed_setting_name,
                ) or _has_path_conflict(
                    target,
                    decision.proposed_scope_name,
                    decision.proposed_setting_name,
                ):
                    raise ValueError("The proposed ADD path conflicts with an existing path.")
            continue
        if decision.operation == WorldSettingOperation.EXCLUDE:
            if decision.matched_property_name is None:
                continue
            if decision.target_ref is None:
                raise ValueError("A matched EXCLUDE requires target_ref.")
            target = references_by_key[decision.target_ref]
            if not _has_property(
                target,
                decision.matched_scope_name,
                decision.matched_property_name,
            ):
                raise ValueError("The matched EXCLUDE path does not exist.")
            if any(
                not _same_optional_name(
                    source.scope_name,
                    decision.matched_scope_name,
                )
                for source in sources
            ):
                raise ValueError("A matched EXCLUDE must preserve the source scope.")
            continue
        if decision.target_ref is None or decision.matched_property_name is None:
            raise ValueError("UPDATE and MERGE require a matched target property.")
        target = references_by_key[decision.target_ref]
        if not _has_property(
            target,
            decision.matched_scope_name,
            decision.matched_property_name,
        ):
            raise ValueError("The matched property path does not exist.")
        if any(
            not _same_optional_name(source.scope_name, decision.matched_scope_name)
            for source in sources
        ):
            raise ValueError("UPDATE and MERGE must preserve the source scope.")
        if not _same_optional_name(
            decision.proposed_scope_name,
            decision.matched_scope_name,
        ):
            raise ValueError("UPDATE and MERGE must preserve the stored scope name.")
        if not _same_optional_name(
            decision.proposed_setting_name,
            decision.matched_property_name,
        ):
            raise ValueError("UPDATE and MERGE must preserve the stored property name.")
    missing_refs = allowed_refs - seen_refs
    if missing_refs:
        raise ValueError(f"Missing source candidate refs: {sorted(missing_refs)}")
    _validate_batch_scope_plan(result, candidates_by_ref, references_by_key)
    projected_result = _project_batch_comparison_result(result, candidates, references)
    _validate_batch_scope_plan(projected_result, candidates_by_ref, references_by_key)


def _project_batch_comparison_result(
    result: WorldSettingComparisonBatchResult,
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonBatchResult:
    """LLM batch 결과에 저장 전 deterministic normalization을 적용한다."""

    normalized_decisions: list[WorldSettingComparisonBatchDecision] = []
    candidates_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
    for decision in result.decisions:
        sources = [candidates_by_ref[ref] for ref in decision.source_candidate_refs]
        normalized = decision
        if len(sources) == 1:
            normalized = _normalize_scope_ambiguity(
                normalized,
                sources[0],
                references,
            )
            normalized = _normalize_deterministic_fields(
                normalized,
                sources[0],
                preserve_canonical_add_path=True,
            )
        if (
            normalized.operation != WorldSettingOperation.ADD
            and normalized.existing_root_property_names_to_move
        ):
            normalized = normalized.model_copy(
                update={"existing_root_property_names_to_move": []}
            )
        normalized = _replace_internal_target_references(normalized, references)
        normalized_decisions.append(normalized)
    return WorldSettingComparisonBatchResult(decisions=normalized_decisions)


def _validate_batch_scope_plan(
    result: WorldSettingComparisonBatchResult,
    candidates_by_ref: dict[str, WorkerWorldSettingComparisonBatchCandidate],
    references_by_key: dict[str, WorkerWorldSettingComparisonTarget],
) -> None:
    """2차가 합성한 범위가 실제 형제 속성과 안전한 root 이동으로 뒷받침되는지 확인한다."""

    scope_members: dict[tuple[str | None, str], set[str]] = {}
    for target_ref, target in references_by_key.items():
        for property in target.properties:
            if property.scope_name is None:
                continue
            scope_members.setdefault(
                (target_ref, _backend_duplicate_key(property.scope_name)),
                set(),
            ).add(_backend_duplicate_key(property.setting_name))

    moved_root_paths: set[tuple[str, str]] = set()
    final_paths: set[tuple[str | None, str | None, str]] = set()
    top_level_kinds: dict[tuple[str | None, str], str] = {}

    def claim_final_path(path: tuple[str | None, str | None, str]) -> None:
        if path in final_paths:
            raise ValueError("Batch decisions must not propose the same final path.")
        final_paths.add(path)
        target_ref, scope_name, setting_name = path
        top_level_name = setting_name if scope_name is None else scope_name
        proposed_kind = "SCALAR" if scope_name is None else "OBJECT"
        top_level_key = (target_ref, top_level_name)
        existing_kind = top_level_kinds.get(top_level_key)
        if existing_kind is not None and existing_kind != proposed_kind:
            raise ValueError(
                "Batch decisions must not propose scalar and scoped paths under the same "
                "top-level name."
            )
        top_level_kinds[top_level_key] = proposed_kind

    for decision in result.decisions:
        proposed_scope_name = decision.proposed_scope_name
        if proposed_scope_name is not None and _same_optional_name(
            proposed_scope_name,
            decision.proposed_setting_name,
        ):
            raise ValueError("A scope name must differ from its setting name.")

        moved_names = decision.existing_root_property_names_to_move
        if decision.operation in {
            WorldSettingOperation.ADD,
            WorldSettingOperation.UPDATE,
            WorldSettingOperation.MERGE,
        }:
            final_path = (
                decision.target_ref,
                (
                    None
                    if proposed_scope_name is None
                    else _backend_duplicate_key(proposed_scope_name)
                ),
                _backend_duplicate_key(decision.proposed_setting_name),
            )
            claim_final_path(final_path)
        if moved_names:
            if (
                decision.operation != WorldSettingOperation.ADD
                or decision.target_ref is None
                or proposed_scope_name is None
            ):
                raise ValueError(
                    "Existing root properties may move only with a scoped ADD target."
                )
            target = references_by_key[decision.target_ref]
            for property_name in moved_names:
                moved_path = (
                    decision.target_ref,
                    _backend_duplicate_key(property_name),
                )
                if moved_path in moved_root_paths:
                    raise ValueError("An existing root property may move only once per batch.")
                moved_root_paths.add(moved_path)
                if not _has_property(target, None, property_name):
                    raise ValueError("A requested root property move does not exist.")
                if _same_optional_name(property_name, proposed_scope_name) or _same_optional_name(
                    property_name,
                    decision.proposed_setting_name,
                ):
                    raise ValueError(
                        "A moved root property must be a distinct child of the proposed scope."
                    )
                if _has_property(
                    target,
                    proposed_scope_name,
                    property_name,
                ) or _has_path_conflict(
                    target,
                    proposed_scope_name,
                    property_name,
                ):
                    raise ValueError("A requested root property move conflicts at its destination.")
                move_destination = (
                    decision.target_ref,
                    _backend_duplicate_key(proposed_scope_name),
                    _backend_duplicate_key(property_name),
                )
                claim_final_path(move_destination)
                scope_members.setdefault(
                    (decision.target_ref, _backend_duplicate_key(proposed_scope_name)),
                    set(),
                ).add(_backend_duplicate_key(property_name))

        if decision.operation == WorldSettingOperation.ADD and proposed_scope_name is not None:
            scope_members.setdefault(
                (decision.target_ref, _backend_duplicate_key(proposed_scope_name)),
                set(),
            ).add(_backend_duplicate_key(decision.proposed_setting_name))

    for decision in result.decisions:
        if (
            decision.operation in {
                WorldSettingOperation.UPDATE,
                WorldSettingOperation.MERGE,
            }
            and decision.target_ref is not None
            and decision.matched_scope_name is None
            and decision.matched_property_name is not None
            and (
                decision.target_ref,
                _backend_duplicate_key(decision.matched_property_name),
            )
            in moved_root_paths
        ):
            raise ValueError("A moved root property must not also be updated or merged.")
        if (
            decision.operation != WorldSettingOperation.ADD
            or decision.proposed_scope_name is None
        ):
            continue
        source_scopes = {
            None
            if candidates_by_ref[source_ref].scope_name is None
            else _backend_duplicate_key(candidates_by_ref[source_ref].scope_name)
            for source_ref in decision.source_candidate_refs
        }
        if len(source_scopes) != 1:
            # The main batch validator reports the more specific source-scope error.
            continue
        raw_scope = next(iter(source_scopes))
        proposed_scope = _backend_duplicate_key(decision.proposed_scope_name)
        if raw_scope == proposed_scope:
            continue
        members = scope_members.get((decision.target_ref, proposed_scope), set())
        if len(members) < 2:
            raise ValueError(
                "A generated scope requires at least two distinct final child properties."
            )


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
    *,
    preserve_canonical_add_path: bool = False,
) -> WorldSettingComparisonDecision:
    """비교 판단과 무관하게 원본에서 결정되는 출력 필드를 복원한다.

    기존 단건 비교는 ADD 경로를 1차 후보로 고정하지만, batch의 신규 ADD
    decision은 source가 하나여도 2차가 제안한 canonical 경로를 유지한다.
    """

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
        if not (
            preserve_canonical_add_path
            and decision.operation == WorldSettingOperation.ADD
        ):
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
    _validate_user_facing_reason(decision.comparison_reason, references)
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
    """모델이 선택했거나 후보명으로 확정되는 대상 안에서 범위 누락을 찾는다."""

    if candidate.scope_name is not None:
        return None

    candidate_name = _backend_duplicate_key(candidate.setting_name)
    selected_path: tuple[str, str] | None = None
    if (
        decision.target_ref is not None
        and decision.matched_scope_name is not None
        and decision.matched_property_name is not None
        and _backend_duplicate_key(decision.matched_property_name) == candidate_name
    ):
        selected_reference = next(
            (
                target_reference
                for target_reference in references
                if target_reference.reference == decision.target_ref
            ),
            None,
        )
        selected_path = (
            decision.matched_scope_name,
            decision.matched_property_name,
        )
    elif (
        decision.operation == WorldSettingOperation.ADD
        and decision.matched_scope_name is None
        and decision.matched_property_name is None
    ):
        if decision.target_ref is not None:
            selected_reference = next(
                (
                    target_reference
                    for target_reference in references
                    if target_reference.reference == decision.target_ref
                ),
                None,
            )
        else:
            candidate_subject = _normalized_name(candidate.subject_name)
            exact_subject_matches = [
                target_reference
                for target_reference in references
                if _normalized_name(target_reference.target.subject_name) == candidate_subject
            ]
            if len(exact_subject_matches) != 1:
                return None
            selected_reference = exact_subject_matches[0]
    else:
        return None

    if selected_reference is None:
        return None

    same_name_properties = [
        property
        for property in selected_reference.target.properties
        if _backend_duplicate_key(property.setting_name) == candidate_name
    ]
    # 선택한 대상에 같은 root 경로가 있으면 scope가 빠진 것이 아니다.
    if any(property.scope_name is None for property in same_name_properties):
        return None

    if selected_path is not None:
        selected_scope_name, selected_property_name = selected_path
        if not _has_property(
            selected_reference.target,
            selected_scope_name,
            selected_property_name,
        ):
            return None
        return ScopeAmbiguityMatch(
            target_ref=selected_reference.reference,
            scope_name=selected_scope_name,
            property_name=selected_property_name,
        )

    scoped_property = next(
        (property for property in same_name_properties if property.scope_name is not None),
        None,
    )
    if scoped_property is None:
        return None
    return ScopeAmbiguityMatch(
        target_ref=selected_reference.reference,
        scope_name=scoped_property.scope_name,
        property_name=scoped_property.setting_name,
    )


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
        or _backend_duplicate_key(decision.matched_property_name)
        != _backend_duplicate_key(candidate.setting_name)
    ):
        return False
    target = references_by_key.get(decision.target_ref)
    if target is None:
        return False
    # 같은 root 경로가 이미 있으면 그 경로를 우선 비교해야 하므로 scope 미확정이 아니다.
    if any(
        property.scope_name is None
        and _backend_duplicate_key(property.setting_name)
        == _backend_duplicate_key(candidate.setting_name)
        for property in target.properties
    ):
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
        _same_optional_name(property.scope_name, scope_name)
        and _same_optional_name(property.setting_name, setting_name)
        for property in target.properties
    )


def _has_path_conflict(
    target: WorkerWorldSettingComparisonTarget,
    scope_name: str | None,
    setting_name: str,
) -> bool:
    """Spring WorldSetting.hasPathConflict와 같은 root-scalar/scope-object 충돌 검사."""

    if scope_name is None:
        return any(
            property.scope_name is not None
            and _same_optional_name(property.scope_name, setting_name)
            for property in target.properties
        )
    return any(
        property.scope_name is None
        and _same_optional_name(property.setting_name, scope_name)
        for property in target.properties
    )


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def _backend_duplicate_key(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return unicodedata.normalize("NFC", value[start:end]).lower()


def _same_optional_name(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return _backend_duplicate_key(left) == _backend_duplicate_key(right)


def _replace_internal_target_references(
    decision: WorldSettingComparisonDecision,
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonDecision:
    comparison_reason = _replace_target_reference_text(
        decision.comparison_reason,
        references,
    )
    if comparison_reason == decision.comparison_reason:
        return decision
    return decision.model_copy(update={"comparison_reason": comparison_reason})


def _validate_user_facing_reason(
    comparison_reason: str,
    references: list[ComparisonTargetReference],
) -> None:
    display_reason = _replace_target_reference_text(comparison_reason, references)
    if LOCAL_REFERENCE_PATTERN.search(display_reason):
        raise ValueError("comparison_reason must not expose a local reference.")
    if UUID_PATTERN.search(display_reason):
        raise ValueError("comparison_reason must not expose a UUID.")
    if INTERNAL_REASON_TOKEN_PATTERN.search(display_reason):
        raise ValueError("comparison_reason must not expose an internal enum or key.")


def _replace_target_reference_text(
    comparison_reason: str,
    references: list[ComparisonTargetReference],
) -> str:
    display_reason = comparison_reason
    for target_reference in sorted(
        references,
        key=lambda reference: len(reference.reference),
        reverse=True,
    ):
        display_reason = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(target_reference.reference)}"
            r"(?![A-Za-z0-9_])",
            f"기존 '{target_reference.target.subject_name}' 설정",
            display_reason,
        )
    return display_reason


def _source_values(candidate: WorkerWorldSettingCandidatePayload) -> list[str]:
    values = [value.strip() for value in candidate.extracted_value.splitlines() if value.strip()]
    return values or [candidate.extracted_value]
