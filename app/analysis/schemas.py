import json
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.domain.setting_values import normalize_setting_display_value

CandidateKind = Literal["SETTING", "CHARACTER_DISCOVERY"]
SettingValueType = Literal["STRING", "NUMBER", "BOOLEAN", "JSON", "UNKNOWN"]
FiniteJsonNumber = StrictInt | Annotated[StrictFloat, Field(allow_inf_nan=False)]


# 후보가 원문 어디에서 나왔는지 보여주기 위한 근거 정보
class ExtractedEvidenceSpan(BaseModel):
    # 실제 원문 일부, 이후 화면에서 근거 문장으로 보여줄 값
    quote: str = Field(min_length=1)
    # offset은 후속 evidence locator에서 채울 수 있어서 현재는 null 허용
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


# 기존 도메인 코드가 사용하는 공통 후보 타입이다. Provider 응답 검증에는 아래의
# discriminator 모델을 사용하고, 직접 생성하는 기존 테스트/서비스 계약은 유지한다.
class ExtractedSettingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_chunk_id: UUID
    candidate_kind: CandidateKind = "SETTING"
    entity_type: Literal["CHARACTER"] = "CHARACTER"
    entity_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    raw_entity_mention: str | None = Field(default=None, max_length=100)
    attribute_name: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        ]
        | None
    ) = None
    attribute_value: str | None = None
    value_type: SettingValueType | None = None
    value_json: dict[str, Any] | None = None
    evidence_spans: list[ExtractedEvidenceSpan] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_candidate_payload(self) -> "ExtractedSettingCandidate":
        if self.candidate_kind == "SETTING":
            if self.attribute_name is None or self.value_type is None or self.value_json is None:
                raise PydanticCustomError(
                    "setting_required_field_missing",
                    "SETTING candidate is missing a required setting field.",
                )
            if (
                self.attribute_name.startswith("status.")
                and "active" in self.value_json
                and type(self.value_json["active"]) is not bool
            ):
                raise PydanticCustomError(
                    "status_active_value_invalid",
                    "STATUS value_json.active must be a JSON boolean.",
                )
            _validate_typed_setting_value(self.value_type, self.value_json, self.attribute_value)
            return self

        if any(
            value is not None
            for value in (
                self.attribute_name,
                self.attribute_value,
                self.value_type,
                self.value_json,
            )
        ):
            raise PydanticCustomError(
                "discovery_setting_field_forbidden",
                "CHARACTER_DISCOVERY candidate must not include setting value fields.",
            )
        return self


class ExtractedCharacterSettingCandidate(ExtractedSettingCandidate):
    """검증된 SETTING 후보. 필수 필드는 JSON Schema에도 required로 노출된다."""

    candidate_kind: Literal["SETTING"]
    attribute_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    value_type: SettingValueType
    value_json: dict[str, Any]


class ExtractedCharacterDiscoveryCandidate(ExtractedSettingCandidate):
    """이름 발견 후보. 설정 필드는 명시적으로 null만 허용한다."""

    candidate_kind: Literal["CHARACTER_DISCOVERY"]
    attribute_name: None
    attribute_value: None
    value_type: None
    value_json: None


ExtractedCharacterCandidate = Annotated[
    ExtractedCharacterSettingCandidate | ExtractedCharacterDiscoveryCandidate,
    Field(discriminator="candidate_kind"),
]


# 청크 하나에서 나온 설정 후보 목록. 후보 하나라도 실패하면 전체 결과가 ValidationError다.
class CharacterSettingExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ExtractedCharacterCandidate] = Field(default_factory=list)


class _StrictProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ProviderEvidenceSpan(_StrictProviderModel):
    quote: str = Field(min_length=1)
    start_offset: int | None = Field(ge=0)
    end_offset: int | None = Field(ge=0)


class _ProviderValueJsonBase(_StrictProviderModel):
    # Strict Structured Outputs는 임의 key object를 허용하지 않는다. 알려지지 않은
    # 부가 key는 JSON object 문자열로 받고 Pydantic 검증 뒤 내부 dict로 복원한다.
    extra_json: str | None

    @field_validator("extra_json")
    @classmethod
    def validate_extra_json(cls, value: str | None) -> str | None:
        if value is not None:
            _parse_extra_json_object(value)
        return value


class _ProviderNumberValueJson(_ProviderValueJsonBase):
    value: FiniteJsonNumber


class _ProviderBooleanValueJson(_ProviderValueJsonBase):
    value: StrictBool


class _ProviderStringValueJson(_ProviderValueJsonBase):
    value: StrictStr


class _ProviderJsonValueJson(_StrictProviderModel):
    extra_json: str

    @field_validator("extra_json")
    @classmethod
    def validate_extra_json(cls, value: str) -> str:
        _parse_extra_json_object(value)
        return value


class _ProviderUnknownValueJson(_ProviderValueJsonBase):
    value: StrictStr | FiniteJsonNumber | StrictBool | None


class _ProviderCandidateCommon(_StrictProviderModel):
    entity_type: Literal["CHARACTER"]
    entity_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    raw_entity_mention: Annotated[str, StringConstraints(max_length=100)] | None
    evidence_spans: list[_ProviderEvidenceSpan] = Field(min_length=1)
    confidence: float | None = Field(ge=0, le=1)


class _ProviderSettingCandidateCommon(_ProviderCandidateCommon):
    candidate_kind: Literal["SETTING"]
    attribute_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    attribute_value: str | None


class _ProviderNumberSettingCandidate(_ProviderSettingCandidateCommon):
    value_type: Literal["NUMBER"]
    value_json: _ProviderNumberValueJson


class _ProviderBooleanSettingCandidate(_ProviderSettingCandidateCommon):
    value_type: Literal["BOOLEAN"]
    value_json: _ProviderBooleanValueJson


class _ProviderStringSettingCandidate(_ProviderSettingCandidateCommon):
    value_type: Literal["STRING"]
    value_json: _ProviderStringValueJson


class _ProviderJsonSettingCandidate(_ProviderSettingCandidateCommon):
    value_type: Literal["JSON"]
    value_json: _ProviderJsonValueJson


class _ProviderUnknownSettingCandidate(_ProviderSettingCandidateCommon):
    value_type: Literal["UNKNOWN"]
    value_json: _ProviderUnknownValueJson


class _ProviderCharacterDiscoveryCandidate(_ProviderCandidateCommon):
    candidate_kind: Literal["CHARACTER_DISCOVERY"]
    attribute_name: None
    attribute_value: None
    value_type: None
    value_json: None


_ProviderSettingCandidate = Annotated[
    _ProviderNumberSettingCandidate
    | _ProviderBooleanSettingCandidate
    | _ProviderStringSettingCandidate
    | _ProviderJsonSettingCandidate
    | _ProviderUnknownSettingCandidate,
    Field(discriminator="value_type"),
]
_ProviderCharacterCandidate = Annotated[
    _ProviderSettingCandidate | _ProviderCharacterDiscoveryCandidate,
    Field(discriminator="candidate_kind"),
]


class CharacterSettingProviderResponse(_StrictProviderModel):
    """OpenAI structured output과 직접 맞물리는 source_chunk_id 없는 wire model."""

    candidates: list[_ProviderCharacterCandidate]

    def to_extraction_result(self, source_chunk_id: UUID) -> CharacterSettingExtractionResult:
        payload = {
            "candidates": [
                _provider_candidate_to_internal_payload(candidate, source_chunk_id)
                for candidate in self.candidates
            ]
        }
        # Provider schema 통과 뒤에도 저장 경계의 Pydantic 모델로 한 번 더 검증한다.
        return CharacterSettingExtractionResult.model_validate(payload)


@lru_cache(maxsize=1)
def character_setting_provider_json_schema() -> dict[str, Any]:
    """Pydantic schema를 Responses API strict subset 형태로 정규화한다."""

    return _to_openai_strict_json_schema(CharacterSettingProviderResponse.model_json_schema())


def _provider_candidate_to_internal_payload(
    candidate: _ProviderCharacterCandidate,
    source_chunk_id: UUID,
) -> dict[str, Any]:
    payload = candidate.model_dump()
    payload["source_chunk_id"] = str(source_chunk_id)
    if candidate.candidate_kind == "CHARACTER_DISCOVERY":
        return payload

    wire_value = payload["value_json"]
    extra_json = wire_value.pop("extra_json")
    value_json = _parse_extra_json_object(extra_json) if extra_json is not None else {}
    if "value" in wire_value and (
        candidate.value_type != "UNKNOWN" or wire_value["value"] is not None
    ):
        # typed value가 부가 JSON의 같은 key보다 항상 우선하는 source of truth다.
        value_json["value"] = wire_value["value"]
    payload["value_json"] = value_json
    return payload


def _parse_extra_json_object(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PydanticCustomError(
            "json_typed_value_invalid",
            "extra_json must contain a valid JSON object.",
        ) from exc
    if not isinstance(payload, dict):
        raise PydanticCustomError(
            "json_typed_value_invalid",
            "extra_json must contain a JSON object.",
        )
    return payload


def _validate_typed_setting_value(
    value_type: SettingValueType,
    value_json: dict[str, Any],
    attribute_value: str | None,
) -> None:
    try:
        normalize_setting_display_value(value_type, value_json, attribute_value)
    except ValueError as exc:
        if value_type == "NUMBER":
            raise PydanticCustomError(
                "number_typed_value_invalid",
                (
                    "NUMBER value_json must contain a typed value field."
                    if "value" not in value_json
                    else "NUMBER value_json.value must be a finite JSON number."
                ),
            ) from exc
        if value_type == "BOOLEAN":
            raise PydanticCustomError(
                "boolean_typed_value_invalid",
                (
                    "BOOLEAN value_json must contain a typed value field."
                    if "value" not in value_json
                    else "BOOLEAN value_json.value must be a JSON boolean."
                ),
            ) from exc
        raise


def _to_openai_strict_json_schema(value: Any) -> Any:
    """Pydantic discriminator 표현을 OpenAI가 지원하는 anyOf subset으로 바꾼다."""

    if isinstance(value, list):
        return [_to_openai_strict_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"title", "discriminator", "default"}:
            continue
        normalized_key = "anyOf" if key == "oneOf" else key
        normalized[normalized_key] = _to_openai_strict_json_schema(item)
    return normalized
