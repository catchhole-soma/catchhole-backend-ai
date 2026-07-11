from decimal import Decimal
from uuid import UUID, uuid4

from app.analysis.character_name_resolver import CharacterNameMatch
from app.analysis.schemas import ExtractedSettingCandidate
from app.domain.enums import (
    SettingCandidateMatchStatus,
    SettingCandidateReviewStatus,
    SettingEntityType,
    SettingValueType,
)
from app.models.setting_candidate import SettingCandidate


class SettingCandidateMapper:
    @staticmethod
    def to_entity(
        work_id: UUID,
        episode_id: UUID | None,
        analysis_job_id: UUID,
        candidate: ExtractedSettingCandidate,
        character_match: CharacterNameMatch | None = None,
    ) -> SettingCandidate:
        character_match = character_match or CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.UNRESOLVED,
        )
        return SettingCandidate(
            id=uuid4(),
            work_id=work_id,
            episode_id=episode_id,
            source_chunk_id=candidate.source_chunk_id,
            analysis_job_id=analysis_job_id,
            entity_type=SettingEntityType(candidate.entity_type),
            entity_name=candidate.entity_name,
            raw_entity_mention=candidate.raw_entity_mention or candidate.entity_name,
            matched_character_id=character_match.matched_character_id,
            match_status=character_match.match_status,
            attribute_name=candidate.attribute_name,
            attribute_value=candidate.attribute_value,
            value_type=SettingValueType(candidate.value_type),
            value_json=candidate.value_json,
            evidence_spans=[
                evidence_span.model_dump(mode="json")
                for evidence_span in candidate.evidence_spans
            ],
            confidence=_to_decimal(candidate.confidence),
            review_status=SettingCandidateReviewStatus.PENDING_REVIEW,
            raw_ai_result_json=candidate.model_dump(mode="json"),
        )


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
