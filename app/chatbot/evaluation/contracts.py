"""Gold, observations and semantic reviews have separate, hash-bound contracts."""

from hashlib import sha256
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["RDB_DIRECT", "VECTOR_ONLY", "HYBRID"]
MODES = ("RDB_DIRECT", "VECTOR_ONLY", "HYBRID")
Outcome = Literal["ANSWER", "CLARIFICATION", "INSUFFICIENT_EVIDENCE"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")]
NonNegative = Annotated[int, Field(ge=0, strict=True)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def fingerprint(value: BaseModel | dict | list) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def unique(values, label: str):
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


class Evidence(Record):
    id: Identifier
    work_id: Identifier
    kind: Literal["CURRENT", "HISTORY", "DIRECT", "CHUNK"]
    version: Identifier
    episode: int | None = Field(default=None, ge=1, strict=True)
    text: str = Field(min_length=1)
    sha256: Digest
    usable: bool = True
    vector_ready: bool = False

    @model_validator(mode="after")
    def valid_hash(self) -> Self:
        if sha256(self.text.encode()).hexdigest() != self.sha256:
            raise ValueError("Evidence text hash mismatch")
        if self.kind != "CURRENT" and self.episode is None:
            raise ValueError("Historical/source evidence requires an episode")
        if self.vector_ready and self.kind != "CHUNK":
            raise ValueError("Only search chunks can be vector ready")
        return self


class Fact(Record):
    id: Identifier
    text: str = Field(min_length=1)
    # Each inner list is one sufficient evidence set; alternatives are OR, members AND.
    support_sets: list[list[Identifier]] = Field(min_length=1)

    @model_validator(mode="after")
    def nonempty_support(self) -> Self:
        for group in self.support_sets:
            if not group:
                raise ValueError("A fact needs an explicit support set")
            unique(group, "fact support")
        return self


class Turn(Record):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class Question(Record):
    id: Identifier
    category: Identifier
    question: str = Field(min_length=1)
    work_id: Identifier
    episode_from: int | None = Field(default=None, ge=1, strict=True)
    episode_to: int | None = Field(default=None, ge=1, strict=True)
    conversation: list[Turn] = Field(default_factory=list, max_length=6)
    conversation_truncated: bool = False
    # The scenario snapshot, not retrieved results or the gold support list.
    evidence_ids: list[Identifier]
    expected_outcomes: dict[Mode, list[Outcome]]
    required_facts: list[Fact]
    forbidden_assertions: list[str] = Field(min_length=1)
    review_guidance: str = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        unique(self.evidence_ids, "scenario evidence")
        unique([f.id for f in self.required_facts], "required fact")
        if set(self.expected_outcomes) != set(MODES) or any(
            not values for values in self.expected_outcomes.values()
        ):
            raise ValueError("Every mode needs an expected outcome")
        if self.episode_from and self.episode_to and self.episode_from > self.episode_to:
            raise ValueError("Invalid episode range")
        return self


class Dataset(Record):
    schema_version: Literal[1]
    version: Identifier
    material: Literal["SYNTHETIC"]
    gold_status: Literal["INTERNAL_REVIEWED_DRAFT", "HUMAN_REVIEWED"]
    review_note: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    questions: list[Question] = Field(min_length=1)

    @model_validator(mode="after")
    def references(self) -> Self:
        unique([e.id for e in self.evidence], "evidence id")
        unique([q.id for q in self.questions], "question id")
        corpus = {e.id: e for e in self.evidence}
        for question in self.questions:
            if not set(question.evidence_ids) <= corpus.keys():
                raise ValueError("Unknown scenario evidence")
            for fact in question.required_facts:
                for group in fact.support_sets:
                    if not set(group) <= set(question.evidence_ids):
                        raise ValueError("Gold support is outside the scenario snapshot")
                    if not all(in_scope(corpus[key], question) for key in group):
                        raise ValueError("Gold support is invalid or outside scope")
        return self


def in_scope(evidence: Evidence, question: Question) -> bool:
    if not evidence.usable or evidence.work_id != question.work_id:
        return False
    # Current settings are not historical snapshots: an episode filter cannot reconstruct them.
    if evidence.kind == "CURRENT":
        return True
    return (question.episode_from is None or evidence.episode >= question.episode_from) and (
        question.episode_to is None or evidence.episode <= question.episode_to
    )


def accessible(evidence: Evidence, question: Question, mode: Mode) -> bool:
    if not in_scope(evidence, question):
        return False
    if mode == "RDB_DIRECT":
        return evidence.kind != "CHUNK"
    if mode == "VECTOR_ONLY":
        return evidence.kind == "CHUNK" and evidence.vector_ready
    return True


class Conditions(Record):
    # Same values required across a comparison. Trace files, not this field, prove execution.
    code_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    prompt_versions: dict[str, str] = Field(min_length=1)
    index_version: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)
    embedding_dimensions: int = Field(ge=1, strict=True)
    cache: Literal["COLD", "WARM", "UNMEASURED"]
    concurrency: int = Field(ge=1, strict=True)


class Manifest(Record):
    schema_version: Literal[1]
    run_id: Identifier
    dataset_sha256: Digest
    mode: Mode
    execution_kind: Literal["AUTHORED_CONTROL", "SCRIPTED_REPLAY", "PROVIDER_CAPTURE"]
    conditions: Conditions
    budgets: dict[str, NonNegative] = Field(min_length=1)
    trace_sha256: Digest
    # Prices are declared only by a real capture, never looked up by this offline tool.
    pricing_basis: str | None = None


class Citation(Record):
    evidence_id: Identifier
    version: Identifier
    sha256: Digest
    quote: str = Field(min_length=1)
    start: NonNegative
    end: int = Field(ge=1, strict=True)


class Call(Record):
    call_id: Identifier
    purpose: Literal["PLAN", "SELECT", "ANSWER", "EMBEDDING", "OTHER"]
    outcome: Literal["SUCCESS", "FAILURE", "UNKNOWN"]
    input_tokens: NonNegative | None = None
    cached_input_tokens: NonNegative | None = None
    output_tokens: NonNegative | None = None
    cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def usage(self) -> Self:
        tokens = (self.input_tokens, self.cached_input_tokens, self.output_tokens)
        if any(v is None for v in tokens) and any(v is not None for v in tokens):
            raise ValueError("Usage must be complete or unknown")
        if self.input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input is part of input tokens")
        if self.outcome == "UNKNOWN" and (
            self.input_tokens is not None or self.cost_usd is not None
        ):
            raise ValueError("UNKNOWN is not measured usage")
        if self.input_tokens is None and self.cost_usd is not None:
            raise ValueError("Unmeasured usage cannot have a measured cost")
        return self


class Observation(Record):
    case_id: Identifier
    status: Literal["COMPLETED", "FAILED", "INCOMPLETE"]
    outcome: Outcome | None = None
    text: str = ""
    # Only evidence actually exposed to this execution; never filled from gold at scoring time.
    observed_evidence_ids: list[Identifier] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    telemetry_complete: bool = False
    latency_ms: float | None = Field(default=None, ge=0)
    failure_code: Identifier | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        unique(self.observed_evidence_ids, "observed evidence")
        unique([c.call_id for c in self.calls], "call id")
        unique([c.evidence_id for c in self.citations], "citation id")
        if self.status == "COMPLETED":
            if self.outcome is None or not self.text.strip() or self.failure_code is not None:
                raise ValueError("Completed observations need an answer and no failure")
        elif self.outcome is not None or self.text or self.citations:
            raise ValueError("Unfinished observations cannot publish an answer")
        return self


class Run(Record):
    manifest: Manifest
    observations: list[Observation]

    @model_validator(mode="after")
    def unique_records(self) -> Self:
        unique([r.case_id for r in self.observations], "observation case")
        unique([c.call_id for r in self.observations for c in r.calls], "run call id")
        return self


class ClaimReview(Record):
    start: NonNegative
    end: int = Field(ge=1, strict=True)
    quote: str = Field(min_length=1)
    correctness: Literal["CORRECT", "INCORRECT", "UNCERTAIN"]
    support: Literal["SUPPORTED", "UNSUPPORTED", "UNCERTAIN", "NOT_APPLICABLE"]
    fact_ids: list[Identifier]
    evidence_ids: list[Identifier]
    rationale: str = Field(min_length=1)


class Review(Record):
    case_id: Identifier
    observation_sha256: Digest
    reviewer_kind: Literal["HUMAN", "INTERNAL_MODEL", "LLM_JUDGE"]
    reviewer: str = Field(min_length=1)
    # Explicit attestation; the evaluator cannot discover all natural-language assertions.
    all_assertions_reviewed: bool
    disposition_appropriate: bool | None
    forbidden_assertion_found: bool | None
    claims: list[ClaimReview]
    rationale: str = Field(min_length=1)


class ReviewSet(Record):
    dataset_sha256: Digest
    run_sha256: Digest
    reviews: list[Review]

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        unique([r.case_id for r in self.reviews], "review case")
        return self
