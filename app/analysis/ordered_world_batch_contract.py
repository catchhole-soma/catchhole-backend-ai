"""Ordered-only world batch output and feedback; source text never enters error metadata."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.analysis.json_response import safe_validation_error_summary
from app.analysis.ordered_world_rule_diagnostics import (
    OrderedWorldRuleDiagnosticError, RULE_CODES, VALIDATION_STAGES,
)
from app.analysis.world_setting_schemas import (
    TrimmedName,
    TrimmedValue,
    WorldSettingComparisonBatchDecision,
    WorldSettingComparisonBatchResult,
)
from app.domain.enums import (
    WorldSettingComparisonReviewReason, WorldSettingConsolidationStatus, WorldSettingOperation,
)
from app.llm.protocols import LlmResponseSchema


class OrderedWorldSettingBatchDecision(WorldSettingComparisonBatchDecision):
    """Keep the domain validators and require explicit nulls on the provider wire."""

    model_config = ConfigDict(extra="forbid")

    review_reason: WorldSettingComparisonReviewReason | None = Field(...)
    target_ref: str | None = Field(...)
    matched_scope_name: TrimmedName | None = Field(...)
    matched_property_name: TrimmedName | None = Field(...)
    proposed_scope_name: TrimmedName | None = Field(...)
    existing_root_property_names_to_move: list[TrimmedName] = Field(..., max_length=20)


class OrderedWorldSettingBatchResult(WorldSettingComparisonBatchResult):
    model_config = ConfigDict(extra="forbid")

    decisions: list[OrderedWorldSettingBatchDecision] = Field(min_length=1, max_length=20)


class OrderedWorldPropertyDecision(BaseModel):
    """Provider selection: existing paths are copied only from the request-owned index."""

    model_config = ConfigDict(extra="forbid")

    source_candidate_refs: list[str] = Field(min_length=1, max_length=20)
    consolidation_status: WorldSettingConsolidationStatus
    operation: WorldSettingOperation
    review_reason: WorldSettingComparisonReviewReason | None
    target_ref: str | None
    matched_property_ref: str | None
    proposed_scope_name: TrimmedName | None
    proposed_setting_name: TrimmedName | None
    proposed_value: TrimmedValue
    comparison_reason: TrimmedValue
    existing_root_property_names_to_move: list[TrimmedName] = Field(max_length=20)

    @model_validator(mode="after")
    def require_existing_property_selection(self):
        if self.operation in {WorldSettingOperation.UPDATE, WorldSettingOperation.MERGE} and (
            self.target_ref is None or self.matched_property_ref is None
        ):
            raise ValueError("UPDATE and MERGE require target_ref and matched_property_ref.")
        return self


class OrderedWorldPropertyBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[OrderedWorldPropertyDecision] = Field(min_length=1, max_length=20)


ORDERED_WORLD_BATCH_RESPONSE_SCHEMA = LlmResponseSchema(
    name="ordered_world_setting_comparison_batch",
    schema=OrderedWorldPropertyBatchResult.model_json_schema(),
)

ORDERED_WORLD_BATCH_INSTRUCTIONS = """
이 순차 비교 응답에서는 아래 선택 참조 계약이 앞선 일반 예시의 matched 경로 필드보다
우선합니다. 기존 속성은 targets.properties[].ref(예: "T1.P1") 하나를 matched_property_ref로
선택하세요. matched_scope_name/matched_property_name은 출력하지 않습니다. Worker가 입력의
실제 경로를 복원하므로 기존 범위나 속성명을 다시 작성하지 마세요. UPDATE/MERGE는
proposed_scope_name과 proposed_setting_name도 모두 null로 반환하고 선택한 속성의 경로를
그대로 사용합니다. ADD/EXCLUDE/REVIEW_REQUIRED의 proposed_setting_name은 실제 제안 이름입니다.
comparison_reason에는 T1.P1 또는 P1 같은 내부 속성 참조를 쓰지 말고 실제 주체·범위·설정명으로 설명하세요.
targets는 이 묶음의 주체 해소에서 이미 연결한 대상입니다. 대상의 confirmation_status가
PROVISIONAL이면 임시 등록된 주체이고 CONFIRMED이면 작품에 저장된 주체입니다.
임시 주체나 properties가 빈 주체도 새로 연결할 대상이 아니라 이미 연결된 대상입니다.
targets가 하나라면 ADD와 EXCLUDE를 포함한 모든 decision에 그 ref를 target_ref로 유지하세요.
예를 들어 새로 발견한 임시 주체 T1에 첫 속성을 ADD해도 target_ref는 "T1"이며 null이 아닙니다.
ADD의 matched 경로가 null인 것과 주체 연결인 target_ref가 null인 것은 다릅니다.
주체의 등록 상태는 개별 속성의 신뢰도와 구분하며, 속성별 confirmation_status와
review_source를 따르세요. 확정되지 않았다는 이유로 주체를 다시 만들지 마세요.

순차 분석에서는 targets.properties의 PROVISIONAL 속성도 이미 존재하는 비교 경로입니다.
새 ADD를 판단하기 전에 해당 target의 모든 scope_name + setting_name 경로를 확인하세요.
아직 사용자 확정 전이라는 이유로 같은 경로를 다시 ADD하지 마세요.
properties가 비어 있으면 비교할 기존 속성이 없습니다. UPDATE/MERGE와 matched 경로를 가진
EXCLUDE를 반환하지 마세요. 독립된 새 사실은 ADD하고, 일시적 사건이나 부적절한 근거처럼
내용 자체를 제외할 이유가 있으면 EXCLUDE의 matched_property_ref를 null로 두세요. target_ref는 이미 연결된 주체를 계속 가리킵니다.
같은 배치의 다른 후보나 이번 응답에서 ADD할 속성은 아직 기존 속성이 아닙니다.
신규 후보끼리 같은 사실이면 source를 묶어 보존하거나 독립된 경로로 ADD하며, 다른 ADD를
이미 저장된 matched 경로처럼 참조해 EXCLUDE하지 마세요.

1. 기존 경로와 같은 사실을 비교한다면 의미에 따라 UPDATE, MERGE 또는 EXCLUDE를 판단하고,
   실제 target_ref와 matched_property_ref를 명시하세요.
   UPDATE/MERGE의 proposed scope/name은 null로 두며 선택한 기존 경로로 복원합니다. 중복 EXCLUDE도 실제 비교한
   matched 경로를 남깁니다. 이름이 같다는 이유만으로 뜻이 다른 사실을 덮어쓰지 마세요.
2. root 속성은 값 하나이며 scope는 하위 속성을 담는 구조입니다. 같은 target에서 같은
   이름을 둘 다 사용할 수 없습니다. 기존 root '신체 능력'이 있다면 새 범위를 같은
   '신체 능력'으로 만들고 그 아래 '생명력'과 '근력'을 ADD할 수 없습니다. 앞의 범위 생성
   예시도 이 충돌 검사를 통과할 때만 적용합니다. 독립된 새 사실이면 충돌하지 않는 root
   경로나 실제 형제 조건을 만족하는 다른 범위를 검토하세요. 기존 값을 임의로 삭제하거나
   옮겨 충돌을 없애지 마세요. 반대로 기존 '신체 능력 › 생명력'이 있다면 root '신체 능력'
   ADD도 금지됩니다. existing_root_property_names_to_move도 충돌을 우회하는 수단이 아닙니다.
3. UPDATE/MERGE 및 기존 속성과 비교하는 EXCLUDE는 후보의 scope_name과 선택한 기존 속성의
   scope_name도 같아야 합니다.
   범위가 없는 후보의 기존 SCOPE_UNRESOLVED 조건은 유지합니다. 범위가 명시된 단일 후보와
   같은 주체의 실제 기존 속성이 의미상 관련되지만 범위가 달라 포함/동일 관계를 확인해야
   한다면 REVIEW_REQUIRED + SCOPE_MISMATCH를 명시적으로 제안할 수 있습니다.
   source_candidate_refs는 한 개이며 proposed_scope_name/proposed_setting_name은 원본 후보
   경로를 그대로 유지합니다. matched 경로는 실제 기존 속성을 가리키며 root라면
   matched_scope_name=null도 가능합니다. comparison_reason에는 두 범위와 속성의 관련성 및
   확인할 내용을 구체적인 한국어로 설명하세요. 의미상 무관한 속성, 다른 주체, 없는 경로,
   ADD 충돌이나 일반 오류를 이 검토로 우회하지 마세요. 모든 후보를 정확히 한 번씩 판단합니다.
   이 범위 검토는 앞선 같은 속성의 source 통합 규칙의 예외입니다. 같은 원본 경로의 여러
   후보가 각각 위 검토 조건을 만족하면 같은 응답 안에서 source 하나당 별도 검토 decision을
   반환하세요. 각 원본값과 근거를 보존하며 정상 ADD/UPDATE/MERGE의 공동 판단은 계속 유지합니다.

4. 범위가 없는 단일 후보가 같은 주체의 실제 scoped 속성과 의미상 관련되지만 이름이 다르면,
   모델이 명시적으로 REVIEW_REQUIRED + SCOPE_UNRESOLVED를 제안할 수 있습니다. 실제 기존
   matched_property_ref를 선택하고 proposed_scope_name=null, proposed_setting_name은 원본
   후보 이름 그대로, 이동 목록은 []로 두세요. comparison_reason에는 이름이 다른 두 설정의
   관련성과 확인할 내용을 구체적인 한국어로 설명하세요. 이름이 다르다는 이유만으로 무관한
   사실을 검토로 묶지 마세요. 범위를 자동 상속하거나 모든 일반 오류를 검토로 바꾸지 않습니다.
   검토 이유는 기존 속성이 아니라 원본 후보의 scope_name을 기준으로 구분합니다. 원본이 null이면
   SCOPE_MISMATCH를 사용할 수 없습니다. 오류를 없애려고 기존 scope를 proposed_scope_name에
   복사하거나 새 scope를 만들지 말고 null을 유지하세요. 명시적 원본 scope가 있을 때만
   SCOPE_MISMATCH 조건을 검토합니다.

5. 대상의 동일성이나 설명의 의미를 확실히 판단하기 어렵지만 위의 범위 검토 조건에 맞지 않으면
   REVIEW_REQUIRED + GENERAL_UNCERTAINTY를 명시적으로 선택하세요. 전체 종류와 특정 하위 종류처럼
   관련은 있어도 같은 대상인지 불확실하거나, 비슷한 항목이 같은 내용을 뜻하는지
   판단하기 어려운 경우입니다. 같은 등급·종류라는 이유로 다른 대상을 합치지 마세요.
   source_candidate_refs는 후보 하나씩 반환하고 proposed_scope_name/proposed_setting_name/
   proposed_value는 그 원본 후보의 scope_name/setting_name/extracted_value를 그대로 유지하세요.
   existing_root_property_names_to_move는 []입니다. 이미 연결된 target_ref는 유지하되 이 판단이
   대상 연결을 확정한다는 뜻은 아닙니다. 실제로 관련성을 비교한 기존 속성이 있으면 그
   matched_property_ref를 선택하고, 특정 기존 속성을 비교하지 않았다면 null로 두세요.
   입력에 없는 대상이나 속성을 만들어 이 검토로 우회하지 마세요. 기존 SCOPE_UNRESOLVED와
   SCOPE_MISMATCH의 조건은 그대로이며 잘못된 응답을 임의로 일반 검토로 바꾸지 않습니다.
   이 결과는 자동 반영하지 않습니다. comparison_reason에는 어떤 대상·내용을 확인해야 하는지
   자연스러운 한국어로 설명하세요. 예: '대상이나 내용을 확실히 판단하기 어려워 확인이 필요합니다.'

출력 schema의 모든 필드를 명시하세요. 적용되지 않는 nullable 필드는 생략하지 말고
JSON null로 쓰며, existing_root_property_names_to_move는 이동이 없으면 []입니다.
ADD에는 matched_property_ref와 review_reason이 null입니다.
UPDATE/MERGE에는 실제 target_ref와 matched_property_ref가 필요하며 review_reason은 null입니다.
EXCLUDE는 기존 속성과 비교했다면 matched_property_ref를 지정하고 review_reason은 null입니다.
SCOPE_UNRESOLVED에는 실제 scoped 속성 ref가 필요합니다. SCOPE_MISMATCH에는 위 조건의
원본 proposed 경로와 실제 기존 속성 ref를 함께 남기고 이동 목록은 []로 두세요.
""".strip()

_CONFLICT_CODES = frozenset({"ADD_EXISTING_PATH", "ADD_ROOT_SCOPE_CONFLICT"})
_PATH_RULES = {
    "GENERAL_UNCERTAINTY_REVIEW_INVALID": (
        "source_candidate_refs",
        "GENERAL_UNCERTAINTY는 source 하나씩 검토하고 원본 scope/name/value를 보존하세요. "
        "이동 목록은 []이며 실제로 비교한 기존 속성만 선택할 수 있습니다.",
    ),
    "COMPARISON_REASON_REFERENCE_INVALID": (
        "comparison_reason",
        "사용자에게 보이는 이유에는 T1.P1/P1 같은 내부 속성 참조를 쓰지 말고 입력의 실제 "
        "주체·범위·설정명을 사용한 자연스러운 한국어로 설명하세요.",
    ),
    "MATCHED_PROPERTY_REF_INVALID": (
        "matched_property_ref",
        "matched_property_ref는 입력 targets.properties의 ref 하나만 선택하세요. 없는 참조나 "
        "이번 응답의 신규 ADD는 선택할 수 없습니다. 비교하지 않는 ADD/EXCLUDE는 null입니다.",
    ),
    "MATCHED_PROPERTY_TARGET_MISMATCH": (
        "matched_property_ref",
        "선택한 기존 속성 ref는 해당 decision의 target_ref에 속해야 합니다. 입력에 연결된 "
        "대상과 그 대상 아래 실제 속성만 선택하세요.",
    ),
    "EXISTING_PROPOSED_PATH_FORBIDDEN": (
        "proposed_setting_name",
        "UPDATE/MERGE는 matched_property_ref 하나로 기존 위치를 선택합니다. "
        "proposed_scope_name과 proposed_setting_name은 모두 null로 반환하세요.",
    ),
    "SCOPE_UNRESOLVED_REVIEW_INVALID": (
        "review_reason",
        "이름이 다른 기존 scoped 속성의 범위 검토는 명시적인 REVIEW_REQUIRED + "
        "SCOPE_UNRESOLVED와 범위 없는 단일 원본 후보만 허용합니다. 원본 proposed 경로를 "
        "보존하고 실제 기존 속성을 선택하며 이동 목록은 []로 두세요.",
    ),
    "RECOVERY_SOURCE_PATH_CHANGED": (
        "proposed_scope_name",
        "개별 복구 비교에서는 모든 source의 원본 범위와 설정명을 보존하고 기존 root 속성을 "
        "이동하지 마세요. 원본 경로 그대로 판단할 수 없으면 허용된 명시적 범위 검토를 제안하세요.",
    ),
    "MATCHED_EXCLUDE_PATH_NOT_FOUND": (
        "matched_property_ref",
        "EXCLUDE의 matched 경로는 입력 target.properties에 실제 존재해야 합니다. 같은 배치의 "
        "신규 후보나 ADD는 기존 속성이 아닙니다. 새 사실은 ADD하고, 내용 자체를 제외할 근거가 "
        "있을 때만 matched 두 필드를 null로 둔 EXCLUDE를 반환하세요.",
    ),
    "MATCHED_PROPERTY_PATH_NOT_FOUND": (
        "matched_property_ref",
        "matched 경로가 입력 target.properties에 없습니다. 실제 존재하는 전체 경로만 선택하세요. "
        "없는 속성은 UPDATE/MERGE나 범위 검토의 대상이 될 수 없습니다.",
    ),
    "GENERATED_SCOPE_REQUIRES_SIBLINGS": (
        "proposed_scope_name",
        "원본과 다른 새 범위에는 서로 다른 최종 하위 속성이 둘 이상 필요합니다. 형제가 없는 "
        "속성은 root ADD로 두거나 원래 입력 범위를 유지하세요. 같은 속성을 중복 생성하거나 "
        "없는 기존 속성을 이동/EXCLUDE하여 조건을 우회하지 마세요.",
    ),
    "SOURCE_SCOPE_MISMATCH": (
        "matched_property_ref",
        "UPDATE/MERGE 및 matched EXCLUDE는 원본 후보 범위를 유지해야 합니다. 검토 이유는 "
        "원본 후보의 scope_name을 기준으로 고르세요. 원본이 null인 단일 후보와 이름이 다른 "
        "실제 scoped 속성의 관련성이 불명확하면 REVIEW_REQUIRED + SCOPE_UNRESOLVED를 "
        "명시적으로 제안하고 proposed_scope_name=null과 원본 이름을 보존하세요. 이 경우 "
        "SCOPE_MISMATCH는 금지하며 기존 scope를 복사하거나 새 scope를 만들지 마세요. "
        "명시적 원본 scope가 있는 단일 후보만 SCOPE_MISMATCH 조건을 검토할 수 있습니다. "
        "matched 경로는 실제 기존 속성 그대로 두고 관련성과 확인할 내용을 설명하세요. "
        "무관한 사실은 새 경로로 판단하세요.",
    ),
    "SCOPE_MISMATCH_REVIEW_INVALID": (
        "review_reason",
        "SCOPE_MISMATCH는 명시적 범위가 있는 단일 후보만 허용합니다. 실제 기존 matched 경로의 "
        "범위는 후보와 달라야 하고 proposed 범위/속성명은 원본과 같아야 합니다. 이동 목록은 []입니다. "
        "원본 scope가 null이면 scope를 만들거나 복사하지 마세요. 이름이 다른 실제 scoped 속성과 "
        "관련된 단일 후보는 SCOPE_UNRESOLVED 조건을 검토하고 원본 null 범위와 이름을 보존하세요.",
    ),
}
_VALIDATION_CODES = _CONFLICT_CODES | {
    "CANONICAL_TARGET_REQUIRED", "SOURCE_REF_UNKNOWN", "SOURCE_REF_DUPLICATED",
    "SOURCE_REF_MISSING", "TARGET_REF_UNKNOWN", "SOURCE_CONSOLIDATION_INVALID",
    "SOURCE_SCOPE_MIXED",
} | _PATH_RULES.keys()
_DECISION_FIELDS = frozenset(OrderedWorldPropertyDecision.model_fields) | frozenset(
    OrderedWorldSettingBatchDecision.model_fields,
)
_SAFE_ERROR_TYPES = frozenset({
    "missing", "extra_forbidden", "value_error", "string_type", "string_too_short",
    "string_too_long", "string_pattern_mismatch", "list_type", "model_type", "dict_type",
    "enum", "literal_error", "too_short", "too_long",
})
_DOMAIN_ERROR_RULES = {
    "UPDATE and MERGE require target_ref and matched_property_ref.": (
        "UPDATE_MERGE_MATCH_REQUIRED",
        "UPDATE/MERGE에는 실제 target_ref와 matched_property_ref를 지정하세요.",
    ),
    "matched_scope_name requires matched_property_name.": (
        "MATCHED_SCOPE_PROPERTY_REQUIRED", "matched_scope_name에는 matched_property_name도 필요합니다.",
    ),
    "matched_property_name requires target_ref.": (
        "MATCHED_TARGET_REQUIRED", "matched_property_name을 지정하면 실제 target_ref도 지정하세요.",
    ),
    "UPDATE and MERGE require target_ref and matched_property_name.": (
        "UPDATE_MERGE_MATCH_REQUIRED",
        "UPDATE/MERGE에는 실제 target_ref와 matched_property_name을 지정하세요.",
    ),
    "ADD must not include matched_property_name.": (
        "ADD_MATCH_FORBIDDEN", "ADD의 matched_scope_name과 matched_property_name은 null입니다.",
    ),
    "Scope REVIEW_REQUIRED requires SCOPE_UNRESOLVED and a scoped matched property.": (
        "SCOPE_REVIEW_MATCH_REQUIRED",
        "SCOPE_UNRESOLVED 검토에는 실제 target_ref와 scoped matched 경로가 필요합니다. "
        "선택한 기존 속성에 scope가 없고 대상이나 내용 자체가 불확실하다면 조건에 맞는 "
        "GENERAL_UNCERTAINTY를 명시적으로 검토하세요. source 하나의 원본 경로와 값을 보존하고 "
        "이동하지 마세요. 모호하지 않은 사실은 의미에 맞는 기존 연산으로 판단하세요.",
    ),
    "Batch-limit REVIEW_REQUIRED must not include a matched path.": (
        "BATCH_LIMIT_MATCH_FORBIDDEN", "모델이 출력 예산 초과 판단을 만들지 마세요.",
    ),
    "REVIEW_REQUIRED requires a supported review_reason.": (
        "REVIEW_REASON_REQUIRED", "REVIEW_REQUIRED는 명시된 GENERAL_UNCERTAINTY/SCOPE_UNRESOLVED/"
        "SCOPE_MISMATCH 조건에 맞는 사유를 선택하세요.",
    ),
    "Scope-mismatch review requires a matched target property and source scope.": (
        "SCOPE_MISMATCH_MATCH_REQUIRED",
        "SCOPE_MISMATCH는 명시적 scope가 있는 원본 후보만 허용합니다. 원본 scope_name이 null이면 "
        "proposed_scope_name을 만들거나 기존 범위에서 복사하지 마세요. 같은 주체의 실제 scoped "
        "속성과 관련된 범위 없는 단일 후보는 REVIEW_REQUIRED + SCOPE_UNRESOLVED를 명시적으로 "
        "제안하고 proposed_scope_name=null, proposed_setting_name=원본 이름, 이동 목록=[]를 "
        "유지하세요. 실제 target_ref와 matched_property_ref를 선택하고 관련성과 확인할 내용을 "
        "설명하세요. 원본에 scope가 있는 경우에만 원본 proposed_scope_name을 보존한 "
        "SCOPE_MISMATCH 조건을 검토하세요.",
    ),
    "Only REVIEW_REQUIRED may include review_reason.": (
        "REVIEW_REASON_FORBIDDEN", "REVIEW_REQUIRED 이외 판단의 review_reason은 null입니다.",
    ),
    "source_candidate_refs must not contain duplicates.": (
        "SOURCE_REF_DUPLICATED", "source_candidate_refs에는 같은 C*를 중복해서 넣지 마세요.",
    ),
    "existing_root_property_names_to_move must not contain duplicates.": (
        "ROOT_MOVE_DUPLICATED", "이동할 기존 root 속성은 한 번씩만 열거하세요.",
    ),
}


class OrderedWorldBatchValidationError(ValueError):
    """Carry only local references and indices; the retry builder rechecks the input."""

    def __init__(self, *, reason_code: str, decision_index: int,
                 source_candidate_refs: list[str], target_ref: str | None,
                 property_indices: list[int]) -> None:
        self.reason_code = (reason_code if reason_code in _VALIDATION_CODES
                            else "COMPARISON_VALIDATION_FAILED")
        self.decision_index = decision_index
        self.source_candidate_refs = tuple(source_candidate_refs)
        self.target_ref = target_ref
        self.property_indices = tuple(property_indices)
        # Only a fixed code can enter exception logs or the final failure message.
        super().__init__(self.reason_code)


def _safe_issue(error: dict) -> dict:
    location = tuple(error.get("loc") or ())
    issue = {
        "field_path": "response",
        "error_type": (error["type"] if error.get("type") in _SAFE_ERROR_TYPES
                       else "schema_invalid"),
    }
    # Domain validators use fixed ValueError literals. Match the exact built-in
    # exception argument; never stringify, copy or emit arbitrary ctx content.
    context = error.get("ctx")
    cause = context.get("error") if isinstance(context, dict) else None
    if (error.get("type") == "value_error" and type(cause) is ValueError
            and len(cause.args) == 1 and type(cause.args[0]) is str):
        rule = _DOMAIN_ERROR_RULES.get(cause.args[0])
        if rule is not None:
            issue["reason_code"], issue["correction"] = rule
        elif cause.args[0] in RULE_CODES:
            issue["reason_code"] = RULE_CODES[cause.args[0]]
    if not location or location[0] != "decisions":
        return issue
    issue["field_path"] = "decisions"
    if len(location) > 1 and type(location[1]) is int and 0 <= location[1] < 20:
        issue["field_path"] = "decisions[]"
        issue["decision_index"] = location[1]
        if len(location) > 2 and location[2] in _DECISION_FIELDS:
            issue["field_path"] += f".{location[2]}"
            if len(location) > 3 and type(location[3]) is int:
                issue["field_path"] += "[]"
    return issue


def _schema_issues(error: ValidationError) -> list[dict]:
    issues = []
    for item in error.errors(include_url=False, include_context=True, include_input=False):
        issue = _safe_issue(item)
        if issue not in issues:
            issues.append(issue)
        if len(issues) == 8:
            break
    return issues


def ordered_world_batch_error_summary(error: Exception | None) -> str:
    """Persist bounded, allowlisted diagnostics even when the final attempt fails."""
    if isinstance(error, OrderedWorldRuleDiagnosticError):
        code = (error.diagnostic_rule_code if error.diagnostic_rule_code in RULE_CODES.values()
                else "COMPARISON_VALIDATION_FAILED")
        stage = (error.validation_stage if isinstance(error.validation_stage, str)
                 and error.validation_stage in VALIDATION_STAGES else "DECISION_VALIDATION")
        index = (f"; decision_index={error.decision_index}"
                 if type(error.decision_index) is int and 0 <= error.decision_index < 20 else "")
        return f"ordered_batch={code}; stage={stage}{index}"
    if isinstance(error, OrderedWorldBatchValidationError):
        code = (error.reason_code if error.reason_code in _VALIDATION_CODES
                else "COMPARISON_VALIDATION_FAILED")
        index = (f"; decision_index={error.decision_index}"
                 if type(error.decision_index) is int and 0 <= error.decision_index < 20 else "")
        return f"ordered_batch={code}{index}; {safe_validation_error_summary(error)}"
    if isinstance(error, ValidationError):
        issues = _schema_issues(error)
        diagnostics = []
        for issue in issues[:3]:
            path = issue["field_path"]
            if "decision_index" in issue:
                path = path.replace("[]", f"[{issue['decision_index']}]", 1)
            detail = f"{path}:{issue['error_type']}"
            if "reason_code" in issue:
                detail += f":{issue['reason_code']}"
            diagnostics.append(detail)
        error_types = ','.join(sorted({issue["error_type"] for issue in issues}))
        return (f"ordered_batch=RESPONSE_SCHEMA_INVALID; issues={','.join(diagnostics)}; "
                f"ValidationError(types={error_types})")
    return safe_validation_error_summary(error)


def _input_conflict(payload: dict, error: OrderedWorldBatchValidationError) -> dict | None:
    if (error.reason_code not in _CONFLICT_CODES or type(error.decision_index) is not int
            or not 0 <= error.decision_index < 20):
        return None
    allowed_sources = {
        item.get("ref") for item in payload.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
        and re.fullmatch(r"C[1-9][0-9]*", item["ref"])
    }
    source_refs = list(dict.fromkeys(
        ref for ref in error.source_candidate_refs if ref in allowed_sources
    ))
    if not source_refs or not re.fullmatch(r"T[1-9][0-9]*", error.target_ref):
        return None
    target = next((item for item in payload.get("targets", [])
                   if isinstance(item, dict) and item.get("ref") == error.target_ref), None)
    if target is None:
        return None
    properties = target.get("properties", [])
    existing_paths = []
    for index in dict.fromkeys(error.property_indices):
        if type(index) is not int or not 0 <= index < len(properties):
            continue
        prop = properties[index]
        if not isinstance(prop, dict) or not isinstance(prop.get("setting_name"), str):
            continue
        scope = prop.get("scope_name")
        if scope is not None and not isinstance(scope, str):
            continue
        existing_paths.append({"property_ref": f"{target['ref']}.P{index + 1}",
                               "property_index": index, "scope_name": scope,
                               "setting_name": prop["setting_name"]})
    if not existing_paths:
        return None
    return {"decision_index": error.decision_index, "source_candidate_refs": source_refs,
            "target_ref": error.target_ref, "existing_paths": existing_paths}


def _input_required_target(payload: dict, error: OrderedWorldBatchValidationError) -> dict | None:
    """Derive the required identity from the original input, never a rejected value."""
    targets = payload.get("targets", [])
    if (error.reason_code != "CANONICAL_TARGET_REQUIRED" or len(targets) != 1
            or type(error.decision_index) is not int or not 0 <= error.decision_index < 20):
        return None
    target = targets[0]
    if (not isinstance(target, dict) or not isinstance(target.get("ref"), str)
            or not re.fullmatch(r"T[1-9][0-9]*", target["ref"])
            or target["ref"] != error.target_ref):
        return None
    allowed_sources = {
        item["ref"] for item in payload.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
        and re.fullmatch(r"C[1-9][0-9]*", item["ref"])
    }
    source_refs = list(dict.fromkeys(error.source_candidate_refs))
    if not source_refs or any(ref not in allowed_sources for ref in source_refs):
        return None
    return {"decision_index": error.decision_index, "source_candidate_refs": source_refs,
            "target_ref": target["ref"]}


def _input_path_rule(payload: dict, error: OrderedWorldBatchValidationError) -> dict | None:
    if (error.reason_code not in _PATH_RULES or type(error.decision_index) is not int
            or not 0 <= error.decision_index < 20):
        return None
    candidates = {
        item["ref"]: item for item in payload.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
        and re.fullmatch(r"C[1-9][0-9]*", item["ref"])
    }
    refs = list(dict.fromkeys(error.source_candidate_refs))
    if not refs or any(ref not in candidates for ref in refs):
        return None
    source_paths = []
    for ref in refs:
        source = candidates[ref]
        if (not isinstance(source.get("setting_name"), str)
                or source.get("scope_name") is not None
                and not isinstance(source["scope_name"], str)):
            return None
        source_paths.append({"candidate_ref": ref, "scope_name": source.get("scope_name"),
                             "setting_name": source["setting_name"]})
    context = {"decision_index": error.decision_index, "source_paths": source_paths}
    if error.target_ref is None:
        return context if error.reason_code == "GENERATED_SCOPE_REQUIRES_SIBLINGS" else None
    if not isinstance(error.target_ref, str) or not re.fullmatch(r"T[1-9][0-9]*", error.target_ref):
        return None
    target = next((item for item in payload.get("targets", [])
                   if isinstance(item, dict) and item.get("ref") == error.target_ref), None)
    if target is None or not isinstance(target.get("properties"), list):
        return None
    properties = target["properties"]
    indices = (error.property_indices if error.property_indices else range(len(properties)))
    paths = []
    for index in dict.fromkeys(indices):
        if type(index) is not int or not 0 <= index < len(properties):
            continue
        prop = properties[index]
        if (not isinstance(prop, dict) or not isinstance(prop.get("setting_name"), str)
                or prop.get("scope_name") is not None and not isinstance(prop["scope_name"], str)):
            continue
        paths.append({"property_ref": f"{target['ref']}.P{index + 1}",
                      "property_index": index, "scope_name": prop.get("scope_name"),
                      "setting_name": prop["setting_name"]})
        if len(paths) == 20:
            break
    context.update(target_ref=target["ref"], existing_paths=paths,
                   existing_property_count=len(properties))
    return context


def ordered_world_batch_retry_prompt(original: str, error: Exception) -> str:
    """Use input-owned paths and fixed diagnostics, never exception or response prose."""
    payload = json.loads(original)
    feedback = {
        "previous_response_rejected": True,
        "reason_code": "COMPARISON_VALIDATION_FAILED",
        "issues": [],
        "correction": (
            "표시한 판단과 입력의 기존 전체 경로를 다시 비교하세요. 기존 exact 경로에 ADD하지 "
            "말고 같은 사실이면 의미에 맞는 UPDATE/MERGE/EXCLUDE와 실제 matched_property_ref를 "
            "반환하세요. 기존 root 속성과 같은 이름의 scope를 만들거나 그 반대로 만들지 "
            "마세요. 독립 사실은 충돌하지 않는 경로로 판단하고 기존 값을 임의로 바꾸지 "
            "마세요. schema의 모든 필드를 포함하며 적용되지 않는 필드는 null 또는 []로 "
            "명시하세요. 모든 C*를 정확히 한 번 포함한 JSON 전체를 다시 반환하세요."
        ),
    }
    if isinstance(error, OrderedWorldBatchValidationError):
        required_target = _input_required_target(payload, error)
        conflict = _input_conflict(payload, error)
        path_rule = _input_path_rule(payload, error)
        if required_target is not None:
            feedback["reason_code"] = "CANONICAL_TARGET_REQUIRED"
            feedback["required_target"] = required_target
            feedback["issues"] = [{"field_path": "decisions[].target_ref",
                                   "decision_index": required_target["decision_index"],
                                   "error_type": "target_required"}]
            feedback["correction"] = (
                "targets에 하나 있는 대상은 이 묶음에 이미 연결된 주체입니다. 임시 주체이거나 "
                "properties가 비어 있어도 ADD와 EXCLUDE를 포함한 모든 decision에 그 ref를 "
                "target_ref로 유지하세요. ADD의 matched 경로는 null이지만 target_ref는 null이 "
                "아닙니다. 새 주체를 만들지 말고 모든 후보를 정확히 한 번 포함한 JSON 전체를 "
                "다시 반환하세요. operation과 값은 원래 근거에 따라 판단하세요."
            )
        elif conflict is not None:
            feedback["reason_code"] = error.reason_code
            feedback["conflict"] = conflict
            feedback["issues"] = [{"field_path": "decisions[]",
                                   "decision_index": conflict["decision_index"],
                                   "error_type": "path_conflict"}]
        elif path_rule is not None:
            field, correction = _PATH_RULES[error.reason_code]
            feedback["reason_code"] = error.reason_code
            feedback["input_paths"] = path_rule
            feedback["issues"] = [{"field_path": f"decisions[].{field}",
                                   "decision_index": error.decision_index,
                                   "error_type": "path_contract_invalid"}]
            feedback["correction"] = correction + " 모든 후보를 포함한 JSON 전체를 다시 반환하세요."
    elif isinstance(error, ValidationError):
        feedback["reason_code"] = "RESPONSE_SCHEMA_INVALID"
        feedback["issues"] = _schema_issues(error)
    elif isinstance(error, json.JSONDecodeError):
        feedback["reason_code"] = "RESPONSE_JSON_INVALID"
        feedback["issues"] = [{"field_path": "response", "error_type": "json_invalid"}]
    if not feedback["issues"]:
        feedback["issues"] = [{"field_path": "response", "error_type": "contract_invalid"}]
    payload["validation_feedback"] = feedback
    return json.dumps(payload, ensure_ascii=False)
