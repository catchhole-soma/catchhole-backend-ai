"""Explicit process settings; never load analysis .env or provider defaults."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HybridSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_RETRIEVAL_", env_file=None, extra="ignore")
    enabled: bool = False
    database_url: str = Field(default="", repr=False)
    embedding_version: str = Field(default="v1", min_length=1, max_length=50)
    # 2026-10-30 #319: 비통과 사례의 필요 근거 54개 중 26개가 모델 입력에 들어가지 못해
    # 후보·근거·라운드 예산을 넓혔다. 입력 32,000토큰·모델 호출 6회 상한은 유지한다.
    keyword_top_k: int = Field(default=16, ge=1, le=50)
    vector_top_k: int = Field(default=16, ge=1, le=50)
    neighbor_radius: int = Field(default=1, ge=0, le=2)
    # PARTIAL 초안이 인용한 장면의 앞뒤를 2라운드에서 더 넓게 읽는 반경. 회차는 맞췄지만 세부가 빠진 경우용.
    focus_radius: int = Field(default=3, ge=1, le=3)
    # 회차 순서 검색 결과 중 일반 keyword 후보 앞에 둘 최대 개수. 초반 언급이 관련 후보를 모두 밀어내지 않게 한다.
    ordered_top_k: int = Field(default=4, ge=1, le=12)
    max_neighbors: int = Field(default=8, ge=1, le=20)
    max_evidence: int = Field(default=12, ge=1, le=12)
    per_episode: int = Field(default=4, ge=1, le=12)
    evidence_tokens: int = Field(default=9000, ge=200, le=12000)
    # 3라운드는 앞 라운드가 PARTIAL/NONE이고 남은 시간이 충분할 때만 실행한다.
    max_rounds: int = Field(default=3, ge=1, le=3)
    # PARTIAL 답의 빠진 요구를 같은 근거로 한 번 더 묻는 보완 호출. 모델 호출 상한 안에서만 실행한다.
    repair_missing_parts: bool = True
    max_model_calls: int = Field(default=6, ge=1, le=6)
    max_query_embeddings: int = Field(default=2, ge=0, le=2)

    @model_validator(mode="after")
    def compatible(self):
        if self.per_episode > self.max_evidence:
            raise ValueError("Per-episode limit exceeds evidence cap")
        if not self.embedding_version.strip():
            raise ValueError("Embedding version is required")
        return self
