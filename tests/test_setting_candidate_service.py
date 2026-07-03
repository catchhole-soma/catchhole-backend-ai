from uuid import UUID

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.domain.enums import SettingCandidateMatchStatus
from app.models.setting_candidate import SettingCandidate
from app.services.setting_candidate_service import (
    SettingCandidateSaveItem,
    SettingCandidateService,
)

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000002")
ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000004")


def test_replace_candidates_for_analysis_job_deletes_old_candidates_and_saves_new_ones() -> None:
    # 같은 analysis_job 기준으로 기존 후보를 지우고 새 후보를 한 트랜잭션으로 저장하는지 확인한다.
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
        known_character_provider=lambda work_id: [
            KnownCharacter(character_id=UUID("00000000-0000-0000-0000-000000000005"), name="비요른")
        ],
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                candidate=_candidate(),
            )
        ],
    )

    assert repository.deleted_analysis_job_ids == [ANALYSIS_JOB_ID]
    assert len(repository.saved_candidates) == 1
    assert saved_candidates == repository.saved_candidates
    assert repository.saved_candidates[0].work_id == WORK_ID
    assert repository.saved_candidates[0].episode_id == EPISODE_ID
    assert repository.saved_candidates[0].matched_character_id == UUID("00000000-0000-0000-0000-000000000005")
    assert repository.saved_candidates[0].match_status == SettingCandidateMatchStatus.MATCHED
    assert session.committed is True
    assert session.rolled_back is False


def _candidate() -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name="비요른",
        raw_entity_mention="비요른",
        attribute_name="level",
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="비요른은 1레벨 바바리안이다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )


class FakeSession:
    # with self.session_factory() as session 흐름과 commit/rollback 호출을 기록한다.
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


class FakeSettingCandidateRepository:
    # 실제 DB repository 대신 삭제/저장 요청을 기록한다.
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.deleted_analysis_job_ids: list[UUID] = []
        self.saved_candidates: list[SettingCandidate] = []

    def delete_by_analysis_job_id(self, analysis_job_id: UUID) -> None:
        self.deleted_analysis_job_ids.append(analysis_job_id)

    def save_all(self, candidates: list[SettingCandidate]) -> list[SettingCandidate]:
        self.saved_candidates.extend(candidates)
        return candidates
