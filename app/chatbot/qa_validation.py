"""Bounded retry feedback made only from schema names and fixed contract rules."""

from pydantic import ValidationError

from app.chatbot.runtime import ProviderFailure


class ResponseValidationFailure(ProviderFailure):
    """Complete responses exhausted schema/contract recovery without a valid answer.

    Provider calls have already been metered. This carries no rejected response,
    validation exception or usage to replay, and retains the legacy failure type
    for callers that do not explicitly recover from invalid model responses.
    """

    def __init__(self):
        super().__init__()


class LookupClarificationRequired(ResponseValidationFailure):
    """Only observed-vocabulary repair failed; no provider/transport failure."""


RULES = {
    "UNOBSERVED_COLLECTION_PREDICATE": "collection.predicate는 이번 질문 또는 명확한 대화의 연속된 조건 구절을 그대로 복사하세요. 여러 조건을 재작성하거나 세미콜론으로 합치지 말고, 필요하면 조건들을 포함한 질문 전체를 그대로 사용하세요. collection의 범위·유형과 subjects=[]를 유지하고 조건을 삭제하거나 비교 기준 인물을 subjects에 추가하지 마세요.",
    "UNOBSERVED_COLLECTION_ATTRIBUTE": "collection.attribute는 이번 질문 또는 명확한 대화의 실제 연속 표현만 사용하세요. 조건은 predicate에 보존하고 collection 조회를 단일 인물 조회로 바꾸지 마세요.",
    "PLAN_COLLECTION": "작품 전체 조건·비교 조회는 collection과 CURRENT, subjects=[], characterFilter=null을 함께 사용하세요. 비교 기준으로 언급된 인물을 subjects에 추가하지 말고 조건 구절을 collection.predicate에 보존하세요. 단일 인물 조회와 collection을 동시에 만들지 마세요.",
    "UNOBSERVED_PART_EPISODE": "requestedParts.sourceEpisodeNo는 현재 질문에 N화로 직접 명시된 출처 회차만 사용하세요. 이전 대화나 작품 지식으로 추정하지 말고 명시되지 않은 부분은 null로 두세요. 요청의 내용은 유지하세요.",
    "REQUESTED_PARTS_INCOMPLETE": "requestedParts의 모든 P*를 실제 답한 claims.answeredFacets와 미확인 missingFacets로 빠짐없이 나누고 두 목록을 겹치지 마세요. 현재값/원문 비교는 confirmed=true인 실제 CURRENT R* 값과 E* 원문을 구분해 답하면 두 요구를 함께 표시할 수 있습니다. 원문만으로 현재값을 확정하지 말고 확인하지 못한 요구는 missingFacets에 남기세요. 요구를 삭제하지 마세요.",
    "ANSWER_COVERAGE_MISMATCH": "답한 요청 부분과 근거·conflicts는 유지하고 coverage를 고치세요. conflicts가 있으면 모든 요구를 답해도 PARTIAL입니다. 미답 행/facet이 있어도 근거 있는 주장·관측·충돌이 있으면 PARTIAL이고, 그런 내용이 전혀 없으면 NONE입니다. 모든 요구를 답하고 conflicts·미답 행/facet이 모두 없을 때만 COMPLETE입니다. COMPLETE를 맞추려고 충돌·미확인 요구를 삭제하지 마세요.",
    "EVENT_CONTEXT_REQUIRED": "원문에서 관측한 사건이 질문의 실제 사건·계획·가정 문맥과 일치하는지 명시적으로 판정하세요.",
    "UNKNOWN_REFERENCE": "참조는 이번 입력에 제공된 목록에서만 선택하고 중복 없이 사용하세요.",
    "UNOBSERVED_LOOKUP_TERM": "조회 이름과 항목 표현은 이번 질문 또는 제공된 대화에 실제로 있는 표현만 사용하세요.",
    "UNOBSERVED_FOCUS_ATTRIBUTE": "attribute에는 이번 질문 또는 남아 있는 대화에 실제로 있는 표현을 그대로 쓰세요. 정정이 값으로만 표현되면 그 값 표현을 쓰고, 실제 항목은 FIELD 목록에서 선택합니다. 없는 항목명이나 저장 key를 만들지 마세요.",
    "UNOBSERVED_SUBJECT_NAME": "subjects.name에는 이번 질문 또는 남아 있는 대화에 그대로 나온 이름을 쓰세요. 원문 지식으로 본명·별칭을 생성하지 말고 대상이 하나로 해소되지 않으면 CLARIFICATION으로 두세요.",
    "UNOBSERVED_SEARCH_TERM": "searchTerms의 공백으로 나뉜 각 단어는 이번 질문 또는 남아 있는 대화 문자열에 그대로 있는 부분 표현만 사용하세요. 관측한 단어의 공백 조합은 허용하지만 동의어·활용형을 새로 만들지 마세요. 원래 대상·사건·조건과 requestedParts를 유지하면서 실제 입력 표현으로 검색어를 고치세요. 단순 삭제로 요구를 없애지 마세요.",
    "PLAN_SEARCH_TERMS": "searchTerms는 비어 있지 않은 실제 입력 표현을 각각 80자 이하로 최대 6개 사용하세요. 긴 표현은 의미 있는 실제 부분 표현을 선택하고 원래 대상·사건·조건은 requestedParts에 보존하세요.",
    "PLAN_RDB_SUBJECT": "등록 자료를 조회할 때는 현재 질문 또는 명확한 대화의 subjects를 지정하거나 작품 전체 조회이면 collection을 사용하세요. 순수 원문 질문은 원래 요구를 유지하며 rdbMode=NONE으로 표현하세요.",
    "PLAN_INFORMATION_NEED": "PROCEED이면 실제 질문에 맞는 rdbMode 또는 sourceNeed를 지정하세요. 둘 다 NONE으로 요구를 없애지 말고, 대상을 해소할 수 없으면 CLARIFICATION을 사용하세요.",
    "PLAN_SUBJECTS": "같은 domain/name/worldCategory는 subject 하나로 유지하세요. 같은 대상의 여러 항목은 함께 포함된 실제 입력의 연속 구절을 attribute에 보존하고, 유형이 다르면 factType=null로 두어 FIELD에서 여러 경로를 고르세요. (null,null) 전체 조회로 넓히지 마세요. 중복 대상만 고치고 원래 HISTORY/CURRENT/원문 요구와 SUMMARY 등 설명·비교 목적을 유지하세요. 서로 다른 인물·분류를 합치거나 독립 요구를 삭제하지 마세요.",
    "CURRENT_REQUIREMENT_ROUTE": "현재 등록값 확인이 필수이면 registeredCurrentRequired=true를 유지하고 CURRENT/CURRENT_AND_HISTORY/DIRECT_EVIDENCE로 조회하며 sourceFallback=false로 두세요. 원문도 요청하면 sourceNeed를 따로 보존하세요.",
    "INVALID_PLAN": "조회 범위·대상·정보 필요·질문 유형에 따른 필드 조건을 schema와 원래 지침에 맞추세요.",
    "PLAN_PARTS_MODE": "requestedParts를 사용하면 requestedFacets는 비우세요. sourceNeed=NONE에서는 sourceFallback=true일 때만 requestedParts를 사용하고, 둘 다 원문 조회가 필요 없으면 비우세요.",
    "PLAN_PARTS_REQUIRED": "PROCEED인 원문 질문에는 실제 질문의 독립된 요구를 requestedParts에 하나 이상 넣으세요. 요구를 없애려고 sourceNeed를 바꾸지 마세요.",
    "PLAN_PARTS_REFERENCES": "requestedParts의 ref는 중복 없이 P1부터 P4까지 순서대로 쓰고 각 description은 실제 질문의 요구를 빈칸 없이 설명하세요.",
    "PLAN_PARTS_FIELD": "P1~P4 참조는 requestedParts의 ref에만 넣고 requestedFacets는 비우세요.",
    "PLAN_EVENT_CONTEXT": "특정 사건의 원문 질문에만 비어 있지 않은 eventContext를 쓰세요. sourceNeed=NONE이거나 특정 사건 질문이 아니면 null로 두세요.",
    "PLAN_FALLBACK_PARTS_REQUIRED": "sourceFallback=true이면 원문에서 보완할 실제 질문의 요구를 requestedParts에 하나 이상 넣으세요. 요구를 없애려고 sourceFallback을 끄지 마세요.",
    "PLAN_CONVERSATION_ONLY": "인사·감사·챗봇 도움말만 있으면 조회 필드를 비우고, 실제 작품 질문이 함께 있으면 일반 조회로 분류하세요.",
    "INVALID_SELECTION": "선택 결과 상태에 허용된 참조만 포함하고 개수·중복 제한을 지키세요.",
    "INCOMPLETE_EVIDENCE": "제공된 완전한 자료로만 판정하고 확인되지 않은 항목은 UNKNOWN으로 남기세요.",
    "RESPONSE_CONTRACT": "제공된 참조·조회 범위·항목별 답변 조건을 원래 지침과 다시 대조하세요.",
    "OUTPUT_TRUNCATED": "출력 상한에 도달했습니다. 설명을 간결하게 하고 필수 필드와 모든 요청 항목을 빠짐없이 포함한 완전한 JSON 하나를 반환하세요.",
}
for _code in (
    "GROUNDING_SUPPORT_REQUIRED",
    "GROUNDING_INVALID_PASSAGE",
    "GROUNDING_UNKNOWN_REFERENCE",
    "GROUNDING_QUOTE_NOT_FOUND",
    "GROUNDING_AMBIGUOUS_QUOTE",
    "GROUNDING_UNCOVERED_QUOTE",
):
    RULES[_code] = (
        "실제 passage에서 줄바꿈·띄어쓰기를 포함해 정확히 복사한 유일한 지원 구절과 같은 passage의 E 참조를 사용하세요."
    )


class ChatResponseContractError(ValueError):
    """A fixed rule code; arbitrary response values cannot become retry instructions."""

    def __init__(self, code: str):
        self.code = code if code in RULES else "RESPONSE_CONTRACT"
        super().__init__(self.code)


_MESSAGES = {
    "UNKNOWN_REFERENCE": (
        "Unknown evidence or setting reference",
        "Unknown registered evidence reference",
        "Unknown field reference",
        "Unknown or excessive row references",
        "Duplicate field references",
        "Duplicate row references",
        "Duplicate unanswered row references",
        "Duplicate logical item references",
    ),
    "UNOBSERVED_LOOKUP_TERM": (
        "Only names explicitly present in the question may be searched",
        "Only explicit question names and terms are accepted",
        "Lookup vocabulary must occur in this question or conversation",
    ),
    "REQUESTED_PARTS_INCOMPLETE": (
        "Every observed item requires one decision",
        "Each requested setting needs an answer or explicit uncertainty",
        "Claims must answer the requested information",
        "Duplicate requested facets",
        "Duplicate unanswered facets",
    ),
    "ANSWER_COVERAGE_MISMATCH": (
        "Coverage must describe requested information",
        "Coverage must describe requested facets, not reference presence",
        "Coverage and grounded statements disagree",
        "Unanswered rows cannot have complete coverage",
        "Unanswered facets cannot have complete coverage",
    ),
    "INCOMPLETE_EVIDENCE": ("Condition verdict must cite complete observed rows",),
    "INVALID_SELECTION": (
        "Only matched fields have references",
        "Only answers select rows",
        "An equality condition must identify one field",
        "No logical item references were supplied",
    ),
    "INVALID_PLAN": (
        "Invalid episode range",
        "Invalid source range",
        "Invalid search terms",
        "Duplicate subjects",
        "RDB lookup requires an explicit subject",
        "Lookup requires a named subject",
        "No information need",
        "Character lists require one exact current-value condition",
        "Character lists support current values without source reconstruction",
        "A character list queries current values across the work",
        "Work collections query current registered information",
        "Narrative facets require source evidence",
        "Event scope describes a requested time or process",
        "Narrative questions must identify requested information",
        "World collections have no character fact type",
        "Character collections have no world category",
        "A subject name is required",
        "World settings do not have character fact types",
        "Characters do not have a world category",
    ),
    "PLAN_PARTS_MODE": ("Requested parts replace narrative facets",),
    "PLAN_PARTS_REQUIRED": ("Narrative questions require explicit requested parts",),
    "PLAN_PARTS_REFERENCES": ("Requested parts must have ordered unique references",),
    "PLAN_PARTS_FIELD": ("Part references belong in requested parts",),
    "PLAN_EVENT_CONTEXT": ("Event context requires a narrative question",),
    "PLAN_FALLBACK_PARTS_REQUIRED": ("Source fallback requires requested parts",),
    "PLAN_CONVERSATION_ONLY": (
        "Conversational requests do not query work information",
        "Conversation replies cannot request factual lookups",
    ),
    "CURRENT_REQUIREMENT_ROUTE": (
        "Required current information needs a current lookup without fallback",
    ),
    "PLAN_SEARCH_TERMS": ("Invalid search terms",),
    "PLAN_RDB_SUBJECT": (
        "RDB lookup requires an explicit subject",
        "Lookup requires a named subject",
    ),
    "PLAN_INFORMATION_NEED": ("No information need",),
    "PLAN_SUBJECTS": ("Duplicate subjects",),
    "PLAN_COLLECTION": ("Work collections query current registered information",),
}
_MESSAGE_CODES = {message: code for code, messages in _MESSAGES.items() for message in messages}
_MESSAGE_CODES.update({code: code for code in RULES})
_RULE_FIELDS = {
    "ANSWER_COVERAGE_MISMATCH": "coverage",
    "PLAN_PARTS_MODE": "requestedParts",
    "PLAN_PARTS_REQUIRED": "requestedParts",
    "PLAN_PARTS_REFERENCES": "requestedParts",
    "PLAN_PARTS_FIELD": "requestedFacets",
    "PLAN_EVENT_CONTEXT": "eventContext",
    "PLAN_FALLBACK_PARTS_REQUIRED": "requestedParts",
    "CURRENT_REQUIREMENT_ROUTE": "registeredCurrentRequired",
    "PLAN_SEARCH_TERMS": "searchTerms",
    "PLAN_RDB_SUBJECT": "subjects",
    "PLAN_INFORMATION_NEED": "rdbMode",
    "PLAN_SUBJECTS": "subjects",
    "PLAN_COLLECTION": "collection",
}
_RULE_PATHS = {
    "UNOBSERVED_COLLECTION_PREDICATE": "$.collection.predicate",
    "UNOBSERVED_COLLECTION_ATTRIBUTE": "$.collection.attribute",
    "UNOBSERVED_PART_EPISODE": "$.requestedParts[].sourceEpisodeNo",
    "REQUESTED_PARTS_INCOMPLETE": "$.missingFacets",
    "ANSWER_COVERAGE_MISMATCH": "$.coverage",
    "UNOBSERVED_FOCUS_ATTRIBUTE": "$.subjects[].focus.attribute",
    "UNOBSERVED_SUBJECT_NAME": "$.subjects[].name",
    "UNOBSERVED_SEARCH_TERM": "$.searchTerms[]",
}
_SCHEMA_TYPES = frozenset(
    {
        "missing",
        "extra_forbidden",
        "literal_error",
        "enum",
        "string_type",
        "list_type",
        "dict_type",
        "model_type",
        "int_type",
        "int_parsing",
        "bool_type",
        "bool_parsing",
        "float_type",
        "float_parsing",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "too_long",
        "too_short",
        "string_too_long",
        "string_too_short",
        "value_error",
        "json_invalid",
    }
)


def _schema_fields(schema):
    fields = set()
    if isinstance(schema, dict):
        fields.update(schema.get("properties", {}))
        for value in schema.values():
            fields.update(_schema_fields(value))
    elif isinstance(schema, list):
        for value in schema:
            fields.update(_schema_fields(value))
    return fields


def _safe_path(location, fields):
    path = "$"
    for part in location[:8]:
        if isinstance(part, int):
            path += "[]"
        elif isinstance(part, str) and part in fields:
            path += "." + part
        else:
            break
    return path


def retry_feedback(exc: ValueError, schema: dict) -> list[dict[str, str]]:
    """Never copy rejected values, dynamic keys, exception messages, or IDs."""
    if isinstance(exc, ChatResponseContractError):
        entry = {"code": exc.code, "rule": RULES[exc.code]}
        if exc.code in _RULE_PATHS:
            entry["path"] = _RULE_PATHS[exc.code]
        return [entry]
    if not isinstance(exc, ValidationError):
        # Comparing an exact known message is safe; unknown text is not interpolated.
        code = _MESSAGE_CODES.get(str(exc), "RESPONSE_CONTRACT")
        entry = {"code": code, "rule": RULES[code]}
        if code in _RULE_PATHS:
            entry["path"] = _RULE_PATHS[code]
        return [entry]
    fields = _schema_fields(schema)
    feedback = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        kind = error["type"] if error["type"] in _SCHEMA_TYPES else "SCHEMA_VALIDATION"
        entry = {"code": kind, "path": _safe_path(error.get("loc", ()), fields)}
        # Local model validators have fixed messages; never emit unknown message text.
        message = error.get("msg", "")
        if message.startswith("Value error, "):
            code = _MESSAGE_CODES.get(message[len("Value error, ") :])
            if code:
                entry.update(code=code, rule=RULES[code])
                # Model-level cross-field errors have an empty Pydantic location.
                # Expose only their fixed schema field, never rejected values.
                field = _RULE_FIELDS.get(code)
                if entry["path"] == "$" and field in fields:
                    entry["path"] = f"$.{field}"
        if entry not in feedback:
            feedback.append(entry)
        if len(feedback) == 6:
            break
    return feedback
