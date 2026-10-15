from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.setting_book_chunk import SettingBookChunk


class SettingBookChunkRepository:
    def __init__(self, session: Session):
        self.session = session

    def find_by_source(self, analysis_job_id: UUID, revision_id: UUID) -> list[SettingBookChunk]:
        return list(self.session.scalars(select(SettingBookChunk).where(
            SettingBookChunk.analysis_job_id == analysis_job_id,
            SettingBookChunk.setting_book_revision_id == revision_id,
        ).order_by(SettingBookChunk.chunk_index)))

    def delete_by_analysis_job_id(self, analysis_job_id: UUID):
        self.session.execute(delete(SettingBookChunk).where(SettingBookChunk.analysis_job_id == analysis_job_id))

    def save_all(self, chunks):
        self.session.add_all(chunks)
        return chunks
