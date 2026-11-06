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
    # 키워드 순위를 단어 희귀도(완만한 BM25 IDF)로 가중한다. 하위 저장소가 별도 평가 전까지 기존
    # 일치 단어 비율 순위를 유지해야 하면 False로 둔다.
    rarity_weighted_keyword = True

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

    def keyword(self, query, *, limit=20, preferred_episode_ids=()):
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
        if self.rarity_weighted_keyword:
            return self._rarity_weighted_keyword(
                terms, matches, params, limit, preferred_episode_ids
            )
        score = " + ".join(f"CASE WHEN {match} THEN 1.0 ELSE 0.0 END" for match in matches)
        return self._rows(
            f"SELECT {FIELDS},({score})/{len(terms)} AS score {self.from_clause} "
            f"AND ({' OR '.join(matches)}) "
            "ORDER BY score DESC,(e.id=ANY(CAST(:preferred AS uuid[]))) DESC,"
            "e.episode_no,c.chunk_index,c.id LIMIT :limit",
            {**params, "limit": limit, "preferred": list(dict.fromkeys(preferred_episode_ids))},
            "KEYWORD",
        )

    def _rarity_weighted_keyword(self, terms, matches, params, limit, preferred_episode_ids):
        """일치한 단어의 희귀도 가중치 합을 전체 가중치 합으로 나눈 값으로 순위를 매긴다.

        일치 비율만 쓰면 1인칭 주인공 이름이나 '어떻게'·'처음'처럼 흔한 단어 여러 개가 겹친 무관한
        청크가 결정적인 단어('죽인')가 든 실제 장면보다 앞선다. 가중치는 같은 조회 범위 청크의 문서
        빈도로 계산한 BM25 IDF ln(1 + (N - df + 0.5) / (df + 0.5))에 ln(1 + x)를 한 번 더 씌운 값이며
        항상 양수다. 원문에 없는 활용형·바꿔 쓴 단어가 희귀하다는 이유만으로 실제 장면을 밀어내지
        않도록 가중치 차이를 완만하게 줄였다. 모든 단어가 일치하면 1.0이고 단어가 하나면 기존 순위와
        같다. 후보 조건(한 단어 이상 일치)과 동점 처리 순서는 기존과 같다.

        한 글자 단어('레벨 업'의 '업')는 다른 단어 안에서도 일치하므로('업적') 희귀하다는 이유로
        높은 무게를 받지 않게, 여러 글자 단어 가중치 중 가장 작은 값을 넘지 않게 한다.
        """
        count = range(len(matches))
        hits = ",".join(
            f"CASE WHEN {match} THEN 1 ELSE 0 END AS hit_{n}" for n, match in enumerate(matches)
        )
        frequencies = ",".join(
            f"CAST(coalesce(sum(hit_{n}),0) AS double precision) AS df_{n}" for n in count
        )
        raw = ",".join(f"ln(1.0+ln(1.0+(total-df_{n}+0.5)/(df_{n}+0.5))) AS r_{n}" for n in count)
        longer = [f"r_{n}" for n in count if len(terms[n]) > 1]
        weights = ",".join(
            f"LEAST(r_{n},{','.join(longer)}) AS w_{n}"
            if len(terms[n]) == 1 and longer
            else f"r_{n} AS w_{n}"
            for n in count
        )
        matched = " + ".join(f"h.hit_{n}*w.w_{n}" for n in count)
        total = " + ".join(f"w.w_{n}" for n in count)
        order = "score DESC,preferred DESC,episode_no,chunk_index,id"
        # 범위 안 모든 청크의 본문을 중간 결과에 쌓지 않도록 일치 표시만 계산하고, 상위 후보만 본문을 읽는다.
        return self._rows(
            f"WITH hits AS (SELECT c.id,c.episode_id AS hit_episode_id,e.episode_no AS hit_episode_no,"
            f"c.chunk_index AS hit_chunk_index,{hits} {self.from_clause}),"
            f"stats AS (SELECT CAST(count(*) AS double precision) AS total,{frequencies} FROM hits),"
            f"raw_weights AS (SELECT {raw} FROM stats),"
            f"weights AS (SELECT {weights} FROM raw_weights),"
            f"ranked AS (SELECT h.id,({matched})/({total}) AS score,"
            "(h.hit_episode_id=ANY(CAST(:preferred AS uuid[]))) AS preferred,"
            "h.hit_episode_no AS episode_no,h.hit_chunk_index AS chunk_index "
            f"FROM hits h CROSS JOIN weights w WHERE {' OR '.join(f'h.hit_{n}=1' for n in count)} "
            f"ORDER BY {order} LIMIT :limit) "
            f"SELECT {FIELDS},r.score FROM ranked r JOIN retrieval_chunks c ON c.id=r.id "
            "JOIN retrieval_sources s ON s.episode_id=c.episode_id "
            "JOIN episodes e ON e.id=s.episode_id AND e.work_id=s.work_id "
            "ORDER BY r.score DESC,r.preferred DESC,r.episode_no,r.chunk_index,r.id",
            {**params, "limit": limit, "preferred": list(dict.fromkeys(preferred_episode_ids))},
            "KEYWORD",
        )

    def keyword_ordered(self, required, optional, *, order, limit=20):
        """필수 표현을 모두, 선택 표현을 절반 이상 포함하는 청크를 출처 회차 순서로 읽는다.

        최초/최후 장면 탐색용이다. 인물명은 필수, 대상·사건 단어는 활용형 차이를 허용하기 위해
        절반 이상(최소 1개) 일치로 둔다. 점수는 반환 순서를 보존하는 1.0 이하 값이라 일반 keyword
        후보보다 먼저 배치된다.
        """
        validate_limit(limit)
        if order not in {"EARLIEST", "LATEST"}:
            raise ValueError("Ordered search direction must be EARLIEST or LATEST")
        required = [t for t in dict.fromkeys(str(a).strip() for a in required) if t][:3]
        optional = [
            t for t in dict.fromkeys(str(a).strip() for a in optional) if t and t not in required
        ][:4]
        validate_query(" ".join([*required, *optional]))

        def pattern(term):
            return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        params = {f"required_{n}": pattern(t) for n, t in enumerate(required)}
        params.update({f"optional_{n}": pattern(t) for n, t in enumerate(optional)})
        conditions = [f"c.chunk_text ILIKE :required_{n} ESCAPE '\\'" for n in range(len(required))]
        if optional:
            matched = " + ".join(
                f"CASE WHEN c.chunk_text ILIKE :optional_{n} ESCAPE '\\' THEN 1 ELSE 0 END"
                for n in range(len(optional))
            )
            conditions.append(f"({matched}) >= {max(1, (len(optional) + 1) // 2)}")
        direction = "ASC" if order == "EARLIEST" else "DESC"
        ordering = f"e.episode_no {direction},c.chunk_index {direction},c.id"
        return self._rows(
            f"SELECT {FIELDS},CAST(1.0-(ROW_NUMBER() OVER (ORDER BY {ordering})-1)/10000.0 "
            f"AS double precision) AS score {self.from_clause} AND {' AND '.join(conditions)} "
            f"ORDER BY {ordering} LIMIT :limit",
            {**params, "limit": limit},
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

    def neighbors(self, seeds, *, radius=1, limit=20):
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
            f"WITH valid AS MATERIALIZED (SELECT {FIELDS} {self.from_clause}), "
            "neighbors AS (SELECT DISTINCT ON (v.id) v.*,"
            "array_position(CAST(:ids AS uuid[]),seed.id) AS seed_rank,"
            "abs(v.chunk_index-seed.chunk_index) AS distance "
            "FROM valid v JOIN valid seed ON seed.episode_id=v.episode_id "
            "AND abs(v.chunk_index-seed.chunk_index)<=:radius "
            "WHERE seed.id=ANY(CAST(:ids AS uuid[])) "
            "AND NOT (v.id=ANY(CAST(:ids AS uuid[]))) "
            "ORDER BY v.id,seed_rank,distance) "
            "SELECT neighbors.*,1.0/(1+distance) AS score FROM neighbors "
            "ORDER BY seed_rank,distance,episode_no,chunk_index,id LIMIT :limit",
            dict(ids=list(dict.fromkeys(c.chunk_id for c in current)), radius=radius, limit=limit),
            "NEIGHBOR",
        )

    def episodes(self, episode_nos, *, limit=100):
        """지정한 출처 회차의 현재 유효한 조각을 회차·본문 순서대로 읽는다(회차 통째 읽기).

        다른 조회와 같은 CURRENT_FROM 조건(작품·회원 권한, 보관·파기, 원문 일치, 허용 회차 범위)을
        순서·개수 제한 전에 적용한다. 범위 밖이거나 없는 회차 번호는 결과가 비어 있을 뿐 오류가 아니다.
        limit에서 잘리면 뒤쪽 회차가 빠지므로 호출자는 반환 개수로 잘림 여부를 확인한다.
        """
        validate_limit(limit)
        numbers = list(dict.fromkeys(episode_nos))
        if (
            not numbers
            or len(numbers) > 10
            or any(
                isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 2147483647
                for n in numbers
            )
        ):
            raise ValueError("Episode read needs 1 to 10 positive episode numbers")
        return self._rows(
            f"SELECT {FIELDS},1.0 AS score {self.from_clause} "
            "AND e.episode_no=ANY(CAST(:episodes AS integer[])) "
            "ORDER BY e.episode_no,c.chunk_index,c.id LIMIT :limit",
            dict(episodes=numbers, limit=limit),
            "EPISODE",
        )

    def keyword_episodes(self, query):
        """검색어의 모든 단어가 함께 든 유효 조각 수를 출처 회차별로 센다. 본문은 읽지 않는다.

        keyword와 같은 단어 분리·최대 12단어·리터럴 이스케이프를 쓰고 같은 CURRENT_FROM 조건을 적용한다.
        결과는 회차 번호 오름차순이며 행 수는 허용 범위의 회차 수를 넘지 않는다.
        """
        validate_query(query)
        terms = list(dict.fromkeys(query.strip().split()))[:12]
        params = {
            f"pattern_{n}": "%"
            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
            for n, term in enumerate(terms)
        }
        matches = [f"c.chunk_text ILIKE :pattern_{n} ESCAPE '\\'" for n in range(len(terms))]
        rows = self.session.execute(
            text(
                f"SELECT e.episode_no AS episode_no,count(*) AS matched {self.from_clause} "
                f"AND {' AND '.join(matches)} GROUP BY e.episode_no ORDER BY e.episode_no"
            ),
            {**self.params, **params},
        ).mappings()
        return [(row["episode_no"], row["matched"]) for row in rows]

    def episode_span(self):
        """현재 범위에서 읽을 수 있는 출처 회차의 (최소, 최대, 개수). 본문은 읽지 않는다."""
        row = (
            self.session.execute(
                text(
                    "SELECT min(e.episode_no) AS lo,max(e.episode_no) AS hi,"
                    f"count(DISTINCT e.id) AS total {self.from_clause}"
                ),
                self.params,
            )
            .mappings()
            .one()
        )
        return row["lo"], row["hi"], row["total"]

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
