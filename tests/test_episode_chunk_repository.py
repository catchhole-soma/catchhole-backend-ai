from uuid import uuid4

from app.models.episode_chunk import EpisodeChunk
from app.repositories.episode_chunk_repository import EpisodeChunkRepository


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
    # force 재분석/재청킹을 위해 기존 청크 삭제 쿼리를 session에 전달하는지 확인한다.
    session = FakeSession()
    repository = EpisodeChunkRepository(session)

    repository.delete_by_episode_id(uuid4())

    assert session.executed_statement is not None


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


class FakeScalarResult:
    # SQLAlchemy scalars(...).all() 반환 흐름을 흉내 내는 테스트용 객체다.
    def __init__(self, items: list[EpisodeChunk]) -> None:
        self.items = items

    def all(self) -> list[EpisodeChunk]:
        return self.items


class FakeSession:
    # 실제 DB session 대신 Repository가 호출한 메서드와 전달된 값을 기록한다.
    def __init__(self, scalar_items: list[EpisodeChunk] | None = None) -> None:
        self.scalar_items = scalar_items or []
        self.added_items: list[EpisodeChunk] = []
        self.scalar_statement = None
        self.executed_statement = None

    def add_all(self, items: list[EpisodeChunk]) -> None:
        self.added_items.extend(items)

    def scalars(self, statement) -> FakeScalarResult:
        self.scalar_statement = statement
        return FakeScalarResult(self.scalar_items)

    def execute(self, statement) -> None:
        self.executed_statement = statement
