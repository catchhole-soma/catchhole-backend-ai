"""Execution-scoped retrieval receipts; model output never supplies these identifiers."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.setting_tools import ContractModel


class RetrievalReference(ContractModel):
    chunk_id: UUID
    episode_id: UUID
    source_episode_no: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_version: str | None = Field(max_length=100)
    quote: str = Field(min_length=1, max_length=4000, repr=False)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)

    @model_validator(mode="after")
    def exact_slice(self) -> Self:
        if not self.quote.strip() or self.end_offset - self.start_offset != len(self.quote):
            raise ValueError("Invalid Unicode source slice")
        return self


class RetrievalReceipt(ContractModel):
    schema_version: Literal[1]
    sources: list[RetrievalReference] = Field(max_length=12)
