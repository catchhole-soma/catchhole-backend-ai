from decimal import Decimal
from uuid import UUID, uuid4

from app.analysis.character_name_resolver import CharacterNameMatch
from app.analysis.schemas import ExtractedSettingCandidate
from app.domain.enums import (
    CharacterFactComparisonStatus,
    SettingCandidateKind,
    SettingCandidateMatchStatus,
    SettingCandidateReviewStatus,
    SettingEntityType,
    SettingValueType,
)
from app.domain.setting_values import normalize_setting_display_value
from app.models.setting_candidate import SettingCandidate


class SettingCandidateMapper:
    @staticmethod
    def to_entity(
        work_id: UUID,
        episode_id: UUID | None,
        source_content_s3_key: str | None,
        analysis_job_id: UUID,
        candidate: ExtractedSettingCandidate,
        character_match: CharacterNameMatch | None = None,
    ) -> SettingCandidate:
        entity_name = candidate.entity_name.strip()
        if not entity_name:
            raise ValueError("entity_name must contain non-whitespace characters.")

        character_match = character_match or CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.UNRESOLVED,
        )
        candidate_kind = SettingCandidateKind(candidate.candidate_kind)
        value_type = (
            SettingValueType(candidate.value_type)
            if candidate.value_type is not None
            else None
        )
        raw_ai_result_json = candidate.model_dump(mode="json")
        return SettingCandidate(
            id=uuid4(),
            work_id=work_id,
            episode_id=episode_id,
            source_chunk_id=candidate.source_chunk_id,
            # 원고 파일이 나중에 교체돼도 근거 조회는 분석 당시 원문을 사용한다.
            source_content_s3_key=source_content_s3_key,
            analysis_job_id=analysis_job_id,
            candidate_kind=candidate_kind,
            entity_type=SettingEntityType(candidate.entity_type),
            entity_name=entity_name,
            # raw_entity_mention은 원문에 실제 존재한 표현만 저장한다.
            # 누락된 경우 entity_name으로 채우면 AI가 정리한 이름을 원문 근거로 오인할 수 있다.
            raw_entity_mention=candidate.raw_entity_mention,
            matched_character_id=character_match.matched_character_id,
            match_status=character_match.match_status,
            attribute_name=candidate.attribute_name,
            attribute_value=normalize_setting_display_value(
                value_type,
                candidate.value_json,
                candidate.attribute_value,
            ),
            value_type=value_type,
            value_json=candidate.value_json,
            evidence_spans=[
                evidence_span.model_dump(mode="json")
                for evidence_span in candidate.evidence_spans
            ],
            confidence=_to_decimal(candidate.confidence),
            review_status=SettingCandidateReviewStatus.PENDING_REVIEW,
            raw_ai_result_json=raw_ai_result_json,
            comparison_status=_initial_comparison_status(candidate_kind, character_match),
        )


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _initial_comparison_status(
    candidate_kind: SettingCandidateKind,
    character_match: CharacterNameMatch,
) -> CharacterFactComparisonStatus:
    if candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
        return CharacterFactComparisonStatus.NOT_REQUIRED
    if (
        character_match.match_status
        in {
            SettingCandidateMatchStatus.MATCHED,
            SettingCandidateMatchStatus.AUTO_MATCHED_BY_NAME,
        }
        and character_match.matched_character_id is not None
    ):
        return CharacterFactComparisonStatus.PENDING
    return CharacterFactComparisonStatus.WAITING_FOR_CHARACTER_MATCH
