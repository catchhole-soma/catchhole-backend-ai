"""Full Java migrations in an isolated schema of an explicitly named local test DB."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.retrieval.chunking import RetrievalSplitter
from app.retrieval.contracts import IndexBusy, IndexSpec, Scope, SourceChanged
from app.retrieval.service import RetrievalIndexService
from app.retrieval.store import RetrievalStore

pytestmark = pytest.mark.integration
ORIGINAL = "아리아는 반지의 추적 마법을 알아챘다.\r\n그녀는 그것을 강물에 던졌다.😀\n" * 80


@pytest.fixture(scope="module")
def database():
    url, java = os.getenv("GH239_TEST_DATABASE_URL"), os.getenv("GH239_JAVA_CHECKOUT")
    if not url or not java:
        pytest.skip("Explicit GH239_TEST_DATABASE_URL and GH239_JAVA_CHECKOUT required")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or parsed.database != "gh239_test":
        pytest.fail("Only disposable localhost gh239_test is allowed")
    schema = "gh239_" + uuid4().hex
    bootstrap = create_engine(url)
    with bootstrap.begin() as c:
        c.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    engine = create_engine(
        url, connect_args={"options": f"-c search_path={schema},public -c timezone=Asia/Seoul"}
    )
    files = sorted(
        (Path(java) / "src/main/resources/db/migration").glob("V*__*.sql"),
        key=lambda p: int(p.name.split("__")[0][1:]),
    )
    assert any(path.name.startswith("V76__") for path in files)
    with engine.begin() as c:
        for path in files:
            c.exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})
    try:
        yield engine
    finally:
        engine.dispose()
        with bootstrap.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        bootstrap.dispose()


@pytest.fixture
def fixture(database):
    work, episode = uuid4(), uuid4()
    member = uuid4().int % (2**61)
    values = dict(
        work=work,
        episode=episode,
        member=member,
        email=f"{member}@test.invalid",
        phone=str(member),
        source_hash=sha256(ORIGINAL.encode()).hexdigest(),
    )
    with database.begin() as c:
        c.execute(
            text("""INSERT INTO members(id,email,password_hash,phone_number,phone_verified,display_name,status,role,created_at,updated_at)
          VALUES (:member,:email,'test',:phone,false,'test','ACTIVE','USER',localtimestamp,localtimestamp)"""),
            values,
        )
        c.execute(
            text("""INSERT INTO works(id,member_id,title,genre,latest_episode_no,created_at,updated_at)
          VALUES (:work,:member,'synthetic','FANTASY',1,localtimestamp,localtimestamp)"""),
            values,
        )
        c.execute(
            text("""INSERT INTO episodes(id,work_id,episode_no,content_s3_key,content_s3_version,content_hash,char_count,status,created_at,updated_at,content_updated_at)
          VALUES (:episode,:work,1,'synthetic-source','version-1',:source_hash,100,'ANALYZED',localtimestamp,localtimestamp,localtimestamp)"""),
            values,
        )
    store = RetrievalStore(sessionmaker(database, expire_on_commit=False))
    scope, spec = Scope(work), IndexSpec()
    source = store.sources(scope)[0][0]

    class Storage:
        def get_text(self, key, **kwargs):
            assert key == "synthetic-source" and kwargs["version_id"] == "version-1"
            return ORIGINAL

    service = RetrievalIndexService(store, Storage(), spec)
    yield database, store, service, source, scope, spec, values
    with database.begin() as c:
        c.execute(text("DELETE FROM episode_source_purge_requests WHERE work_id=:work"), values)
        c.execute(text("DELETE FROM episodes WHERE work_id=:work"), values)
        c.execute(text("DELETE FROM works WHERE id=:work"), values)
        c.execute(text("DELETE FROM members WHERE id=:member"), values)


def prepared(f):
    engine, store, service, source, scope, spec, values = f
    service.prepare(scope)
    return store.existing(source, spec)


def test_real_vectors_resume_unchanged_ids_and_do_not_touch_analysis_chunks(fixture):
    engine, store, service, source, scope, spec, values = fixture
    before = service.dry_run(scope)
    assert before["targetMissingChunks"] > 1
    chunks = prepared(fixture)
    store.save(source, spec, chunks[:1], [[1.0] * 1536])
    service.prepare(scope)
    assert [c.id for c in store.existing(source, spec)] == [c.id for c in chunks]
    ready = service.readiness(scope)
    assert not ready["supportedScopeReady"] and ready["sources"][0]["counts"]["READY"] == 1
    store.save(source, spec, chunks[1:], [[1.0] * 1536 for _ in chunks[1:]])
    assert service.readiness(scope)["supportedScopeReady"]
    assert len(store.valid_chunks(scope.work_id, spec)) == len(chunks)
    assert service.dry_run(scope)["targetMissingChunks"] == 0
    with engine.connect() as c:
        assert (
            c.scalar(text("SELECT count(*) FROM episode_chunks WHERE episode_id=:episode"), values)
            == 0
        )
        assert (
            c.scalar(text("SELECT count(*) FROM ai_token_usages WHERE work_id=:work"), values) == 0
        )
        vector = "[" + ",".join(["1"] * 1536) + "]"
        assert float(
            c.scalar(
                text(
                    "SELECT embedding <=> CAST(:vector AS vector) FROM retrieval_current_chunks WHERE work_id=:work LIMIT 1"
                ),
                {**values, "vector": vector},
            )
        ) == pytest.approx(0)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE episodes SET content_hash=repeat('b',64) WHERE id=:episode",
        "UPDATE episodes SET content_s3_key='changed' WHERE id=:episode",
        "UPDATE episodes SET content_s3_version='changed' WHERE id=:episode",
        "UPDATE episodes SET status='ARCHIVED' WHERE id=:episode",
        "UPDATE works SET lifecycle_status='PURGING' WHERE id=:work",
        "UPDATE members SET status='PURGING' WHERE id=:member",
        "DELETE FROM retrieval_chunks WHERE episode_id=:episode",
    ],
)
def test_old_result_is_fenced_after_source_or_chunk_change(fixture, mutation):
    engine, store, service, source, scope, spec, values = fixture
    chunks = prepared(fixture)
    store.save(source, spec, chunks, [[1.0] * 1536 for _ in chunks])
    with engine.begin() as c:
        c.execute(text(mutation), values)
    with pytest.raises(SourceChanged):
        store.save(source, spec, chunks, [[1.0] * 1536 for _ in chunks])
    assert store.valid_chunks(scope.work_id, spec) == []


def test_delayed_purge_excludes_physical_vectors_and_purge_removes_original_slices(fixture):
    engine, store, service, source, scope, spec, values = fixture
    chunks = prepared(fixture)
    store.save(source, spec, chunks, [[1.0] * 1536 for _ in chunks])
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO episode_source_purge_requests
          (id,episode_id,work_id,previous_episode_no,previous_content_key,retained_content_key,status,requested_at,attempt_count,created_at,updated_at)
          VALUES (:id,:episode,:work,1,'synthetic-source','synthetic-source','REQUESTED',localtimestamp,0,localtimestamp,localtimestamp)"""),
            {**values, "id": uuid4()},
        )
    assert service.readiness(scope)["sources"][0]["state"] == "PURGE_PENDING"
    assert store.valid_chunks(scope.work_id, spec) == []
    with pytest.raises(SourceChanged):
        store.prepare(source, spec, RetrievalSplitter().split(ORIGINAL))
    with engine.begin() as c:
        c.execute(text("DELETE FROM retrieval_sources WHERE episode_id=:episode"), values)
        assert (
            c.scalar(
                text("SELECT count(*) FROM retrieval_chunks WHERE episode_id=:episode"), values
            )
            == 0
        )


def test_mismatch_wrong_work_and_episode_bounds_do_not_mix_embeddings(fixture):
    engine, store, service, source, scope, spec, values = fixture
    chunks = prepared(fixture)
    store.save(source, spec, chunks, [[1.0] * 1536 for _ in chunks])
    assert store.valid_chunks(uuid4(), spec) == []
    assert store.valid_chunks(scope.work_id, spec, episode_from=2) == []
    other = replace(spec, version="v2")
    assert store.valid_chunks(scope.work_id, other) == []
    assert store.inspect(source, other)["counts"]["MODEL_VERSION_MISMATCH"] == len(chunks)
    with pytest.raises(ValueError):
        store.sources(Scope(uuid4(), (source.episode_id,)))


def test_concurrent_source_edit_work_lock_is_observed_before_save(fixture):
    engine, store, service, source, scope, spec, values = fixture
    chunks = prepared(fixture)
    entered = Event()

    def save():
        entered.set()
        store.save(source, spec, chunks, [[1.0] * 1536 for _ in chunks])

    with ThreadPoolExecutor(1) as pool:
        with engine.begin() as c:
            c.execute(text("SELECT id FROM works WHERE id=:work FOR UPDATE"), values)
            future = pool.submit(save)
            assert entered.wait(2)
            c.execute(
                text("UPDATE episodes SET content_hash=repeat('d',64) WHERE id=:episode"), values
            )
        with pytest.raises(SourceChanged):
            future.result(timeout=5)
    assert store.valid_chunks(scope.work_id, spec) == []


def test_work_advisory_lock_prevents_overlapping_paid_operators(fixture):
    _, store, _, _, scope, _, _ = fixture
    with store.exclusive(scope.work_id):
        with pytest.raises(IndexBusy):
            with store.exclusive(scope.work_id):
                pytest.fail("same work was acquired twice")
    with store.exclusive(scope.work_id):
        pass


def test_real_index_service_resumes_only_missing_vectors_with_fake_provider(fixture):
    engine, store, service, source, scope, spec, values = fixture

    class Provider:
        attempts = input_tokens = unknown_usage = 0

        async def embed(self, src, chunks, before):
            # No real ledger/provider: lifecycle and pgvector assertions only.
            self.attempts += 1
            self.input_tokens += sum(c.token_count for c in chunks)
            return [[1.0] * 1536 for _ in chunks]

    from app.retrieval.contracts import Limits

    first = asyncio.run(service.run(scope, Provider(), enabled=True, limits=Limits(max_chunks=1)))
    assert first["embeddedChunks"] == 1
    second = asyncio.run(service.run(scope, Provider(), enabled=True))
    assert second["skippedReadyChunks"] == 1 and second["state"] == "COMPLETED"


def test_source_pagination_excludes_tombstones_with_reused_episode_numbers(fixture):
    engine, store, service, source, scope, spec, values = fixture
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO episodes(id,work_id,episode_no,char_count,status,created_at,updated_at,content_updated_at)
            VALUES (:id,:work,1,0,'ARCHIVED',localtimestamp,localtimestamp,localtimestamp)"""),
            {**values, "id": uuid4()},
        )
    selected, more = store.sources(replace(scope, max_sources=1))
    assert [s.episode_id for s in selected] == [source.episode_id] and not more
    result = service.readiness(scope)
    assert result["excludedSources"]["archivedEpisodesInRange"] == 1
    assert result["excludedSources"]["pastEpisodeVersions"] is None
