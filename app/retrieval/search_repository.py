"""Current-corpus SQL search. No provider, source text I/O, or writes are performed here."""

import math

from sqlalchemy import text

from app.retrieval.contracts import IndexSpec
from app.retrieval.search_contracts import (
    ChunkEvidence,
    SearchReadiness,
    SearchScope,
    SearchScopeUnavailable,
    validate_ids,
    validate_limit,
    validate_query,
)

# Shared predicates apply BEFORE ranking/limit to lexical, ID, neighbor and vector reads.
# sha256(bytea) is a PostgreSQL built-in, so this needs no pgcrypto extension.
CURRENT_FROM = """
FROM retrieval_chunks c
JOIN retrieval_sources s ON s.episode_id=c.episode_id
JOIN episodes e ON e.id=s.episode_id AND e.work_id=s.work_id
JOIN works w ON w.id=s.work_id
JOIN members m ON m.id=w.member_id
WHERE w.id=:work AND w.member_id=:member
 AND w.lifecycle_status='ACTIVE' AND m.status='ACTIVE' AND e.status <> 'ARCHIVED'
 AND e.episode_no BETWEEN :lo AND :hi
 AND e.content_s3_key=s.source_key AND e.content_hash=s.source_hash
 AND e.content_s3_version IS NOT DISTINCT FROM s.source_version
 AND s.source_hash ~ '^[a-f0-9]{64}$' AND s.source_key <> ''
 AND NOT EXISTS (SELECT 1 FROM episode_source_purge_requests p WHERE p.episode_id=e.id)
 AND s.chunk_policy=:policy
 AND c.start_offset >= 0 AND c.end_offset > c.start_offset
 AND char_length(c.chunk_text)=c.end_offset-c.start_offset
 AND encode(sha256(convert_to(c.chunk_text,'UTF8')),'hex')=c.chunk_hash
 AND (c.embedding IS NULL OR (c.embedding_model=:model AND c.embedding_version=:version))
"""
FIELDS = """c.id,c.episode_id,e.episode_no,c.chunk_index,c.chunk_text,c.start_offset,c.end_offset,
c.chunk_hash,s.source_key,s.source_version,s.source_hash,s.chunk_policy"""


class RetrievalSearchRepository:
    from_clause = CURRENT_FROM

    def __init__(self, session, scope: SearchScope, spec: IndexSpec):
        self.session, self.scope, self.spec = session, scope, spec
        self.params = dict(
            work=scope.work_id,
            member=scope.member_id,
            lo=scope.episode_from,
            hi=scope.episode_to,
            policy=spec.policy,
            model=spec.model,
            version=spec.version,
        )

    def _rows(self, sql, params, method):
        return [
            self._evidence(row, method)
            for row in self.session.execute(text(sql), {**self.params, **params}).mappings()
        ]

    @staticmethod
    def _evidence(row, method):
        return ChunkEvidence(
            chunk_id=row["id"],
            episode_id=row["episode_id"],
            episode_no=row["episode_no"],
            chunk_index=row["chunk_index"],
            text=row["chunk_text"],
            start_offset=row["start_offset"],
            end_offset=row["end_offset"],
            chunk_hash=row["chunk_hash"],
            source_key=row["source_key"],
            source_version=row["source_version"],
            source_hash=row["source_hash"],
            policy=row["chunk_policy"],
            methods=(method,),
            score=float(row["score"]),
        )

    def direct(self, chunk_ids, *, limit=20):
        validate_ids(chunk_ids)
        validate_limit(limit)
        if not chunk_ids:
            return []
        return self._rows(
            f"SELECT {FIELDS},1.0 AS score {self.from_clause} "
            "AND c.id=ANY(CAST(:ids AS uuid[])) "
            "ORDER BY array_position(CAST(:ids AS uuid[]),c.id),c.id LIMIT :limit",
            dict(ids=list(dict.fromkeys(chunk_ids)), limit=limit),
            "DIRECT",
        )

    def keyword(self, query, *, limit=20, preferred_episode_ids=(), cover_terms=False):
        validate_query(query)
        validate_limit(limit)
        validate_ids(preferred_episode_ids)
        # Bind and literal-escape every term; %, _ and backslash are never wildcards.
        terms = list(dict.fromkeys(query.strip().split()))[:12]
        params = {
            f"pattern_{n}": "%"
            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
            for n, term in enumerate(terms)
        }
        matches = [f"c.chunk_text ILIKE :pattern_{n} ESCAPE '\\'" for n in range(len(terms))]
        score = " + ".join(f"CASE WHEN {match} THEN 1.0 ELSE 0.0 END" for match in matches)
        if cover_terms:
            # A long list can otherwise exhaust top-k on one facet: two common
            # terms outrank the only passage containing a separately requested term.
            # Rank on the authorized corpus once, then reserve each term's best hit.
            # This remains a bounded search, not a claim of exhaustive coverage.
            hits = ",".join(f"CASE WHEN {match} THEN {n} END" for n, match in enumerate(matches))
            return self._rows(
                f"WITH scored AS MATERIALIZED (SELECT {FIELDS},"
                f"({score})/{len(terms)} AS score,"
                f"array_remove(ARRAY[{hits}],NULL) AS terms,"
                "(e.id=ANY(CAST(:preferred AS uuid[]))) AS preferred "
                f"{self.from_clause} AND ({' OR '.join(matches)})), "
                "ranked AS (SELECT *,max(score) OVER (PARTITION BY episode_id) AS episode_score "
                "FROM scored), winners AS (SELECT DISTINCT ON (term) id FROM ranked "
                "CROSS JOIN LATERAL unnest(terms) AS term "
                "ORDER BY term,score DESC,preferred DESC,episode_score DESC,episode_no,chunk_index,id) "
                "SELECT * FROM ranked ORDER BY (id IN (SELECT id FROM winners)) DESC,"
                "score DESC,preferred DESC,episode_score DESC,episode_no,chunk_index,id LIMIT :limit",
                {**params, "limit": limit, "preferred": list(dict.fromkeys(preferred_episode_ids))},
                "KEYWORD",
            )
        return self._rows(
            f"SELECT {FIELDS},({score})/{len(terms)} AS score {self.from_clause} "
            f"AND ({' OR '.join(matches)}) "
            "ORDER BY score DESC,(e.id=ANY(CAST(:preferred AS uuid[]))) DESC,"
            "e.episode_no,c.chunk_index,c.id LIMIT :limit",
            {**params, "limit": limit, "preferred": list(dict.fromkeys(preferred_episode_ids))},
            "KEYWORD",
        )

    def vector(self, embedding, *, limit=20, excluded_chunk_ids=()):
        validate_limit(limit)
        validate_ids(excluded_chunk_ids)
        if (
            len(embedding) != self.spec.dimensions
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in embedding
            )
            or not any(v != 0 for v in embedding)
        ):
            raise ValueError("Invalid query embedding")
        # Exact ranking of the whole authorized corpus. No pre-ranking candidate LIMIT.
        # The current view applies lifecycle fencing as well as READY/vector availability.
        return self._rows(
            f"SELECT {FIELDS},1.0-(c.embedding <=> CAST(:vector AS vector)) AS score "
            f"{self.from_clause} AND c.id IN (SELECT id FROM retrieval_current_chunks) "
            "AND c.embedding_model=:model AND c.embedding_version=:version "
            "AND NOT (c.id=ANY(CAST(:excluded AS uuid[]))) "
            "ORDER BY c.embedding <=> CAST(:vector AS vector),e.episode_no,c.chunk_index,c.id "
            "LIMIT :limit",
            dict(
                vector="[" + ",".join(str(float(v)) for v in embedding) + "]",
                excluded=list(excluded_chunk_ids),
                limit=limit,
            ),
            "VECTOR",
        )

    def neighbors(self, seeds, *, radius=1, limit=20, include_episode_edges=False):
        validate_limit(limit)
        validate_ids([seed.chunk_id for seed in seeds])
        if isinstance(radius, bool) or not isinstance(radius, int) or not 0 <= radius <= 3:
            raise ValueError("Neighbor radius must be between 0 and 3")
        current = self.revalidate(seeds)
        if not current:
            return []
        # Preserve the caller's ranked, revalidated seeds. Otherwise all radius-1
        # neighbors tie on score and early episodes exhaust the expansion budget.
        # Pick each neighbor's earliest supporting seed before applying the limit.
        return self._rows(
            f"WITH valid AS MATERIALIZED (SELECT {FIELDS},s.chunk_count AS source_chunk_count {self.from_clause}), "
            "neighbors AS (SELECT DISTINCT ON (v.id) v.*,"
            "array_position(CAST(:ids AS uuid[]),seed.id) AS seed_rank,"
            "CASE WHEN v.episode_id=seed.episode_id THEN abs(v.chunk_index-seed.chunk_index) "
            "WHEN v.episode_no>seed.episode_no THEN seed.source_chunk_count-seed.chunk_index+v.chunk_index "
            "ELSE seed.chunk_index+v.source_chunk_count-v.chunk_index END AS distance "
            "FROM valid v JOIN valid seed ON (seed.episode_id=v.episode_id "
            "AND abs(v.chunk_index-seed.chunk_index)<=:radius) "
            "OR (:edges AND :radius>0 "
            "AND seed.chunk_index>=0 AND seed.chunk_index<seed.source_chunk_count "
            "AND v.chunk_index>=0 AND v.chunk_index<v.source_chunk_count AND ("
            "(v.episode_no=seed.episode_no+1 AND seed.source_chunk_count-seed.chunk_index+v.chunk_index<=:radius) OR "
            "(v.episode_no=seed.episode_no-1 AND seed.chunk_index+v.source_chunk_count-v.chunk_index<=:radius))) "
            "WHERE seed.id=ANY(CAST(:ids AS uuid[])) "
            "AND NOT (v.id=ANY(CAST(:ids AS uuid[]))) "
            "ORDER BY v.id,seed_rank,distance) "
            "SELECT neighbors.*,1.0/(1+distance) AS score FROM neighbors "
            "ORDER BY seed_rank,distance,episode_no,chunk_index,id LIMIT :limit",
            dict(
                ids=list(dict.fromkeys(c.chunk_id for c in current)),
                radius=radius,
                limit=limit,
                edges=include_episode_edges,
            ),
            "NEIGHBOR",
        )

    def revalidate(self, chunks):
        validate_ids([chunk.chunk_id for chunk in chunks])
        if not chunks:
            return []
        current = {c.chunk_id: c for c in self.direct([c.chunk_id for c in chunks], limit=100)}
        return [
            chunk
            for chunk in chunks
            if chunk.chunk_id in current
            and chunk.fingerprint == current[chunk.chunk_id].fingerprint
        ]

    def readiness(self):
        active = self.session.scalar(
            text(
                "SELECT count(*) FROM works w JOIN members m ON m.id=w.member_id "
                "WHERE w.id=:work AND w.member_id=:member "
                "AND w.lifecycle_status='ACTIVE' AND m.status='ACTIVE'"
            ),
            self.params,
        )
        if active != 1:
            raise SearchScopeUnavailable()
        row = (
            self.session.execute(
                text(
                    f"WITH valid AS MATERIALIZED (SELECT c.episode_id,c.status,c.embedding {self.from_clause}), "
                    "counts AS (SELECT episode_id,count(*) AS texts,"
                    "count(*) FILTER (WHERE status='READY' AND embedding IS NOT NULL) AS vectors "
                    "FROM valid GROUP BY episode_id) "
                    "SELECT count(*) AS sources,"
                    "count(*) FILTER (WHERE s.chunk_count>0 AND counts.texts=s.chunk_count) AS text_sources,"
                    "count(*) FILTER (WHERE s.chunk_count>0 AND counts.vectors=s.chunk_count) AS vector_sources,"
                    "coalesce(sum(counts.texts),0) AS text_chunks,coalesce(sum(counts.vectors),0) AS vector_chunks "
                    "FROM episodes e LEFT JOIN retrieval_sources s ON s.episode_id=e.id "
                    "LEFT JOIN counts ON counts.episode_id=e.id "
                    "WHERE e.work_id=:work AND e.episode_no BETWEEN :lo AND :hi AND e.status<>'ARCHIVED'"
                ),
                self.params,
            )
            .mappings()
            .one()
        )
        limitations = []
        if row["sources"] == 0:
            limitations.append("NO_SUPPORTED_SOURCES")
        if row["text_sources"] < row["sources"]:
            limitations.append("TEXT_INDEX_PARTIAL")
        if row["vector_sources"] < row["sources"]:
            limitations.append("VECTOR_INDEX_PARTIAL")
        return SearchReadiness(
            source_count=row["sources"],
            text_ready_sources=row["text_sources"],
            vector_ready_sources=row["vector_sources"],
            text_chunks=row["text_chunks"],
            vector_chunks=row["vector_chunks"],
            limitations=tuple(limitations),
        )


class CurrentRetrievalVectorAdapter:
    """Repository adapter for the existing vector service with trusted scope bound once."""

    def __init__(self, repository):
        self.repository = repository

    def search_similar_chunks(
        self,
        *,
        query_embedding,
        work_id,
        embedding_model,
        embedding_version,
        top_k,
        episode_no_from=None,
        episode_no_to=None,
        excluded_chunk_ids=None,
    ):
        scope, spec = self.repository.scope, self.repository.spec
        if (
            work_id != scope.work_id
            or embedding_model != spec.model
            or embedding_version != spec.version
            or episode_no_from != scope.episode_from
            or episode_no_to != scope.episode_to
        ):
            raise ValueError("Vector adapter scope mismatch")
        return self.repository.vector(
            query_embedding, limit=top_k, excluded_chunk_ids=excluded_chunk_ids or ()
        )
