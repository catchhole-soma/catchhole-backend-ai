from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.analysis.character_name_resolver import (
    KnownCharacter,
    resolve_candidate_character,
)
from app.analysis.schemas import ExtractedSettingCandidate
from app.mappers.setting_candidate_mapper import SettingCandidateMapper
from app.models.setting_candidate import SettingCandidate
from app.repositories.setting_candidate_repository import SettingCandidateRepository


@dataclass(frozen=True)
class SettingCandidateSaveItem:
    episode_id: UUID | None
    candidate: ExtractedSettingCandidate


class SettingCandidateService:
    def __init__(
        self,
        session_factory: Callable[[], Session], # 아무 인자도 안 받고 Session을 반환
        # Session을 인자로 Repository를 반환 암시
        repository_factory: Callable[[Session], SettingCandidateRepository] = SettingCandidateRepository,
        known_character_provider: Callable[[UUID], list[KnownCharacter]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.repository_factory = repository_factory
        self.known_character_provider = known_character_provider or (lambda work_id: [])

    def replace_candidates_for_analysis_job(
        self,
        work_id: UUID,
        analysis_job_id: UUID,
        save_items: list[SettingCandidateSaveItem],
        known_characters: list[KnownCharacter] | None = None,
    ) -> list[SettingCandidate]:
        known_characters = (
            self.known_character_provider(work_id)
            if known_characters is None
            else known_characters
        )
        # LLM 검증을 통과한 내부 후보 객체를 setting_candidates 저장 모델로 변환한다.
        candidates = [
            SettingCandidateMapper.to_entity(
                work_id=work_id,
                episode_id=item.episode_id,
                analysis_job_id=analysis_job_id,
                candidate=item.candidate,
                character_match=resolve_candidate_character(
                    item.candidate,
                    known_characters,
                ),
            )
            for item in save_items
        ]

        with self.session_factory() as session:
            repository = self.repository_factory(session)
            try:
                # 같은 analysis_job_id 기준으로 재실행해도 후보가 중복 저장되지 않게 교체한다.
                repository.delete_by_analysis_job_id(analysis_job_id)
                saved_candidates = repository.save_all(candidates)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return saved_candidates
