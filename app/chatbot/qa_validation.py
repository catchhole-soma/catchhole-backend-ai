"""Bounded retry feedback made only from schema names and fixed contract rules."""

from pydantic import ValidationError


RULES = {
    "REQUESTED_PARTS_INCOMPLETE": "요청된 모든 답변 부분을 제공된 자료로 답하거나 미확인으로 표시하고 coverage를 일치시키세요.",
    "EVENT_CONTEXT_REQUIRED": "원문에서 관측한 사건이 질문의 실제 사건·계획·가정 문맥과 일치하는지 명시적으로 판정하세요.",
    "UNKNOWN_REFERENCE": "참조는 이번 입력에 제공된 목록에서만 선택하고 중복 없이 사용하세요.",
    "UNOBSERVED_LOOKUP_TERM": "조회 이름과 항목 표현은 이번 질문 또는 제공된 대화에 실제로 있는 표현만 사용하세요.",
    "INVALID_PLAN": "조회 범위·대상·정보 필요·질문 유형에 따른 필드 조건을 schema와 원래 지침에 맞추세요.",
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
        "Coverage must describe requested information",
        "Coverage and grounded statements disagree",
        "Unanswered rows cannot have complete coverage",
        "Unanswered facets cannot have complete coverage",
        "Duplicate requested facets",
        "Duplicate unanswered facets",
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
    ),
}
_MESSAGE_CODES = {message: code for code, messages in _MESSAGES.items() for message in messages}
_MESSAGE_CODES.update({code: code for code in RULES})
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
        return [{"code": exc.code, "rule": RULES[exc.code]}]
    if not isinstance(exc, ValidationError):
        # Comparing an exact known message is safe; unknown text is not interpolated.
        code = _MESSAGE_CODES.get(str(exc), "RESPONSE_CONTRACT")
        return [{"code": code, "rule": RULES[code]}]
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
        if entry not in feedback:
            feedback.append(entry)
        if len(feedback) == 6:
            break
    return feedback
