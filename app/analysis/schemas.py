from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.domain.setting_values import normalize_setting_display_value


# 후보가 원문 어디에서 나왔는지 보여주기 위한 근거 정보
class ExtractedEvidenceSpan(BaseModel):
    # 실제 원문 일부, 이후 화면에서 근거 문장으로 보여줄 값
    quote: str = Field(min_length=1)
    # offset은 후속 evidence locator에서 채울 수 있어서 현재는 null 허용
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


# LLM이 추출한 검토 전 설정 후보, Spring의 setting_candidates 저장 구조를 고려한 중간 형태
class ExtractedSettingCandidate(BaseModel):
    # 어떤 청크에서 나온 후보인지 나타내는 값, 현재는 FK 없이 UUID 값으로 저장될 수 있다.
    source_chunk_id: UUID
    # 기존 설정 후보와 이름만 확인된 캐릭터 발견 후보를 같은 검토 흐름에서 구분한다.
    candidate_kind: Literal["SETTING", "CHARACTER_DISCOVERY"] = "SETTING"
    # 캐릭터 설정 관련으로만 받음
    entity_type: Literal["CHARACTER"] = "CHARACTER"
    entity_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    raw_entity_mention: str | None = Field(default=None, max_length=100)
    attribute_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ] | None = None
    # 목록/검색 표시용 요약값
    attribute_value: str | None = None
    # Spring SettingValueType과 맞춘 값 타입
    value_type: Literal["STRING", "NUMBER", "BOOLEAN", "JSON", "UNKNOWN"] | None = None
    # 실제 구조화 값, 예: {"value": 12} 또는 {"근력": 80, "민첩": 65}
    value_json: dict[str, Any] | None = None
    evidence_spans: list[ExtractedEvidenceSpan] = Field(min_length=1)
    # LLM이 스스로 판단한 신뢰도, 0~1 범위로 둔다.
    confidence: float | None = Field(default=None, ge=0, le=1) # 0 <= confidence <= 1

    @model_validator(mode="after")
    def validate_candidate_kind_payload(self) -> "ExtractedSettingCandidate":
        if self.candidate_kind == "SETTING":
            if self.attribute_name is None or self.value_type is None or self.value_json is None:
                raise ValueError(
                    "SETTING candidate requires attribute_name, value_type, and value_json."
                )
            normalize_setting_display_value(
                self.value_type,
                self.value_json,
                self.attribute_value,
            )
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
            raise ValueError(
                "CHARACTER_DISCOVERY candidate must not include setting value fields."
            )
        return self


# 청크 하나에서 나온 설정 후보 목록
class CharacterSettingExtractionResult(BaseModel):
    candidates: list[ExtractedSettingCandidate] = Field(default_factory=list)
