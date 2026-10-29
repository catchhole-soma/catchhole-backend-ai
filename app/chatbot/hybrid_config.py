"""Explicit process settings; never load analysis .env or provider defaults."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HybridSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_RETRIEVAL_", env_file=None, extra="ignore")
    enabled: bool = False
    database_url: str = Field(default="", repr=False)
    embedding_version: str = Field(default="v1", min_length=1, max_length=50)
    keyword_top_k: int = Field(default=12, ge=1, le=50)
    vector_top_k: int = Field(default=12, ge=1, le=50)
    neighbor_radius: int = Field(default=1, ge=0, le=2)
    max_neighbors: int = Field(default=6, ge=1, le=20)
    max_evidence: int = Field(default=8, ge=1, le=12)
    per_episode: int = Field(default=3, ge=1, le=12)
    evidence_tokens: int = Field(default=6000, ge=200, le=12000)
    max_rounds: int = Field(default=2, ge=1, le=2)
    max_model_calls: int = Field(default=6, ge=1, le=6)
    max_query_embeddings: int = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def compatible(self):
        if self.per_episode > self.max_evidence:
            raise ValueError("Per-episode limit exceeds evidence cap")
        if not self.embedding_version.strip():
            raise ValueError("Embedding version is required")
        return self
