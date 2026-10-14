"""Model-visible QA decisions. Database IDs, SQL, URLs and credentials are absent."""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.setting_tools import ContractModel


CharacterFactType = Literal["PROFILE", "AGE", "LEVEL", "STAT", "SKILL", "ITEM", "STATUS", "TIME"]


class SettingFocus(ContractModel):
    """Question vocabulary, never a model-authored storage key or scope."""

    fact_type: CharacterFactType | None
    attribute: str | None = Field(min_length=1, max_length=100)


class CharacterFilter(ContractModel):
    fact_type: CharacterFactType
    attribute: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=200)


class QuestionSubject(ContractModel):
    domain: Literal["CHARACTER", "WORLD"]
    name: str = Field(min_length=1, max_length=100)
    # New structured-output schemas require a non-null focus. The default only
    # keeps pre-extension recorded plans readable; fields_set identifies those.
    focus: SettingFocus = Field(
        default_factory=lambda: SettingFocus(fact_type=None, attribute=None)
    )

    @model_validator(mode="after")
    def nonblank(self) -> Self:
        if not self.name.strip():
            raise ValueError("A subject name is required")
        if self.domain == "WORLD" and self.focus and self.focus.fact_type is not None:
            raise ValueError("World settings do not have character fact types")
        return self


class QuestionPlan(ContractModel):
    intent: Literal[
        "CURRENT",
        "HISTORY",
        "CURRENT_AND_HISTORY",
        "DIRECT_EVIDENCE",
        "CHARACTER_LIST",
        "NEEDS_SEARCH",
        "PAST_STATE",
        "CLARIFICATION",
        "UNSUPPORTED",
    ]
    subjects: list[QuestionSubject] = Field(max_length=3)
    include_evidence: bool
    episode_from: int | None = Field(ge=1, strict=True)
    episode_to: int | None = Field(ge=1, strict=True)
    character_filter: CharacterFilter | None = None

    @model_validator(mode="after")
    def valid_plan(self) -> Self:
        lookup = self.intent in {"CURRENT", "HISTORY", "CURRENT_AND_HISTORY", "DIRECT_EVIDENCE"}
        if lookup and not self.subjects:
            raise ValueError("Lookup requires a named subject")
        if (self.intent == "CHARACTER_LIST") != (self.character_filter is not None):
            raise ValueError("Character lists require one exact current-value condition")
        if self.intent == "CHARACTER_LIST" and (
            self.subjects or self.episode_from is not None or self.episode_to is not None
        ):
            raise ValueError("A character list queries current values across the work")
        identities = {(s.domain, s.name.strip().casefold()) for s in self.subjects}
        if len(identities) != len(self.subjects):
            raise ValueError("Duplicate subjects")
        if self.episode_from is not None and self.episode_to is not None:
            if self.episode_from > self.episode_to:
                raise ValueError("Invalid source range")
        return self


class FieldSelection(ContractModel):
    outcome: Literal["ANSWER", "CLARIFICATION", "INSUFFICIENT_EVIDENCE"]
    field_refs: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if (self.outcome == "ANSWER") != bool(self.field_refs):
            raise ValueError("Only matched fields have references")
        if len(set(self.field_refs)) != len(self.field_refs):
            raise ValueError("Duplicate field references")
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
