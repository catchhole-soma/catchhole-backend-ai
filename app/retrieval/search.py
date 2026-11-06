"""Bounded asynchronous facade; embeddings must be injected by the metered caller."""

import asyncio

from app.embeddings.services.episode_chunk_vector_search import EpisodeChunkVectorSearchService
from app.retrieval.contracts import IndexSpec
from app.retrieval.search_contracts import (
    SearchResult,
    validate_ids,
    validate_limit,
    validate_query,
)
from app.retrieval.search_repository import (
    CurrentRetrievalVectorAdapter,
    RetrievalSearchRepository,
)


class RetrievalSearchService:
    def __init__(
        self,
        session_factory,
        spec=None,
        *,
        blocking_runner=None,
        repository_factory=RetrievalSearchRepository,
    ):
        self.sessions = session_factory
        self.spec = spec or IndexSpec()
        self.blocking_runner = blocking_runner or asyncio.to_thread
        self.repository_factory = repository_factory

    def _read(self, scope, method, *args, limit=20, **kwargs):
        with self.sessions() as session:
            repository = self.repository_factory(session, scope, self.spec)
            readiness = repository.readiness()
            chunks = tuple(getattr(repository, method)(*args, limit=limit, **kwargs))
            return SearchResult(chunks, readiness, limited=len(chunks) >= limit)

    async def keyword(self, scope, query, limit=20, preferred_episode_ids=()):
        validate_query(query)
        validate_limit(limit)
        validate_ids(preferred_episode_ids)
        result = await self.blocking_runner(
            lambda: self._read(
                scope, "keyword", query, limit=limit, preferred_episode_ids=preferred_episode_ids
            )
        )
        if len(set(query.strip().split())) > 12:
            return SearchResult(result.chunks, result.readiness, limited=True)
        return result

    async def direct(self, scope, chunk_ids, limit=20):
        validate_ids(chunk_ids)
        validate_limit(limit)
        return await self.blocking_runner(
            lambda: self._read(scope, "direct", chunk_ids, limit=limit)
        )

    async def neighbors(self, scope, seeds, radius=1, limit=20):
        validate_limit(limit)
        return await self.blocking_runner(
            lambda: self._read(scope, "neighbors", seeds, radius=radius, limit=limit)
        )

    async def keyword_explore(self, scope, query, *, limit=20, order="RELEVANCE"):
        """원문 탐색용 키워드 조회 한 번: 조각(관련도 또는 회차 순서)과 회차별 일치 분포.

        RELEVANCE는 기존 keyword와 같은 순위다. EARLIEST/LATEST는 앞의 세 단어를 필수, 그다음 네 단어를
        선택 표현으로 하는 기존 keyword_ordered를 쓴다. 분포(episode_hits)는 검색어의 모든 단어가 함께 든
        조각 수를 본문 없이 (회차, 개수)로 돌려준다. 세 조회는 같은 범위·유효성 조건을 쓴다.
        """
        validate_query(query)
        validate_limit(limit)
        if order not in {"RELEVANCE", "EARLIEST", "LATEST"}:
            raise ValueError("Unknown exploration search order")
        words = list(dict.fromkeys(query.strip().split()))

        def read():
            with self.sessions() as session:
                repository = self.repository_factory(session, scope, self.spec)
                readiness = repository.readiness()
                if order == "RELEVANCE":
                    chunks = tuple(repository.keyword(query, limit=limit))
                else:
                    chunks = tuple(
                        repository.keyword_ordered(words[:3], words[3:7], order=order, limit=limit)
                    )
                return SearchResult(
                    chunks,
                    readiness,
                    limited=len(chunks) >= limit,
                    episode_hits=tuple(repository.keyword_episodes(query)),
                )

        return await self.blocking_runner(read)

    async def episodes(self, scope, episode_nos, limit=100):
        """회차 통째 읽기. 권한·유효성·회차 범위는 저장소의 공통 조건이 적용한다."""
        validate_limit(limit)
        return await self.blocking_runner(
            lambda: self._read(scope, "episodes", list(episode_nos), limit=limit)
        )

    async def episode_span(self, scope):
        """현재 범위에서 읽을 수 있는 출처 회차의 (최소, 최대, 개수)."""

        def read():
            with self.sessions() as session:
                repository = self.repository_factory(session, scope, self.spec)
                repository.readiness()  # revoked work/member is not silently an empty range
                return repository.episode_span()

        return await self.blocking_runner(read)

    async def readiness(self, scope):
        def read():
            with self.sessions() as session:
                return self.repository_factory(session, scope, self.spec).readiness()

        return await self.blocking_runner(read)

    async def revalidate(self, scope, chunks):
        def read():
            with self.sessions() as session:
                repository = self.repository_factory(session, scope, self.spec)
                repository.readiness()  # revoked work/member is not silently an empty result
                return tuple(repository.revalidate(chunks))

        return await self.blocking_runner(read)

    async def keyword_ordered(self, scope, required, optional, *, order, limit=20):
        validate_query(" ".join(str(a) for a in [*required, *optional]))
        validate_limit(limit)
        return await self.blocking_runner(
            lambda: self._read(
                scope, "keyword_ordered", required, optional, order=order, limit=limit
            )
        )

    async def vector(self, scope, query, embedding_client, limit=20, excluded_chunk_ids=()):
        validate_query(query)
        validate_limit(limit)
        validate_ids(excluded_chunk_ids)
        if (
            not embedding_client
            or embedding_client.model != self.spec.model
            or embedding_client.version != self.spec.version
        ):
            raise ValueError("Query embedding client must match the retrieval index")
        # Authorize and inspect before incurring query embedding cost. No default provider.
        readiness = await self.readiness(scope)
        if readiness.vector_chunks == 0:
            return SearchResult((), readiness)
        service = EpisodeChunkVectorSearchService(
            session_factory=self.sessions,
            embedding_client=embedding_client,
            repository_factory=lambda session: CurrentRetrievalVectorAdapter(
                self.repository_factory(session, scope, self.spec)
            ),
            blocking_runner=self.blocking_runner,
        )
        chunks = await service.search_similar_chunks(
            query_text=query,
            work_id=scope.work_id,
            top_k=limit,
            episode_no_from=scope.episode_from,
            episode_no_to=scope.episode_to,
            excluded_chunk_ids=list(excluded_chunk_ids),
        )
        current = await self.revalidate(scope, chunks)
        readiness = await self.readiness(scope)
        return SearchResult(
            current, readiness, limited=len(chunks) >= limit or len(current) < len(chunks)
        )
