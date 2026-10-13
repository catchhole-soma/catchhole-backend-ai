"""Read-only search contracts. Scope is trusted server context, never model input."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

SearchMethod = Literal["DIRECT", "KEYWORD", "VECTOR", "NEIGHBOR"]


@dataclass(frozen=True)
class SearchScope:
    member_id: int
    work_id: UUID
    episode_from: int = 1
    episode_to: int = 2147483647

    def __post_init__(self):
        if (
            isinstance(self.member_id, bool)
            or not isinstance(self.member_id, int)
            or self.member_id < 1
            or not isinstance(self.work_id, UUID)
            or any(
                isinstance(n, bool) or not isinstance(n, int)
                for n in (self.episode_from, self.episode_to)
            )
            or not 1 <= self.episode_from <= self.episode_to <= 2147483647
        ):
            raise ValueError("Invalid trusted search scope")


@dataclass(frozen=True)
class ChunkEvidence:
    chunk_id: UUID
    episode_id: UUID
    episode_no: int
    chunk_index: int
    text: str = field(repr=False)
    start_offset: int
    end_offset: int
    chunk_hash: str
    source_key: str = field(repr=False)
    source_version: str | None
    source_hash: str
    policy: str
    methods: tuple[SearchMethod, ...]
    score: float

    @property
    def fingerprint(self):
        return (
            self.chunk_id,
            self.episode_id,
            self.episode_no,
            self.chunk_index,
            self.source_key,
            self.source_version,
            self.source_hash,
            self.policy,
            self.start_offset,
            self.end_offset,
            self.chunk_hash,
            self.text,
        )

    @property
    def source_fingerprint(self):
        return self.episode_id, self.source_key, self.source_version, self.source_hash, self.policy


@dataclass(frozen=True)
class SearchReadiness:
    source_count: int
    text_ready_sources: int
    vector_ready_sources: int
    text_chunks: int
    vector_chunks: int
    limitations: tuple[str, ...] = ()

    @property
    def partial(self):
        return bool(self.limitations)


@dataclass(frozen=True)
class SearchResult:
    chunks: tuple[ChunkEvidence, ...]
    readiness: SearchReadiness
    limited: bool = False


class SearchScopeUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("SEARCH_SCOPE_UNAVAILABLE")


def validate_limit(limit: int):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Search limit must be between 1 and 100")


def validate_query(query: str):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("Search query must contain 1 to 1000 characters")


def validate_ids(ids):
    if len(ids) > 100 or any(not isinstance(value, UUID) for value in ids):
        raise ValueError("Invalid search chunk references")
