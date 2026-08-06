from dataclasses import dataclass
from difflib import SequenceMatcher

from evals.setting_extraction.models import GoldCandidate, PredictionCandidate
from evals.setting_extraction.normalization import normalize_text


@dataclass(frozen=True)
class EvidenceEvaluation:
    quote_count: int
    locatable_quote_count: int
    gold_quote_count: int
    covered_gold_quote_count: int

    @property
    def all_prediction_quotes_locatable(self) -> bool:
        return self.quote_count > 0 and self.quote_count == self.locatable_quote_count

    @property
    def has_gold_quote_coverage(self) -> bool:
        return self.gold_quote_count > 0 and self.covered_gold_quote_count > 0


def evaluate_evidence(
    gold: GoldCandidate,
    prediction: PredictionCandidate,
    source_text: str | None,
) -> EvidenceEvaluation:
    predicted_quotes = [span.quote for span in prediction.evidence_spans]
    normalized_source = normalize_text(source_text)
    # LLM offset은 없거나 어긋날 수 있어 인용문이 정규화 원문에 실제 존재하는지를 우선한다.
    locatable_count = sum(
        1
        for quote in predicted_quotes
        if normalized_source and normalize_text(quote) in normalized_source
    )
    covered_gold_count = sum(
        1
        for gold_quote in gold.evidence_quotes
        if any(_quotes_cover_same_span(gold_quote, predicted) for predicted in predicted_quotes)
    )
    return EvidenceEvaluation(
        quote_count=len(predicted_quotes),
        locatable_quote_count=locatable_count,
        gold_quote_count=len(gold.evidence_quotes),
        covered_gold_quote_count=covered_gold_count,
    )


def _quotes_cover_same_span(left: str, right: str) -> bool:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return False
    # 같은 문장을 조금 길거나 짧게 인용한 정상 예측은 허용한다.
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return True
    return SequenceMatcher(None, normalized_left, normalized_right).ratio() >= 0.9
