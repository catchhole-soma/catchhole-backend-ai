"""User-facing comparison explanations, distinct from internal diagnostic codes."""
import re


USER_FACING_REASON_INSTRUCTIONS = (
    "comparison_reason은 독자가 바로 읽는 판단 이유입니다. 원문 내용과 기존 설정의 관계, "
    "확인할 사항을 자연스러운 한국어로 설명하세요. root/루트, slot/슬롯, scope/스코프, "
    "key, enum/열거형, ref, canonical, snapshot, UUID, 내부 식별자나 연산 이름으로 "
    "자료 구조를 설명하지 마세요. 예를 들어 'root를 scope로 변경' 대신 '기존 숙소 정보에 "
    "출입 규정을 함께 정리할지 확인이 필요합니다', '동일 slot에 ADD 불가' 대신 "
    "'이미 기록된 인물 특징과 새 습관을 함께 남길지 확인이 필요합니다'처럼 쓰세요. "
    "입력에 실제로 등장하는 인물·장소·기술·설정의 고유한 이름은 그대로 보존하세요. "
    "원본 후보의 내용이나 근거를 수정하지 마세요."
)

def mask_display_names(reason: str, names) -> str:
    """Mask only names owned by the input, never names invented in a response."""
    for name in sorted({name for name in names if isinstance(name, str) and name},
                       key=len, reverse=True):
        reason = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            "작품 속 이름", reason, flags=re.IGNORECASE,
        )
    return reason
