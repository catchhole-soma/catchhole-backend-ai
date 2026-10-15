"""Same raw text cannot bypass a frozen reference-time write fence."""

from dataclasses import replace

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.models.setting_book_revision import SettingBookRevision
from tests.test_setting_book_storage import book_database as book_database


def test_reference_time_mismatch_cannot_replace_persisted_book_chunks(book_database):
    factory, service, write, work_id, job_id, text = book_database
    chunks = service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
        work_id=work_id, ordered_write_context=write)
    changed_context = write.context.model_copy(update={"reference_time_kind": "WORK_START"})
    changed = replace(write, context=changed_context, reference_time_kind="WORK_START")
    with pytest.raises(ComparisonValidationError):
        service.replace_setting_book_chunks(job_id, changed.setting_book_revision_id, text,
            work_id=work_id, ordered_write_context=changed)
    assert [chunk.id for chunk in service.get_setting_book_chunks(job_id, write.setting_book_revision_id)] == [chunk.id for chunk in chunks]


def test_validated_book_write_retains_selected_time_and_keeps_raw_hash(book_database):
    factory, service, write, work_id, job_id, text = book_database
    with factory() as session:
        revision = session.get(SettingBookRevision, write.setting_book_revision_id)
        revision.reference_time_kind = "EPISODE"
        revision.reference_episode_no = 30
        session.commit()
    context = write.context.model_copy(update={"reference_time_kind": "EPISODE", "reference_episode_no": 30})
    current = replace(write, context=context, reference_time_kind="EPISODE", reference_episode_no=30)
    chunks = service.replace_setting_book_chunks(job_id, current.setting_book_revision_id, text,
        work_id=work_id, ordered_write_context=current)
    assert chunks[0].analysis_job_id == job_id and chunks[0].setting_book_revision_id == write.setting_book_revision_id
    with factory() as session:
        revision = session.get(SettingBookRevision, write.setting_book_revision_id)
        assert revision.content_hash == write.context.source_hash
        assert revision.reference_episode_no == 30

