"""Independent chat configuration; constructing this object never loads .env."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChatQaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_QA_", extra="ignore", frozen=True)

    enabled: bool = False
    model: str = Field(default="gpt-6-luna", min_length=1, max_length=100)
    reasoning_effort: str = "medium"
    max_attempts: int = Field(default=2, ge=1, le=3)
    max_output_tokens: int = Field(default=2000, ge=128, le=8000)
    answer_max_output_tokens: int = Field(default=4500, ge=128, le=8000)
    truncation_retry_max_output_tokens: int = Field(default=6000, ge=128, le=16000)
    # 상한(le)은 원문 탐색(CHAT_RETRIEVAL_AGENT)이 읽은 원문 묶음을 한 번에 넣을 수 있게 넓혔다.
    # 기본값은 그대로이며, 탐색을 켤 때 함께 올릴 값은 docs/chat-hybrid-retrieval-v1.md에 있다.
    max_input_tokens: int = Field(default=32000, ge=1000, le=200000)
    max_context_characters: int = Field(default=32000, ge=2000, le=400000)
    page_size: int = Field(default=20, ge=1, le=50)
    # 항목 목록(FIELDS)은 값이 없는 메타데이터라 한 번에 많이 읽어 도구 예산을 아낀다.
    fields_page_size: int = Field(default=50, ge=1, le=50)
    max_pages: int = Field(default=2, ge=1, le=10)
    max_items: int = Field(default=80, ge=1, le=200)
    max_tool_calls: int = Field(default=16, ge=1, le=100)
    max_selected_rows: int = Field(default=10, ge=1, le=20)
    max_evidence_calls: int = Field(default=8, ge=0, le=50)
    max_answer_characters: int = Field(default=12000, ge=1000, le=16000)
    finalization_reserve_seconds: float = Field(default=10, ge=1, le=30)
    # provider HTTP 응답 대기 시간(초). 큰 입력의 답변 호출이 기본 90초를 넘길 때만 올린다.
    provider_timeout_seconds: float = Field(default=90, ge=10, le=300)

    @model_validator(mode="after")
    def output_budgets(self):
        if self.truncation_retry_max_output_tokens < max(
            self.max_output_tokens, self.answer_max_output_tokens
        ):
            raise ValueError("Truncation retry cap must cover both stage output caps")
        return self

    def output_tokens_for(self, purpose: str, *, truncated_retry: bool = False) -> int:
        base = self.answer_max_output_tokens if purpose == "CHAT_ANSWER" else self.max_output_tokens
        return min(base * 2, self.truncation_retry_max_output_tokens) if truncated_retry else base
