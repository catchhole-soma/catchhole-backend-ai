from dataclasses import dataclass
from typing import Any, Protocol

from app.llm.responses import LlmTextResponse


@dataclass(frozen=True)
class LlmResponseSchema:
    """Provider 호출 하나에만 적용할 structured output 계약."""

    name: str
    schema: dict[str, Any]
    strict: bool = True


class TextGenerationClient(Protocol):
    """설정 추출기와 계량 wrapper가 공유하는 텍스트 생성 최소 계약."""

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
        response_schema: LlmResponseSchema | None = None,
    ) -> LlmTextResponse: ...
