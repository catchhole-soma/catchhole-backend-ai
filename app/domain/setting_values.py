import json
import math
from typing import Any

from app.domain.enums import SettingValueType


def normalize_setting_display_value(
    value_type: SettingValueType | str | None,
    value_json: dict[str, Any] | None,
    fallback_display_value: str | None,
) -> str | None:
    """NUMBER/BOOLEAN의 저장 표시값을 구조화 값에서 결정한다.

    이 두 타입은 Backend가 표시값과 ``value_json.value``의 동등성을 검증하므로,
    LLM이 만든 설명 문구를 그대로 저장하지 않는다. 나머지 타입은 기존 표시 계약을
    유지한다.
    """

    if value_type not in {
        SettingValueType.NUMBER,
        SettingValueType.BOOLEAN,
        SettingValueType.NUMBER.value,
        SettingValueType.BOOLEAN.value,
    }:
        return fallback_display_value
    if not isinstance(value_json, dict) or "value" not in value_json:
        raise ValueError(f"{value_type} value_json must contain a typed value field.")

    value = value_json["value"]
    if value_type in {SettingValueType.NUMBER, SettingValueType.NUMBER.value}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("NUMBER value_json.value must be a JSON number.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("NUMBER value_json.value must be finite.")
        return json.dumps(value, allow_nan=False, separators=(",", ":"))

    if not isinstance(value, bool):
        # LLM JSON의 스키마 값 오류이며 Pydantic이 ValidationError로 감싸 재시도해야 한다.
        raise ValueError("BOOLEAN value_json.value must be a JSON boolean.")  # noqa: TRY004
    return "true" if value else "false"
