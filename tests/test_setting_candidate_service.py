from uuid import UUID

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.domain.enums import SettingCandidateKind, SettingCandidateMatchStatus
from app.models.setting_candidate import SettingCandidate
from app.services.setting_candidate_service import (
    SettingCandidateSaveItem,
    SettingCandidateService,
)

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000002")
ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000004")
OTHER_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000006")
SOURCE_CONTENT_S3_KEY = "works/work-id/episodes/episode-id/source.txt"


def test_replace_candidates_for_analysis_job_deletes_old_candidates_and_saves_new_ones() -> None:
    # 같은 analysis_job 기준으로 기존 후보를 지우고 새 후보를 한 트랜잭션으로 저장하는지 확인한다.
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(),
            )
        ],
        known_characters=[
            KnownCharacter(character_id=UUID("00000000-0000-0000-0000-000000000005"), name="비요른")
        ],
    )

    assert repository.deleted_analysis_job_ids == [ANALYSIS_JOB_ID]
    assert len(repository.saved_candidates) == 1
    assert saved_candidates == repository.saved_candidates
    assert repository.saved_candidates[0].work_id == WORK_ID
    assert repository.saved_candidates[0].episode_id == EPISODE_ID
    assert repository.saved_candidates[0].source_content_s3_key == SOURCE_CONTENT_S3_KEY
    assert repository.saved_candidates[0].matched_character_id == UUID("00000000-0000-0000-0000-000000000005")
    assert repository.saved_candidates[0].match_status == SettingCandidateMatchStatus.MATCHED
    assert session.committed is True
    assert session.rolled_back is False


def test_replace_candidates_saves_unknown_subject_as_ambiguous() -> None:
    # subject fallback이 해소하지 못한 표준 placeholder는 새 캐릭터 후보가 아니라
    # 사용자가 기존 캐릭터 연결을 확인해야 하는 AMBIGUOUS 상태로 저장한다.
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(
                    entity_name="미상",
                    raw_entity_mention="내려다 본 손",
                ),
            )
        ],
        known_characters=[
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000005"),
                name="비요른",
            )
        ],
    )

    assert saved_candidates[0].entity_name == "미상"
    assert saved_candidates[0].matched_character_id is None
    assert saved_candidates[0].match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert session.committed is True
    assert session.rolled_back is False


def test_replace_candidates_skips_discovery_for_known_character() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_discovery_candidate("비요른"),
            )
        ],
        known_characters=[
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000005"),
                name="비요른",
            )
        ],
    )

    assert saved_candidates == []
    assert repository.saved_candidates == []
    assert session.committed is True


def test_replace_candidates_deduplicates_new_character_discoveries_by_name() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_discovery_candidate("세룸", "케닉의 넷째 아들 세룸"),
            ),
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_discovery_candidate(" 세룸 ", "세룸"),
            ),
        ],
        known_characters=[
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000005"),
                name="케닉",
            )
        ],
    )

    assert len(saved_candidates) == 1
    assert saved_candidates[0].candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY
    assert saved_candidates[0].entity_name == "세룸"
    assert saved_candidates[0].raw_entity_mention == "케닉의 넷째 아들 세룸"
    assert saved_candidates[0].match_status == SettingCandidateMatchStatus.UNRESOLVED


def test_replace_candidates_deduplicates_identical_settings_and_keeps_clearer_evidence() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(
                    source_chunk_id=CHUNK_ID,
                    raw_entity_mention="그",
                    confidence=0.7,
                ),
            ),
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(
                    source_chunk_id=OTHER_CHUNK_ID,
                    raw_entity_mention="비요른",
                    attribute_value="Lv.1",
                    confidence=0.95,
                ),
            ),
        ],
        known_characters=[
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000005"),
                name="비요른",
            )
        ],
    )

    assert len(saved_candidates) == 1
    assert saved_candidates[0].source_chunk_id == OTHER_CHUNK_ID
    assert saved_candidates[0].raw_entity_mention == "비요른"
    assert str(saved_candidates[0].confidence) == "0.95"


def test_replace_candidates_keeps_same_setting_when_structured_value_changes() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(source_chunk_id=CHUNK_ID, level=1),
            ),
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(source_chunk_id=OTHER_CHUNK_ID, level=2),
            ),
        ],
        known_characters=[],
    )

    assert len(saved_candidates) == 2
    assert [candidate.value_json for candidate in saved_candidates] == [
        {"value": 1},
        {"value": 2},
    ]


def test_replace_candidates_does_not_deduplicate_ambiguous_setting_subjects() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session,
        repository_factory=lambda session: repository,
    )

    saved_candidates = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(
                    entity_name="미상",
                    raw_entity_mention="그",
                    source_chunk_id=CHUNK_ID,
                ),
            ),
            SettingCandidateSaveItem(
                episode_id=EPISODE_ID,
                source_content_s3_key=SOURCE_CONTENT_S3_KEY,
                candidate=_candidate(
                    entity_name="미상",
                    raw_entity_mention="그녀",
                    source_chunk_id=OTHER_CHUNK_ID,
                ),
            ),
        ],
        known_characters=[],
    )

    assert len(saved_candidates) == 2
    assert all(
        candidate.match_status == SettingCandidateMatchStatus.AMBIGUOUS
        for candidate in saved_candidates
    )


def _candidate(
    entity_name: str = "비요른",
    raw_entity_mention: str | None = "비요른",
    source_chunk_id: UUID = CHUNK_ID,
    level: int = 1,
    attribute_value: str | None = None,
    confidence: float = 0.9,
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=source_chunk_id,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name="level",
        attribute_value=attribute_value or str(level),
        value_type="NUMBER",
        value_json={"value": level},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote=f"비요른은 {level}레벨 바바리안이다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=confidence,
    )


def _discovery_candidate(
    entity_name: str,
    raw_entity_mention: str | None = None,
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        candidate_kind="CHARACTER_DISCOVERY",
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention or entity_name,
        attribute_name=None,
        attribute_value=None,
        value_type=None,
        value_json=None,
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="케닉의 넷째 아들 세룸은 나와라!",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.95,
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
