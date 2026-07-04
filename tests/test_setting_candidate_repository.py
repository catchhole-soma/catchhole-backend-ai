from uuid import uuid4

from app.models.setting_candidate import SettingCandidate
from app.repositories.setting_candidate_repository import SettingCandidateRepository


def test_save_all_adds_setting_candidates() -> None:
    # 검증된 후보 목록을 session.add_all에 전달하는지 확인한다.
    session = FakeSession()
    repository = SettingCandidateRepository(session)
    candidates = [_candidate(), _candidate()]

    saved_candidates = repository.save_all(candidates)

    assert saved_candidates == candidates
    assert session.added_items == candidates


def test_delete_by_analysis_job_id_executes_delete_statement() -> None:
    # 같은 분석 작업을 재실행할 때 이전 후보 삭제 쿼리를 session에 전달하는지 확인한다.
    session = FakeSession()
    repository = SettingCandidateRepository(session)

    repository.delete_by_analysis_job_id(uuid4())

    assert session.executed_statement is not None


def _candidate() -> SettingCandidate:
    return SettingCandidate(
        id=uuid4(),
        work_id=uuid4(),
        episode_id=uuid4(),
        source_chunk_id=uuid4(),
        analysis_job_id=uuid4(),
        entity_type="CHARACTER",
        entity_name="비요른",
        raw_entity_mention="비요른",
        matched_character_id=None,
        match_status="UNRESOLVED",
        attribute_name="level",
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[],
        confidence=None,
        review_status="PENDING_REVIEW",
        raw_ai_result_json={},
        created_at=None,
        updated_at=None,
    )


class FakeSession:
    # 실제 DB session 대신 Repository가 호출한 메서드와 전달된 값을 기록한다.
    def __init__(self) -> None:
        self.added_items: list[SettingCandidate] = []
        self.executed_statement = None

    def add_all(self, items: list[SettingCandidate]) -> None:
        self.added_items.extend(items)

    def execute(self, statement) -> None:
        self.executed_statement = statement
