from uuid import uuid4

import pytest

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.models.episode import Episode
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService


def test_replace_chunks_from_s3_content_reads_s3_and_replaces_chunks() -> None:
    # Episode에 content_s3_key가 있을 때,
    # 1. DB에서 Episode를 조회하고
    # 2. content_s3_key로 S3 원문을 읽고
    # 3. 읽은 원문을 EpisodeChunkService.replace_episode_chunks에 넘기는지 확인
    episode = _episode(content_s3_key="works/work-id/episodes/episode-id.txt")
    storage = FakeStorage(text="첫 번째 문단입니다.\n\n두 번째 문단입니다.")
    chunk_service = FakeEpisodeChunkService()
    service = EpisodeS3ChunkingService(
        session_factory=lambda: FakeSession(episode),
        storage=storage,
        chunk_service=chunk_service,
    )

    result = service.replace_chunks_from_s3_content(episode.id)
    # chunk_service가 반환한 결과가 그대로 반환되는지 확인
    assert result == ["chunk-1"]
    # S3 조회에 사용된 key가 Episode.content_s3_key와 같은지 확인
    assert storage.requested_key == "works/work-id/episodes/episode-id.txt"
    # S3에서 읽은 원문이 청킹 서비스에 정확히 전달됐는지 확인
    assert chunk_service.request == {
        "episode_id": episode.id,
        "raw_text": "첫 번째 문단입니다.\n\n두 번째 문단입니다.",
    }


def test_replace_chunks_from_s3_content_raises_when_content_key_missing() -> None:
    # Episode는 존재하지만 content_s3_key가 없을 때,
    # S3를 읽거나 청킹하지 않고 INVALID_REQUEST 예외를 던지는지 확인
    episode = _episode(content_s3_key=None)
    service = EpisodeS3ChunkingService(
        session_factory=lambda: FakeSession(episode),
        storage=FakeStorage(text="사용되지 않는 원문"),
        chunk_service=FakeEpisodeChunkService(),
    )

    with pytest.raises(AppException) as exc_info:
        service.replace_chunks_from_s3_content(episode.id)

    # 예외의 error_code와 detail이 기대한 값인지 확인
    assert exc_info.value.error_code == ErrorCode.INVALID_REQUEST
    assert exc_info.value.detail == {
        "episode_id": str(episode.id),
        "reason": "content_s3_key is missing",
    }


def _episode(content_s3_key: str | None) -> Episode:
    return Episode(
        id=uuid4(),
        work_id=uuid4(),
        source_file_id=None,
        episode_no=1,
        title="첫 번째 회차",
        content_s3_key=content_s3_key,
        content_s3_version=None,
        content_hash=None,
        char_count=100,
        status="UPLOADED",
        created_at=None,
        updated_at=None,
    )


class FakeSession:
    # with self.session_factory() as session 흐름을 흉내
    def __init__(self, episode: Episode) -> None:
        self.episode = episode

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, model, primary_key):
        if self.episode.id == primary_key:
            return self.episode
        return None


class FakeStorage:
    # 실제 S3 대신 요청 key와 반환 원문을 기록
    def __init__(self, text: str) -> None:
        self.text = text
        self.requested_key = None

    def get_text(self, key: str) -> str:
        self.requested_key = key
        return self.text


class FakeEpisodeChunkService:
    # 실제 청킹 저장 서비스 대신 전달받은 원문을 기록
    def __init__(self) -> None:
        self.request = None

    def replace_episode_chunks(self, episode_id, raw_text):
        self.request = {"episode_id": episode_id, "raw_text": raw_text}
        return ["chunk-1"]
