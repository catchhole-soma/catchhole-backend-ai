from datetime import datetime
from uuid import UUID

import pytest

from app.embeddings.responses import EmbeddingBatchResponse
from app.embeddings.exceptions import EmbeddingDataIntegrityError
from app.embeddings.services.episode_chunk_embedding import EpisodeChunkEmbeddingService
from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import EpisodeChunkEmbeddingUpdate

EPISODE_ID = UUID("00000000-0000-0000-0000-000000000001")
EMBEDDED_AT = datetime(2026, 7, 15, 12, 0, 0)


def test_embed_chunks_generates_vectors_before_updating_and_commits() -> None:
    # 청크 텍스트 순서대로 생성된 벡터와 메타데이터가 한 번에 저장되고 commit되는 흐름을 검증한다.
    chunks = [_chunk(0, "첫 번째 청크"), _chunk(1, "두 번째 청크")]
    embedding_client = FakeEmbeddingClient(
        response=EmbeddingBatchResponse(
            embeddings=[[0.1, 0.2], [0.3, 0.4]],
            model="text-embedding-3-small",
            input_token_count=12,
        )
    )
    session = FakeSession()
    repositories: list[FakeEpisodeChunkRepository] = []
    service = EpisodeChunkEmbeddingService(
        session_factory=lambda: session,
        embedding_client=embedding_client,
        repository_factory=lambda current_session: _repository(current_session, repositories),
        now_factory=lambda: EMBEDDED_AT,
    )

    result = service.embed_chunks(chunks)

    assert embedding_client.requests == [["첫 번째 청크", "두 번째 청크"]]
    assert repositories[0].embedding_updates == [
        EpisodeChunkEmbeddingUpdate(
            chunk_id=chunks[0].id,
            embedding=[0.1, 0.2],
            embedding_model="text-embedding-3-small",
            embedding_version="v1",
            embedded_at=EMBEDDED_AT,
        ),
        EpisodeChunkEmbeddingUpdate(
            chunk_id=chunks[1].id,
            embedding=[0.3, 0.4],
            embedding_model="text-embedding-3-small",
            embedding_version="v1",
            embedded_at=EMBEDDED_AT,
        ),
    ]
    assert result.embedded_chunk_count == 2
    assert result.input_token_count == 12
    assert session.committed is True
    assert session.rolled_back is False


def test_embed_chunks_skips_api_and_database_when_chunks_are_empty() -> None:
    # 처리할 청크가 없을 때 불필요한 OpenAI 호출과 DB 세션 생성을 모두 건너뛰는지 검증한다.
    embedding_client = FakeEmbeddingClient(
        response=EmbeddingBatchResponse(embeddings=[], model="text-embedding-3-small")
    )
    session_factory_call_count = 0

    def session_factory():
        nonlocal session_factory_call_count
        session_factory_call_count += 1
        return FakeSession()

    service = EpisodeChunkEmbeddingService(
        session_factory=session_factory,
        embedding_client=embedding_client,
    )

    result = service.embed_chunks([])

    assert result.embedded_chunk_count == 0
    assert embedding_client.requests == []
    assert session_factory_call_count == 0


def test_embed_chunks_rejects_duplicate_chunk_ids_before_external_calls() -> None:
    # 중복 청크 ID를 OpenAI 호출과 DB 세션 생성 전에 정합성 오류로 차단하는지 검증한다.
    chunk = _chunk(0, "중복 청크")
    embedding_client = FakeEmbeddingClient(
        response=EmbeddingBatchResponse(
            embeddings=[[0.1, 0.2], [0.1, 0.2]],
            model="text-embedding-3-small",
        )
    )
    session_factory_call_count = 0

    def session_factory():
        nonlocal session_factory_call_count
        session_factory_call_count += 1
        return FakeSession()

    service = EpisodeChunkEmbeddingService(
        session_factory=session_factory,
        embedding_client=embedding_client,
    )

    with pytest.raises(EmbeddingDataIntegrityError, match="Duplicate chunk IDs"):
        service.embed_chunks([chunk, chunk])

    assert embedding_client.requests == []
    assert session_factory_call_count == 0


def test_embed_chunks_does_not_open_database_session_when_api_fails() -> None:
    # 임베딩 API 호출이 실패하면 DB 트랜잭션을 시작하지 않고 예외를 그대로 전달하는지 검증한다.
    embedding_client = FakeEmbeddingClient(error=RuntimeError("embedding API failed"))
    session_factory_call_count = 0

    def session_factory():
        nonlocal session_factory_call_count
        session_factory_call_count += 1
        return FakeSession()

    service = EpisodeChunkEmbeddingService(
        session_factory=session_factory,
        embedding_client=embedding_client,
    )

    with pytest.raises(RuntimeError, match="embedding API failed"):
        service.embed_chunks([_chunk(0, "청크")])

    assert session_factory_call_count == 0


def test_embed_chunks_rolls_back_when_database_update_fails() -> None:
    # 벡터 생성 후 DB 반영에 실패하면 commit하지 않고 해당 트랜잭션을 rollback하는지 검증한다.
    embedding_client = FakeEmbeddingClient(
        response=EmbeddingBatchResponse(
            embeddings=[[0.1, 0.2]],
            model="text-embedding-3-small",
        )
    )
    session = FakeSession()
    service = EpisodeChunkEmbeddingService(
        session_factory=lambda: session,
        embedding_client=embedding_client,
        repository_factory=lambda current_session: FailingEpisodeChunkRepository(current_session),
    )

    with pytest.raises(RuntimeError, match="embedding update failed"):
        service.embed_chunks([_chunk(0, "청크")])

    assert session.committed is False
    assert session.rolled_back is True


class FakeEmbeddingClient:
    version = "v1"

    def __init__(
        self,
        response: EmbeddingBatchResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[list[str]] = []

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        self.requests.append(inputs)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class FakeEpisodeChunkRepository:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.embedding_updates: list[EpisodeChunkEmbeddingUpdate] = []

    def update_embeddings(
        self,
        embedding_updates: list[EpisodeChunkEmbeddingUpdate],
    ) -> list[EpisodeChunk]:
        self.embedding_updates = embedding_updates
        return [
            _chunk(index, f"저장된 청크 {index}")
            for index, _ in enumerate(embedding_updates)
        ]


class FailingEpisodeChunkRepository(FakeEpisodeChunkRepository):
    def update_embeddings(
        self,
        embedding_updates: list[EpisodeChunkEmbeddingUpdate],
    ) -> list[EpisodeChunk]:
        raise RuntimeError("embedding update failed")


def _repository(
    session: FakeSession,
    repositories: list[FakeEpisodeChunkRepository],
) -> FakeEpisodeChunkRepository:
    repository = FakeEpisodeChunkRepository(session)
    repositories.append(repository)
    return repository


def _chunk(chunk_index: int, chunk_text: str) -> EpisodeChunk:
    return EpisodeChunk(
        id=UUID(f"00000000-0000-0000-0000-00000000010{chunk_index}"),
        episode_id=EPISODE_ID,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
        start_offset=0,
        end_offset=len(chunk_text),
        paragraph_start_index=0,
        paragraph_end_index=0,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )
