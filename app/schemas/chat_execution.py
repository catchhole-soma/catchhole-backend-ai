"""Chat execution v1; identity is obtained from Spring, never model arguments."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID
from pydantic import Field, model_validator
from app.schemas.setting_tools import ContractModel, SourceReference


class Dispatch(ContractModel):
    schema_version: Literal[1]
    attempt_id: UUID
    capability: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$", repr=False)
    deadline: datetime


class ExecutionContext(ContractModel):
    schema_version: Literal[1]
    question_id: UUID | None
    attempt_id: UUID
    work_id: UUID
    member_id: int = Field(ge=1)
    question: str | None = Field(max_length=4000, repr=False)
    episode_from: int | None = Field(ge=1)
    episode_to: int | None = Field(ge=1)
    deadline: datetime
    token_budget: int = Field(ge=1)
    max_calls: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    operational: bool


class ChatResult(ContractModel):
    outcome: Literal["ANSWER", "CLARIFICATION", "INSUFFICIENT_EVIDENCE"]
    text: str = Field(min_length=1, max_length=16000, repr=False)
    sources: list[SourceReference] = Field(max_length=50)
    limitations: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def limits(self) -> Self:
        if not self.text.strip() or any(
            not item.strip() or len(item) > 200 for item in self.limitations
        ):
            raise ValueError("Invalid limitations")
        return self


class Finish(ContractModel):
    result: ChatResult | None = None
    failure: (
        Literal[
            "PROVIDER_FAILURE",
            "NETWORK_FAILURE",
            "QUOTA_EXHAUSTED",
            "HANDLER_UNAVAILABLE",
            "INTERRUPTED",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def one_outcome(self) -> Self:
        if (self.result is None) == (self.failure is None):
            raise ValueError("Exactly one outcome is required")
        return self


class Usage(ContractModel):
    kind: Literal["SETTLE", "RELEASE", "UNKNOWN"]
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    cached_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    outcome: Literal["SUCCESS", "FAILURE"] | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        values = (self.input_tokens, self.cached_input_tokens, self.output_tokens, self.outcome)
        if self.kind == "SETTLE":
            if (
                any(value is None for value in values)
                or self.cached_input_tokens > self.input_tokens
            ):
                raise ValueError("Measured usage is required")
        elif any(value is not None for value in values):
            raise ValueError("Unknown usage cannot contain measured values")
        return self


class CallView(ContractModel):
    call_id: UUID
    status: Literal["RESERVED", "SETTLED", "RELEASED", "UNKNOWN"]
    reserved_tokens: int = Field(ge=1)
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
