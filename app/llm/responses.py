from dataclasses import dataclass, field
from typing import Any


# OpenAI 응답에서 필요한 값만 꺼내 내부에서 전달하는 값 객체
@dataclass(frozen=True)
class LlmTextResponse:
    # LLM이 생성한 최종 텍스트, 현재는 JSON 문자열을 기대한다.
    text: str
    # 사용량 집계를 위해 OpenAI usage에서 가져오는 입력/출력 token 수
    input_token_count: int | None = None
    output_token_count: int | None = None
    # 디버깅이나 후속 분석을 위해 원본 응답을 보관한다.
    raw_response: dict[str, Any] = field(default_factory=dict)
