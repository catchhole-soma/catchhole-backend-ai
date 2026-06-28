from decimal import Decimal
from uuid import UUID

from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.domain.enums import SettingCandidateReviewStatus, SettingEntityType, SettingValueType
from app.mappers.setting_candidate_mapper import SettingCandidateMapper

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000002")
ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000004")


def test_to_entity_maps_extracted_candidate_to_setting_candidate() -> None:
    # LLM 검증을 통과한 내부 후보가 Spring setting_candidates 컬럼 구조로 변환되는지 확인한다.
    candidate = ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name="비요른",
        attribute_name="level",
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="비요른은 1레벨 바바리안이다.",
                start_offset=0,
                end_offset=18,
            )
        ],
        confidence=0.95,
    )

    entity = SettingCandidateMapper.to_entity(
        work_id=WORK_ID,
        episode_id=EPISODE_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        candidate=candidate,
    )

    assert entity.work_id == WORK_ID
    assert entity.episode_id == EPISODE_ID
    assert entity.source_chunk_id == CHUNK_ID
    assert entity.analysis_job_id == ANALYSIS_JOB_ID
    assert entity.entity_type == SettingEntityType.CHARACTER
    assert entity.entity_name == "비요른"
    assert entity.attribute_name == "level"
    assert entity.attribute_value == "1"
    assert entity.value_type == SettingValueType.NUMBER
    assert entity.value_json == {"value": 1}
    assert entity.evidence_spans == [
        {
            "quote": "비요른은 1레벨 바바리안이다.",
            "start_offset": 0,
            "end_offset": 18,
        }
    ]
    assert entity.confidence == Decimal("0.95")
    assert entity.review_status == SettingCandidateReviewStatus.PENDING_REVIEW
    assert entity.raw_ai_result_json["entity_name"] == "비요른"
    assert entity.created_at is None
    assert entity.updated_at is None
