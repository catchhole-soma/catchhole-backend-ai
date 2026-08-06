from dataclasses import dataclass
from enum import StrEnum

from evals.setting_extraction.models import GoldCandidate, PredictionCandidate
from evals.setting_extraction.normalization import (
    json_contains,
    normalize_text,
    parse_boolean,
    parse_decimal,
)


class ValueComparisonStatus(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    SEMANTIC_JUDGE_REQUIRED = "SEMANTIC_JUDGE_REQUIRED"


@dataclass(frozen=True)
class ValueComparison:
    status: ValueComparisonStatus
    reason: str
    value_type_matched: bool
    structured_value_matched: bool | None
    attribute_value_matched: bool | None


def compare_candidate_value(
    gold: GoldCandidate,
    prediction: PredictionCandidate,
) -> ValueComparison:
    # valueJson은 Fact 정답과 분리된 구조화 품질 지표다. 정답표가 핵심 필드를
    # 적은 경우에만 subset 비교하되, 불일치해도 attributeValue 판정을 중단하지 않는다.
    structured_value_matched = (
        json_contains(gold.value_json, prediction.value_json) if gold.value_json else None
    )

    # 타입이 다르면 값 표현이 같아도 저장 계약이 다르므로 즉시 실패한다.
    if gold.value_type is None:
        return ValueComparison(
            ValueComparisonStatus.MISMATCH,
            "gold valueType is missing",
            value_type_matched=False,
            structured_value_matched=structured_value_matched,
            attribute_value_matched=None,
        )
    if gold.value_type.casefold() != prediction.value_type.casefold():
        return ValueComparison(
            ValueComparisonStatus.MISMATCH,
            f"valueType differs: gold={gold.value_type} prediction={prediction.value_type}",
            value_type_matched=False,
            structured_value_matched=structured_value_matched,
            attribute_value_matched=None,
        )

    value_type = gold.value_type.upper()
    attribute_status, attribute_reason = _compare_attribute_value(
        value_type,
        gold.attribute_value,
        prediction.attribute_value,
    )
    if attribute_status == ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED:
        return ValueComparison(
            ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED,
            attribute_reason,
            value_type_matched=True,
            structured_value_matched=structured_value_matched,
            attribute_value_matched=None,
        )
    return ValueComparison(
        attribute_status,
        attribute_reason,
        value_type_matched=True,
        structured_value_matched=structured_value_matched,
        attribute_value_matched=attribute_status == ValueComparisonStatus.MATCH,
    )


def _compare_attribute_value(
    value_type: str,
    gold_value: str | None,
    prediction_value: str | None,
) -> tuple[ValueComparisonStatus, str]:
    if value_type == "NUMBER":
        gold_number = parse_decimal(gold_value)
        predicted_number = parse_decimal(prediction_value)
        if gold_number is None or predicted_number is None:
            return ValueComparisonStatus.MISMATCH, "attributeValue number could not be parsed"
        if gold_number == predicted_number:
            return ValueComparisonStatus.MATCH, "attributeValue numbers are equal"
        return ValueComparisonStatus.MISMATCH, "attributeValue numbers differ"

    if value_type == "BOOLEAN":
        gold_boolean = parse_boolean(gold_value)
        predicted_boolean = parse_boolean(prediction_value)
        if gold_boolean is None or predicted_boolean is None:
            return ValueComparisonStatus.MISMATCH, "attributeValue boolean could not be parsed"
        if gold_boolean == predicted_boolean:
            return ValueComparisonStatus.MATCH, "attributeValue booleans are equal"
        return ValueComparisonStatus.MISMATCH, "attributeValue booleans differ"

    if normalize_text(gold_value) == normalize_text(prediction_value):
        return ValueComparisonStatus.MATCH, "normalized attributeValues are equal"
    return (
        ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED,
        "text attributeValue requires semantic comparison",
    )
