"""Campaign enumeration against all Java migrations in disposable localhost schemas."""

import asyncio
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.retrieval.backfill import (
    ZERO_UUID,
    CampaignConfig,
    CampaignRunner,
    CampaignState,
    _check_database_permissions,
)
from app.retrieval.contracts import IndexSpec, SourceChanged
from app.retrieval.service import RetrievalIndexService
from app.retrieval.store import RetrievalStore, SettingBookRetrievalStore
from tests import test_retrieval_postgres as postgres_tests
from tests.test_retrieval_search_postgres import Corpus

pytestmark = pytest.mark.integration
database = postgres_tests.database


@pytest.fixture
def writer_permissions(database):
    # All role/grant changes are rolled back in the explicitly disposable DB.
    with database.connect() as connection, connection.begin():
        role = "backfill_writer_" + uuid4().hex
        schema = connection.scalar(text("SELECT current_schema()"))
        connection.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        connection.exec_driver_sql(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"')
        connection.exec_driver_sql(f'GRANT INSERT, UPDATE ON retrieval_sources TO "{role}"')
        connection.exec_driver_sql(f'GRANT INSERT, UPDATE, DELETE ON retrieval_chunks TO "{role}"')
        connection.exec_driver_sql(
            f'GRANT INSERT, UPDATE ON retrieval_setting_book_sources TO "{role}"'
        )
        connection.exec_driver_sql(
            f'GRANT INSERT, UPDATE, DELETE ON retrieval_setting_book_chunks TO "{role}"'
        )
        # FOR UPDATE requires UPDATE on any column, not table-wide UPDATE.
        connection.exec_driver_sql(f'GRANT UPDATE(id) ON works TO "{role}"')
        connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        try:
            yield connection, role
        finally:
            connection.rollback()


def test_preflight_accepts_index_writer_with_only_column_level_work_lock_permission(
    writer_permissions,
):
    connection, _ = writer_permissions
    _check_database_permissions(connection)
    connection.execute(text("SELECT id FROM works WHERE false FOR UPDATE"))


@pytest.mark.parametrize(
    "table,privilege",
    [
        ("retrieval_sources", "INSERT"),
        ("retrieval_sources", "UPDATE"),
        ("retrieval_chunks", "INSERT"),
        ("retrieval_chunks", "UPDATE"),
        ("retrieval_chunks", "DELETE"),
        ("works", "UPDATE(id)"),
        ("works", "SELECT"),
        ("members", "SELECT"),
        ("episodes", "SELECT"),
        ("episode_source_purge_requests", "SELECT"),
        ("upload_files", "SELECT"),
        ("upload_batches", "SELECT"),
        ("setting_book_revisions", "SELECT"),
        ("retrieval_setting_book_sources", "SELECT"),
        ("retrieval_setting_book_sources", "INSERT"),
        ("retrieval_setting_book_sources", "UPDATE"),
        ("retrieval_setting_book_chunks", "SELECT"),
        ("retrieval_setting_book_chunks", "INSERT"),
        ("retrieval_setting_book_chunks", "UPDATE"),
        ("retrieval_setting_book_chunks", "DELETE"),
    ],
)
def test_preflight_rejects_each_missing_writer_or_source_privilege(
    writer_permissions, table, privilege
):
    connection, role = writer_permissions
    connection.exec_driver_sql("RESET ROLE")
    connection.exec_driver_sql(f'REVOKE {privilege} ON {table} FROM "{role}"')
    connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
    with pytest.raises(ValueError, match="writer permissions"):
        _check_database_permissions(connection)


def test_preflight_rejects_readonly_connection_even_with_superuser_privileges(database):
    with database.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        with pytest.raises(ValueError, match="writer permissions"):
            _check_database_permissions(connection)


@pytest.fixture
def corpora(database):
    created = []

    def create():
        corpus = Corpus(database)
        created.append(corpus)
        return corpus

    yield create, RetrievalStore(sessionmaker(database, expire_on_commit=False))
    for corpus in reversed(created):
        delete_books(corpus)
        corpus.mutate("DELETE FROM retrieval_sources WHERE work_id=:work")
        corpus.mutate("DELETE FROM episode_source_purge_requests WHERE work_id=:work")
        corpus.mutate("DELETE FROM episodes WHERE work_id=:work")
        corpus.mutate("DELETE FROM works WHERE id=:work")
        corpus.mutate("DELETE FROM members WHERE id=:member")


def test_snapshot_selects_multiple_active_works_and_explicit_scope(corpora):
    create, store = corpora
    first, second, purging_work, purging_owner, archived, empty, deleted = [
        create() for _ in range(7)
    ]
    for corpus in [first, second, purging_work, purging_owner, archived, deleted]:
        corpus.episode(1, prepared=False)
    second.episode(5, prepared=False)
    purging_work.mutate("UPDATE works SET lifecycle_status='PURGING' WHERE id=:work")
    purging_owner.mutate("UPDATE members SET status='PURGING' WHERE id=:member")
    archived.mutate("UPDATE episodes SET status='ARCHIVED' WHERE work_id=:work")
    deleted.mutate("DELETE FROM episodes WHERE work_id=:work")
    deleted.mutate("DELETE FROM works WHERE id=:work")

    snapshot = store.backfill_snapshot()
    assert {work: upper for work, upper, _ in snapshot} == {first.work: 1, second.work: 5}
    assert len({at for _, _, at in snapshot}) == 1  # one statement snapshot for every work
    explicit = store.backfill_snapshot(
        (second.work, purging_work.work, purging_owner.work, uuid4())
    )
    assert [(work, upper) for work, upper, _ in explicit] == [(second.work, 5)]
    assert store.backfill_snapshot((empty.work, archived.work, deleted.work)) == []


def test_keyset_pages_keep_snapshot_bounds_after_insert_archive_and_delete(corpora):
    create, store = corpora
    corpus, unrelated = create(), create()
    original = {number: corpus.episode(number, prepared=False) for number in [1, 3, 7, 9]}
    unrelated.episode(1, prepared=False)
    corpus.mutate("UPDATE episodes SET status='ARCHIVED' WHERE id=:episode", episode=original[9])
    work, upper, at = store.backfill_snapshot((corpus.work,))[0]
    assert upper == 7
    first_page = store.backfill_sources(work, upper, at, 0, UUID(int=0), 1)
    assert [source.episode_id for source in first_page] == [original[1]]

    late_low = corpus.episode(2, prepared=False)
    late_middle = corpus.episode(6, prepared=False)
    late_high = corpus.episode(8, prepared=False)
    # Test the numeric upper bound independently of the creation timestamp filter.
    corpus.mutate(
        "UPDATE episodes SET created_at=CAST(:snapshot AS timestamptz)-interval '1 second' WHERE id=:episode",
        snapshot=at,
        episode=late_high,
    )
    corpus.mutate("DELETE FROM episodes WHERE id=:episode", episode=original[3])
    first = first_page[0]
    next_page = store.backfill_sources(work, upper, at, first.episode_no, first.episode_id, 1)
    assert [source.episode_id for source in next_page] == [original[7]]
    last = next_page[0]
    assert store.backfill_sources(work, upper, at, last.episode_no, last.episode_id, 100) == []
    assert late_low not in {source.episode_id for source in first_page + next_page}
    assert late_middle not in {source.episode_id for source in first_page + next_page}

    work, upper, at = store.backfill_snapshot((corpus.work,))[0]
    assert upper == 8
    cursor, seen, sizes = (0, UUID(int=0)), [], []
    while True:
        page = store.backfill_sources(work, upper, at, *cursor, 2)
        if not page:
            break
        sizes.append(len(page))
        seen.extend(source.episode_id for source in page)
        cursor = page[-1].episode_no, page[-1].episode_id
    assert sizes == [2, 2, 1]
    assert seen == [original[1], late_low, late_middle, original[7], late_high]
    # A current source change is still rejected by the existing store fence.
    corpus.mutate(
        "UPDATE episodes SET content_hash=repeat('b',64) WHERE id=:episode", episode=last.episode_id
    )
    with pytest.raises(SourceChanged):
        store.check_source(last)


def test_current_lifecycle_and_deletion_are_rechecked_inside_frozen_scope(corpora):
    create, store = corpora
    corpus = create()
    episode = corpus.episode(1, prepared=False)
    work, upper, at = store.backfill_snapshot((corpus.work,))[0]
    corpus.mutate("UPDATE works SET lifecycle_status='PURGING' WHERE id=:work")
    page = store.backfill_sources(work, upper, at, 0, UUID(int=0), 100)
    assert len(page) == 1 and page[0].state == "UNAVAILABLE"
    with pytest.raises(SourceChanged):
        store.check_source(page[0])
    corpus.mutate("DELETE FROM episodes WHERE id=:episode", episode=episode)
    corpus.mutate("DELETE FROM works WHERE id=:work")
    assert store.backfill_sources(work, upper, at, 0, UUID(int=0), 100) == []
    assert store.backfill_snapshot((work,)) == []


# Setting books in the operational backfill (2026-11-03): real V114 tables and the real listing/snapshot SQL.


def add_book(corpus, value, *, current=True, archived=False):
    batch, book = uuid4(), uuid4()
    corpus.mutate(
        """INSERT INTO upload_batches (id,work_id,member_id,upload_type,source_type,status,file_count,
        created_at,updated_at) VALUES (:batch,:work,:member,'SINGLE_EPISODE','FILE','COMPLETED',1,
        localtimestamp,localtimestamp)""",
        batch=batch,
    )
    corpus.mutate(
        """INSERT INTO upload_files (id,batch_id,file_role,original_filename,mime_type,storage_url,file_size,
        parse_status,created_at,updated_at) VALUES (:book,:batch,'SETTING_BOOK','book.txt','text/plain',
        's3://synthetic/book',10,'PARSED',localtimestamp,localtimestamp)""",
        book=book,
        batch=batch,
    )
    revision = add_revision(corpus, book, 1, value) if current else None
    if archived:
        corpus.mutate("UPDATE upload_files SET archived_at=localtimestamp WHERE id=:book", book=book)
    return book, revision


def add_revision(corpus, book, number, value, *, key=None, current=True):
    revision = uuid4()
    corpus.mutate(
        """INSERT INTO setting_book_revisions (id,setting_book_id,revision_no,content_s3_key,content_s3_version,
        content_hash,char_count,reference_time_kind,created_at,updated_at)
        VALUES (:revision,:book,:number,:key,'v1',:hash,:chars,'WORK_START',localtimestamp,localtimestamp)""",
        revision=revision,
        book=book,
        number=number,
        key=f"synthetic/book/{revision}" if key is None else key,
        hash=sha256(value.encode()).hexdigest(),
        chars=len(value),
    )
    if current:
        corpus.mutate(
            "UPDATE upload_files SET current_setting_book_revision_id=:revision WHERE id=:book",
            revision=revision,
            book=book,
        )
    return revision


def spring_cleanup(corpus, book):
    # SettingBookRetrievalIndexCleanup#deleteSettingBookIndex, in the edit/archive transaction.
    corpus.mutate(
        """DELETE FROM retrieval_setting_book_chunks WHERE revision_id IN
        (SELECT revision_id FROM retrieval_setting_book_sources WHERE setting_book_id=:book)""",
        book=book,
    )
    corpus.mutate("DELETE FROM retrieval_setting_book_sources WHERE setting_book_id=:book", book=book)


def delete_books(corpus):
    books = "SELECT f.id FROM upload_files f JOIN upload_batches b ON b.id=f.batch_id WHERE b.work_id=:work"
    corpus.mutate("DELETE FROM retrieval_setting_book_sources WHERE work_id=:work")
    corpus.mutate(f"UPDATE upload_files SET current_setting_book_revision_id=NULL WHERE id IN ({books})")
    corpus.mutate(f"DELETE FROM setting_book_revisions WHERE setting_book_id IN ({books})")
    corpus.mutate(f"DELETE FROM upload_files WHERE id IN ({books})")
    corpus.mutate("DELETE FROM upload_batches WHERE work_id=:work")


def test_snapshot_includes_works_whose_only_sources_are_current_setting_books(corpora):
    create, store = corpora
    book_only, mixed, archived_book, no_revision, purging = [create() for _ in range(5)]
    add_book(book_only, "book only")
    mixed.episode(4, prepared=False)
    add_book(mixed, "with episodes")
    add_book(archived_book, "archived", archived=True)
    add_book(no_revision, "never revised", current=False)
    add_book(purging, "purging work")
    purging.mutate("UPDATE works SET lifecycle_status='PURGING' WHERE id=:work")
    works = (book_only.work, mixed.work, archived_book.work, no_revision.work, purging.work)
    snapshot = store.backfill_snapshot(works)
    assert {work: upper for work, upper, _ in snapshot} == {book_only.work: 0, mixed.work: 4}
    assert [work for work, _, _ in snapshot] == sorted([book_only.work, mixed.work])
    work, upper, at = snapshot[[w for w, _, _ in snapshot].index(book_only.work)]
    assert store.backfill_sources(work, upper, at, 0, UUID(int=0), 100) == []
    assert {w for w, _, _ in store.backfill_snapshot()} >= {book_only.work, mixed.work}


def test_book_listing_returns_current_non_archived_revisions_keyset_by_book(corpora):
    create, store = corpora
    books = SettingBookRetrievalStore(store.sessions)
    corpus, other = create(), create()
    revised, first_revision = add_book(corpus, "old revision")
    second_revision = add_revision(corpus, revised, 2, "new revision")
    plain, plain_revision = add_book(corpus, "plain")
    add_book(corpus, "archived", archived=True)
    add_book(corpus, "no revision", current=False)
    add_book(other, "another work")
    page = books.backfill_sources(corpus.work, ZERO_UUID, 100)
    expected = sorted([(revised, second_revision), (plain, plain_revision)])
    assert [(s.setting_book_id, s.revision_id) for s in page] == expected
    assert first_revision not in {s.revision_id for s in page}
    assert all(s.state == "AVAILABLE" and s.work_id == corpus.work and len(s.content_hash) == 64 for s in page)
    assert {s.revision_id: s.revision_no for s in page} == {second_revision: 2, plain_revision: 1}
    assert {s.key for s in page} == {f"synthetic/book/{second_revision}", f"synthetic/book/{plain_revision}"}
    first = books.backfill_sources(corpus.work, ZERO_UUID, 1)
    assert [s.setting_book_id for s in first] == [expected[0][0]]
    following = books.backfill_sources(corpus.work, first[0].setting_book_id, 1)
    assert [s.setting_book_id for s in following] == [expected[1][0]]
    assert books.backfill_sources(corpus.work, following[0].setting_book_id, 100) == []
    # A non-current revision is never writable, even with a correct fingerprint.
    current = next(s for s in page if s.setting_book_id == revised)
    books.check_source(current)
    stale = replace(
        current,
        revision_id=first_revision,
        revision_no=1,
        key=f"synthetic/book/{first_revision}",
        content_hash=sha256(b"old revision").hexdigest(),
    )
    with pytest.raises(SourceChanged):
        books.check_source(stale)
    corpus.mutate("UPDATE setting_book_revisions SET content_s3_key='' WHERE id=:id", id=plain_revision)
    states = {s.revision_id: s.state for s in books.backfill_sources(corpus.work, ZERO_UUID, 100)}
    assert states == {second_revision: "AVAILABLE", plain_revision: "SOURCE_UNIDENTIFIED"}
    corpus.mutate("UPDATE members SET status='PURGING' WHERE id=:member")
    assert {s.state for s in books.backfill_sources(corpus.work, ZERO_UUID, 100)} == {"UNAVAILABLE"}
    with pytest.raises(ValueError):
        books.backfill_sources(corpus.work, ZERO_UUID, 101)


class SyntheticProvider:
    """No ledger and no paid call: vectors only. Books get a real operation ID (no V114 column for it)."""

    attempts = input_tokens = unknown_usage = 0

    def __init__(self):
        self.kinds = []

    async def embed(self, source, chunks, before_call):
        book = hasattr(source, "revision_id")
        await before_call(uuid4() if book else None)
        self.kinds.append("BOOK" if book else "EPISODE")
        self.attempts += 1
        self.input_tokens += sum(c.token_count for c in chunks)
        return [[1.0] + [0.0] * 1535 for _ in chunks]


def test_runner_fills_v114_tables_and_follows_spring_revision_and_archive(corpora, tmp_path):
    create, store = corpora
    corpus = create()
    episode_text, first_text, second_text = "회차 원문입니다. " * 30, "설정집 첫 판. " * 30, "설정집 둘째 판. " * 30
    episode = corpus.episode(1, prepared=False)
    corpus.mutate(
        "UPDATE episodes SET content_hash=:hash WHERE id=:episode",
        hash=sha256(episode_text.encode()).hexdigest(),
        episode=episode,
    )
    book, first = add_book(corpus, first_text)
    texts = {
        (f"synthetic/{episode}", "source-v1"): episode_text,
        (f"synthetic/book/{first}", "v1"): first_text,
    }
    reads = []

    def get_text(key, *, version_id, max_bytes):
        assert max_bytes == 2_000_000
        reads.append(key)
        return texts[(key, version_id)]

    storage = SimpleNamespace(get_text=get_text)
    sessions = store.sessions
    cfg = CampaignConfig(
        "synthetic-pg-campaign",
        1_000_000,
        work_ids=(corpus.work,),
        state_path=str(tmp_path / "state.sqlite"),
        outbox_path=str(tmp_path / "outbox.sqlite"),
        actor="synthetic-operator",
    )

    def sweep():
        state = CampaignState(cfg, IndexSpec())
        provider = SyntheticProvider()
        try:
            runner = CampaignRunner(
                cfg,
                state,
                RetrievalIndexService(RetrievalStore(sessions), storage, IndexSpec()),
                provider,
                book_service=RetrievalIndexService(SettingBookRetrievalStore(sessions), storage, IndexSpec()),
            )

            async def run():
                return await runner.run(asyncio.get_running_loop().time() + 60)

            return asyncio.run(run()), provider
        finally:
            state.close()

    def book_rows():
        with store.sessions() as session:
            return [
                dict(row)
                for row in session.execute(
                    text("""SELECT s.revision_id, s.setting_book_id, s.source_key, s.chunk_count, c.status,
                        c.embedding_model, c.embedding_version, c.attempt_count, c.embedding IS NOT NULL AS vector
                        FROM retrieval_setting_book_sources s
                        JOIN retrieval_setting_book_chunks c ON c.revision_id=s.revision_id
                        WHERE s.work_id=:work ORDER BY c.chunk_index"""),
                    {"work": corpus.work},
                ).mappings()
            ]

    report, provider = sweep()
    assert report["state"] == "READY" and provider.kinds == ["EPISODE", "BOOK"]
    assert report["checkedSettingBooks"] == report["readySettingBooks"] == 1
    rows = book_rows()
    assert rows and {r["revision_id"] for r in rows} == {first}
    assert all(
        r["status"] == "READY" and r["vector"] and r["embedding_model"] == "text-embedding-3-small"
        and r["embedding_version"] == "v1" and r["attempt_count"] == 1 and r["setting_book_id"] == book
        for r in rows
    )
    assert rows[0]["chunk_count"] == len(rows)
    with store.sessions() as session:
        assert session.scalar(
            text("SELECT count(*) FROM retrieval_chunks c JOIN retrieval_sources s USING (episode_id) "
                 "WHERE s.work_id=:work AND c.status='READY'"),
            {"work": corpus.work},
        ) >= 1
    assert sweep()[0]["providerAttempts"] == 0  # next sweep: everything READY

    # Spring saves a new current revision and deletes the book's index rows; the next sweep fills it.
    second = add_revision(corpus, book, 2, second_text)
    spring_cleanup(corpus, book)
    texts[(f"synthetic/book/{second}", "v1")] = second_text
    report, provider = sweep()
    assert report["state"] == "READY" and provider.kinds == ["BOOK"]
    assert {r["revision_id"] for r in book_rows()} == {second}
    assert reads[-1] == f"synthetic/book/{second}"

    # Archiving deletes the rows and the book is no longer listed or written.
    corpus.mutate("UPDATE upload_files SET archived_at=localtimestamp WHERE id=:book", book=book)
    spring_cleanup(corpus, book)
    report, provider = sweep()
    assert report["state"] == "READY" and provider.attempts == 0
    assert report["checkedSettingBooks"] == 0 and book_rows() == []
