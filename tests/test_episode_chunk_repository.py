from datetime import datetime
from uuid import uuid4

import pytest

from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import (
    EpisodeChunkEmbeddingUpdate,
    EpisodeChunkRepository,
    EpisodeChunkSearchResult,
)


def test_save_all_adds_episode_chunks() -> None:
    # split 결과로 만들어진 청크 목록을 session.add_all에 전달하는지 확인한다.
    session = FakeSession()
    repository = EpisodeChunkRepository(session)
    chunks = [_episode_chunk(chunk_index=0), _episode_chunk(chunk_index=1)]

    saved_chunks = repository.save_all(chunks)

    assert saved_chunks == chunks
    assert session.added_items == chunks


def test_find_by_episode_id_returns_chunks() -> None:
    # 회차별 청크를 조회할 때 Repository가 조회 쿼리를 만들고 결과 목록을 반환하는지 확인한다.
    chunks = [_episode_chunk(chunk_index=0), _episode_chunk(chunk_index=1)]
    session = FakeSession(scalar_items=chunks)
    repository = EpisodeChunkRepository(session)

    found_chunks = repository.find_by_episode_id(uuid4())

    assert found_chunks == chunks
    assert session.scalar_statement is not None


def test_delete_by_episode_id_executes_delete_statement() -> None:
    # 같은 회차를 다시 청킹할 때 기존 청크 삭제 쿼리를 session에 전달하는지 확인한다.
    session = FakeSession()
    repository = EpisodeChunkRepository(session)

    repository.delete_by_episode_id(uuid4())

    assert session.executed_statement is not None


def test_update_embeddings_updates_only_embedding_fields_in_request_order() -> None:
    # 조회 결과 순서와 무관하게 요청한 청크 순서대로 임베딩 필드만 갱신하는 흐름을 검증한다.
    first_chunk = _episode_chunk(chunk_index=0)
    second_chunk = _episode_chunk(chunk_index=1)
    embedded_at = datetime(2026, 7, 15, 12, 0, 0)
    session = FakeSession(scalar_items=[second_chunk, first_chunk])
    repository = EpisodeChunkRepository(session)
    embedding_updates = [
        _embedding_update(first_chunk, [0.1, 0.2], embedded_at),
        _embedding_update(second_chunk, [0.3, 0.4], embedded_at),
    ]

    updated_chunks = repository.update_embeddings(embedding_updates)

    assert updated_chunks == [first_chunk, second_chunk]
    assert first_chunk.embedding == [0.1, 0.2]
    assert second_chunk.embedding == [0.3, 0.4]
    assert first_chunk.embedding_model == "text-embedding-3-small"
    assert first_chunk.embedding_version == "v1"
    assert first_chunk.embedded_at == embedded_at
    assert session.scalar_statement is not None


def test_update_embeddings_rejects_missing_chunk_before_updating_any_chunk() -> None:
    # 재청킹 등으로 대상 하나가 사라졌다면 일부 청크만 갱신하지 않고 전체 요청을 거부하는지 검증한다.
    existing_chunk = _episode_chunk(chunk_index=0)
    missing_chunk = _episode_chunk(chunk_index=1)
    session = FakeSession(scalar_items=[existing_chunk])
    repository = EpisodeChunkRepository(session)
    embedding_updates = [
        _embedding_update(existing_chunk, [0.1], datetime.now()),
        _embedding_update(missing_chunk, [0.2], datetime.now()),
    ]

    with pytest.raises(ValueError, match="Embedding update targets do not exist"):
        repository.update_embeddings(embedding_updates)

    assert existing_chunk.embedding is None


def test_update_embeddings_rejects_duplicate_chunk_ids() -> None:
    # 같은 청크를 한 저장 요청에서 중복 갱신하는 잘못된 입력을 조회 전에 차단하는지 검증한다.
    chunk = _episode_chunk(chunk_index=0)
    update = _embedding_update(chunk, [0.1], datetime.now())
    session = FakeSession(scalar_items=[chunk])
    repository = EpisodeChunkRepository(session)

    with pytest.raises(ValueError, match="Duplicate chunk IDs"):
        repository.update_embeddings([update, update])

    assert session.scalar_statement is None


def test_search_similar_chunks_returns_results_with_episode_number_and_similarity() -> None:
    # DB 조회 row를 이후 Service가 사용할 검색 결과 객체로 변환하는지 확인한다.
    chunk = _episode_chunk(chunk_index=0)
    session = FakeSession(execute_rows=[(chunk, 7, 0.82)])
    repository = EpisodeChunkRepository(session)
    work_id = uuid4()

    results = repository.search_similar_chunks(
        query_embedding=[0.1, 0.2],
        work_id=work_id,
        embedding_model="text-embedding-3-small",
        embedding_version="v1",
        top_k=3,
    )

    assert results == [
        EpisodeChunkSearchResult(
            chunk=chunk,
            episode_no=7,
            similarity=0.82,
        )
    ]
    assert session.executed_statement is not None


def test_search_similar_chunks_applies_scope_and_cosine_top_k_conditions() -> None:
    # 작품·회차·제외 ID·임베딩 계약과 cosine Top-K 조건이 SQL에 모두 포함되는지 검증한다.
    session = FakeSession()
    repository = EpisodeChunkRepository(session)

    repository.search_similar_chunks(
        query_embedding=[0.1, 0.2],
        work_id=uuid4(),
        embedding_model="text-embedding-3-small",
        embedding_version="v1",
        top_k=5,
        episode_no_from=2,
        episode_no_to=8,
        excluded_chunk_ids=[uuid4(), uuid4()],
    )

    statement = session.executed_statement
    assert statement is not None
    sql = str(statement)
    assert "JOIN episodes" in sql
    assert "episodes.work_id" in sql
    assert "episodes.episode_no >=" in sql
    assert "episodes.episode_no <=" in sql
    assert "episode_chunks.id NOT IN" in sql
    assert "episode_chunks.embedding IS NOT NULL" in sql
    assert "episode_chunks.embedding_model" in sql
    assert "episode_chunks.embedding_version" in sql
    assert "<=>" in sql
    assert "ORDER BY" in sql
    assert statement._limit_clause.value == 5


@pytest.mark.parametrize(
    ("query_embedding", "top_k", "episode_no_from", "episode_no_to", "message"),
    [
        ([], 5, None, None, "Query embedding must not be empty"),
        ([0.1], 0, None, None, "Top-K must be greater than zero"),
        ([0.1], 5, 9, 3, "Episode number range is invalid"),
    ],
)
def test_search_similar_chunks_rejects_invalid_conditions_before_query(
    query_embedding: list[float],
    top_k: int,
    episode_no_from: int | None,
    episode_no_to: int | None,
    message: str,
) -> None:
    # DB가 해석하기 어려운 빈 벡터·잘못된 개수·역전된 회차 범위를 조회 전에 차단한다.
    session = FakeSession()
    repository = EpisodeChunkRepository(session)

    with pytest.raises(ValueError, match=message):
        repository.search_similar_chunks(
            query_embedding=query_embedding,
            work_id=uuid4(),
            embedding_model="text-embedding-3-small",
            embedding_version="v1",
            top_k=top_k,
            episode_no_from=episode_no_from,
            episode_no_to=episode_no_to,
        )

    assert session.executed_statement is None


def _episode_chunk(chunk_index: int) -> EpisodeChunk:
    return EpisodeChunk(
        id=uuid4(),
        episode_id=uuid4(),
        chunk_index=chunk_index,
        chunk_text=f"{chunk_index}번째 청크입니다.",
        start_offset=chunk_index * 10,
        end_offset=chunk_index * 10 + 9,
        paragraph_start_index=chunk_index,
        paragraph_end_index=chunk_index,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )


def _embedding_update(
    chunk: EpisodeChunk,
    embedding: list[float],
    embedded_at: datetime,
) -> EpisodeChunkEmbeddingUpdate:
    return EpisodeChunkEmbeddingUpdate(
        chunk_id=chunk.id,
        embedding=embedding,
        embedding_model="text-embedding-3-small",
        embedding_version="v1",
        embedded_at=embedded_at,
    )


class FakeScalarResult:
    # SQLAlchemy scalars(...).all() 반환 흐름을 흉내 내는 테스트용 객체다.
    def __init__(self, items: list[EpisodeChunk]) -> None:
        self.items = items

    def all(self) -> list[EpisodeChunk]:
        return self.items


class FakeSession:
    # 실제 DB session 대신 Repository가 호출한 메서드와 전달된 값을 기록한다.
    def __init__(
        self,
        scalar_items: list[EpisodeChunk] | None = None,
        execute_rows: list[tuple[EpisodeChunk, int, float]] | None = None,
    ) -> None:
        self.scalar_items = scalar_items or []
        self.execute_rows = execute_rows or []
        self.added_items: list[EpisodeChunk] = []
        self.scalar_statement = None
        self.executed_statement = None

    def add_all(self, items: list[EpisodeChunk]) -> None:
        self.added_items.extend(items)

    def scalars(self, statement) -> FakeScalarResult:
        self.scalar_statement = statement
        return FakeScalarResult(self.scalar_items)

    def execute(self, statement):
        self.executed_statement = statement
        return FakeExecuteResult(self.execute_rows)


class FakeExecuteResult:
    # SQLAlchemy execute(...).all()의 row 목록 반환 흐름을 흉내 낸다.
    def __init__(self, rows: list[tuple[EpisodeChunk, int, float]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[EpisodeChunk, int, float]]:
        return self.rows
