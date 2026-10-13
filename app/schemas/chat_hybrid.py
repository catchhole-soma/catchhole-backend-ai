"""Independent information needs and request-local evidence references for GH240."""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.chat_qa import QuestionSubject
from app.schemas.setting_tools import ContractModel


class HybridPlan(ContractModel):
    disposition: Literal["PROCEED", "PAST_STATE", "CLARIFICATION", "UNSUPPORTED"]
    rdb_mode: Literal["NONE", "CURRENT", "HISTORY", "CURRENT_AND_HISTORY", "DIRECT_EVIDENCE"]
    source_need: Literal["NONE", "SCENE", "REASON", "PROCESS"]
    subjects: list[QuestionSubject] = Field(max_length=3)
    search_terms: list[str] = Field(max_length=6)
    include_evidence: bool
    episode_from: int | None = Field(ge=1, le=2147483647, strict=True)
    episode_to: int | None = Field(ge=1, le=2147483647, strict=True)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.episode_from and self.episode_to and self.episode_from > self.episode_to:
            raise ValueError("Invalid episode range")
        if any(not term.strip() or len(term) > 80 for term in self.search_terms):
            raise ValueError("Invalid search terms")
        if len({(s.domain, s.name.strip().casefold()) for s in self.subjects}) != len(
            self.subjects
        ):
            raise ValueError("Duplicate subjects")
        if self.disposition == "PROCEED":
            if self.rdb_mode != "NONE" and not self.subjects:
                raise ValueError("RDB lookup requires an explicit subject")
            if self.rdb_mode == "NONE" and self.source_need == "NONE":
                raise ValueError("No information need")
        return self


class GroundedClaim(ContractModel):
    text: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=6)


class EvidenceConflict(ContractModel):
    row_refs: list[str] = Field(min_length=1, max_length=3)
    evidence_refs: list[str] = Field(min_length=1, max_length=3)


class GroundedAnswer(ContractModel):
    coverage: Literal["COMPLETE", "PARTIAL", "NONE"]
    claims: list[GroundedClaim] = Field(max_length=8)
    conflicts: list[EvidenceConflict] = Field(max_length=4)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.coverage == "NONE") != (not self.claims and not self.conflicts):
            raise ValueError("Coverage and grounded statements disagree")
        for claim in self.claims:
            if not claim.text.strip() or len(set(claim.evidence_refs)) != len(claim.evidence_refs):
                raise ValueError("Invalid claim")
        return self
