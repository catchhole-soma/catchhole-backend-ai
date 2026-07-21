from uuid import UUID

import pytest

from app.embeddings.services.episode_chunk_vector_search import (
    EpisodeChunkVectorSearchService,
)
from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import EpisodeChunkSearchResult

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
EXCLUDED_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000002")
FOUND_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000003")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000004")


def test_search_similar_chunks_embeds_query_and_passes_all_conditions_to_repository() -> None:
    # 검색 문장을 먼저 임베딩하고 현재 모델·버전 및 범위를 Repository에 전달하는지 확인한다.
    embedding_client = FakeQueryEmbeddingClient(query_embedding=[0.1, 0.2])
    expected_results = [
        EpisodeChunkSearchResult(
            chunk=_chunk(),
            episode_no=5,
            similarity=0.91,
        )
    ]
    repository = FakeEpisodeChunkRepository(results=expected_results)
    session = FakeSession()
    service = EpisodeChunkVectorSearchService(
        session_factory=lambda: session,
        embedding_client=embedding_client,
        repository_factory=lambda current_session: _repository(
            current_session, session, repository
        ),
    )

    results = service.search_similar_chunks(
        query_text="주인공의 나이가 갑자기 어려졌다.",
        work_id=WORK_ID,
        top_k=5,
        episode_no_from=1,
        episode_no_to=10,
        excluded_chunk_ids=[EXCLUDED_CHUNK_ID],
    )

    assert results == expected_results
    assert embedding_client.requested_texts == ["주인공의 나이가 갑자기 어려졌다."]
    assert repository.search_requests == [
        {
            "query_embedding": [0.1, 0.2],
            "work_id": WORK_ID,
            "embedding_model": "text-embedding-3-small",
            "embedding_version": "v1",
            "top_k": 5,
            "episode_no_from": 1,
            "episode_no_to": 10,
            "excluded_chunk_ids": [EXCLUDED_CHUNK_ID],
        }
    ]
    assert session.entered is True
    assert session.exited is True


@pytest.mark.parametrize(
    ("query_text", "top_k", "episode_no_from", "episode_no_to", "message"),
    [
        ("   ", 5, None, None, "Query text must not be blank"),
        ("검색 문장", 0, None, None, "Top-K must be greater than zero"),
        ("검색 문장", 5, 10, 3, "Episode number range is invalid"),
    ],
)
def test_search_similar_chunks_rejects_invalid_conditions_before_external_calls(
    query_text: str,
    top_k: int,
    episode_no_from: int | None,
    episode_no_to: int | None,
    message: str,
) -> None:
    # 입력이 잘못됐다면 임베딩 비용과 DB 연결이 모두 발생하지 않는지 확인한다.
    embedding_client = FakeQueryEmbeddingClient(query_embedding=[0.1, 0.2])
    session_factory_call_count = 0

    def session_factory() -> FakeSession:
        nonlocal session_factory_call_count
        session_factory_call_count += 1
        return FakeSession()

    service = EpisodeChunkVectorSearchService(
        session_factory=session_factory,
        embedding_client=embedding_client,
    )

    with pytest.raises(ValueError, match=message):
        service.search_similar_chunks(
            query_text=query_text,
            work_id=WORK_ID,
            top_k=top_k,
            episode_no_from=episode_no_from,
            episode_no_to=episode_no_to,
        )

    assert embedding_client.requested_texts == []
    assert session_factory_call_count == 0


def test_search_similar_chunks_does_not_open_database_session_when_embedding_fails() -> None:
    # 외부 임베딩 API가 실패하면 아직 열지 않은 DB 세션 없이 예외를 그대로 전달하는지 검증한다.
    embedding_client = FakeQueryEmbeddingClient(error=RuntimeError("embedding API failed"))
    session_factory_call_count = 0

    def session_factory() -> FakeSession:
        nonlocal session_factory_call_count
        session_factory_call_count += 1
        return FakeSession()

    service = EpisodeChunkVectorSearchService(
        session_factory=session_factory,
        embedding_client=embedding_client,
    )

    with pytest.raises(RuntimeError, match="embedding API failed"):
        service.search_similar_chunks(
            query_text="검색 문장",
            work_id=WORK_ID,
            top_k=5,
        )

    assert embedding_client.requested_texts == ["검색 문장"]
    assert session_factory_call_count == 0


class FakeQueryEmbeddingClient:
    # 실제 OpenAI 대신 요청 문장을 기록하고 준비된 query vector를 반환한다.
    model = "text-embedding-3-small"
    version = "v1"

    def __init__(
        self,
        query_embedding: list[float] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.query_embedding = query_embedding
        self.error = error
        self.requested_texts: list[str] = []

    def create_embedding(self, text: str) -> list[float]:
        self.requested_texts.append(text)
        if self.error is not None:
            raise self.error
        assert self.query_embedding is not None
        return self.query_embedding


class FakeEpisodeChunkRepository:
    # 실제 pgvector 조회 대신 Service가 전달한 검색 조건을 기록한다.
    def __init__(self, results: list[EpisodeChunkSearchResult]) -> None:
        self.results = results
        self.search_requests: list[dict] = []

    def search_similar_chunks(self, **search_conditions) -> list[EpisodeChunkSearchResult]:
        self.search_requests.append(search_conditions)
        return self.results


class FakeSession:
    # 검색 Service가 조회 시점에만 세션을 열고 정상적으로 닫는지 기록한다.
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeSession":
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.exited = True


def _repository(
    current_session: FakeSession,
    expected_session: FakeSession,
    repository: FakeEpisodeChunkRepository,
) -> FakeEpisodeChunkRepository:
    # session_factory가 연 세션과 Repository 생성에 전달된 세션이 같은지 확인한다.
    assert current_session is expected_session
    return repository


def _chunk() -> EpisodeChunk:
    return EpisodeChunk(
        id=FOUND_CHUNK_ID,
        episode_id=EPISODE_ID,
        chunk_index=0,
        chunk_text="주인공은 거울을 보며 어려진 얼굴을 확인했다.",
        start_offset=0,
        end_offset=26,
        paragraph_start_index=0,
        paragraph_end_index=0,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )
