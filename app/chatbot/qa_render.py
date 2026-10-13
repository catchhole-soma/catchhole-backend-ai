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


NOTICES = {
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
    "UNSUPPORTED_REQUEST": "등록 설정과 직접 근거의 읽기 전용 단일 질문만 지원합니다.",
}


def safe_text(value: str) -> str:
    # Stored strings may contain Markdown instructions/links; present them as literal data.
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return re.sub(r"([\\`*_{}\[\]<>()#!|])", r"\\\1", value)


def notices(codes: list[str]) -> list[str]:
    return list(dict.fromkeys(NOTICES[c] for c in codes if c in NOTICES))[:20]


def message(outcome: str, text: str, codes: list[str]) -> Finish:
    return Finish(
        result=ChatResult(outcome=outcome, text=text, sources=[], limitations=notices(codes))
    )


def render_answer(
    rows: list[AnswerRow], codes: list[str], *, direct_only: bool, maximum: int
) -> Finish:
    parts = ["조회한 등록 정보입니다."]
    refs: list[SourceReference] = []
    rendered = 0
    for row in rows:
        item = row.item
        name = item.display_name or item.setting_key or "설정"
        path = f"{item.scope_name or '루트'} / {name}" if row.domain == "WORLD" else name
        mode = "현재 등록값" if item.kind == "CURRENT" else "확정 이력"
        value = item.value if item.value is not None else item.structured_value_json
        block = f"\n- {safe_text(item.subject_name)} · {safe_text(path)} ({mode}): "
        block += safe_text(value) if value is not None else "표시값 확인 불가"
        if item.kind == "HISTORY":
            block += (
                f" [출처 {item.source_episode_no}화]"
                if item.source_episode_no
                else " [출처 회차 미상]"
            )
            if item.operation:
                block += f" [처리: {safe_text(item.operation)}]"
            if item.history_only:
                block += " [과거 기록 전용]"
            current = {
                True: "현재 근거에 기여",
                False: "현재 근거에 기여하지 않음",
                None: "현재 기여 여부 미상",
            }
            block += f" [{current[item.contributes_to_current]}]"
        if item.origin == "USER_INPUT":
            block += " [사용자 직접 입력]"
            codes.append("USER_INPUT")
        elif item.origin == "UNKNOWN":
            block += " [출처 미상]"
            codes.append("UNKNOWN_ORIGIN")
        if item.value_truncated:
            block += " [값 일부만 표시]"
        if len("".join(parts)) + len(block) > maximum:
            codes.append("ANSWER_LIMIT")
            break
        parts.append(block)
        rendered += 1
        for ref, evidence_item in row.evidence:
            evidence = evidence_item.evidence
            for quote in evidence.quotes:
                number = refs.index(ref) + 1 if ref in refs else len(refs) + 1
                episode = (
                    f"출처 {evidence.source_episode_no}화"
                    if evidence.source_episode_no
                    else "출처 회차 미상"
                )
                # Copy the verified quotation, never accept a model-authored quotation.
                quoted = f"\n  [{number}] {episode}: {safe_text(json.dumps(quote.text, ensure_ascii=False))}"
                if len("".join(parts)) + len(quoted) > maximum:
                    codes.append("ANSWER_LIMIT")
                    break
                parts.append(quoted)
                if ref not in refs:
                    refs.append(ref)
    if not rendered:
        return message(
            "INSUFFICIENT_EVIDENCE", "답변 한도 안에서 등록 정보를 표시할 수 없습니다.", codes
        )
    outcome = "INSUFFICIENT_EVIDENCE" if direct_only and not refs else "ANSWER"
    return Finish(
        result=ChatResult(
            outcome=outcome,
            text="".join(parts),
            sources=refs,
            limitations=notices(codes),
        )
    )
