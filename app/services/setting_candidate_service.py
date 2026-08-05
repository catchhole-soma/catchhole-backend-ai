from collections.abc import Callable
from dataclasses import dataclass
import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.analysis.character_name_resolver import (
    CharacterNameMatch,
    KnownCharacter,
    normalize_character_name,
    normalize_known_characters,
    resolve_candidate_character,
)
from app.analysis.schemas import ExtractedSettingCandidate
from app.domain.enums import SettingCandidateKind, SettingCandidateMatchStatus
from app.mappers.setting_candidate_mapper import SettingCandidateMapper
from app.models.setting_candidate import SettingCandidate
from app.repositories.setting_candidate_repository import SettingCandidateRepository


@dataclass(frozen=True)
class SettingCandidateSaveItem:
    episode_id: UUID | None
    source_content_s3_key: str | None
    candidate: ExtractedSettingCandidate


class SettingCandidateService:
    def __init__(
        self,
        session_factory: Callable[[], Session], # 아무 인자도 안 받고 Session을 반환
        # Session을 인자로 Repository를 반환 암시
        repository_factory: Callable[[Session], SettingCandidateRepository] = SettingCandidateRepository,
    ) -> None:
        self.session_factory = session_factory
        self.repository_factory = repository_factory

    def replace_candidates_for_analysis_job(
        self,
        work_id: UUID,
        analysis_job_id: UUID,
        save_items: list[SettingCandidateSaveItem],
        known_characters: list[KnownCharacter],
    ) -> list[SettingCandidate]:
        normalized_known_characters = normalize_known_characters(known_characters)

        # 기존 캐릭터 발견과 분석 내 동일 이름/동일 설정 반복은 검토 후보에서 줄이되,
        # 값이 달라졌거나 주체가 모호한 설정은 정보 손실을 피하기 위해 유지한다.
        candidates: list[SettingCandidate] = []
        seen_discovery_names: set[str] = set()
        setting_candidate_by_key: dict[tuple[str, str, str, str], tuple[int, float]] = {}
        for item in save_items:
            character_match = resolve_candidate_character(
                item.candidate,
                normalized_known_characters,
            )
            if item.candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
                if character_match.match_status == SettingCandidateMatchStatus.MATCHED:
                    continue
                normalized_name = normalize_character_name(item.candidate.entity_name)
                if normalized_name in seen_discovery_names:
                    continue
                seen_discovery_names.add(normalized_name)

            mapped_candidate = SettingCandidateMapper.to_entity(
                work_id=work_id,
                episode_id=item.episode_id,
                source_content_s3_key=item.source_content_s3_key,
                analysis_job_id=analysis_job_id,
                candidate=item.candidate,
                character_match=character_match,
            )
            duplicate_key = _setting_duplicate_key(item.candidate, character_match)
            if duplicate_key is not None:
                confidence = item.candidate.confidence
                confidence_score = confidence if confidence is not None else -1.0
                existing = setting_candidate_by_key.get(duplicate_key)
                if existing is not None:
                    existing_index, existing_confidence = existing
                    if confidence_score > existing_confidence:
                        candidates[existing_index] = mapped_candidate
                        setting_candidate_by_key[duplicate_key] = (
                            existing_index,
                            confidence_score,
                        )
                    continue
                setting_candidate_by_key[duplicate_key] = (
                    len(candidates),
                    confidence_score,
                )

            candidates.append(mapped_candidate)

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


def _setting_duplicate_key(
    candidate: ExtractedSettingCandidate,
    character_match: CharacterNameMatch,
) -> tuple[str, str, str, str] | None:
    if (
        candidate.candidate_kind != SettingCandidateKind.SETTING
        or character_match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
        or candidate.attribute_name is None
        or candidate.value_type is None
        or candidate.value_json is None
    ):
        return None

    if character_match.matched_character_id is not None:
        subject_key = f"id:{character_match.matched_character_id}"
    else:
        normalized_name = normalize_character_name(candidate.entity_name)
        if not normalized_name:
            return None
        subject_key = f"name:{normalized_name}"

    canonical_value_json = json.dumps(
        candidate.value_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        subject_key,
        candidate.attribute_name,
        candidate.value_type,
        canonical_value_json,
    )
