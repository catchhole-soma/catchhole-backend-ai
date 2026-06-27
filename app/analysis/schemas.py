from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


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
    # 캐릭터 설정 관련으로만 받음
    entity_type: Literal["CHARACTER"] = "CHARACTER"
    entity_name: str = Field(min_length=1, max_length=100)
    attribute_name: str = Field(min_length=1, max_length=100)
    # 목록/검색 표시용 요약값
    attribute_value: str | None = None
    # Spring SettingValueType과 맞춘 값 타입
    value_type: Literal["STRING", "NUMBER", "BOOLEAN", "JSON", "UNKNOWN"]
    # 실제 구조화 값, 예: {"value": 12} 또는 {"근력": 80, "민첩": 65}
    value_json: dict[str, Any] = Field(default_factory=dict) # 값을 안 넣으면 매번 새로운 빈 {}를 기본값으로 생성
    evidence_spans: list[ExtractedEvidenceSpan] = Field(min_length=1)
    # LLM이 스스로 판단한 신뢰도, 0~1 범위로 둔다.
    confidence: float | None = Field(default=None, ge=0, le=1) # 0 <= confidence <= 1


# 청크 하나에서 나온 설정 후보 목록
class CharacterSettingExtractionResult(BaseModel):
    candidates: list[ExtractedSettingCandidate] = Field(default_factory=list)
