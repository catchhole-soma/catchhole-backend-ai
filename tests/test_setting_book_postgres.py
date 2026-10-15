"""Book SQL/lock proof on the explicitly disposable, fully migrated local DB.

The shared integration fixture requires GH180_TEST_DATABASE_URL, localhost and
the exact gh180_ai_test name. No application .env or default DB URL is read.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import OrderedCandidateBinding
from app.analysis.schemas import ExtractedSettingCandidate
from app.services.setting_book_chunk_service import SettingBookChunkService, setting_book_char_count
from app.services.setting_candidate_service import SettingCandidateSaveItem
from tests.test_setting_extractor import _valid_discovery_payload, _valid_setting_payload

pytest_plugins = ["tests.test_ordered_analysis_fence_postgres"]
pytestmark = pytest.mark.integration


@pytest.fixture
def book_postgres(database):
    engine, candidates, old_kwargs, original = database
    params = {**original, "batch_id": uuid4(), "book_id": uuid4(), "revision_id": uuid4(), "next_revision_id": uuid4()}
    raw = "\n\n\ufeffSynthetic Character appeared.\r\nSynthetic Character reached level 12.\r\n\r\n"
    source_hash = sha256(raw.encode()).hexdigest()
    params.update(book_hash=source_hash, char_count=setting_book_char_count(raw))
    with engine.begin() as connection:
        for statement in (
            """INSERT INTO upload_batches(id,work_id,member_id,upload_type,source_type,status,file_count,created_at,updated_at)
            VALUES (:batch_id,:work_id,:member_id,'SETTING_BOOK','FILE','PROCESSING',1,localtimestamp,localtimestamp)""",
            """INSERT INTO upload_files(id,batch_id,file_role,original_filename,mime_type,file_size,parse_status,created_at,updated_at)
            VALUES (:book_id,:batch_id,'SETTING_BOOK','synthetic.txt','text/plain',100,'PARSED',localtimestamp,localtimestamp)""",
            """INSERT INTO setting_book_revisions(id,setting_book_id,revision_no,content_s3_key,content_s3_version,content_hash,char_count,created_at,updated_at)
            VALUES (:revision_id,:book_id,1,'test-book-source','v1',:book_hash,:char_count,localtimestamp,localtimestamp),
                   (:next_revision_id,:book_id,2,'test-book-next','v2',:book_hash,:char_count,localtimestamp,localtimestamp)""",
            "UPDATE upload_files SET current_setting_book_revision_id=:revision_id WHERE id=:book_id",
            """UPDATE analysis_jobs SET batch_id=:batch_id,episode_id=NULL,source_episode_no=NULL,source_kind='SETTING_BOOK',
            source_setting_book_id=:book_id,source_setting_book_revision_id=:revision_id,review_mode='AUTOMATIC',
            source_content_hash=:book_hash,source_content_s3_key='test-book-source',source_content_s3_version='v1',checkpoint_stage=NULL
            WHERE id=:job_id""",
        ):
            connection.execute(text(statement), params)
    write = replace(old_kwargs["ordered_write_context"], episode_id=None, episode_no=None,
        source_kind="SETTING_BOOK", setting_book_id=params["book_id"], setting_book_revision_id=params["revision_id"],
        content_s3_key="test-book-source", content_s3_version="v1", char_count=params["char_count"],
        context=old_kwargs["ordered_write_context"].context.model_copy(update={"source_hash": source_hash}))
    chunks = SettingBookChunkService(sessionmaker(engine, expire_on_commit=False))
    chunk_kwargs = dict(analysis_job_id=params["job_id"], setting_book_revision_id=params["revision_id"],
        work_id=params["work_id"], ordered_write_context=write, raw_text=raw)
    saved_chunks = chunks.replace_setting_book_chunks(**chunk_kwargs)
    discovery_id = uuid4()
    items = []
    for payload, candidate_id in [(_valid_discovery_payload(), discovery_id), (_valid_setting_payload(), uuid4())]:
        payload["source_chunk_id"] = str(saved_chunks[0].id)
        quote = payload["evidence_spans"][0]["quote"]
        start = raw.index(quote)
        payload["evidence_spans"][0].update(start_offset=start, end_offset=start + len(quote))
        items.append(SettingCandidateSaveItem(None, "test-book-source", ExtractedSettingCandidate.model_validate(payload),
            OrderedCandidateBinding(candidate_id, provisional_subject_key=f"provisional-character:{discovery_id}", match_status="MATCHED"),
            source_kind="SETTING_BOOK", setting_book_id=params["book_id"], setting_book_revision_id=params["revision_id"]))
    kwargs = {**old_kwargs, "ordered_write_context": write, "save_items": items}
    try:
        yield engine, candidates, kwargs, params, chunks, chunk_kwargs, saved_chunks
    finally:
        with engine.begin() as connection:
            for statement in (
                "DELETE FROM setting_candidates WHERE analysis_job_id=:job_id",
                "DELETE FROM setting_book_chunks WHERE analysis_job_id=:job_id",
                """UPDATE analysis_jobs SET batch_id=NULL,episode_id=:episode_id,source_episode_no=1,source_kind='EPISODE',
                source_setting_book_id=NULL,source_setting_book_revision_id=NULL,review_mode='MANUAL',source_content_hash=:source_hash
                WHERE id=:job_id""",
                "UPDATE upload_files SET current_setting_book_revision_id=NULL WHERE id=:book_id",
                "DELETE FROM setting_book_revisions WHERE setting_book_id=:book_id",
                "DELETE FROM upload_files WHERE id=:book_id",
                "DELETE FROM upload_batches WHERE id=:batch_id",
            ):
                connection.execute(text(statement), params)


def _candidate_ids(engine, params):
    with engine.connect() as connection:
        return list(connection.execute(text("SELECT id FROM setting_candidates WHERE analysis_job_id=:job_id ORDER BY id"), params).scalars())


def test_migrated_book_chunks_candidates_and_checkpoint_reload_preserve_source(book_postgres):
    engine, service, kwargs, params, chunks, chunk_kwargs, original_chunks = book_postgres
    saved = service.replace_candidates_for_analysis_job(**kwargs)
    assert len(saved) == 2
    assert all(row.episode_id is None and row.source_kind == "SETTING_BOOK" for row in saved)
    assert all(row.source_setting_book_revision_id == params["revision_id"] for row in saved)
    assert all(row.source_chunk_id == original_chunks[0].id for row in saved)
    with engine.begin() as connection:
        connection.execute(text("UPDATE analysis_jobs SET checkpoint_stage='CHARACTER_CANDIDATES_SAVED' WHERE id=:job_id"), params)
    reloaded = chunks.get_setting_book_chunks(**chunk_kwargs)
    assert [row.id for row in reloaded] == [row.id for row in original_chunks]
    assert reloaded[0].start_offset == 2 and reloaded[-1].end_offset < len(chunk_kwargs["raw_text"])


@pytest.mark.parametrize("mutation", ["archive", "revision-pointer", "lease", "work", "foreign-chunk"])
def test_migrated_book_source_fence_never_deletes_saved_candidates(book_postgres, mutation):
    engine, service, kwargs, params, _chunks, _chunk_kwargs, _original_chunks = book_postgres
    service.replace_candidates_for_analysis_job(**kwargs)
    before = _candidate_ids(engine, params)
    changes = {"archive": "UPDATE upload_files SET archived_at=localtimestamp WHERE id=:book_id",
        "revision-pointer": "UPDATE upload_files SET current_setting_book_revision_id=:next_revision_id WHERE id=:book_id",
        "lease": "UPDATE analysis_jobs SET lease_token=gen_random_uuid() WHERE id=:job_id",
        "work": "UPDATE works SET lifecycle_status='PURGING' WHERE id=:work_id"}
    if mutation == "foreign-chunk":
        item = kwargs["save_items"][0]
        kwargs = {**kwargs, "save_items": [replace(item, candidate=item.candidate.model_copy(update={"source_chunk_id": uuid4()}))]}
    else:
        with engine.begin() as connection:
            connection.execute(text(changes[mutation]), params)
    with pytest.raises(ComparisonValidationError):
        service.replace_candidates_for_analysis_job(**kwargs)
    assert _candidate_ids(engine, params) == before


@pytest.mark.parametrize("mutation", ["reclaimed-lease", "same-hash-new-revision", "chunks-checkpoint"])
def test_real_book_work_lock_serializes_source_changes_before_waiting_worker_writes(book_postgres, mutation):
    engine, service, kwargs, params, chunks, chunk_kwargs, original_chunks = book_postgres
    service.replace_candidates_for_analysis_job(**kwargs)
    before = _candidate_ids(engine, params)
    query_started = Event()

    def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FROM works" in statement and "FOR UPDATE" in statement:
            query_started.set()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as winner:
                winner.execute(text("SELECT id FROM works WHERE id=:work_id FOR UPDATE"), params)
                query_started.clear()
                pending = pool.submit(chunks.replace_setting_book_chunks, **chunk_kwargs) if mutation == "chunks-checkpoint" else pool.submit(service.replace_candidates_for_analysis_job, **kwargs)
                assert query_started.wait(5), "The worker did not attempt its actual Work row lock."
                with pytest.raises(TimeoutError):
                    pending.result(timeout=0.1)
                statement = {"reclaimed-lease": "UPDATE analysis_jobs SET lease_token=gen_random_uuid() WHERE id=:job_id",
                    "same-hash-new-revision": "UPDATE upload_files SET current_setting_book_revision_id=:next_revision_id WHERE id=:book_id",
                    "chunks-checkpoint": "UPDATE analysis_jobs SET checkpoint_stage='CHUNKS_READY' WHERE id=:job_id"}[mutation]
                winner.execute(text(statement), params)
            with pytest.raises(ComparisonValidationError, match="stale"):
                pending.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    assert _candidate_ids(engine, params) == before
    assert [row.id for row in chunks.get_setting_book_chunks(params["job_id"], params["revision_id"])] == [row.id for row in original_chunks]
