"""Real PostgreSQL races against the migrated, disposable gh180_ai_test database.

Never reads DATABASE_URL or the application's .env. Set GH180_TEST_DATABASE_URL
explicitly; this suite refuses any database other than gh180_ai_test on localhost.
"""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import OrderedCandidateBinding
from app.analysis.schemas import ExtractedSettingCandidate
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext
from app.services.setting_candidate_service import SettingCandidateSaveItem, SettingCandidateService
from app.services.episode_chunk_service import EpisodeChunkService
from tests.test_ordered_analysis_runtime import _input_context
from tests.test_setting_extractor import _valid_discovery_payload, _valid_setting_payload

pytestmark = pytest.mark.integration


@pytest.fixture
def database():
    value = os.environ.get("GH180_TEST_DATABASE_URL")
    if not value:
        pytest.skip("GH180_TEST_DATABASE_URL was not explicitly provided.")
    url = make_url(value)
    if url.database != "gh180_ai_test" or url.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Only the task's disposable localhost gh180_ai_test database is allowed.")
    engine = create_engine(url, connect_args={"options": "-c timezone=Asia/Seoul"})
    member_id = uuid4().int % (2**62)
    work_id, episode_id, job_id, lease_token = (uuid4() for _ in range(4))
    ctx = _input_context()
    params = dict(
        member_id=member_id,
        work_id=work_id,
        episode_id=episode_id,
        job_id=job_id,
        lease_token=lease_token,
        run_id=ctx.run_id,
        input_hash=ctx.input_state_hash,
        source_hash=ctx.source_hash,
        email=f"gh180-{member_id}@test.invalid",
        phone=str(member_id),
    )
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO members
            (id,email,password_hash,phone_number,phone_verified,display_name,status,role,created_at,updated_at)
            VALUES (:member_id,:email,'test-only',:phone,false,'test','ACTIVE','USER',localtimestamp,localtimestamp)"""),
            params,
        )
        connection.execute(
            text("""INSERT INTO works
            (id,member_id,title,genre,latest_episode_no,created_at,updated_at)
            VALUES (:work_id,:member_id,'ordered fence test','FANTASY',1,localtimestamp,localtimestamp)"""),
            params,
        )
        connection.execute(
            text("""INSERT INTO episodes
            (id,work_id,episode_no,content_s3_key,content_s3_version,content_hash,char_count,status,
             created_at,updated_at,content_updated_at)
            VALUES (:episode_id,:work_id,1,'test-source','version-1',:source_hash,100,'ANALYZING',
                    localtimestamp,localtimestamp,localtimestamp)"""),
            params,
        )
        connection.execute(
            text("""INSERT INTO analysis_jobs
            (id,work_id,episode_id,job_type,status,created_at,updated_at,lease_token,lease_expires_at,
             checkpoint_stage,analysis_mode,analysis_run_id,run_generation,run_sequence,run_base_state,
             input_state_hash,source_content_hash,source_content_s3_key,source_content_s3_version,
             source_episode_no,journal_status)
            VALUES (:job_id,:work_id,:episode_id,'SETTING_EXTRACTION','RUNNING',localtimestamp,localtimestamp,
                    :lease_token,localtimestamp + interval '5 minutes','CHUNKS_READY','ORDERED_PROVISIONAL',
                    :run_id,1,0,'{}',:input_hash,:source_hash,'test-source','version-1',1,'PENDING')"""),
            params,
        )
    factory = sessionmaker(engine, expire_on_commit=False)
    service = SettingCandidateService(factory)
    write_context = OrderedCandidateWriteContext(
        lease_token, ctx, episode_id, "test-source", "version-1", 1
    )
    discovery_id = uuid4()
    key = f"provisional-character:{discovery_id}"
    items = [
        SettingCandidateSaveItem(
            episode_id,
            "test-source",
            ExtractedSettingCandidate.model_validate(payload),
            OrderedCandidateBinding(
                candidate_id=candidate_id, provisional_subject_key=key, match_status="MATCHED"
            ),
        )
        for payload, candidate_id in (
            (_valid_discovery_payload(), discovery_id),
            (_valid_setting_payload(), uuid4()),
        )
    ]
    kwargs = dict(
        work_id=work_id,
        analysis_job_id=job_id,
        save_items=items,
        known_characters=[],
        ordered_write_context=write_context,
    )
    try:
        yield engine, service, kwargs, params
    finally:
        with engine.begin() as connection:
            for statement in (
                "DELETE FROM setting_candidates WHERE analysis_job_id=:job_id",
                "DELETE FROM analysis_jobs WHERE id=:job_id",
                "DELETE FROM episode_chunks WHERE episode_id=:episode_id",
                "DELETE FROM episodes WHERE id=:episode_id",
                "DELETE FROM works WHERE id=:work_id",
                "DELETE FROM members WHERE id=:member_id",
            ):
                connection.execute(text(statement), params)
        engine.dispose()


def _saved_ids(engine, params):
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT id FROM setting_candidates WHERE analysis_job_id=:job_id ORDER BY id"),
                params,
            )
            .scalars()
            .all()
        )


def test_migrated_schema_accepts_real_provisional_candidates_without_real_character(database):
    engine, service, kwargs, params = database
    saved = service.replace_candidates_for_analysis_job(**kwargs)
    assert len(_saved_ids(engine, params)) == 2
    assert saved[0].matched_character_id is None
    assert saved[1].comparison_status == "PENDING"
    assert saved[1].provisional_subject_key == f"provisional-character:{saved[0].id}"
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT value_json IS NULL FROM setting_candidates WHERE id=:id"),
            {"id": saved[0].id},
        ).scalar_one()


@pytest.mark.parametrize(
    "mutation",
    [
        "lease_token = gen_random_uuid()",
        "lease_expires_at = localtimestamp - interval '1 second'",
        "input_state_hash = repeat('c',64)",
        "run_generation = 2",
        "journal_status = 'INVALIDATED'",
        "checkpoint_stage = 'CHARACTER_CANDIDATES_SAVED'",
        "source_content_s3_version = 'version-2'",
    ],
)
def test_stale_worker_never_deletes_existing_candidates(database, mutation):
    engine, service, kwargs, params = database
    service.replace_candidates_for_analysis_job(**kwargs)
    before = _saved_ids(engine, params)
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE analysis_jobs SET {mutation} WHERE id=:job_id"), params)
    with pytest.raises(ComparisonValidationError, match="stale"):
        service.replace_candidates_for_analysis_job(**kwargs)
    assert _saved_ids(engine, params) == before


@pytest.mark.parametrize("mutation", ["content_hash=repeat('d',64)", "episode_no=2"])
def test_changed_episode_source_is_fenced_even_when_the_job_snapshot_is_unchanged(
    database, mutation
):
    engine, service, kwargs, params = database
    service.replace_candidates_for_analysis_job(**kwargs)
    before = _saved_ids(engine, params)
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE episodes SET {mutation} WHERE id=:episode_id"), params)
    with pytest.raises(ComparisonValidationError, match="stale"):
        service.replace_candidates_for_analysis_job(**kwargs)
    assert _saved_ids(engine, params) == before


def test_reclaim_winning_a_real_row_lock_rejects_the_waiting_old_worker(database):
    engine, service, kwargs, params = database
    service.replace_candidates_for_analysis_job(**kwargs)
    before = _saved_ids(engine, params)
    query_started = Event()

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE OF analysis_jobs, episodes" in statement:
            query_started.set()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as winner:
                winner.execute(
                    text("SELECT id FROM analysis_jobs WHERE id=:job_id FOR UPDATE"), params
                )
                pending = pool.submit(service.replace_candidates_for_analysis_job, **kwargs)
                assert query_started.wait(5), "The stale worker never attempted the row lock."
                assert not pending.done()
                winner.execute(
                    text("UPDATE analysis_jobs SET lease_token=gen_random_uuid() WHERE id=:job_id"),
                    params,
                )
            with pytest.raises(ComparisonValidationError, match="stale"):
                pending.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    assert _saved_ids(engine, params) == before


def test_late_chunk_writer_cannot_replace_chunks_after_the_checkpoint(database):
    engine, _, kwargs, params = database
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE analysis_jobs SET checkpoint_stage=NULL WHERE id=:job_id"), params
        )
    chunks = EpisodeChunkService(sessionmaker(engine, expire_on_commit=False))
    chunk_kwargs = dict(
        episode_id=params["episode_id"],
        raw_text="첫 회차 원문입니다.",
        ordered_write_context=kwargs["ordered_write_context"],
        analysis_job_id=params["job_id"],
        work_id=params["work_id"],
    )
    saved = chunks.replace_episode_chunks(**chunk_kwargs)
    assert saved
    original_ids = [chunk.id for chunk in saved]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE analysis_jobs SET checkpoint_stage='CHUNKS_READY' WHERE id=:job_id"),
            params,
        )
    with pytest.raises(ComparisonValidationError, match="stale"):
        chunks.replace_episode_chunks(**{**chunk_kwargs, "raw_text": "늦게 도착한 이전 Worker"})
    assert [chunk.id for chunk in chunks.get_episode_chunks(params["episode_id"])] == original_ids
