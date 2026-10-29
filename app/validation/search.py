"""Exact frozen-manifest predicates apply before every rank/LIMIT/neighbor expansion."""

import asyncio
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text

from app.retrieval.contracts import IndexSpec
from app.retrieval.search import RetrievalSearchService
from app.retrieval.search_contracts import SearchReadiness, SearchScope, SearchScopeUnavailable
from app.retrieval.search_repository import CURRENT_FROM, RetrievalSearchRepository
from app.validation.contracts import IncompleteInspection


@dataclass(frozen=True)
class FrozenSearchScope(SearchScope):
    report_id: UUID = field(kw_only=True)
    baseline_hash: str = field(kw_only=True)
    target_hash: str = field(kw_only=True)

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
 AND r.baseline_hash=:baseline AND r.source_content_hash=:target_hash
 AND r.baseline_status='READY' AND r.status<>'INVALIDATED'
 AND r.baseline_seed IS NOT NULL AND r.baseline_entries IS NOT NULL
 AND target.content_hash=r.source_content_hash AND target.content_s3_key=r.source_content_s3_key
 AND target.content_s3_version IS NOT DISTINCT FROM r.source_content_s3_version
 AND target.episode_no=r.source_episode_no AND target.status<>'ARCHIVED'
 AND NOT EXISTS (SELECT 1 FROM episode_source_purge_requests p WHERE p.episode_id=target.id)
"""

FROZEN_FILTER = f"""
 AND EXISTS (SELECT 1 {REPORT_FROM}
 AND e.episode_no < r.source_episode_no
 AND EXISTS (SELECT 1 FROM validation_baseline_sources b WHERE b.report_id=r.id AND b.episode_id=e.id
  AND b.source_manifest->>'sourceKind'='EPISODE'
  AND b.source_manifest->>'episodeId'=e.id::text
  AND b.source_manifest->>'episodeNo'=e.episode_no::text
  AND b.source_manifest->>'contentHash'=s.source_hash
  AND b.source_manifest->>'contentS3Key'=s.source_key
  AND b.source_manifest->>'contentS3Version' IS NOT DISTINCT FROM s.source_version))
"""


class FrozenRetrievalRepository(RetrievalSearchRepository):
    from_clause = CURRENT_FROM + FROZEN_FILTER

    def __init__(self, session, scope: FrozenSearchScope, spec):
        if not isinstance(scope, FrozenSearchScope):
            raise TypeError("Frozen search scope required")
        super().__init__(session, scope, spec)
        self.params.update(
            report=scope.report_id, baseline=scope.baseline_hash, target_hash=scope.target_hash
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

    def indexed_episode_ids(self):
        self.readiness()
        return set(
            self.session.scalars(
                text(f"""
            WITH valid AS MATERIALIZED (SELECT c.episode_id {self.from_clause})
            SELECT s.episode_id FROM retrieval_sources s JOIN valid v ON v.episode_id=s.episode_id
            GROUP BY s.episode_id,s.chunk_count HAVING s.chunk_count>0 AND count(*)=s.chunk_count
            """),
                self.params,
            )
        )


class FrozenRetrievalSearchService:
    """Uses an explicitly injected read-only session factory; no default DB or embeddings."""

    def __init__(self, session_factory, spec=None):
        self.sessions, self.spec = session_factory, spec or IndexSpec()
        self.service = RetrievalSearchService(
            session_factory, self.spec, repository_factory=FrozenRetrievalRepository
        )

    async def search(self, frozen, intent, embedding_client, limit):
        context = frozen.context
        scope = FrozenSearchScope(
            context.member_id,
            context.work_id,
            report_id=context.report_id,
            baseline_hash=context.baseline_hash,
            target_hash=context.target_hash,
        )
        try:
            if intent.operation == "KEYWORD":
                result = await self.service.keyword(scope, intent.query, limit=limit)
            else:
                readiness = await self.service.readiness(scope)
                if readiness.vector_chunks == 0 or embedding_client is None:
                    raise IncompleteInspection("VECTOR_NOT_READY")
                result = await self.service.vector(
                    scope, intent.query, embedding_client, limit=limit
                )

            def indexed():
                with self.sessions() as session:
                    return FrozenRetrievalRepository(
                        session, scope, self.spec
                    ).indexed_episode_ids()

            ids = await asyncio.to_thread(indexed)
            refs = {
                source.ref
                for source in frozen.sources
                if source.kind == "EPISODE" and UUID(source.metadata["episodeId"]) in ids
            }
            return result, refs
        except IncompleteInspection:
            raise
        except SearchScopeUnavailable:
            raise IncompleteInspection("INPUT_CHANGED") from None
        except Exception:  # noqa: BLE001 - database/embedding boundary must not expose source or credentials.
            raise IncompleteInspection("SEARCH_FAILED") from None
