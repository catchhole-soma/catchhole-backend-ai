import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import tiktoken
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis.comparison_reason import USER_FACING_REASON_INSTRUCTIONS
from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.ordered_context import (
    BoundedOrderedClient, ORDERED_STATE_INSTRUCTIONS, ordered_provenance_data, ordered_reference_data,
)
from app.schemas.analysis_context import WorkerAnalysisReference
from app.analysis.ordered_world_batch_contract import (
    ORDERED_WORLD_BATCH_INSTRUCTIONS,
    OrderedWorldBatchValidationError,
    OrderedWorldPropertyBatchResult,
    ordered_world_batch_error_summary,
    ordered_world_batch_retry_prompt,
)
from app.analysis.ordered_world_diagnostics import (
    property_response_schema, restore_selected_properties, validation_diagnostics,
)
from app.analysis.json_response import compact_error_message, request_validated_model
from app.analysis.ordered_world_rule_diagnostics import diagnose_world_rule
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
from app.llm.protocols import LlmResponseSchema, TextGenerationClient
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
WORLD_SETTING_COMPARISON_BATCH_CACHE_KEY = "world-setting-comparison-batch:v8"
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
    r"SINGLE|MERGED|CONFLICT|GENERAL_UNCERTAINTY|SCOPE_UNRESOLVED|BATCH_LIMIT_EXCEEDED|"
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
    world_setting_id: UUID | None
    subject_name: str
    provisional_subject_key: str | None = None


class _OrderedSubjectSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_subject_refs: list[str] = Field(max_length=3)
    ambiguous: bool


ORDERED_WORLD_SUBJECT_RESPONSE_SCHEMA = LlmResponseSchema(
    name="ordered_world_subject_resolution",
    schema=_OrderedSubjectSelection.model_json_schema(),
)

_ORDERED_WORLD_SUBJECT_FORMAT_INSTRUCTIONS = """
응답은 selected_subject_refs와 ambiguous를 모두 가진 JSON 객체 하나로만 반환하세요.
다른 필드는 추가하지 마세요. selected_subject_refs에는 입력 subjects의 ref에 있는
S* 문자열만 최대 3개까지 중복 없이 넣으세요. ambiguous에는 JSON boolean만 사용하세요.
같은 주체로 연결한 경우의 형식 예시: {"selected_subject_refs":["S1"],"ambiguous":false}
명확한 신규 주체의 형식: {"selected_subject_refs":[],"ambiguous":false}
연결이 모호한 경우의 형식: {"selected_subject_refs":[],"ambiguous":true}
모호하면 대상을 선택하지 마세요. 예시는 형식만 보여주며 S1을 자동으로 선택하지 마세요.
""".strip()

_ORDERED_WORLD_SUBJECT_ERROR_TYPES = frozenset({
    "missing", "extra_forbidden", "string_type", "list_type", "bool_type",
    "bool_parsing", "too_long", "model_type", "dict_type",
})


def _ordered_world_subject_retry_prompt(original: str, error: Exception) -> str:
    """Return fixed contract paths and error types without rejected response data."""
    issues: list[dict[str, str]] = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_url=False, include_context=False, include_input=False):
            location = tuple(item.get("loc") or ())
            if location and location[0] in {"selected_subject_refs", "ambiguous"}:
                field = location[0]
                if field == "selected_subject_refs" and len(location) > 1:
                    field += "[]"
            else:
                field = "response"
            issue = {
                "field": field,
                "type": (item["type"] if item.get("type") in _ORDERED_WORLD_SUBJECT_ERROR_TYPES
                         else "schema_invalid"),
            }
            if issue not in issues:
                issues.append(issue)
            if len(issues) == 8:
                break
    else:
        issues.append({
            "field": "response",
            "type": "json_invalid" if isinstance(error, json.JSONDecodeError) else "schema_invalid",
        })
    return (
        original + "\n\nresponse_format_feedback:\n"
        + json.dumps({"issues": issues}, ensure_ascii=False)
        + "\nselected_subject_refs와 ambiguous를 모두 명시한 JSON 객체를 다시 반환하세요. "
        "참조는 입력의 S*만 사용하고, 새 주체와 모호한 주체는 빈 목록과 ambiguous로 구분하세요. "
        "실패 응답을 복사하지 마세요."
    )


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
                provisional_subject_key=subject.provisional_subject_key,
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

    async def select_ordered_subjects(self, candidate, subjects, *, unresolved_references=()):
        if not subjects and not unresolved_references:
            return [], False
        references = {f"S{index}": subject for index, subject in enumerate(subjects, 1)}

        def validate(selection):
            refs = selection.selected_subject_refs
            if len(refs) != len(set(refs)) or any(ref not in references for ref in refs):
                raise ComparisonValidationError("Unknown ordered world subject reference.")

        result = await request_validated_model(
            client=BoundedOrderedClient(self.llm_client),
            response_model=_OrderedSubjectSelection,
            system_prompt=(ORDERED_STATE_INSTRUCTIONS + "\n"
                "입력 subjects는 같은 분류에서 선택 가능한 대상입니다. 원문 근거와 대상 설명으로 "
                "같은 주체를 판단하세요. 같은 이름의 임시 대상이 같은 개념·존재를 가리키면 "
                "그 참조를 재사용하세요. 비교할 특성이 다르거나 아직 설정을 확정하지 않았다는 "
                "이유로 새 주체를 만들지 마세요. 예를 들어 게임의 일반적인 캐릭터에 관한 "
                "장비 수치와 전투 수치 설명은 같은 개념의 다른 특성일 수 있습니다. 반면 이름이 "
                "같아도 서로 다른 장소·개별 존재라는 근거가 있으면 별도 주체입니다. 이름 일치는 "
                "검토할 단서이며 자동 연결의 충분한 근거가 아닙니다. 연관된 분류나 상위·하위 종류는 "
                "같은 대상의 별칭이 아닙니다. 원문이 변이종·상위종·희귀종·상위 변이종을 각각 다른 "
                "분류로 설명하면 단어가 겹치거나 서로 관련돼도 구분하세요. 다른 이름을 연결하려면 "
                "별칭·약칭·번역 차이 또는 원문이 같은 대상을 달리 부른다는 근거를 확인하세요. "
                "앞서 보류된 참고 주장은 "
                "선택 가능한 대상이나 확정 사실이 아니며 그것만으로 같은 주체라고 결정하지 마세요. "
                "연결이 모호하면 ambiguous=true 및 빈 목록을 "
                "반환하세요. 명확한 신규 주체는 ambiguous=false 및 빈 목록입니다.\n\n"
                + _ORDERED_WORLD_SUBJECT_FORMAT_INSTRUCTIONS),
            user_prompt=json.dumps({
                "candidate": {"category": candidate.category,
                              "subject_name": candidate.subject_name,
                              "evidence": [span.quote for span in candidate.evidence_spans]},
                "subjects": [{
                    "ref": ref, "name": subject.subject_name, "aliases": subject.aliases,
                    "identity_evidence": [span.quote for span in subject.identity_evidence],
                    **(subject.provenance.prompt_data() if subject.provenance else {
                        "confirmation_status": ("PROVISIONAL" if subject.provisional_subject_key else "CONFIRMED"),
                        "source_episode_no": None}),
                } for ref, subject in references.items()],
                "unresolved_references": ordered_reference_data(unresolved_references),
            }, ensure_ascii=False),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="ordered-world-subject-resolution:v4",
            operation_name="Ordered world subject resolution", logger=logger,
            validate_model=validate,
            retry_user_prompt_builder=_ordered_world_subject_retry_prompt,
            response_schema=ORDERED_WORLD_SUBJECT_RESPONSE_SCHEMA,
        )
        if result.ambiguous:
            # Explicit uncertainty cannot establish identity, even when the model
            # also names a valid option. Match Spring's conservative resolution
            # contract without retrying or adding its evidence to that subject.
            if result.selected_subject_refs:
                logger.info("Ordered world subject ambiguity retained; selected targets discarded.")
            return [], True
        return [references[ref] for ref in result.selected_subject_refs], False


class WorldSettingComparator:
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = COMPARISON_PROMPT_PATH,
        model: str | None = None,
        max_attempts: int | None = None,
        max_output_tokens: int | None = None,
        batch_max_output_tokens: int | None = None,
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
        self.batch_max_output_tokens = (
            settings.llm_world_setting_batch_comparison_max_output_tokens
            if batch_max_output_tokens is None
            else batch_max_output_tokens
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
                        property.model_dump(mode="json", exclude={"provenance"})
                        for property in target_reference.target.properties
                    ],
                }
                for target_reference in references
            ],
        }
        decision = await request_validated_model(
            client=self.llm_client,
            response_model=WorldSettingComparisonDecision,
            system_prompt=self.prompt_path.read_text(encoding="utf-8") + "\n\n" + USER_FACING_REASON_INSTRUCTIONS,
            user_prompt=json.dumps(raw_payload, ensure_ascii=False),
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key="world-setting-comparison:v11",
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
        ordered_context: bool = False,
        unresolved_references: tuple[WorkerAnalysisReference, ...] = (),
        max_attempts_override: int | None = None,
        preserve_source_paths: bool = False,
    ) -> tuple[WorldSettingComparisonBatchResult, dict]:
        if not candidates:
            raise ValueError("World-setting comparison batch must include candidates.")
        if not ordered_context and any(target.provisional_subject_key for target in targets):
            raise ComparisonValidationError("Provisional targets require explicit ordered analysis.")
        if unresolved_references and not ordered_context:
            raise ComparisonValidationError("Unresolved references require explicit ordered analysis.")
        if max_attempts_override is not None and (
            type(max_attempts_override) is not int or max_attempts_override < 1
        ):
            raise OrderedInputContextError("Comparison attempt override must be a positive integer.")
        if preserve_source_paths and not ordered_context:
            raise OrderedInputContextError("Source-path recovery requires explicit ordered analysis.")
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
                        property.model_dump(mode="json", exclude={"provenance"})
                        for property in target_reference.target.properties
                    ],
                }
                for target_reference in references
            ],
        }
        system_prompt = self.batch_prompt_path.read_text(encoding="utf-8") + "\n\n" + USER_FACING_REASON_INSTRUCTIONS
        if ordered_context:
            system_prompt += (
                "\n\n" + ORDERED_STATE_INSTRUCTIONS + "\n\n" + ORDERED_WORLD_BATCH_INSTRUCTIONS
            )
            raw_payload["unresolved_references"] = ordered_reference_data(unresolved_references)
            for target_data, target in zip(raw_payload["targets"], targets, strict=True):
                target_data["confirmation_status"] = (
                    "PROVISIONAL" if target.provisional_subject_key is not None else "CONFIRMED"
                )
                for property_index, (property_data, prop) in enumerate(
                    zip(target_data["properties"], target.properties, strict=True), 1,
                ):
                    property_data["ref"] = f"{target_data['ref']}.P{property_index}"
                    property_data.update(ordered_provenance_data(prop.provenance))
            if preserve_source_paths:
                system_prompt += (
                    "\n\n이번 요청은 개별 복구 비교입니다. 이 요청에서는 앞선 일반 canonical 분류·이름 "
                    "정리 지침보다 원본 경로 보존이 우선합니다. ADD의 proposed scope/name은 모든 원본 "
                    "source의 scope/name 그대로 유지하고 모든 operation의 "
                    "existing_root_property_names_to_move는 []로 두세요. UPDATE/MERGE의 "
                    "proposed scope/name은 null이며 선택한 실제 기존 속성 경로를 사용합니다."
                )
        user_prompt = json.dumps(raw_payload, ensure_ascii=False)
        minimum_output_tokens = _estimate_batch_minimum_output_tokens(
            candidates,
            targets,
            self.model,
        )
        if minimum_output_tokens > self.batch_max_output_tokens:
            oversized_result = _batch_limit_review_result(candidates, references)
            raw_result = oversized_result.model_dump(mode="json")
            raw_result["output_budget"] = {
                "estimated_minimum_tokens": minimum_output_tokens,
                "max_output_tokens": self.batch_max_output_tokens,
            }
            return oversized_result, raw_result
        diagnostics = []
        restored_result = None

        def validate_ordered(selection):
            nonlocal restored_result
            with diagnose_world_rule(True, "PROPERTY_SELECTION"):
                restored_result = restore_selected_properties(selection, raw_payload)
            _validate_batch_comparison_result(
                restored_result, candidates, references, ordered_context=True,
                preserve_source_paths=preserve_source_paths,
            )

        def record_diagnostics(attempt_number, error, response_payload):
            entries = validation_diagnostics(
                raw_payload, attempt_number, error, response_payload,
                phase="RECOVERY" if preserve_source_paths else "BATCH",
            )
            diagnostics.extend(entries)
            for entry in entries:
                logger.warning("World-setting ordered validation diagnostic=%s",
                               json.dumps(entry, ensure_ascii=False))

        try:
            result = await request_validated_model(
                client=(BoundedOrderedClient(self.llm_client) if ordered_context else self.llm_client),
                response_model=(OrderedWorldPropertyBatchResult if ordered_context
                                else WorldSettingComparisonBatchResult),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                max_output_tokens=self.batch_max_output_tokens,
                max_attempts=(self.max_attempts if max_attempts_override is None else max_attempts_override),
                prompt_cache_key=(WORLD_SETTING_COMPARISON_BATCH_CACHE_KEY
                                  + (":ordered-provisional-v6" if ordered_context else "")),
                operation_name="World-setting batch comparison",
                logger=logger,
                validate_model=(validate_ordered if ordered_context else
                               lambda comparison_result: _validate_batch_comparison_result(
                                   comparison_result, candidates, references,
                                   ordered_context=ordered_context,
                               )),
                retry_user_prompt_builder=(ordered_world_batch_retry_prompt if ordered_context
                                           else _build_batch_retry_user_prompt),
                **({"response_schema": property_response_schema(raw_payload),
                    "validation_error_summary": ordered_world_batch_error_summary,
                    "validation_failure_callback": record_diagnostics}
                   if ordered_context else {}),
            )
        except OrderedInputContextError:
            raise
        except ComparisonValidationError as exc:
            if ordered_context:
                exc.validation_diagnostics = tuple(diagnostics)
            raise
        if ordered_context:
            result = restored_result
        normalized_result = _project_batch_comparison_result(
            result,
            candidates,
            references,
        )
        raw_result = normalized_result.model_dump(mode="json")
        if ordered_context:
            raw_result["validation_diagnostics"] = diagnostics
        return normalized_result, raw_result


def _resolve_max_attempts(max_attempts: int | None) -> int:
    resolved = get_settings().llm_extraction_max_attempts if max_attempts is None else max_attempts
    if resolved < 1:
        raise ValueError("max_attempts must be at least 1.")
    return resolved


def _estimate_batch_minimum_output_tokens(
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    targets: list[WorkerWorldSettingComparisonTarget],
    model: str,
) -> int:
    """Estimate a conservative contract-complete batch response size.

    The worst valid shape has one decision per candidate and may need to repeat
    every source value verbatim for conflict review. JSON framing is measured by
    the same tokenizer family used for GPT-5.6 quota reservation, then padded for
    short comparison reasons and provider/tokenizer variance.
    """

    try:
        encoding = (
            tiktoken.get_encoding("o200k_base")
            if model.startswith("gpt-5.6")
            else tiktoken.encoding_for_model(model)
        )
    except Exception:  # noqa: BLE001 - byte counts keep the guard active.
        encoding = None

    def token_weight(value: str) -> int:
        serialized_value = json.dumps(value, ensure_ascii=False)
        if encoding is None:
            return len(serialized_value.encode("utf-8"))
        return len(encoding.encode(serialized_value, disallowed_special=()))

    scope_names = [
        name
        for name in (
            [candidate.scope_name for candidate in candidates]
            + [
                property.scope_name
                for target in targets
                for property in target.properties
            ]
        )
        if name is not None
    ]
    setting_names = [
        candidate.setting_name for candidate in candidates
    ] + [
        property.setting_name
        for target in targets
        for property in target.properties
    ]
    longest_scope_name = max(scope_names, key=token_weight, default=None)
    longest_setting_name = max(setting_names, key=token_weight)
    longest_path = (
        f"{longest_scope_name} › {longest_setting_name}"
        if longest_scope_name is not None
        else longest_setting_name
    )

    decisions = [
        {
            "source_candidate_refs": [candidate.candidate_ref],
            "existing_root_property_names_to_move": [],
            "consolidation_status": "CONFLICT",
            "operation": "REVIEW_REQUIRED",
            "review_reason": "SCOPE_UNRESOLVED",
            "target_ref": "T20",
            "matched_scope_name": longest_scope_name,
            "matched_property_name": longest_setting_name,
            "proposed_scope_name": longest_scope_name,
            "proposed_setting_name": longest_setting_name,
            "proposed_value": candidate.extracted_value,
            "comparison_reason": (
                f"기존 '{longest_path}' 설정과 원문 값을 비교한 결과를 "
                "사용자가 확인할 수 있도록 보존합니다."
            ),
        }
        for candidate in candidates
    ]
    serialized = json.dumps(
        {"decisions": decisions},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if encoding is not None:
        measured_tokens = len(encoding.encode(serialized, disallowed_special=()))
    else:
        measured_tokens = len(serialized.encode("utf-8"))
    return math.ceil(measured_tokens * 1.2) + 512


def _batch_limit_review_result(
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonBatchResult:
    """Hold an output-oversized batch for review without calling the provider."""

    target_ref = references[0].reference if len(references) == 1 else None
    decisions = [
        WorldSettingComparisonBatchDecision(
            source_candidate_refs=[candidate.candidate_ref],
            existing_root_property_names_to_move=[],
            consolidation_status=(
                WorldSettingConsolidationStatus.SINGLE
                if len(_source_values(candidate)) == 1
                else WorldSettingConsolidationStatus.CONFLICT
            ),
            operation=WorldSettingOperation.REVIEW_REQUIRED,
            review_reason=WorldSettingComparisonReviewReason.BATCH_LIMIT_EXCEEDED,
            target_ref=target_ref,
            matched_scope_name=None,
            matched_property_name=None,
            proposed_scope_name=candidate.scope_name,
            proposed_setting_name=candidate.setting_name,
            proposed_value=candidate.extracted_value,
            comparison_reason=(
                "비교 결과가 출력 한도를 넘어 자동 비교하지 않았습니다."
            ),
        )
        for candidate in candidates
    ]
    return WorldSettingComparisonBatchResult(decisions=decisions)


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


def _ordered_path_error(code, decision_index, decision, target=None):
    indices = [index for index, prop in enumerate(target.properties)
               if _same_optional_name(prop.scope_name, decision.matched_scope_name)
               and _same_optional_name(prop.setting_name, decision.matched_property_name)] if target else []
    return OrderedWorldBatchValidationError(
        reason_code=code, decision_index=decision_index,
        source_candidate_refs=decision.source_candidate_refs, target_ref=decision.target_ref,
        property_indices=indices,
    )


def _validate_batch_comparison_result(
    result: WorldSettingComparisonBatchResult,
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
    *,
    ordered_context: bool = False,
    preserve_source_paths: bool = False,
) -> None:
    candidates_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
    allowed_refs = set(candidates_by_ref)
    seen_refs: set[str] = set()
    references_by_key = {
        target_reference.reference: target_reference.target for target_reference in references
    }
    for decision_index, decision in enumerate(result.decisions):
        with diagnose_world_rule(ordered_context, "DECISION_VALIDATION", decision_index,
                                 decision.source_candidate_refs):
            source_refs = set(decision.source_candidate_refs)
            unknown_refs = source_refs - allowed_refs
            if unknown_refs:
                if ordered_context:
                    raise _ordered_path_error("SOURCE_REF_UNKNOWN", decision_index, decision)
                raise ValueError(f"Unknown source candidate refs: {sorted(unknown_refs)}")
            duplicated_refs = seen_refs & source_refs
            if duplicated_refs:
                if ordered_context:
                    raise _ordered_path_error("SOURCE_REF_DUPLICATED", decision_index, decision)
                raise ValueError(f"Duplicated source candidate refs: {sorted(duplicated_refs)}")
            seen_refs.update(source_refs)
            selected_source_refs = set(decision.source_candidate_refs)
            sources = [
                candidate
                for candidate in candidates
                if candidate.candidate_ref in selected_source_refs
            ]
            source_values = [value for source in sources for value in _source_values(source)]
            if len(source_values) > 1 and decision.consolidation_status == "SINGLE":
                if ordered_context:
                    raise _ordered_path_error("SOURCE_CONSOLIDATION_INVALID", decision_index, decision)
                raise ValueError("Multiple extracted values must use MERGED or CONFLICT status.")
            normalized_scopes = {
                None
                if source.scope_name is None
                else _backend_duplicate_key(source.scope_name)
                for source in sources
            }
            if len(normalized_scopes) != 1:
                if ordered_context:
                    raise _ordered_path_error("SOURCE_SCOPE_MIXED", decision_index, decision)
                raise ValueError("A decision must not mix different explicit source scopes.")
            if decision.target_ref is not None and decision.target_ref not in references_by_key:
                if ordered_context:
                    raise _ordered_path_error("TARGET_REF_UNKNOWN", decision_index, decision)
                raise ValueError(f"Unknown comparison target_ref: {decision.target_ref}")
            if ordered_context and re.search(
                r"(?<![A-Za-z0-9_])(?:T\d+\.)?P\d+(?![A-Za-z0-9_])",
                _mask_target_display_names(decision.comparison_reason, references), re.IGNORECASE,
            ):
                raise _ordered_path_error("COMPARISON_REASON_REFERENCE_INVALID", decision_index, decision)
            _validate_user_facing_reason(
                decision.comparison_reason, references,
                display_names=tuple(name for source in sources
                                    for name in (source.subject_name, source.scope_name, source.setting_name)),
            )
            if len(references_by_key) == 1 and decision.target_ref is None:
                if ordered_context:
                    raise OrderedWorldBatchValidationError(
                        reason_code="CANONICAL_TARGET_REQUIRED",
                        decision_index=decision_index,
                        source_candidate_refs=decision.source_candidate_refs,
                        target_ref=next(iter(references_by_key)),
                        property_indices=[],
                    )
                raise ValueError(
                    "Every decision in an existing canonical-subject cluster must preserve "
                    "its target_ref."
                )

            if preserve_source_paths and (
                decision.existing_root_property_names_to_move
                or decision.operation == WorldSettingOperation.ADD and any(
                    source.scope_name != decision.proposed_scope_name
                    or source.setting_name != decision.proposed_setting_name for source in sources
                )
            ):
                raise _ordered_path_error("RECOVERY_SOURCE_PATH_CHANGED", decision_index, decision)

            if (
                decision.operation == WorldSettingOperation.REVIEW_REQUIRED
                and decision.review_reason
                == WorldSettingComparisonReviewReason.BATCH_LIMIT_EXCEEDED
            ):
                raise ValueError("The provider must not classify output-budget overflow.")
            if decision.review_reason == WorldSettingComparisonReviewReason.GENERAL_UNCERTAINTY:
                if len(sources) != 1 or decision.existing_root_property_names_to_move:
                    if ordered_context:
                        raise _ordered_path_error("GENERAL_UNCERTAINTY_REVIEW_INVALID", decision_index, decision)
                    raise ValueError("General uncertainty review requires one source and no property moves.")
                target = references_by_key.get(decision.target_ref)
                if decision.matched_property_name is not None and (
                    target is None or not _has_property(
                        target, decision.matched_scope_name, decision.matched_property_name,
                    )
                ):
                    if ordered_context:
                        raise _ordered_path_error(
                            "MATCHED_PROPERTY_PATH_NOT_FOUND", decision_index, decision, target,
                        )
                    raise ValueError("The matched property path does not exist in the selected target.")
                continue
            if decision.review_reason == WorldSettingComparisonReviewReason.SCOPE_MISMATCH:
                if not ordered_context:
                    raise ValueError("SCOPE_MISMATCH requires ordered batch comparison.")
                target = references_by_key.get(decision.target_ref)
                if target is None or not _has_property(
                    target, decision.matched_scope_name, decision.matched_property_name,
                ):
                    raise _ordered_path_error(
                        "MATCHED_PROPERTY_PATH_NOT_FOUND", decision_index, decision, target,
                    )
                if (len(sources) != 1 or sources[0].scope_name is None
                        or _same_optional_name(sources[0].scope_name, decision.matched_scope_name)
                        or decision.proposed_scope_name != sources[0].scope_name
                        or decision.proposed_setting_name != sources[0].setting_name
                        or decision.existing_root_property_names_to_move):
                    raise _ordered_path_error(
                        "SCOPE_MISMATCH_REVIEW_INVALID", decision_index, decision, target,
                    )
                continue
            if (ordered_context and decision.operation == WorldSettingOperation.REVIEW_REQUIRED
                    and decision.review_reason == WorldSettingComparisonReviewReason.SCOPE_UNRESOLVED
                    and not all(_same_optional_name(source.setting_name, decision.matched_property_name)
                                for source in sources)):
                target = references_by_key.get(decision.target_ref)
                if target is None or not _has_property(
                    target, decision.matched_scope_name, decision.matched_property_name,
                ):
                    raise _ordered_path_error(
                        "MATCHED_PROPERTY_PATH_NOT_FOUND", decision_index, decision, target,
                    )
                if (len(sources) != 1 or sources[0].scope_name is not None
                        or decision.matched_scope_name is None
                        or decision.proposed_scope_name is not None
                        or decision.proposed_setting_name != sources[0].setting_name
                        or decision.existing_root_property_names_to_move):
                    raise _ordered_path_error(
                        "SCOPE_UNRESOLVED_REVIEW_INVALID", decision_index, decision, target,
                    )
                continue
            scope_ambiguity_match = _find_batch_scope_ambiguity_match(
                decision,
                sources,
                references,
            )
            if decision.operation == WorldSettingOperation.REVIEW_REQUIRED:
                if scope_ambiguity_match is None:
                    raise ValueError(
                        "SCOPE_UNRESOLVED must identify compatible ambiguous source candidates."
                    )
                continue
            if scope_ambiguity_match is not None:
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
                        if ordered_context:
                            exact_indices = [
                                index for index, prop in enumerate(target.properties)
                                if _same_optional_name(prop.scope_name, decision.proposed_scope_name)
                                and _same_optional_name(prop.setting_name, decision.proposed_setting_name)
                            ]
                            conflict_indices = [
                                index for index, prop in enumerate(target.properties)
                                if (
                                    decision.proposed_scope_name is None
                                    and prop.scope_name is not None
                                    and _same_optional_name(
                                        prop.scope_name, decision.proposed_setting_name,
                                    )
                                ) or (
                                    decision.proposed_scope_name is not None
                                    and prop.scope_name is None
                                    and _same_optional_name(
                                        prop.setting_name, decision.proposed_scope_name,
                                    )
                                )
                            ]
                            raise OrderedWorldBatchValidationError(
                                reason_code=("ADD_EXISTING_PATH" if exact_indices
                                             else "ADD_ROOT_SCOPE_CONFLICT"),
                                decision_index=decision_index,
                                source_candidate_refs=decision.source_candidate_refs,
                                target_ref=decision.target_ref,
                                property_indices=exact_indices or conflict_indices,
                            )
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
                    if ordered_context:
                        raise _ordered_path_error(
                            "MATCHED_EXCLUDE_PATH_NOT_FOUND", decision_index, decision, target,
                        )
                    raise ValueError("The matched EXCLUDE path does not exist.")
                if any(
                    not _same_optional_name(
                        source.scope_name,
                        decision.matched_scope_name,
                    )
                    for source in sources
                ):
                    if ordered_context:
                        raise _ordered_path_error(
                            "SOURCE_SCOPE_MISMATCH", decision_index, decision, target,
                        )
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
                if ordered_context:
                    raise _ordered_path_error(
                        "MATCHED_PROPERTY_PATH_NOT_FOUND", decision_index, decision, target,
                    )
                raise ValueError("The matched property path does not exist.")
            if any(
                not _same_optional_name(source.scope_name, decision.matched_scope_name)
                for source in sources
            ):
                if ordered_context:
                    raise _ordered_path_error(
                        "SOURCE_SCOPE_MISMATCH", decision_index, decision, target,
                    )
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
        if ordered_context:
            raise OrderedWorldBatchValidationError(
                reason_code="SOURCE_REF_MISSING", decision_index=-1,
                source_candidate_refs=sorted(missing_refs), target_ref=None, property_indices=[],
            )
        raise ValueError(f"Missing source candidate refs: {sorted(missing_refs)}")
    _validate_batch_scope_plan(result, candidates_by_ref, references_by_key,
                               ordered_context=ordered_context)
    with diagnose_world_rule(ordered_context, "PROJECTED_SCOPE_PLAN"):
        projected_result = _project_batch_comparison_result(result, candidates, references)
    _validate_batch_scope_plan(projected_result, candidates_by_ref, references_by_key,
                               ordered_context=ordered_context, diagnostic_stage="PROJECTED_SCOPE_PLAN")


def _project_batch_comparison_result(
    result: WorldSettingComparisonBatchResult,
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonBatchResult:
    """LLM batch 결과에 저장 전 deterministic normalization을 적용한다."""

    normalized_decisions: list[WorldSettingComparisonBatchDecision] = []
    for decision in result.decisions:
        selected_source_refs = set(decision.source_candidate_refs)
        sources = [
            candidate
            for candidate in candidates
            if candidate.candidate_ref in selected_source_refs
        ]
        normalized = _normalize_batch_scope_ambiguity(
            decision,
            sources,
            references,
        )
        normalized = _normalize_batch_deterministic_fields(normalized, sources)
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
    *,
    ordered_context: bool = False,
    diagnostic_stage: str = "SCOPE_PLAN",
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
    final_path_sources: dict[tuple, list[str]] = {}
    top_level_sources: dict[tuple, list[str]] = {}

    def claim_final_path(path: tuple[str | None, str | None, str]) -> None:
        if path in final_paths:
            with diagnose_world_rule(ordered_context, diagnostic_stage, decision_index,
                                     final_path_sources[path] + decision.source_candidate_refs):
                raise ValueError("Batch decisions must not propose the same final path.")
        final_paths.add(path)
        final_path_sources[path] = list(decision.source_candidate_refs)
        target_ref, scope_name, setting_name = path
        top_level_name = setting_name if scope_name is None else scope_name
        proposed_kind = "SCALAR" if scope_name is None else "OBJECT"
        top_level_key = (target_ref, top_level_name)
        existing_kind = top_level_kinds.get(top_level_key)
        if existing_kind is not None and existing_kind != proposed_kind:
            with diagnose_world_rule(ordered_context, diagnostic_stage, decision_index,
                                     top_level_sources[top_level_key] + decision.source_candidate_refs):
                raise ValueError(
                    "Batch decisions must not propose scalar and scoped paths under the same "
                    "top-level name."
                )
        top_level_kinds[top_level_key] = proposed_kind
        top_level_sources.setdefault(top_level_key, []).extend(decision.source_candidate_refs)

    for decision_index, decision in enumerate(result.decisions):
        with diagnose_world_rule(ordered_context, diagnostic_stage, decision_index,
                                 decision.source_candidate_refs):
            if decision.review_reason == WorldSettingComparisonReviewReason.GENERAL_UNCERTAINTY:
                # This result writes no setting path. Its one source is preserved
                # by the deterministic projection after identity/path validation.
                continue
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

    for decision_index, decision in enumerate(result.decisions):
        with diagnose_world_rule(ordered_context, diagnostic_stage, decision_index,
                                 decision.source_candidate_refs):
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
                if ordered_context:
                    raise _ordered_path_error(
                        "GENERATED_SCOPE_REQUIRES_SIBLINGS", decision_index, decision,
                    )
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
    if decision.review_reason == WorldSettingComparisonReviewReason.GENERAL_UNCERTAINTY:
        # Uncertainty must not turn a paraphrase or merged guess into new evidence.
        updates["proposed_value"] = candidate.extracted_value
    return decision.model_copy(update=updates)


def _normalize_batch_deterministic_fields(
    decision: WorldSettingComparisonBatchDecision,
    sources: list[WorkerWorldSettingComparisonBatchCandidate],
) -> WorldSettingComparisonBatchDecision:
    """Restore fields that are deterministic across every source in a batch decision."""

    normalized = decision
    if len(sources) == 1:
        normalized = _normalize_deterministic_fields(
            normalized,
            sources[0],
            preserve_canonical_add_path=True,
        )
    if normalized.consolidation_status == WorldSettingConsolidationStatus.CONFLICT:
        normalized = normalized.model_copy(
            update={
                "proposed_value": "\n".join(
                    source.extracted_value for source in sources
                )
            }
        )
    return normalized


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
    if decision.review_reason == WorldSettingComparisonReviewReason.SCOPE_MISMATCH:
        raise ValueError("SCOPE_MISMATCH requires ordered batch comparison.")
    source_values = _source_values(candidate)
    if len(source_values) > 1 and decision.consolidation_status == "SINGLE":
        raise ValueError("Multiple extracted values must use MERGED or CONFLICT status.")

    references_by_key = {
        target_reference.reference: target_reference.target for target_reference in references
    }
    if decision.target_ref is not None and decision.target_ref not in references_by_key:
        raise ValueError(f"Unknown comparison target_ref: {decision.target_ref}")
    _validate_user_facing_reason(
        decision.comparison_reason, references,
        display_names=(candidate.subject_name, candidate.scope_name, candidate.setting_name),
    )
    if decision.review_reason == WorldSettingComparisonReviewReason.GENERAL_UNCERTAINTY:
        if decision.matched_property_name is not None and not _has_property(
            references_by_key[decision.target_ref], decision.matched_scope_name, decision.matched_property_name,
        ):
            raise ValueError("The matched property path does not exist in the selected target.")
        return
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


def _normalize_batch_scope_ambiguity(
    decision: WorldSettingComparisonBatchDecision,
    sources: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
) -> WorldSettingComparisonBatchDecision:
    match = _find_batch_scope_ambiguity_match(decision, sources, references)
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
            "proposed_scope_name": None,
            "proposed_setting_name": sources[0].setting_name,
            "comparison_reason": (
                f"후보들에는 범위가 없지만 기존 '{matched_path}' 설정과 "
                "관련될 수 있어 적용 범위 확인이 필요합니다."
            ),
            "existing_root_property_names_to_move": [],
        }
    )


def _find_batch_scope_ambiguity_match(
    decision: WorldSettingComparisonBatchDecision,
    sources: list[WorkerWorldSettingComparisonBatchCandidate],
    references: list[ComparisonTargetReference],
) -> ScopeAmbiguityMatch | None:
    """Return one shared ambiguity path or reject a partially compatible group."""

    matches = [
        _find_scope_ambiguity_match(decision, source, references)
        for source in sources
    ]
    matched = [match for match in matches if match is not None]
    if not matched:
        return None
    normalized_setting_names = {
        _backend_duplicate_key(source.setting_name) for source in sources
    }
    if (
        len(matched) != len(sources)
        or any(source.scope_name is not None for source in sources)
        or len(normalized_setting_names) != 1
    ):
        raise ValueError(
            "A scope-unresolved decision must contain only compatible unscoped same-name sources."
        )
    match_keys = {
        (
            match.target_ref,
            _backend_duplicate_key(match.scope_name),
            _backend_duplicate_key(match.property_name),
        )
        for match in matched
    }
    if len(match_keys) != 1:
        raise ValueError(
            "All scope-unresolved sources must identify the same existing scoped property."
        )
    return matched[0]


def _find_scope_ambiguity_match(
    decision: WorldSettingComparisonDecision,
    candidate: WorkerWorldSettingCandidatePayload,
    references: list[ComparisonTargetReference],
) -> ScopeAmbiguityMatch | None:
    """모델이 선택했거나 후보명으로 확정되는 대상 안에서 범위 누락을 찾는다."""

    if (candidate.scope_name is not None
            or decision.review_reason == WorldSettingComparisonReviewReason.GENERAL_UNCERTAINTY):
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
    *,
    display_names: tuple[str | None, ...] = (),
) -> None:
    display_reason = _replace_target_reference_text(comparison_reason, references)
    if LOCAL_REFERENCE_PATTERN.search(display_reason):
        raise ValueError("comparison_reason must not expose a local reference.")
    if UUID_PATTERN.search(display_reason):
        raise ValueError("comparison_reason must not expose a UUID.")
    internal_token_reason = _mask_target_display_names(display_reason, references, display_names)
    if INTERNAL_REASON_TOKEN_PATTERN.search(internal_token_reason):
        raise ValueError("comparison_reason must not expose an internal enum or key.")


def _mask_target_display_names(
    display_reason: str,
    references: list[ComparisonTargetReference],
    additional_names: tuple[str | None, ...] = (),
) -> str:
    display_names: set[str] = {name for name in additional_names if name}
    for target_reference in references:
        target = target_reference.target
        if target.subject_name:
            display_names.add(target.subject_name)
        for property_value in target.properties:
            if property_value.scope_name:
                display_names.add(property_value.scope_name)
            if property_value.setting_name:
                display_names.add(property_value.setting_name)

    masked_reason = display_reason
    for display_name in sorted(display_names, key=len, reverse=True):
        masked_reason = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(display_name)}(?![A-Za-z0-9_])",
            "사용자 표시명",
            masked_reason,
            flags=re.IGNORECASE,
        )
    return masked_reason


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
