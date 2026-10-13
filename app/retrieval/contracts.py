"""Trusted operator scope and versioned readiness contract; no model parameters."""

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from app.retrieval.chunking import POLICY


@dataclass(frozen=True)
class IndexSpec:
    model: str = "text-embedding-3-small"
    version: str = "v1"
    dimensions: int = 1536
    policy: str = POLICY

    def __post_init__(self):
        if (
            self.model != "text-embedding-3-small"
            or self.dimensions != 1536
            or self.policy != POLICY
        ):
            raise ValueError("Unsupported retrieval model, dimensions or chunk policy")
        if not self.version or len(self.version) > 50:
            raise ValueError("Invalid embedding version")


@dataclass(frozen=True)
class Scope:
    work_id: UUID
    episode_ids: tuple[UUID, ...] = ()
    episode_from: int = 1
    episode_to: int = 2147483647
    max_sources: int = 20

    def __post_init__(self):
        if not 1 <= self.max_sources <= 100 or not 1 <= self.episode_from <= self.episode_to:
            raise ValueError("Invalid retrieval scope limits")
        if len(self.episode_ids) > self.max_sources or len(set(self.episode_ids)) != len(
            self.episode_ids
        ):
            raise ValueError("Invalid episode IDs")


@dataclass(frozen=True)
class Limits:
    max_chunks: int = 100
    max_input_tokens: int = 100000
    batch_size: int = 8
    max_retries: int = 1
    max_seconds: int = 900
    usd_per_million: Decimal | None = None
    max_cost_usd: Decimal | None = None

    def __post_init__(self):
        if (
            not 1 <= self.max_chunks <= 1000
            or not 1 <= self.max_input_tokens <= 1000000
            or not 1 <= self.batch_size <= 32
            or not 0 <= self.max_retries <= 2
            or not 1 <= self.max_seconds <= 3600
        ):
            raise ValueError("Invalid retrieval execution limits")
        for value in (self.usd_per_million, self.max_cost_usd):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("Price and cost cap must be positive finite values")
        if self.max_cost_usd is not None and self.usd_per_million is None:
            raise ValueError("An explicit price is required for a monetary cap")


@dataclass(frozen=True)
class Source:
    work_id: UUID
    episode_id: UUID
    episode_no: int
    key: str | None = field(repr=False)
    version: str | None
    content_hash: str | None
    state: str

    @property
    def fingerprint(self):
        return (self.work_id, self.episode_id, self.key, self.version, self.content_hash)


class SourceChanged(RuntimeError):
    def __init__(self):
        super().__init__("SOURCE_CHANGED")


class IndexBusy(RuntimeError):
    def __init__(self):
        super().__init__("INDEX_BUSY")
