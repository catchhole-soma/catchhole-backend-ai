import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from math import sqrt
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import EpisodeChunkRepository

PGVECTOR_TEST_DATABASE_URL_ENV = "PGVECTOR_TEST_DATABASE_URL"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_VERSION = "v1"

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
EPISODE_1_ID = UUID("00000000-0000-0000-0000-000000000011")
EPISODE_2_ID = UUID("00000000-0000-0000-0000-000000000012")
EPISODE_3_ID = UUID("00000000-0000-0000-0000-000000000013")
OTHER_WORK_EPISODE_ID = UUID("00000000-0000-0000-0000-000000000014")
SAME_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000021")
SIMILAR_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000022")
DIFFERENT_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000023")
OTHER_WORK_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000024")
STALE_VERSION_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000025")
NULL_EMBEDDING_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000026")

pytestmark = pytest.mark.integration


def test_pgvector_search_orders_chunks_by_cosine_similarity_and_work() -> None:
    # 실제 PostgreSQL이 query vector와 각 청크 벡터의 cosine distance를 계산하고,
    # 다른 작품과 오래된 embedding version을 제외한 뒤 Top-K 순서로 반환하는지 검증한다.
    with _pgvector_session() as session:
        repository = EpisodeChunkRepository(session)

        results = repository.search_similar_chunks(
            query_embedding=_vector(1.0, 0.0),
            work_id=WORK_ID,
            embedding_model=EMBEDDING_MODEL,
            embedding_version=EMBEDDING_VERSION,
            top_k=2,
        )
        iterative_scan = session.scalar(
            text("SELECT current_setting('hnsw.iterative_scan')")
        )

    assert [result.chunk.id for result in results] == [
        SAME_CHUNK_ID,
        SIMILAR_CHUNK_ID,
    ]
    assert [result.episode_no for result in results] == [1, 2]
    assert results[0].similarity == pytest.approx(1.0)
    assert results[1].similarity == pytest.approx(0.8)
    assert iterative_scan == "strict_order"


def test_pgvector_search_applies_episode_range_and_excluded_chunk_ids() -> None:
    # 회차 범위 양 끝이 포함되고, 호출자가 제외한 현재 후보 청크와 NULL 벡터는
    # 실제 DB 조회 결과에 들어오지 않는지 검증한다.
    with _pgvector_session() as session:
        repository = EpisodeChunkRepository(session)

        results = repository.search_similar_chunks(
            query_embedding=_vector(1.0, 0.0),
            work_id=WORK_ID,
            embedding_model=EMBEDDING_MODEL,
            embedding_version=EMBEDDING_VERSION,
            top_k=5,
            episode_no_from=2,
            episode_no_to=3,
            excluded_chunk_ids=[SIMILAR_CHUNK_ID],
        )

    assert [result.chunk.id for result in results] == [DIFFERENT_CHUNK_ID]
    assert results[0].episode_no == 3
    assert results[0].similarity == pytest.approx(0.0)


@contextmanager
def _pgvector_session() -> Iterator[Session]:
    """실제 pgvector DB 연결과 테스트별 임시 schema 생명주기를 관리한다."""

    database_url = os.getenv(PGVECTOR_TEST_DATABASE_URL_ENV)
    if not database_url:
        pytest.skip(f"{PGVECTOR_TEST_DATABASE_URL_ENV} is required for pgvector integration tests.")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                # 임시 테이블을 만들기 전에 실제 PostgreSQL에 vector extension이 있는지 보장한다.
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                connection.execute(text(_CREATE_TEMP_EPISODES_SQL))
                connection.execute(text(_CREATE_TEMP_EPISODE_CHUNKS_SQL))
                connection.execute(text(_CREATE_TEMP_HNSW_INDEX_SQL))

                # ORM Repository가 같은 연결의 임시 테이블을 조회하도록 Session을 직접 묶는다.
                with Session(bind=connection) as session:
                    _seed_search_rows(session)
                    yield session
            finally:
                # 테스트 데이터와 임시 테이블을 한 번에 제거해 기존 데이터를 건드리지 않는다.
                transaction.rollback()
    finally:
        engine.dispose()


def _seed_search_rows(session: Session) -> None:
    # EpisodeChunk에 없는 work_id와 episode_no 필터를 실제 JOIN으로 확인할 회차를 준비한다.
    session.execute(
        text(
            """
            INSERT INTO episodes (id, work_id, episode_no)
            VALUES (:id, :work_id, :episode_no)
            """
        ),
        [
            {"id": EPISODE_1_ID, "work_id": WORK_ID, "episode_no": 1},
            {"id": EPISODE_2_ID, "work_id": WORK_ID, "episode_no": 2},
            {"id": EPISODE_3_ID, "work_id": WORK_ID, "episode_no": 3},
            {"id": OTHER_WORK_EPISODE_ID, "work_id": OTHER_WORK_ID, "episode_no": 1},
        ],
    )

    now = datetime(2026, 7, 17, 12, 0, 0)
    session.execute(
        EpisodeChunk.__table__.insert(),
        [
            _chunk_row(SAME_CHUNK_ID, EPISODE_1_ID, 0, _vector(1.0, 0.0), now),
            _chunk_row(SIMILAR_CHUNK_ID, EPISODE_2_ID, 0, _vector(0.8, 0.6), now),
            _chunk_row(DIFFERENT_CHUNK_ID, EPISODE_3_ID, 0, _vector(0.0, 1.0), now),
            _chunk_row(
                OTHER_WORK_CHUNK_ID,
                OTHER_WORK_EPISODE_ID,
                0,
                _vector(0.95, sqrt(1 - 0.95**2)),
                now,
            ),
            _chunk_row(
                STALE_VERSION_CHUNK_ID,
                EPISODE_2_ID,
                1,
                _vector(0.99, sqrt(1 - 0.99**2)),
                now,
                embedding_version="v0",
            ),
            _chunk_row(NULL_EMBEDDING_CHUNK_ID, EPISODE_2_ID, 2, None, now),
        ],
    )


def _chunk_row(
    chunk_id: UUID,
    episode_id: UUID,
    chunk_index: int,
    embedding: list[float] | None,
    now: datetime,
    embedding_version: str = EMBEDDING_VERSION,
) -> dict:
    return {
        "id": chunk_id,
        "episode_id": episode_id,
        "chunk_index": chunk_index,
        "chunk_text": f"통합 테스트 청크 {chunk_id}",
        "start_offset": 0,
        "end_offset": 10,
        "paragraph_start_index": 0,
        "paragraph_end_index": 0,
        "metadata_json": None,
        "embedding": embedding,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_version": embedding_version,
        "embedded_at": now if embedding is not None else None,
        "created_at": now,
        "updated_at": now,
    }


def _vector(first: float, second: float) -> list[float]:
    """읽기 쉬운 두 좌표 뒤를 0으로 채워 실제 컬럼과 같은 1536차원 벡터를 만든다."""

    return [first, second, *([0.0] * 1534)]


_CREATE_TEMP_EPISODES_SQL = """
CREATE TEMPORARY TABLE episodes (
    id UUID PRIMARY KEY,
    work_id UUID NOT NULL,
    episode_no INTEGER NOT NULL
) ON COMMIT DROP
"""

_CREATE_TEMP_EPISODE_CHUNKS_SQL = """
CREATE TEMPORARY TABLE episode_chunks (
    id UUID PRIMARY KEY,
    episode_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    paragraph_start_index INTEGER NOT NULL,
    paragraph_end_index INTEGER NOT NULL,
    metadata_json JSONB,
    embedding VECTOR(1536),
    embedding_model VARCHAR(100),
    embedding_version VARCHAR(50),
    embedded_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
) ON COMMIT DROP
"""

_CREATE_TEMP_HNSW_INDEX_SQL = """
CREATE INDEX idx_test_episode_chunks_embedding_cosine
    ON episode_chunks USING hnsw (embedding vector_cosine_ops)
"""
