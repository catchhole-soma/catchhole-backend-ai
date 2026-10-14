"""Independent information needs and request-local evidence references for GH240."""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.chat_qa import AnswerShape, CharacterFilter, QuestionSubject, WorkCollection
from app.schemas.setting_tools import ContractModel


RequestedFacet = Literal["TIME", "HOW", "WHY", "SCENE"]
EventScope = Literal["ACTUAL", "PLANNED"]
EventStatus = Literal["OCCURRED", "PLANNED", "UNCERTAIN"]


class HybridPlan(ContractModel):
    disposition: Literal["PROCEED", "PAST_STATE", "CLARIFICATION", "UNSUPPORTED"]
    rdb_mode: Literal[
        "NONE", "CURRENT", "HISTORY", "CURRENT_AND_HISTORY", "DIRECT_EVIDENCE", "CHARACTER_LIST"
    ]
    source_need: Literal["NONE", "SCENE", "REASON", "PROCESS"]
    subjects: list[QuestionSubject] = Field(max_length=3)
    search_terms: list[str] = Field(max_length=6)
    include_evidence: bool
    episode_from: int | None = Field(ge=1, le=2147483647, strict=True)
    episode_to: int | None = Field(ge=1, le=2147483647, strict=True)
    character_filter: CharacterFilter | None = None
    per_item: bool = False
    requested_facets: list[RequestedFacet] = Field(default_factory=list, max_length=4)
    collection: WorkCollection | None = None
    answer_shape: AnswerShape = "FACTS"
    include_registered_values: bool = False
    event_scope: EventScope | None = None

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
            if (
                self.rdb_mode not in {"NONE", "CHARACTER_LIST"}
                and not self.subjects
                and self.collection is None
            ):
                raise ValueError("RDB lookup requires an explicit subject")
            if self.rdb_mode == "NONE" and self.source_need == "NONE":
                raise ValueError("No information need")
        if (self.rdb_mode == "CHARACTER_LIST") != (self.character_filter is not None):
            raise ValueError("Character lists require one exact current-value condition")
        if self.rdb_mode == "CHARACTER_LIST" and (
            self.subjects
            or self.source_need != "NONE"
            or self.episode_from is not None
            or self.episode_to is not None
        ):
            raise ValueError("Character lists support current values without source reconstruction")
        if self.collection is not None and (
            self.subjects or self.character_filter is not None or self.rdb_mode != "CURRENT"
        ):
            raise ValueError("Work collections query current registered information")
        if len(set(self.requested_facets)) != len(self.requested_facets):
            raise ValueError("Duplicate requested facets")
        if self.source_need == "NONE" and self.requested_facets:
            raise ValueError("Narrative facets require source evidence")
        if self.event_scope is not None and (
            self.source_need == "NONE" or not set(self.requested_facets) & {"TIME", "HOW"}
        ):
            raise ValueError("Event scope describes a requested time or process")
        if (
            self.source_need != "NONE"
            and "requested_facets" in self.model_fields_set
            and not self.requested_facets
        ):
            raise ValueError("Narrative questions must identify requested information")
        return self


class FacetAnswer(ContractModel):
    facet: RequestedFacet
    text: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=6)
    event_status: EventStatus | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.text.strip() or len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("Invalid facet answer")
        return self


class ItemAnswer(ContractModel):
    item_ref: str = Field(min_length=1, max_length=10)
    answers: list[FacetAnswer] = Field(max_length=4)
    missing_facets: list[RequestedFacet] = Field(max_length=4)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        facets = [answer.facet for answer in self.answers]
        if (
            len(set(facets)) != len(facets)
            or len(set(self.missing_facets)) != len(self.missing_facets)
            or any(
                answer.facet in self.missing_facets and answer.event_status is None
                for answer in self.answers
            )
        ):
            raise ValueError("Only classified event observations may leave their facet unanswered")
        return self


class GroundedClaim(ContractModel):
    text: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=6)
    row_refs: list[str] = Field(default_factory=list, max_length=50)
    answered_facets: list[RequestedFacet] = Field(default_factory=list, max_length=4)
    event_status: EventStatus | None = None


class EvidenceConflict(ContractModel):
    row_refs: list[str] = Field(min_length=1, max_length=3)
    evidence_refs: list[str] = Field(min_length=1, max_length=3)


class GroundedAnswer(ContractModel):
    coverage: Literal["COMPLETE", "PARTIAL", "NONE"]
    claims: list[GroundedClaim] = Field(max_length=8)
    conflicts: list[EvidenceConflict] = Field(max_length=4)
    unanswered_row_refs: list[str] = Field(default_factory=list, max_length=100)
    item_answers: list[ItemAnswer] = Field(default_factory=list, max_length=100)
    missing_facets: list[RequestedFacet] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        supported = bool(self.claims or self.conflicts or any(i.answers for i in self.item_answers))
        if (self.coverage == "NONE") == supported:
            raise ValueError("Coverage and grounded statements disagree")
        for claim in self.claims:
            if (
                not claim.text.strip()
                or len(set(claim.evidence_refs)) != len(claim.evidence_refs)
                or len(set(claim.row_refs)) != len(claim.row_refs)
                or len(set(claim.answered_facets)) != len(claim.answered_facets)
            ):
                raise ValueError("Invalid claim")
        if len(set(self.unanswered_row_refs)) != len(self.unanswered_row_refs):
            raise ValueError("Duplicate unanswered row references")
        if self.coverage == "COMPLETE" and self.unanswered_row_refs:
            raise ValueError("Unanswered rows cannot have complete coverage")
        if len({item.item_ref for item in self.item_answers}) != len(self.item_answers):
            raise ValueError("Duplicate logical item references")
        if self.coverage == "COMPLETE" and any(i.missing_facets for i in self.item_answers):
            raise ValueError("Unanswered facets cannot have complete coverage")
        if len(set(self.missing_facets)) != len(self.missing_facets):
            raise ValueError("Duplicate unanswered facets")
        if self.coverage == "COMPLETE" and self.missing_facets:
            raise ValueError("Unanswered facets cannot have complete coverage")
        return self
