from uuid import uuid4

import pytest

from app.models.episode_chunk import EpisodeChunk
from app.services.episode_chunk_service import EpisodeChunkService


def test_replace_episode_chunks_normalizes_splits_and_saves_chunks() -> None:
    # 원문을 정규화하고 청크로 나눈 뒤 같은 회차의 기존 청크를 대체 저장하는지 확인한다.
    episode_id = uuid4()
    session = FakeSession()
    repositories: list[FakeEpisodeChunkRepository] = []
    service = EpisodeChunkService(
        session_factory=lambda: session,
        repository_factory=lambda session: _repository(session, repositories),
    )

    chunks = service.replace_episode_chunks(
        episode_id=episode_id,
        raw_text="\ufeff첫 번째 문단입니다.\r\n\r\n두 번째 문단입니다.\t",
        metadata_json={"source": "single_episode_upload"},
    )

    repository = repositories[0]
    assert repository.deleted_episode_id == episode_id
    assert repository.saved_chunks == chunks
    assert session.committed is True
    assert session.rolled_back is False
    assert len(chunks) == 1
    assert chunks[0].episode_id == episode_id
    assert chunks[0].chunk_index == 0
    assert chunks[0].chunk_text == "첫 번째 문단입니다.\n\n두 번째 문단입니다."
    assert chunks[0].paragraph_start_index == 0
    assert chunks[0].paragraph_end_index == 1
    assert chunks[0].metadata_json == {"source": "single_episode_upload"}


def test_replace_episode_chunks_rolls_back_when_save_fails() -> None:
    # 삭제와 저장 중 하나라도 실패하면 commit하지 않고 rollback하는지 확인한다.
    episode_id = uuid4()
    session = FakeSession()
    service = EpisodeChunkService(
        session_factory=lambda: session,
        repository_factory=lambda session: FailingEpisodeChunkRepository(session),
    )

    with pytest.raises(RuntimeError):
        service.replace_episode_chunks(
            episode_id=episode_id,
            raw_text="첫 번째 문단입니다.",
        )

    assert session.committed is False
    assert session.rolled_back is True


def _repository(
    session: "FakeSession",
    repositories: list["FakeEpisodeChunkRepository"],
) -> "FakeEpisodeChunkRepository":
    repository = FakeEpisodeChunkRepository(session)
    repositories.append(repository)
    return repository


class FakeSession:
    # SQLAlchemy Session 대신 commit/rollback 호출 여부를 기록한다.
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
    # 실제 DB Repository 대신 삭제 대상과 저장 요청 청크를 기록한다.
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.deleted_episode_id = None
        self.saved_chunks: list[EpisodeChunk] = []

    def delete_by_episode_id(self, episode_id) -> None:
        self.deleted_episode_id = episode_id

    def save_all(self, chunks: list[EpisodeChunk]) -> list[EpisodeChunk]:
        self.saved_chunks = chunks
        return chunks


class FailingEpisodeChunkRepository(FakeEpisodeChunkRepository):
    def save_all(self, chunks: list[EpisodeChunk]) -> list[EpisodeChunk]:
        raise RuntimeError("chunk save failed")
