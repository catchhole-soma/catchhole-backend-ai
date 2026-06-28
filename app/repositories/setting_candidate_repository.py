from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.setting_candidate import SettingCandidate


class SettingCandidateRepository:
    # AI가 추출한 검토 전 후보의 DB 저장만 담당한다. commit/rollback은 Service에서 처리한다.
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_all(self, candidates: list[SettingCandidate]) -> list[SettingCandidate]:
        # 검증이 끝난 후보들을 한 번에 저장 대기 상태로 올린다.
        self.session.add_all(candidates)
        return candidates

    def delete_by_analysis_job_id(self, analysis_job_id: UUID) -> None:
        # 같은 분석 job을 재실행할 때 이전 후보가 중복으로 남지 않도록 정리한다.
        statement = delete(SettingCandidate).where(
            SettingCandidate.analysis_job_id == analysis_job_id
        )
        self.session.execute(statement)
