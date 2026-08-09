from typing import Protocol

from app.llm.responses import LlmTextResponse


class TextGenerationClient(Protocol):
    """설정 추출기와 계량 wrapper가 공유하는 텍스트 생성 최소 계약."""

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse: ...
