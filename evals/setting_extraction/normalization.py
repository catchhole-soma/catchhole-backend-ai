from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any


_WHITESPACE_PATTERN = re.compile(r"\s+")
_NUMBER_PREFIX_PATTERN = re.compile(r"^[^\d+\-]*([+\-]?\d+(?:\.\d+)?)")


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE_PATTERN.sub(" ", normalized).strip().casefold()


def normalize_fact_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    segments = [segment.strip().replace(" ", "_") for segment in normalized.split(".")]
    return ".".join(segments)


def parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    normalized = unicodedata.normalize("NFKC", str(value)).replace(",", "").strip()
    # "36 (New +1)" 같은 표시값에서는 첫 숫자 36만 현재값으로 해석한다.
    match = _NUMBER_PREFIX_PATTERN.match(normalized)
    if match is None:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def parse_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = normalize_text(str(value) if value is not None else None)
    if normalized in {"true", "1", "yes", "y", "예", "참"}:
        return True
    if normalized in {"false", "0", "no", "n", "아니오", "거짓"}:
        return False
    return None


def json_contains(expected: Any, actual: Any) -> bool:
    """정답 JSON을 핵심 필드로 보고 예측의 추가 구조화 정보는 허용한다."""

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and json_contains(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(json_contains(left, right) for left, right in zip(expected, actual, strict=True))
    if isinstance(expected, bool):
        return parse_boolean(actual) == expected
    if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        return parse_decimal(actual) == parse_decimal(expected)
    if expected is None:
        return actual is None
    return normalize_text(str(expected)) == normalize_text(str(actual))
