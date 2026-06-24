from uuid import uuid4

import pytest

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.models.episode import Episode
from app.repositories.episode_repository import EpisodeRepository


def test_get_by_id_or_throw_returns_episode() -> None:
    # Episode가 존재할 때 get_by_id_or_throw가 해당 Episode를 그대로 반환하는지 확인
    episode = _episode()
    repository = EpisodeRepository(FakeSession(episode))

    found_episode = repository.get_by_id_or_throw(episode.id)

    assert found_episode == episode


def test_get_by_id_or_throw_raises_when_episode_missing() -> None:
    # Episode가 존재하지 않을 때 EPISODE_NOT_FOUND 예외를 던지는지 확인
    missing_id = uuid4()
    repository = EpisodeRepository(FakeSession(None))

    with pytest.raises(AppException) as exc_info:
        repository.get_by_id_or_throw(missing_id)

    assert exc_info.value.error_code == ErrorCode.EPISODE_NOT_FOUND
    assert exc_info.value.detail == {"episode_id": str(missing_id)}


def _episode() -> Episode:
    return Episode(
        id=uuid4(),
        work_id=uuid4(),
        source_file_id=None,
        episode_no=1,
        title="첫 번째 회차",
        content_s3_key="works/work-id/episodes/episode-id.txt",
        content_s3_version=None,
        content_hash=None,
        char_count=100,
        status="UPLOADED",
        created_at=None,
        updated_at=None,
    )


class FakeSession:
    # SQLAlchemy session.get 호출 결과를 제어하는 테스트용 session
    def __init__(self, episode: Episode | None) -> None:
        self.episode = episode

    def get(self, model, primary_key):
        if self.episode and self.episode.id == primary_key:
            return self.episode
        return None
