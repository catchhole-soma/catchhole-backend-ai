from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.llm.openai_client import OpenAIResponsesClient
from evals.setting_extraction.models import GoldCandidate, PredictionCandidate
from evals.setting_extraction.normalization import normalize_text


DEFAULT_JUDGE_PROMPT_PATH = Path(__file__).parent / "prompts" / "semantic_value_judge.md"
DEFAULT_SEMANTIC_JUDGE_MODEL = "gpt-5.6-luna"
DEFAULT_SEMANTIC_JUDGE_BATCH_SIZE = 8


class SemanticJudgeDecision(BaseModel):
    core_meaning_covered: bool
    supported_by_evidence: bool
    contradiction: bool
    unsupported_detail: bool
    reason: str

    @property
    def matched(self) -> bool:
        return (
            self.core_meaning_covered
            and self.supported_by_evidence
            and not self.contradiction
            and not self.unsupported_detail
        )


class _SemanticJudgeBatchItem(SemanticJudgeDecision):
    model_config = ConfigDict(populate_by_name=True)

    case_id: int = Field(alias="caseId", ge=0)


class _SemanticJudgeBatchResponse(BaseModel):
    results: list[_SemanticJudgeBatchItem]


@dataclass(frozen=True)
class SemanticJudgeCase:
    gold: GoldCandidate
    prediction: PredictionCandidate
    source_text: str | None


@dataclass(frozen=True)
class SemanticJudgeResult:
    decision: SemanticJudgeDecision
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class SemanticJudgeBatchResult:
    decisions: tuple[SemanticJudgeDecision, ...]
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


class SemanticValueJudge(Protocol):
    def judge_many(
        self,
        cases: Sequence[SemanticJudgeCase],
    ) -> SemanticJudgeBatchResult:
        pass


class OpenAISemanticValueJudge:
    def __init__(
        self,
        client: OpenAIResponsesClient | None = None,
        model: str | None = None,
        prompt_path: Path = DEFAULT_JUDGE_PROMPT_PATH,
        batch_size: int = DEFAULT_SEMANTIC_JUDGE_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("Semantic judge batch_size must be at least 1.")
        self.client = client or OpenAIResponsesClient.from_settings()
        # 제품 분석 모델과 독립적으로, 평가의 서술형 의미 판정만 저비용 Luna를 기본 사용한다.
        self.model = model or DEFAULT_SEMANTIC_JUDGE_MODEL
        self.prompt_path = prompt_path
        self.batch_size = batch_size

    def judge(
        self,
        gold: GoldCandidate,
        prediction: PredictionCandidate,
        source_text: str | None,
    ) -> SemanticJudgeResult:
        batch = self.judge_many(
            [SemanticJudgeCase(gold=gold, prediction=prediction, source_text=source_text)]
        )
        return SemanticJudgeResult(
            decision=batch.decisions[0],
            input_tokens=batch.input_tokens,
            cached_input_tokens=batch.cached_input_tokens,
            output_tokens=batch.output_tokens,
        )

    def judge_many(
        self,
        cases: Sequence[SemanticJudgeCase],
    ) -> SemanticJudgeBatchResult:
        if not cases:
            return SemanticJudgeBatchResult(decisions=())

        decisions: list[SemanticJudgeDecision] = []
        input_tokens = 0
        cached_input_tokens = 0
        output_tokens = 0
        for start in range(0, len(cases), self.batch_size):
            chunk = cases[start : start + self.batch_size]
            chunk_result = self._judge_chunk(chunk)
            decisions.extend(chunk_result.decisions)
            input_tokens += chunk_result.input_tokens
            cached_input_tokens += chunk_result.cached_input_tokens
            output_tokens += chunk_result.output_tokens
        return SemanticJudgeBatchResult(
            decisions=tuple(decisions),
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )

    def _judge_chunk(
        self,
        cases: Sequence[SemanticJudgeCase],
    ) -> SemanticJudgeBatchResult:
        # 규칙으로 확정하지 못한 행만 최대 8개씩 묶고, 각 행의 근거 문맥은 독립적으로 준다.
        response = self.client.create_text_response(
            system_prompt=self.prompt_path.read_text(encoding="utf-8"),
            user_prompt=json.dumps(
                {
                    "cases": [
                        {
                            "caseId": case_id,
                            "entityName": case.gold.entity_name,
                            "factKey": case.gold.fact_key,
                            "goldValue": case.gold.attribute_value,
                            "predictionValue": case.prediction.attribute_value,
                            "goldEvidence": case.gold.evidence_quotes,
                            "predictionEvidence": [
                                span.quote for span in case.prediction.evidence_spans
                            ],
                            "sourceExcerpt": _build_source_excerpt(
                                case.source_text,
                                [
                                    *case.gold.evidence_quotes,
                                    *(span.quote for span in case.prediction.evidence_spans),
                                ],
                            ),
                        }
                        for case_id, case in enumerate(cases)
                    ]
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            model=self.model,
            max_output_tokens=min(4000, 300 + 400 * len(cases)),
            prompt_cache_key="setting-extraction-eval:semantic-judge:v2",
        )
        try:
            payload = _SemanticJudgeBatchResponse.model_validate(
                _parse_json_object(response.text)
            )
        except ValueError:
            # provider 응답의 reason·입력값이 Actions traceback에 포함되지 않게 경계를 닫는다.
            raise ValueError("Semantic judge response is invalid.") from None
        decision_by_id: dict[int, SemanticJudgeDecision] = {}
        for item in payload.results:
            if item.case_id in decision_by_id:
                raise ValueError(f"Semantic judge returned duplicate caseId={item.case_id}.")
            decision_by_id[item.case_id] = SemanticJudgeDecision.model_validate(
                item.model_dump(exclude={"case_id"})
            )
        expected_ids = set(range(len(cases)))
        if set(decision_by_id) != expected_ids:
            raise ValueError(
                "Semantic judge caseIds do not match the request: "
                f"expected={sorted(expected_ids)} actual={sorted(decision_by_id)}"
            )
        return SemanticJudgeBatchResult(
            decisions=tuple(decision_by_id[index] for index in range(len(cases))),
            input_tokens=response.input_token_count or 0,
            cached_input_tokens=response.cached_input_token_count or 0,
            output_tokens=response.output_token_count or 0,
        )


def _build_source_excerpt(source_text: str | None, quotes: list[str], radius: int = 300) -> str:
    if not source_text:
        return ""
    # 전체 원고 대신 첫 번째로 찾은 인용 주변만 전달해 비용과 무관한 문맥 유입을 줄인다.
    for quote in quotes:
        if not quote:
            continue
        index = source_text.find(quote)
        if index >= 0:
            start = max(0, index - radius)
            end = min(len(source_text), index + len(quote) + radius)
            return source_text[start:end]

    normalized_source = normalize_text(source_text)
    for quote in quotes:
        normalized_quote = normalize_text(quote)
        if normalized_quote and normalized_quote in normalized_source:
            # 정규화 좌표는 원문 좌표와 다를 수 있으므로 전체를 제한해 fallback 문맥으로 준다.
            return source_text[: radius * 2]
    return source_text[: radius * 2]


def _parse_json_object(text: str) -> dict:
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Semantic judge response must contain a JSON object.")
    return json.loads(content[start : end + 1])
