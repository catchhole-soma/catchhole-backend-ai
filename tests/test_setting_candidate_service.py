from typing import Self
from uuid import UUID

import pytest

from app.analysis.character_name_resolver import CharacterNameMatch, KnownCharacter
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


def test_matched_aliases_save_one_representative_name_without_rewriting_source_candidates() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session, repository_factory=lambda session: repository,
    )
    character_id = UUID("00000000-0000-0000-0000-000000000005")
    originals = []
    for index, (name, raw, active) in enumerate([
        ("리안", "리안", True),
        ("리안 플로", "그", False),
        ("리안", "리안", True),
    ]):
        originals.append(_candidate(entity_name=name, raw_entity_mention=raw).model_copy(update={
            "attribute_name": "status.발열", "attribute_value": "발열 관찰",
            "value_type": "JSON", "value_json": {"name": "발열", "active": active},
            "evidence_spans": [ExtractedEvidenceSpan(
                quote="리안의 상태를 확인했다.", start_offset=index * 30,
                end_offset=index * 30 + len("리안의 상태를 확인했다."),
            )],
        }))
    before = [candidate.model_dump(mode="json") for candidate in originals]
    saved = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID, analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[SettingCandidateSaveItem(
            episode_id=EPISODE_ID, source_content_s3_key=SOURCE_CONTENT_S3_KEY, candidate=candidate,
        ) for candidate in originals],
        known_characters=[KnownCharacter(character_id=character_id, name="리안 플로")],
    )

    assert len(saved) == 3
    assert len({candidate.id for candidate in saved}) == 3
    assert [candidate.entity_name for candidate in saved] == ["리안 플로"] * 3
    assert [candidate.model_dump(mode="json") for candidate in originals] == before
    for row, source in zip(saved, before, strict=True):
        assert row.matched_character_id == character_id
        assert row.match_status == SettingCandidateMatchStatus.MATCHED
        assert row.work_id == WORK_ID and row.episode_id == EPISODE_ID
        assert row.analysis_job_id == ANALYSIS_JOB_ID and row.source_chunk_id == CHUNK_ID
        assert row.source_content_s3_key == SOURCE_CONTENT_S3_KEY
        assert row.raw_entity_mention == source["raw_entity_mention"]
        assert row.evidence_spans == source["evidence_spans"]
        assert row.value_json == source["value_json"]
        assert row.attribute_name == source["attribute_name"]
        assert row.attribute_value == source["attribute_value"]
        assert row.raw_ai_result_json == source


@pytest.mark.parametrize(("status", "matched_id", "known_name"), [
    (SettingCandidateMatchStatus.AMBIGUOUS, 5, "리안 플로"),
    (SettingCandidateMatchStatus.UNRESOLVED, 5, "리안 플로"),
    (SettingCandidateMatchStatus.AUTO_MATCHED_BY_NAME, 5, "리안 플로"),
    (SettingCandidateMatchStatus.MATCHED, 7, "리안 플로"),
    (SettingCandidateMatchStatus.MATCHED, None, "리안 플로"),
    (SettingCandidateMatchStatus.MATCHED, 5, " \t "),
])
def test_representative_name_requires_matched_known_id_and_nonblank_name(
    monkeypatch: pytest.MonkeyPatch,
    status: SettingCandidateMatchStatus,
    matched_id: int | None,
    known_name: str,
) -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session, repository_factory=lambda session: repository,
    )
    match = CharacterNameMatch(
        matched_character_id=UUID(int=matched_id) if matched_id is not None else None,
        match_status=status,
    )
    monkeypatch.setattr(
        "app.services.setting_candidate_service.resolve_candidate_character", lambda *args: match,
    )
    original = _candidate(entity_name="리안", raw_entity_mention="리안")
    saved = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID, analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[SettingCandidateSaveItem(
            episode_id=EPISODE_ID, source_content_s3_key=SOURCE_CONTENT_S3_KEY, candidate=original,
        )],
        known_characters=[KnownCharacter(character_id=UUID(int=5), name=known_name)],
    )

    assert saved[0].entity_name == "리안"
    assert saved[0].matched_character_id == match.matched_character_id
    assert saved[0].match_status == status
    assert saved[0].raw_ai_result_json == original.model_dump(mode="json")


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


def test_replace_candidates_skips_ambiguous_discovery_for_existing_characters() -> None:
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
                name="비요른 얀델",
            ),
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000007"),
                name="비요른 라프손",
            ),
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


def test_identity_unresolved_discoveries_are_preserved_without_unknown_name_dedup() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session, repository_factory=lambda session: repository,
    )
    originals = [_discovery_candidate("미상", name) for name in ("리안", "리안 플로")]
    saved = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID, analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[SettingCandidateSaveItem(
            episode_id=EPISODE_ID, source_content_s3_key=SOURCE_CONTENT_S3_KEY, candidate=row,
        ) for row in originals],
        known_characters=[],
    )
    assert len(saved) == 2
    assert [row.raw_entity_mention for row in saved] == ["리안", "리안 플로"]
    assert all(row.entity_name == "미상" for row in saved)
    assert all(row.match_status == SettingCandidateMatchStatus.AMBIGUOUS for row in saved)
    assert all(row.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY for row in saved)
    assert all(row.source_chunk_id == CHUNK_ID and row.episode_id == EPISODE_ID for row in saved)
    assert all(row.matched_character_id is None for row in saved)


def test_status_recurrence_keeps_each_event_but_deduplicates_the_same_observation() -> None:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session, repository_factory=lambda session: repository,
    )
    observations = []
    for active, quote, offset in (
        (False, "카엘의 열이 가라앉았다.", 0),
        (True, "카엘은 다시 열이 올랐다.", 30),
        (False, "밤이 되자 카엘의 열이 내렸다.", 60),
    ):
        observations.append(_candidate().model_copy(update={
            "entity_name": "카엘", "raw_entity_mention": "카엘",
            "attribute_name": "status.발열", "value_type": "JSON",
            "value_json": {"name": "발열", "active": active},
            "evidence_spans": [ExtractedEvidenceSpan(
                quote=quote, start_offset=offset, end_offset=offset + len(quote),
            )],
            "confidence": 0.8,
        }))
    duplicate = observations[-1].model_copy(update={"confidence": 0.95})
    saved = service.replace_candidates_for_analysis_job(
        work_id=WORK_ID, analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[SettingCandidateSaveItem(
            episode_id=EPISODE_ID, source_content_s3_key=SOURCE_CONTENT_S3_KEY,
            candidate=candidate,
        ) for candidate in [*observations, duplicate]],
        known_characters=[],
    )

    assert [candidate.value_json["active"] for candidate in saved] == [False, True, False]
    assert str(saved[-1].confidence) == "0.95"
    assert [candidate.evidence_spans[0]["start_offset"] for candidate in saved] == [0, 30, 60]


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
                    entity_name="비요른 얀델",
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
    assert saved_candidates[0].entity_name == "비요른"
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


def _save_dedup_candidates(candidates: list[ExtractedSettingCandidate]) -> list[SettingCandidate]:
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(
        session_factory=lambda: session, repository_factory=lambda session: repository,
    )
    return service.replace_candidates_for_analysis_job(
        work_id=WORK_ID, analysis_job_id=ANALYSIS_JOB_ID,
        save_items=[SettingCandidateSaveItem(
            episode_id=EPISODE_ID, source_content_s3_key=SOURCE_CONTENT_S3_KEY, candidate=candidate,
        ) for candidate in candidates], known_characters=[],
    )


def _status_phase_candidate(kind: str | None, *, active: bool | None = True) -> ExtractedSettingCandidate:
    quote = "리안은 다쳤고 상처가 더 심해졌다."
    value = {"name": "부상"}
    if active is not None:
        value["active"] = active
    candidate = _candidate().model_copy(update={
        "entity_name": "리안", "raw_entity_mention": "리안", "attribute_name": "status.부상",
        "attribute_value": "원문의 상태 관찰", "value_type": "JSON", "value_json": value,
        "evidence_spans": [ExtractedEvidenceSpan(quote=quote, start_offset=0, end_offset=len(quote))],
    })
    candidate._status_observation_kind = kind
    candidate._status_observation_group = (CHUNK_ID, 0)
    return candidate


@pytest.mark.parametrize("first_kind,second_kind,active", [
    ("START", "CHANGE", True), ("START", "CONTINUE", True), ("PAST", "HYPOTHETICAL", None),
])
def test_status_phases_with_identical_source_and_value_preserve_both_observations(
    first_kind, second_kind, active,
):
    candidates = [_status_phase_candidate(first_kind, active=active),
                  _status_phase_candidate(second_kind, active=active)]
    candidates[0].attribute_value = "첫 번째 원형 설명"
    candidates[1].attribute_value = "두 번째 원형 설명"
    before = [candidate.model_dump(mode="json") for candidate in candidates]

    saved = _save_dedup_candidates(candidates)

    assert len(saved) == 2
    assert len({row.id for row in saved}) == 2
    assert [row.raw_ai_result_json for row in saved] == before
    assert [row.attribute_value for row in saved] == [candidate.attribute_value for candidate in candidates]
    assert [row.value_json for row in saved] == [candidate.value_json for candidate in candidates]
    assert [candidate.model_dump(mode="json") for candidate in candidates] == before
    assert all("_status_observation_kind" not in row.raw_ai_result_json
               and "_status_observation_group" not in row.raw_ai_result_json for row in saved)


@pytest.mark.parametrize("legacy_first", [False, True])
def test_legacy_inactive_and_explicit_end_still_deduplicate_exact_same_observation(legacy_first):
    legacy = _status_phase_candidate(None, active=False).model_copy(update={"confidence": 0.95})
    legacy._status_observation_group = None
    explicit = _status_phase_candidate("END", active=False).model_copy(update={"confidence": 0.5})
    candidates = [legacy, explicit] if legacy_first else [explicit, legacy]

    saved = _save_dedup_candidates(candidates)

    assert len(saved) == 1
    assert saved[0].raw_ai_result_json == legacy.model_dump(mode="json")
    assert str(saved[0].confidence) == "0.95"


def test_producer_group_index_does_not_make_same_phase_observation_distinct():
    first = _status_phase_candidate("START").model_copy(update={"confidence": 0.5})
    second = first.model_copy(update={"confidence": 0.95})
    second._status_observation_group = (CHUNK_ID, 7)

    saved = _save_dedup_candidates([first, second])

    assert len(saved) == 1
    assert saved[0].raw_ai_result_json == second.model_dump(mode="json")


def test_nonstatus_duplicate_key_ignores_status_private_metadata():
    first = _candidate(confidence=0.5)
    first._status_observation_kind = "START"
    first._status_observation_group = (CHUNK_ID, 0)
    second = _candidate(source_chunk_id=OTHER_CHUNK_ID, confidence=0.95)
    second._status_observation_kind = "CHANGE"
    second._status_observation_group = (OTHER_CHUNK_ID, 1)

    saved = _save_dedup_candidates([first, second])

    assert len(saved) == 1
    assert saved[0].source_chunk_id == OTHER_CHUNK_ID
    assert saved[0].raw_ai_result_json == second.model_dump(mode="json")


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

    def __enter__(self) -> Self:
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
