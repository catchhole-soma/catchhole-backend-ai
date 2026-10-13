"""Model-visible QA decisions. Database IDs, SQL, URLs and credentials are absent."""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.setting_tools import ContractModel


class QuestionSubject(ContractModel):
    domain: Literal["CHARACTER", "WORLD"]
    name: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def nonblank(self) -> Self:
        if not self.name.strip():
            raise ValueError("A subject name is required")
        return self


class QuestionPlan(ContractModel):
    intent: Literal[
        "CURRENT",
        "HISTORY",
        "CURRENT_AND_HISTORY",
        "DIRECT_EVIDENCE",
        "NEEDS_SEARCH",
        "PAST_STATE",
        "CLARIFICATION",
        "UNSUPPORTED",
    ]
    subjects: list[QuestionSubject] = Field(max_length=3)
    include_evidence: bool
    episode_from: int | None = Field(ge=1, strict=True)
    episode_to: int | None = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def valid_plan(self) -> Self:
        lookup = self.intent in {"CURRENT", "HISTORY", "CURRENT_AND_HISTORY", "DIRECT_EVIDENCE"}
        if lookup and not self.subjects:
            raise ValueError("Lookup requires a named subject")
        identities = {(s.domain, s.name.strip().casefold()) for s in self.subjects}
        if len(identities) != len(self.subjects):
            raise ValueError("Duplicate subjects")
        if self.episode_from is not None and self.episode_to is not None:
            if self.episode_from > self.episode_to:
                raise ValueError("Invalid source range")
        return self


class AnswerSelection(ContractModel):
    outcome: Literal["ANSWER", "CLARIFICATION", "INSUFFICIENT_EVIDENCE"]
    row_refs: list[str] = Field(max_length=50)

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if (self.outcome == "ANSWER") != bool(self.row_refs):
            raise ValueError("Only answers select rows")
        if len(set(self.row_refs)) != len(self.row_refs):
            raise ValueError("Duplicate row references")
        return self
