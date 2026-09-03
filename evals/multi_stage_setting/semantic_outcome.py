from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol, Sequence

from pydantic import BaseModel, Field

from app.analysis.json_response import parse_json_object
from app.llm.openai_client import OpenAIResponsesClient


DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "semantic_outcome_judge.md"
DEFAULT_MODEL = "gpt-5.6-luna"


@dataclass(frozen=True)
class SemanticOutcomeCase:
    case_id: str
    expected_value: str | None
    actual_value: str | None
    before_value: str | None = None
    source_values: tuple[str, ...] = ()
    required_facts: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    evidence_quotes: tuple[str, ...] = ()


class SemanticOutcomeDecision(BaseModel):
    case_id: str = Field(alias="caseId")
    core_meaning_covered: bool = Field(alias="coreMeaningCovered")
    required_facts_covered: bool = Field(alias="requiredFactsCovered")
    forbidden_facts_absent: bool = Field(alias="forbiddenFactsAbsent")
    contradiction: bool
    unsupported_detail: bool = Field(alias="unsupportedDetail")
    reason: str

    @property
    def matched(self) -> bool:
        return (
            self.core_meaning_covered
            and self.required_facts_covered
            and self.forbidden_facts_absent
            and not self.contradiction
            and not self.unsupported_detail
        )


class SemanticOutcomeResponse(BaseModel):
    results: list[SemanticOutcomeDecision]


@dataclass(frozen=True)
class SemanticOutcomeBatchResult:
    decisions: tuple[SemanticOutcomeDecision, ...]
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


class SemanticOutcomeJudge(Protocol):
    async def judge_many(
        self,
        cases: Sequence[SemanticOutcomeCase],
    ) -> SemanticOutcomeBatchResult: ...


class OpenAISemanticOutcomeJudge:
    def __init__(
        self,
        client: OpenAIResponsesClient | None = None,
        *,
        model: str = DEFAULT_MODEL,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        batch_size: int = 8,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")
        self.client = client or OpenAIResponsesClient.from_settings()
        self.model = model
        self.prompt_path = prompt_path
        self.batch_size = batch_size

    async def judge_many(
        self,
        cases: Sequence[SemanticOutcomeCase],
    ) -> SemanticOutcomeBatchResult:
        if not cases:
            return SemanticOutcomeBatchResult(decisions=())
        decisions: list[SemanticOutcomeDecision] = []
        input_tokens = cached_input_tokens = output_tokens = 0
        for start in range(0, len(cases), self.batch_size):
            chunk = cases[start : start + self.batch_size]
            result = await self._judge_chunk(chunk)
            decisions.extend(result.decisions)
            input_tokens += result.input_tokens
            cached_input_tokens += result.cached_input_tokens
            output_tokens += result.output_tokens
        return SemanticOutcomeBatchResult(
            decisions=tuple(decisions),
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )

    async def _judge_chunk(
        self,
        cases: Sequence[SemanticOutcomeCase],
    ) -> SemanticOutcomeBatchResult:
        response = await self.client.create_text_response(
            system_prompt=self.prompt_path.read_text(encoding="utf-8"),
            user_prompt=json.dumps(
                {
                    "cases": [
                        {
                            "caseId": case.case_id,
                            "beforeValue": case.before_value,
                            "sourceValues": list(case.source_values),
                            "expectedValue": case.expected_value,
                            "actualValue": case.actual_value,
                            "requiredFacts": list(case.required_facts),
                            "forbiddenFacts": list(case.forbidden_facts),
                            "evidenceQuotes": list(case.evidence_quotes),
                        }
                        for case in cases
                    ]
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            model=self.model,
            max_output_tokens=min(5000, 400 + 450 * len(cases)),
            prompt_cache_key="multi-stage-setting-eval:semantic-outcome:v1",
        )
        try:
            parsed = SemanticOutcomeResponse.model_validate(parse_json_object(response.text))
        except ValueError:
            raise ValueError("Semantic outcome judge response is invalid.") from None
        expected_ids = [case.case_id for case in cases]
        decision_by_id: dict[str, SemanticOutcomeDecision] = {}
        for decision in parsed.results:
            if decision.case_id in decision_by_id:
                raise ValueError("Semantic outcome judge returned a duplicate caseId.")
            decision_by_id[decision.case_id] = decision
        if set(decision_by_id) != set(expected_ids):
            raise ValueError("Semantic outcome judge caseIds do not match the request.")
        return SemanticOutcomeBatchResult(
            decisions=tuple(decision_by_id[case_id] for case_id in expected_ids),
            input_tokens=response.input_token_count or 0,
            cached_input_tokens=response.cached_input_token_count or 0,
            output_tokens=response.output_token_count or 0,
        )
