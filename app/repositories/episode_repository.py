from uuid import UUID

from sqlalchemy.orm import Session

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ErrorCode
from app.models.episode import Episode


class EpisodeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_id(self, episode_id: UUID) -> Episode | None:
        return self.session.get(Episode, episode_id)

    def get_by_id_or_throw(self, episode_id: UUID) -> Episode:
        episode = self.find_by_id(episode_id)
        if episode is None:
            raise AppException(
                ErrorCode.EPISODE_NOT_FOUND,
                detail={"episode_id": str(episode_id)},
            )
        return episode
