"""The persisted BOOK marker follows only extraction-owned sourceCandidates edges."""

from typing import Literal

BookEpistemicStatus = Literal["ASSERTED", "UNRESOLVED_CLAIM"]
BOOK_CLAIM_REVIEW_ERROR = "Book content uncertainty requires review before changing current state."


def is_asserted_book_claim(raw: object) -> bool:
    pending = [raw]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or node.get("epistemic_status") != "ASSERTED":
            return False
        if "sourceCandidates" in node:
            children = node["sourceCandidates"]
            if not isinstance(children, list) or not children:
                return False
            pending.extend(children)
    return True


def book_claim_prompt_data(raw: object) -> dict:
    # A missing/unknown legacy marker is deliberately not reconstructed as truth.
    return {"epistemic_status": "ASSERTED" if is_asserted_book_claim(raw) else "UNRESOLVED_CLAIM"}


BOOK_WORLD_CLAIM_INSTRUCTIONS = """
설정집 후보에는 epistemic_status를 반드시 반환하세요. ASSERTED는 본문이 확정 설정으로
서술한 주장입니다. 확정된 과거 사실도 ASSERTED이며 temporal_scope는 별도로 PAST입니다.
UNRESOLVED_CLAIM은 미확인 전승·소문·서로 충돌하는 기록·미채택 구상·지시 대상이 불명확한
주장입니다. 내용의 확실성과 이야기 시점을 별도로 판단하고 UNKNOWN을 불확실성 만능값으로
사용하지 마세요. 같은 대상의 명확한 다른 항목까지 불확실하게 만들지 마세요.
주체가 사람인지 사물인지 모르는 소문도 원문의 이름/주제와 불확실성을 보존하는
WORLD_RULE_HISTORY 후보로 남기세요. 그 주제가 실제 사람/배/별칭이라는 뜻은 아니며
새 인물이나 기존 인물의 확정 별칭을 만들지 않습니다. 원문이 미확인이라고 한 주장은
UNRESOLVED_CLAIM이며 비교에서는 GENERAL_UNCERTAINTY 검토만 허용됩니다.
제목 아래의 짧은 항목을 빠뜨리지 마세요. 도구의 지정 보관 장소/관리 장소는 지속 설정이며,
한 장면에서 누가 들고 있다는 일시적 위치와 구별하세요. 현재 기술의 확정된 유래/계승 관계는
현재 속성(PRESENT)일 수 있습니다. 고대라는 단어만으로 현재 대상의 기원 속성을 PAST로
바꾸지 마세요. 과거 제목의 당시 제도는 그 역사적 주체의 PAST 주장으로 구분하세요.
source_layout은 원문 제목과 행의 구조입니다. 제목과 아래 필드를 결합해 주체를 읽되,
명시적으로 다른 주체를 설명하는 교차 참조는 그 실제 주체에 연결하세요. 모든 항목을
검토하고 지속 설정·과거·미확인 주장 근거를 보존하되 메모마다 새 사실을 강제로 만들지는 마세요.
""".strip()

BOOK_WORLD_REVIEW_INSTRUCTIONS = """
BOOK 후보의 epistemic_status가 UNRESOLVED_CLAIM이면 내용 확인이 끝나지 않은 주장입니다.
누락/알 수 없는 legacy 표식도 확정이라고 추측하지 않습니다. 현재 적용이나 중복 EXCLUDE로
버리지 말고 REVIEW_REQUIRED + GENERAL_UNCERTAINTY로 원본 후보 하나씩 보존하세요.
원본 경로와 값, 이미 연결된 target_ref를 유지하며 기존 속성을 이동하지 마세요.
이 주장을 현재값/별칭/동일 주체의 근거로 승격하지 않습니다. ASSERTED인 독립 항목은
이 규칙으로 막지 말고 이야기 시점과 실제 원문에 맞춰 정상적으로 비교하세요.
""".strip()
