"""Real deterministic persistence with a local SQLite transaction, never a shared DB."""

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import OrderedCandidateBinding
from app.analysis.schemas import ExtractedSettingCandidate
from app.models.analysis_job import AnalysisJob
from app.models.setting_candidate import SettingCandidate
from app.models.upload_batch import UploadBatch
from app.models.upload_file import UploadFile
from app.models.work import Work
from app.schemas.analysis_context import WorkerAnalysisContext
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext
from app.services.setting_candidate_service import SettingCandidateSaveItem, SettingCandidateService
from tests.test_setting_extractor import _valid_setting_payload


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def book_database(monkeypatch, request):
    import app.services.ordered_analysis_fence as fence
    from app.models.setting_book_chunk import SettingBookChunk
    from app.models.setting_book_revision import SettingBookRevision
    from app.services.setting_book_chunk_service import (
        SettingBookChunkService,
        setting_book_char_count,
    )

    now = datetime(2026, 10, 6, 12)
    monkeypatch.setattr(fence.func, "localtimestamp", lambda: literal(now))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for model in (Work, UploadBatch, UploadFile, SettingBookRevision, AnalysisJob,
                  SettingBookChunk, SettingCandidate):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    work_id, batch_id, book_id, revision_id, job_id, token = (uuid4() for _ in range(6))
    raw_text = getattr(request, "param", "\ufeff| 인물 | 근력 |\r\n| 아린 | 10 |\r\n\r\n- 세계 규칙: 낮은 길다.")
    char_count = setting_book_char_count(raw_text)
    source_hash = sha256(raw_text.encode()).hexdigest()
    context = WorkerAnalysisContext(run_id=uuid4(), generation=1, input_state_hash="a" * 64,
                                    source_hash=source_hash, format_version=1)
    write = OrderedCandidateWriteContext(
        token, context, None, "works/test/setting-books/revision.txt", "v1", None,
        source_kind="SETTING_BOOK", setting_book_id=book_id, setting_book_revision_id=revision_id,
        char_count=char_count,
    )
    with factory() as session:
        session.add_all([
            Work(id=work_id, member_id=1, title="테스트 작품", latest_episode_no=0, lifecycle_status="ACTIVE"),
            UploadBatch(id=batch_id, work_id=work_id, member_id=1, upload_type="SETTING_BOOK",
                        source_type="SINGLE", status="PENDING", file_count=1),
            UploadFile(id=book_id, batch_id=batch_id, file_role="SETTING_BOOK", original_filename="설정.txt",
                       file_size=100, parse_status="PARSED", current_setting_book_revision_id=revision_id),
            SettingBookRevision(id=revision_id, setting_book_id=book_id, revision_no=1,
                                content_s3_key=write.content_s3_key, content_s3_version="v1",
                                content_hash=source_hash, char_count=char_count),
            AnalysisJob(id=job_id, work_id=work_id, batch_id=batch_id, source_kind="SETTING_BOOK",
                        source_setting_book_id=book_id, source_setting_book_revision_id=revision_id,
                        job_type="SETTING_EXTRACTION", status="RUNNING", lease_token=token,
                        lease_expires_at=now + timedelta(minutes=5), analysis_mode="ORDERED_PROVISIONAL",
                        review_mode="AUTOMATIC", analysis_run_id=context.run_id, run_generation=1,
                        run_sequence=0, input_state_hash=context.input_state_hash,
                        source_content_hash=source_hash, source_content_s3_key=write.content_s3_key,
                        source_content_s3_version="v1", journal_status="PENDING"),
        ])
        session.commit()
    service = SettingBookChunkService(factory)
    try:
        yield factory, service, write, work_id, job_id, raw_text
    finally:
        engine.dispose()


def test_book_chunks_preserve_full_source_offsets_and_reload_same_ids(book_database):
    factory, service, write, work_id, job_id, text = book_database
    chunks = service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                                 work_id=work_id, ordered_write_context=write)
    assert "".join(chunk.chunk_text for chunk in chunks) == text
    assert chunks[0].start_offset == 0 and chunks[-1].end_offset == len(text)
    assert chunks[0].analysis_job_id == job_id
    assert chunks[0].setting_book_revision_id == write.setting_book_revision_id
    assert service.get_setting_book_chunks(job_id, write.setting_book_revision_id)[0].id == chunks[0].id
    with factory() as session:
        job = session.get(AnalysisJob, job_id)
        job.checkpoint_stage = "CHUNKS_READY"
        session.commit()
    with pytest.raises(ComparisonValidationError):
        service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                             work_id=work_id, ordered_write_context=write)
    assert service.get_setting_book_chunks(job_id, write.setting_book_revision_id)[0].id == chunks[0].id


@pytest.mark.parametrize("mutation", ["pointer", "archive", "lease", "generation", "hash", "key", "work", "source", "batch"])
def test_book_fence_rejects_stale_source_before_deleting_chunks(book_database, mutation):
    factory, service, write, work_id, job_id, text = book_database
    chunks = service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                                 work_id=work_id, ordered_write_context=write)
    with factory() as session:
        job = session.get(AnalysisJob, job_id)
        book = session.get(UploadFile, write.setting_book_id)
        if mutation == "pointer":
            book.current_setting_book_revision_id = uuid4()  # Even returning to the same text cannot revive a lease.
        elif mutation == "archive":
            book.archived_at = datetime(2026, 10, 6)
        elif mutation == "lease":
            job.lease_token = uuid4()
        elif mutation == "generation":
            job.run_generation = 2
        elif mutation == "hash":
            job.source_content_hash = "c" * 64
        elif mutation == "key":
            job.source_content_s3_key = "other-key"
        elif mutation == "work":
            session.get(Work, work_id).lifecycle_status = "PURGING"
        elif mutation == "batch":
            job.batch_id = uuid4()
        else:
            job.source_kind = "EPISODE"
        session.commit()
    with pytest.raises(ComparisonValidationError):
        service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                             work_id=work_id, ordered_write_context=write)
    assert service.get_setting_book_chunks(job_id, write.setting_book_revision_id)[0].id == chunks[0].id


@pytest.mark.parametrize("mutation", ["text", "offset", "index", "revision", "paragraph"])
def test_fenced_checkpoint_reload_rejects_corrupt_or_foreign_book_chunks(book_database, mutation):
    from app.models.setting_book_chunk import SettingBookChunk
    factory, service, write, work_id, job_id, text = book_database
    chunks = service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                                 work_id=work_id, ordered_write_context=write)
    with factory() as session:
        session.get(AnalysisJob, job_id).checkpoint_stage = "CHUNKS_READY"
        chunk = session.get(SettingBookChunk, chunks[0].id)
        if mutation == "text":
            chunk.chunk_text += "변조"
        elif mutation == "offset":
            chunk.end_offset += 1
        elif mutation == "index":
            chunk.chunk_index = 1
        elif mutation == "paragraph":
            chunk.paragraph_start_index += 1
        else:
            chunk.setting_book_revision_id = uuid4()
        session.commit()
    with pytest.raises(ComparisonValidationError):
        service.get_setting_book_chunks(job_id, write.setting_book_revision_id,
                                        work_id=work_id, ordered_write_context=write, raw_text=text)


def test_character_book_candidate_validates_chunk_owner_and_keeps_source_provenance(book_database):
    factory, chunks_service, write, work_id, job_id, text = book_database
    chunks = chunks_service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                                        work_id=work_id, ordered_write_context=write)
    data = _valid_setting_payload()
    data["source_chunk_id"] = str(chunks[0].id)
    candidate = ExtractedSettingCandidate.model_validate(data)
    item = SettingCandidateSaveItem(None, write.content_s3_key, candidate,
        OrderedCandidateBinding(uuid4(), actual_character_id=uuid4(), match_status="MATCHED"),
        source_kind="SETTING_BOOK", setting_book_id=write.setting_book_id,
        setting_book_revision_id=write.setting_book_revision_id)
    service = SettingCandidateService(factory)
    saved = service.replace_candidates_for_analysis_job(work_id, job_id, [item], [], write)
    assert saved[0].episode_id is None
    assert saved[0].source_kind == "SETTING_BOOK"
    assert saved[0].source_setting_book_id == write.setting_book_id
    assert saved[0].source_setting_book_revision_id == write.setting_book_revision_id
    bad = replace(item, candidate=candidate.model_copy(update={"source_chunk_id": uuid4()}))
    with pytest.raises(ComparisonValidationError):
        service.replace_candidates_for_analysis_job(work_id, job_id, [bad], [], write)
    with factory() as session:
        assert list(session.scalars(select(SettingCandidate.id))) == [saved[0].id]


def test_changed_text_cannot_replace_book_chunks_even_if_lease_is_current(book_database):
    _factory, service, write, work_id, job_id, text = book_database
    chunks = service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, text,
                                                 work_id=work_id, ordered_write_context=write)
    with pytest.raises(ComparisonValidationError):
        service.replace_setting_book_chunks(job_id, write.setting_book_revision_id, "다른 원문",
                                             work_id=work_id, ordered_write_context=write)
    assert service.get_setting_book_chunks(job_id, write.setting_book_revision_id)[0].id == chunks[0].id


def test_book_char_count_matches_backend_code_points_and_whitespace_rule():
    from app.services.setting_book_chunk_service import setting_book_char_count
    assert setting_book_char_count(" \t아린\u00a0😀\r\n") == 4
    assert setting_book_char_count("\ufeffA\u2007B\u202fC\u2003D") == 7
