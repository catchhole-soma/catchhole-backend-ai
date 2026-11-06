"""Exact frozen-manifest predicates apply before every rank/LIMIT; validation searches are VECTOR only.

The frozen RAG scope is every frozen prior episode plus every frozen setting-book revision the server marked
`currentRevision` at intake (the book's current, non-archived revision). Both indexes are ranked together.
"""

import asyncio
import math
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text

from app.retrieval.contracts import IndexSpec
from app.retrieval.search_contracts import (
    SearchReadiness,
    SearchScope,
    SearchScopeUnavailable,
    validate_limit,
)
from app.retrieval.search_repository import CURRENT_FROM, RetrievalSearchRepository
from app.validation.contracts import IncompleteInspection

EPISODE, SETTING_BOOK = "EPISODE", "SETTING_BOOK"


def indexed_sources(sources):
    """The frozen RAG scope in manifest order: prior episodes and current setting-book revisions."""
    return tuple(source for source in sources
                 if source.kind == EPISODE or source.kind == SETTING_BOOK and source.metadata.get("currentRevision") is True)


@dataclass(frozen=True)
class FrozenChunk:
    """One ranked chunk of the frozen scope; `source_id` is the episode ID or the setting-book revision ID."""

    source_kind: str
    source_id: UUID
    chunk_id: UUID
    chunk_index: int
    text: str = field(repr=False)
    start_offset: int
    end_offset: int
    chunk_hash: str
    source_hash: str
    score: float


@dataclass(frozen=True)
class FrozenSearchResult:
    chunks: tuple[FrozenChunk, ...]
    readiness: SearchReadiness
    limited: bool = False


@dataclass(frozen=True)
class FrozenSearchScope(SearchScope):
    report_id: UUID = field(kw_only=True)
    baseline_hash: str = field(kw_only=True)
    target_hash: str = field(kw_only=True)
    job_id: UUID | None = field(default=None, kw_only=True)
    lease_token: UUID | None = field(default=None, kw_only=True, repr=False)
    generation: int | None = field(default=None, kw_only=True)
    input_digest: str | None = field(default=None, kw_only=True)

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.report_id, UUID) or any(
            not re.fullmatch(r"[a-f0-9]{64}", value)
            for value in (self.baseline_hash, self.target_hash)
        ):
            raise ValueError("Trusted frozen search context required")


REPORT_FROM = """
FROM validation_reports r
JOIN works rw ON rw.id=r.work_id JOIN members rm ON rm.id=rw.member_id
JOIN episodes target ON target.id=r.episode_id AND target.work_id=r.work_id
WHERE r.id=:report AND r.work_id=:work AND r.member_id=:member AND rw.member_id=:member
 AND rw.lifecycle_status='ACTIVE' AND rm.status='ACTIVE'
 AND r.is_current=true AND target.content_revision=r.source_content_revision
 AND ((CAST(:job AS uuid) IS NULL AND NOT EXISTS (SELECT 1 FROM validation_jobs j WHERE j.report_id=r.id))
      OR EXISTS (SELECT 1 FROM validation_jobs j WHERE j.report_id=r.id AND j.id=CAST(:job AS uuid)
          AND j.status='RUNNING' AND j.lease_token=CAST(:lease AS uuid) AND j.lease_expires_at>now()
          AND j.generation=:generation AND j.input_digest=:input_digest))
 AND r.baseline_hash=:baseline AND r.source_content_hash=:target_hash
 AND r.baseline_status='READY' AND r.status<>'INVALIDATED'
 AND target.content_hash=r.source_content_hash AND target.content_s3_key=r.source_content_s3_key
 AND target.content_s3_version IS NOT DISTINCT FROM r.source_content_s3_version
 AND target.episode_no=r.source_episode_no AND target.status<>'ARCHIVED'
 AND NOT EXISTS (SELECT 1 FROM episode_source_purge_requests p WHERE p.episode_id=target.id
     AND (p.retained_content_key IS NULL OR p.retained_content_key<>target.content_s3_key))
"""

FROZEN_FILTER = f"""
 AND EXISTS (SELECT 1 {REPORT_FROM}
 AND e.episode_no < r.source_episode_no
 AND EXISTS (SELECT 1 FROM validation_baseline_sources b WHERE b.report_id=r.id AND b.episode_id=e.id
  AND b.source_manifest->>'sourceKind'='EPISODE'
  AND b.source_manifest->>'episodeId'=e.id::text
  AND b.source_manifest->>'episodeNo'=e.episode_no::text
  AND COALESCE((b.source_manifest->>'contentRevision')::bigint,0)=e.content_revision
  AND b.source_manifest->>'contentHash'=s.source_hash
  AND b.source_manifest->>'contentS3Key'=s.source_key
  AND b.source_manifest->>'contentS3Version' IS NOT DISTINCT FROM s.source_version))
"""


# Setting-book chunks of one frozen, current revision (V114). A book edit or archive invalidates the report and
# deletes its baseline sources, so the manifest identity plus the running report fence is the authority here.
BOOK_FROM = f"""
FROM retrieval_setting_book_chunks c
JOIN retrieval_setting_book_sources s ON s.revision_id=c.revision_id
JOIN works w ON w.id=s.work_id
JOIN members m ON m.id=w.member_id
WHERE w.id=:work AND w.member_id=:member AND w.lifecycle_status='ACTIVE' AND m.status='ACTIVE'
 AND s.source_hash ~ '^[a-f0-9]{{64}}$' AND s.source_key <> ''
 AND s.chunk_policy=:policy
 AND c.start_offset >= 0 AND c.end_offset > c.start_offset
 AND char_length(c.chunk_text)=c.end_offset-c.start_offset
 AND encode(sha256(convert_to(c.chunk_text,'UTF8')),'hex')=c.chunk_hash
 AND (c.embedding IS NULL OR (c.embedding_model=:model AND c.embedding_version=:version))
 AND EXISTS (SELECT 1 {REPORT_FROM}
 AND EXISTS (SELECT 1 FROM validation_baseline_sources b WHERE b.report_id=r.id
  AND b.setting_book_revision_id=s.revision_id
  AND b.source_manifest->>'sourceKind'='SETTING_BOOK'
  AND b.source_manifest->>'currentRevision'='true'
  AND b.source_manifest->>'revisionId'=s.revision_id::text
  AND b.source_manifest->>'settingBookId'=s.setting_book_id::text
  AND b.source_manifest->>'contentHash'=s.source_hash
  AND b.source_manifest->>'contentS3Key'=s.source_key
  AND b.source_manifest->>'contentS3Version' IS NOT DISTINCT FROM s.source_version))
"""


class FrozenRetrievalRepository(RetrievalSearchRepository):
    from_clause = CURRENT_FROM.replace(
        "WHERE p.episode_id=e.id)",
        "WHERE p.episode_id=e.id AND (p.retained_content_key IS NULL OR p.retained_content_key<>e.content_s3_key))",
    ) + FROZEN_FILTER
    book_from_clause = BOOK_FROM
    # 검수 실행의 근거 검색은 별도 평가 전까지 기존 일치 단어 비율 순위를 유지한다.
    rarity_weighted_keyword = False

    def __init__(self, session, scope: FrozenSearchScope, spec):
        if not isinstance(scope, FrozenSearchScope):
            raise TypeError("Frozen search scope required")
        super().__init__(session, scope, spec)
        self.params.update(
            report=scope.report_id, baseline=scope.baseline_hash, target_hash=scope.target_hash,
            job=scope.job_id, lease=scope.lease_token, generation=scope.generation,
            input_digest=scope.input_digest
        )

    def readiness(self):
        if self.session.scalar(text(f"SELECT count(*) {REPORT_FROM}"), self.params) != 1:
            raise SearchScopeUnavailable()
        rows = (
            self.session.execute(
                text(f"""
            WITH valid AS MATERIALIZED (SELECT c.episode_id,c.status,c.embedding {self.from_clause})
            SELECT b.episode_id, s.chunk_count,
              count(v.episode_id) AS texts,
              count(v.episode_id) FILTER (WHERE v.status='READY' AND v.embedding IS NOT NULL) AS vectors
            FROM validation_baseline_sources b
            LEFT JOIN retrieval_sources s ON s.episode_id=b.episode_id
            LEFT JOIN valid v ON v.episode_id=b.episode_id
            WHERE b.report_id=:report AND b.episode_id IS NOT NULL
            GROUP BY b.episode_id,s.chunk_count
            """),
                self.params,
            )
            .mappings()
            .all()
        )
        # Only the current setting-book revisions marked at intake are in the RAG scope (V114 index).
        rows += (
            self.session.execute(
                text(f"""
            WITH valid AS MATERIALIZED (SELECT c.revision_id,c.status,c.embedding {self.book_from_clause})
            SELECT b.setting_book_revision_id, s.chunk_count,
              count(v.revision_id) AS texts,
              count(v.revision_id) FILTER (WHERE v.status='READY' AND v.embedding IS NOT NULL) AS vectors
            FROM validation_baseline_sources b
            LEFT JOIN retrieval_setting_book_sources s ON s.revision_id=b.setting_book_revision_id
            LEFT JOIN valid v ON v.revision_id=b.setting_book_revision_id
            WHERE b.report_id=:report AND b.setting_book_revision_id IS NOT NULL
              AND b.source_manifest->>'currentRevision'='true'
            GROUP BY b.setting_book_revision_id,s.chunk_count
            """),
                self.params,
            )
            .mappings()
            .all()
        )
        text_sources = sum(
            row["chunk_count"] is not None
            and row["chunk_count"] > 0
            and row["texts"] == row["chunk_count"]
            for row in rows
        )
        vector_sources = sum(
            row["chunk_count"] is not None
            and row["chunk_count"] > 0
            and row["vectors"] == row["chunk_count"]
            for row in rows
        )
        limitations = []
        if text_sources < len(rows):
            limitations.append("TEXT_INDEX_PARTIAL")
        if vector_sources < len(rows):
            limitations.append("VECTOR_INDEX_PARTIAL")
        return SearchReadiness(
            len(rows),
            text_sources,
            vector_sources,
            sum(row["texts"] for row in rows),
            sum(row["vectors"] for row in rows),
            tuple(limitations),
        )


    def frozen_vector(self, embedding, *, limit=20):
        """Exact top-k over the whole frozen scope: episode and current setting-book chunks ranked together.

        Every validity and frozen-manifest predicate applies inside each branch before the shared ORDER BY/LIMIT.
        Equal distances order episodes before setting books, then by episode number / revision, chunk and ID.
        """
        validate_limit(limit)
        if (
            len(embedding) != self.spec.dimensions
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in embedding)
            or not any(v != 0 for v in embedding)
        ):
            raise ValueError("Invalid query embedding")
        rows = self.session.execute(
            text(f"""
            SELECT * FROM (
              SELECT 'EPISODE' AS source_kind, 0 AS kind_order, c.episode_id AS source_id, e.episode_no AS source_order,
                c.id, c.chunk_index, c.chunk_text, c.start_offset, c.end_offset, c.chunk_hash, s.source_hash,
                c.embedding <=> CAST(:vector AS vector) AS distance
              {self.from_clause} AND c.id IN (SELECT id FROM retrieval_current_chunks)
               AND c.embedding_model=:model AND c.embedding_version=:version
              UNION ALL
              SELECT 'SETTING_BOOK' AS source_kind, 1 AS kind_order, c.revision_id AS source_id, 0 AS source_order,
                c.id, c.chunk_index, c.chunk_text, c.start_offset, c.end_offset, c.chunk_hash, s.source_hash,
                c.embedding <=> CAST(:vector AS vector) AS distance
              {self.book_from_clause} AND c.status='READY' AND c.embedding IS NOT NULL
               AND c.embedding_model=:model AND c.embedding_version=:version
            ) ranked
            ORDER BY distance, kind_order, source_order, source_id, chunk_index, id LIMIT :limit
            """),
            {**self.params, "vector": "[" + ",".join(str(float(v)) for v in embedding) + "]", "limit": limit},
        ).mappings()
        return [
            FrozenChunk(row["source_kind"], row["source_id"], row["id"], row["chunk_index"], row["chunk_text"],
                        row["start_offset"], row["end_offset"], row["chunk_hash"], row["source_hash"],
                        1.0 - float(row["distance"]))
            for row in rows
        ]


def vector_ready(readiness, embedding_client):
    """A VECTOR read can run: a query embedding client and every frozen RAG source (episodes and current
    setting-book revisions) fully embedded. A partial index is never offered as ready."""
    return (embedding_client is not None and readiness.vector_chunks > 0
            and readiness.vector_ready_sources == readiness.source_count)


class FrozenRetrievalSearchService:
    """Uses an explicitly injected read-only session factory; no default DB or embeddings."""

    def __init__(self, session_factory, spec=None, *, blocking_runner=None):
        self.sessions, self.spec = session_factory, spec or IndexSpec()
        self.blocking_runner = blocking_runner or asyncio.to_thread

    def _read(self, scope, action):
        with self.sessions() as session:
            return action(FrozenRetrievalRepository(session, scope, self.spec))

    @staticmethod
    def _scope(frozen):
        context = frozen.context
        return FrozenSearchScope(
            context.member_id,
            context.work_id,
            report_id=context.report_id,
            baseline_hash=context.baseline_hash,
            target_hash=context.target_hash, job_id=context.job_id, lease_token=context.lease_token,
            generation=context.generation, input_digest=context.input_digest,
        )

    async def readiness(self, frozen):
        """GH344: one read of the job's frozen index readiness, before VECTOR is offered to the model."""
        scope = self._scope(frozen)
        return await self.blocking_runner(lambda: self._read(scope, lambda repository: repository.readiness()))

    async def search(self, frozen, query, embedding_client, limit):
        """One VECTOR search over the job's frozen scope (grid v4: no keyword, direct or neighbor reads).

        Readiness is read (and the job authorized) before the query embedding is paid for and again after the
        ranking; the corpus checks that readiness covers every frozen RAG source before accepting any evidence.
        The Backend verifies every cited quote against the hash-verified full source at publication.
        """
        scope = self._scope(frozen)
        try:
            if (embedding_client is None or embedding_client.model != self.spec.model
                    or embedding_client.version != self.spec.version):
                raise ValueError("Query embedding client must match the retrieval index")
            readiness = await self.readiness(frozen)
            if readiness.vector_chunks == 0:
                return FrozenSearchResult((), readiness)
            embedding = await embedding_client.create_embedding(query)
            chunks = await self.blocking_runner(
                lambda: self._read(scope, lambda repository: repository.frozen_vector(embedding, limit=limit)))
            readiness = await self.readiness(frozen)
            return FrozenSearchResult(tuple(chunks), readiness, limited=len(chunks) >= limit)
        except IncompleteInspection:
            raise
        except SearchScopeUnavailable:
            raise IncompleteInspection("INPUT_CHANGED") from None
        except Exception:  # noqa: BLE001 - database/embedding boundary must not expose source or credentials.
            raise IncompleteInspection("SEARCH_FAILED") from None
