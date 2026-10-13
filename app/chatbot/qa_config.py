"""Independent chat configuration; constructing this object never loads .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChatQaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_QA_", extra="ignore", frozen=True)

    enabled: bool = False
    model: str = Field(default="gpt-5.6-terra", min_length=1, max_length=100)
    reasoning_effort: str = "medium"
    max_attempts: int = Field(default=2, ge=1, le=3)
    max_output_tokens: int = Field(default=1500, ge=128, le=8000)
    max_input_tokens: int = Field(default=8000, ge=1000, le=32000)
    max_context_characters: int = Field(default=24000, ge=2000, le=64000)
    page_size: int = Field(default=20, ge=1, le=50)
    max_pages: int = Field(default=2, ge=1, le=10)
    max_items: int = Field(default=80, ge=1, le=200)
    max_tool_calls: int = Field(default=16, ge=1, le=100)
    max_selected_rows: int = Field(default=10, ge=1, le=20)
    max_evidence_calls: int = Field(default=8, ge=0, le=50)
    max_answer_characters: int = Field(default=12000, ge=1000, le=16000)
