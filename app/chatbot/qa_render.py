"""Extractive output: only server values and verified quotes become answer text."""

import json
import re
from dataclasses import dataclass, field

from app.schemas.chat_execution import ChatResult, Finish
from app.schemas.setting_tools import SettingToolItem, SourceReference


@dataclass
class AnswerRow:
    ref: str
    item: SettingToolItem
    domain: str = "CHARACTER"
    evidence: list[tuple[SourceReference, SettingToolItem]] = field(default_factory=list)
    evidence_requested: bool = False


NOTICES = {
    "CURRENT_CHANGED": "조회 중 설정이 바뀌어 이전 값으로 답변하지 않았습니다.",
    "CONDITION_UNCERTAIN": "일부 대상은 현재 설정만으로 조건 충족 여부를 확정하지 못했습니다.",
    "SCALAR_UNAVAILABLE": "일부 설정은 단일 값으로 확인할 수 없어 종류 집계에서 제외했습니다.",
    "CONVERSATION_LIMIT": "최근 대화 일부만 참고했습니다. 대상이 불분명하면 이름과 항목을 다시 알려주세요.",
    "NARRATIVE_SOURCE_RANGE": "회차는 근거가 서술된 출처이며 사건이 일어난 시점과 다를 수 있습니다.",
    "INDEX_PARTIAL": "검색 데이터가 일부만 준비되어 작품 전체를 검색한 결과가 아닙니다.",
    "NO_SUPPORTED_SOURCES": "허용 범위에 검색할 현재 회차 원문이 없습니다.",
    "TEXT_INDEX_PARTIAL": "일부 회차의 검색용 원문 청크가 없거나 현재 원문과 다릅니다.",
    "VECTOR_INDEX_PARTIAL": "일부 회차의 임베딩이 없거나 모델·버전이 달라 의미 검색에서 제외됐습니다.",
    "SEARCH_LIMIT": "검색 후보·검색 라운드 상한이 적용되어 모든 관련 원문을 확인한 것은 아닙니다.",
    "SEARCH_QUERY_LIMIT": "검색어 길이 한도로 질문의 일부만 검색에 사용했습니다.",
    "EVIDENCE_CONTEXT_LIMIT": "답변에 참고할 원문 분량을 제한해 일부 검색 후보를 검토하지 못했습니다.",
    "PARTIAL_ANSWER": "확인한 근거로 질문의 일부만 답변했습니다.",
    "SETTING_SOURCE_CONFLICT": "등록 설정과 원문을 함께 표시했습니다. 어느 쪽이 옳은지 자동 확정하거나 수정하지 않았습니다.",
    "SEARCH_UNAVAILABLE": "검색 범위나 저장소 확인에 실패했습니다. 근거가 없다는 뜻은 아닙니다.",
    "NO_SEARCH_EVIDENCE": "이번 조회에서 충분한 원문 근거를 찾지 못했습니다.",
    "SETTING_BOOK_UNSUPPORTED": "업로드한 설정집은 아직 조회하지 않습니다.",
    "PARTIAL_RESULTS": "조회 한도에 도달해 일부 등록 정보만 확인했습니다.",
    "CONTEXT_LIMIT": "입력 문맥 한도로 일부 항목을 검토하지 못했습니다.",
    "SELECTION_LIMIT": "답변 항목 상한에 도달해 일부 항목만 표시합니다.",
    "ANSWER_LIMIT": "답변 길이 한도로 일부 항목 또는 인용을 표시하지 못했습니다.",
    "EVIDENCE_LIMIT": "직접 근거 조회 한도로 일부 연결 원문을 확인하지 못했습니다.",
    "VALUE_LIMIT": "서버가 반환한 설정값 일부가 잘려 있습니다.",
    "SOURCE_REFERENCE_LIMIT": "연결된 근거 참조 중 일부만 반환됐습니다.",
    "WORLD_SOURCE_SCAN_LIMIT": "세계관 근거 탐색 상한으로 일부 연결을 확인하지 못했습니다.",
    "HISTORY_IS_NOT_A_STATE_SNAPSHOT": "이력은 출처 회차 기준 기록이며 해당 회차의 전체 상태가 아닙니다.",
    "SOURCE_RANGE": "회차 범위는 이력·근거의 출처 필터입니다. 현재값은 현재 등록 설정입니다.",
    "OUT_OF_SCOPE": "질문의 출처 회차가 이번 요청에서 허용된 범위를 벗어납니다.",
    "SEMANTIC_SEARCH_REQUIRED": "장면·이유를 확인할 원문 의미 검색은 아직 지원하지 않습니다.",
    "PAST_STATE_UNSUPPORTED": "특정 회차 당시의 전체 상태나 사건 시점 복원은 지원하지 않습니다.",
    "USER_INPUT": "사용자가 직접 입력한 설정은 원문 인용과 구분해 표시합니다.",
    "UNKNOWN_ORIGIN": "출처가 불명확한 설정을 사용자 입력이라고 추정하지 않습니다.",
    "NO_SOURCE": "현재 설정은 확인되지만 연결된 원문 근거가 없습니다.",
    "LEGACY_UNVERIFIED": "과거 근거의 원문 일치를 검증할 수 없습니다.",
    "SOURCE_MISSING": "연결된 원문을 찾을 수 없습니다.",
    "SOURCE_PURGED": "연결된 원문이 파기되어 인용할 수 없습니다.",
    "SOURCE_CHANGED": "원문이 변경되어 기존 인용을 사용할 수 없습니다.",
    "STORAGE_UNAVAILABLE": "원문 저장소 장애로 인용을 확인하지 못했습니다.",
    "OUT_OF_RANGE": "연결된 원문이 허용된 출처 회차 범위 밖에 있습니다.",
    "INVALID_QUOTE": "원문과 일치하지 않는 인용을 제외했습니다.",
    "STALE_CURRENT_SOURCE": "과거 근거가 현재 설정을 뒷받침하는지 확인되지 않아 인용에서 제외했습니다.",
    "NO_RELATED_SETTING": "조회한 등록 정보에서 질문에 해당하는 항목을 확인하지 못했습니다.",
    "UNSUPPORTED_REQUEST": "현재는 작품 정보를 조회하는 질문에 답할 수 있습니다.",
}


def safe_text(value: str) -> str:
    # The chat UI renders plain text, not Markdown or HTML. Keep data inline without
    # inserting visible backslashes into names, quotations, or bracketed skill labels.
    value = " ".join(value.splitlines())
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)


def notices(codes: list[str], *, include_conversation_limit: bool = False) -> list[str]:
    # Trimming old turns is a planner constraint, not a failure of current evidence.
    # Only a clarification needs to explain why the user may have to name a target again.
    return list(
        dict.fromkeys(
            NOTICES[c]
            for c in codes
            if c in NOTICES and (include_conversation_limit or c != "CONVERSATION_LIMIT")
        )
    )[:20]


def message(outcome: str, text: str, codes: list[str]) -> Finish:
    return Finish(
        result=ChatResult(
            outcome=outcome,
            text=text,
            sources=[],
            limitations=notices(codes, include_conversation_limit=outcome == "CLARIFICATION"),
        )
    )


def setting_label(item: SettingToolItem) -> str:
    name = item.display_name or "설정"
    return safe_text(f"{item.scope_name} · {name}" if item.scope_name else name)


def display_value(item: SettingToolItem) -> str:
    if item.value is not None:
        return safe_text(item.value)
    if item.structured_value_json is None:
        return "표시할 값 없음"
    try:
        value = json.loads(item.structured_value_json)
    except (ValueError, TypeError):
        return "표시할 값 확인 필요"
    labels = {
        "value": "값",
        "name": "이름",
        "level": "레벨",
        "quantity": "수량",
        "description": "설명",
        "effect": "효과",
        "active": "활성 여부",
    }

    def human(value):
        if isinstance(value, dict):
            return ", ".join(f"{labels.get(k, k)}: {human(v)}" for k, v in value.items())
        if isinstance(value, list):
            return ", ".join(human(v) for v in value)
        if value is True:
            return "예"
        if value is False:
            return "아니요"
        return "없음" if value is None else str(value)

    return safe_text(human(value))


def render_character_list(items, attribute, value, codes, *, maximum):
    condition = f"‘{safe_text(attribute)}: {safe_text(value)}’ 조건에 맞는"
    if not items:
        text = f"현재 등록 설정에서 {condition} 인물을 찾지 못했습니다."
        return message("INSUFFICIENT_EVIDENCE", text, codes)
    parts = [f"현재 등록 설정에서 {condition} 인물입니다."]
    for item in items:
        block = f"\n- {safe_text(item.subject_name)}"
        if len("".join(parts)) + len(block) > maximum:
            codes.append("ANSWER_LIMIT")
            break
        parts.append(block)
    return message("ANSWER", "".join(parts), codes)


def render_answer(
    rows: list[AnswerRow], codes: list[str], *, direct_only: bool, maximum: int
) -> Finish:
    only_history = all(row.item.kind == "HISTORY" for row in rows)
    parts = [
        "등록된 변경 기록을 출처 회차순으로 정리했어요."
        if only_history
        else "현재 등록된 설정을 확인했어요."
    ]
    refs: list[SourceReference] = []
    rendered = 0
    mixed = any(row.item.kind == "HISTORY" for row in rows) and not only_history
    for row in rows:
        item = row.item
        name = setting_label(item)
        block = f"\n- {safe_text(item.subject_name)} · {name}: {display_value(item)}"
        if item.kind == "HISTORY":
            block += (
                f" (출처 {item.source_episode_no}화)"
                if item.source_episode_no
                else " (출처 회차 미상)"
            )
            if item.operation == "REMOVE":
                block += " · 삭제 기록"
            elif item.history_only:
                block += " · 현재 설정에 반영하지 않은 기록"
        elif mixed:
            block += " (현재 설정)"
        if item.origin == "USER_INPUT":
            block += " · 직접 입력"
        if item.value_truncated:
            block += " · 값 일부만 확인됨"
        row_refs = list(dict.fromkeys(ref for ref, _ in row.evidence))
        next_refs = list(dict.fromkeys([*refs, *row_refs]))
        if row_refs:
            numbers = ", ".join(str(next_refs.index(ref) + 1) for ref in row_refs)
            block += f" · 근거 {numbers}"
        elif row.evidence_requested:
            block += " · 원문 미확인"
        if len("".join(parts)) + len(block) > maximum:
            codes.append("ANSWER_LIMIT")
            break
        parts.append(block)
        refs = next_refs
        rendered += 1
    if not rendered:
        return message(
            "INSUFFICIENT_EVIDENCE", "답변 한도 안에서 등록 정보를 표시할 수 없습니다.", codes
        )
    if len(rows) == 1 and rows[0].item.kind == "CURRENT" and not rows[0].item.value_truncated:
        item = rows[0].item
        label = setting_label(item)
        # Only a scalar answer is made into a sentence; descriptive values retain
        # their stored wording instead of getting a second sentence ending appended.
        value = display_value(item)
        numeric = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value) is not None
        if value and len(value) <= 80 and (numeric or not any(c in value for c in ".!?\n")):
            last = ord(label[-1]) if label else 0
            particle = "은" if 0xAC00 <= last <= 0xD7A3 and (last - 0xAC00) % 28 else "는"
            sentence = f"{safe_text(item.subject_name)}의 현재 {label}{particle} {value}입니다."
            if item.origin == "USER_INPUT":
                sentence += " (직접 입력한 설정)"
            if refs:
                sentence += " 근거 " + ", ".join(str(n) for n in range(1, len(refs) + 1))
            elif rows[0].evidence_requested:
                sentence += " 연결된 원문은 확인하지 못했어요."
            if len(sentence) <= maximum:
                parts = [sentence]
    outcome = "INSUFFICIENT_EVIDENCE" if direct_only and not refs else "ANSWER"
    return Finish(
        result=ChatResult(
            outcome=outcome, text="".join(parts), sources=refs, limitations=notices(codes)
        )
    )
