"""Intake index fill against the full Java migrations (incl. V114) in a disposable loopback PostgreSQL schema.

Real stores, real gap SQL, real frozen readiness and VECTOR ranking; only the text reader and the embedding
provider are synthetic (no paid calls).
"""

import asyncio
import json
import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.retrieval.contracts import IndexSpec
from app.validation.data import SourceDocument, SourceRead
from app.validation.index_fill import ValidationIndexFill

pytestmark = pytest.mark.integration

SPEC = IndexSpec()
TEXTS = {1: "민수는 왼손을 잃었다. " * 300, 2: "민수는 의수를 얻었고 그 의수로 검을 쥘 수 있었다. " * 200}
BOOK = {1: "옛 설정: 민수는 양손잡이다.", 2: "설정집: 의수는 마력으로 움직이며 밤에는 쓸 수 없다."}


@pytest.fixture(scope="module")
def database():
    url, checkout = os.getenv("GH306_TEST_DATABASE_URL"), os.getenv("GH306_JAVA_CHECKOUT")
    if not url or not checkout:
        pytest.skip("Explicit disposable GH306_TEST_DATABASE_URL and GH306_JAVA_CHECKOUT required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.database == "gh306_test"
    schema = "gh306_fill_" + uuid4().hex
    bootstrap = create_engine(url)
    with bootstrap.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema},public"})
    migrations = sorted((Path(checkout) / "src/main/resources/db/migration").glob("V*__*.sql"),
                        key=lambda path: int(path.name.split("__")[0][1:]))
    # V114 creates the setting-book retrieval tables (V113 is the validation run-start generation).
    assert any(path.name == "V114__add_setting_book_retrieval_index.sql" for path in migrations), (
        "Backend checkout without V114__add_setting_book_retrieval_index.sql")
    try:
        with engine.begin() as connection:
            for migration in migrations:
                connection.exec_driver_sql(migration.read_text(), execution_options={"no_parameters": True})
        yield engine
    finally:
        engine.dispose()
        with bootstrap.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        bootstrap.dispose()


def digest(value):
    return sha256(value.encode()).hexdigest()


class Work:
    """One synthetic work: two prior episodes, a target, a setting book with an old and a current revision."""

    def __init__(self, engine, *, book_current=True):
        self.engine, self.sessions = engine, sessionmaker(engine, autoflush=False, expire_on_commit=False)
        self.member, self.work = uuid4().int % (2 ** 61), uuid4()
        self.episodes, self.report, self.batch, self.book = {}, uuid4(), uuid4(), uuid4()
        self.revisions = {1: uuid4(), 2: uuid4()}
        with engine.begin() as c:
            c.execute(text("""INSERT INTO members (id,email,password_hash,phone_number,phone_verified,display_name,
                status,role,created_at,updated_at) VALUES (:id,:email,'test',:phone,false,'synthetic','ACTIVE','USER',
                localtimestamp,localtimestamp)"""), {"id": self.member, "email": f"{self.member}@test.invalid",
                                                         "phone": str(self.member)})
            c.execute(text("""INSERT INTO works (id,member_id,title,genre,latest_episode_no,created_at,updated_at)
                VALUES (:work,:member,'synthetic','FANTASY',3,localtimestamp,localtimestamp)"""),
                      {"work": self.work, "member": self.member})
            for number, value in [*TEXTS.items(), (3, "민수는 양손으로 검을 쥐었다.")]:
                episode = self.episodes[number] = uuid4()
                c.execute(text("""INSERT INTO episodes (id,work_id,episode_no,content_s3_key,content_s3_version,
                    content_hash,char_count,status,created_at,updated_at,content_updated_at)
                    VALUES (:id,:work,:no,:key,'v1',:hash,:chars,'ANALYZED',localtimestamp,localtimestamp,localtimestamp)"""),
                          {"id": episode, "work": self.work, "no": number, "key": f"synthetic/{episode}", "hash": digest(value),
                               "chars": len(value)})
            c.execute(text("""INSERT INTO upload_batches (id,work_id,member_id,upload_type,source_type,status,file_count,
                created_at,updated_at) VALUES (:id,:work,:member,'SINGLE_EPISODE','FILE','COMPLETED',1,localtimestamp,
                localtimestamp)"""), {"id": self.batch, "work": self.work, "member": self.member})
            c.execute(text("""INSERT INTO upload_files (id,batch_id,file_role,original_filename,mime_type,storage_url,
                file_size,parse_status,created_at,updated_at) VALUES (:id,:batch,'SETTING_BOOK','book.txt','text/plain',
                's3://synthetic/book',10,'PARSED',localtimestamp,localtimestamp)"""), {"id": self.book, "batch": self.batch})
            for number, revision in self.revisions.items():
                c.execute(text("""INSERT INTO setting_book_revisions (id,setting_book_id,revision_no,content_s3_key,
                    content_s3_version,content_hash,char_count,reference_time_kind,created_at,updated_at)
                    VALUES (:id,:book,:no,:key,'v1',:hash,:chars,'WORK_START',localtimestamp,localtimestamp)"""),
                          {"id": revision, "book": self.book, "no": number, "key": f"synthetic/book/{number}",
                               "hash": digest(BOOK[number]), "chars": len(BOOK[number])})
            c.execute(text("UPDATE upload_files SET current_setting_book_revision_id=:r WHERE id=:id"),
                      {"r": self.revisions[2], "id": self.book})
            target = self.episodes[3]
            c.execute(text("""INSERT INTO validation_reports (id,work_id,member_id,batch_id,episode_id,source_episode_no,
                source_content_hash,source_content_s3_key,source_content_s3_version,request_hash,baseline_seed,
                baseline_entries,baseline_hash,baseline_status,status,created_at,updated_at)
                VALUES (:report,:work,:member,:batch,:target,3,:hash,:key,'v1',:hash,'{}','[]',:baseline,'READY',
                'WAITING_FOR_ERROR_REVIEW',localtimestamp,localtimestamp)"""),
                      {"report": self.report, "work": self.work, "member": self.member, "batch": self.batch, "target": target,
                           "hash": digest("민수는 양손으로 검을 쥐었다."), "key": f"synthetic/{target}", "baseline": "b" * 64})
        self.documents = []
        for ref, number in (("R1", 1), ("R2", 2)):
            meta = {"sourceKind": "EPISODE", "episodeId": str(self.episodes[number]), "episodeNo": number,
                    "contentHash": digest(TEXTS[number]), "contentS3Key": f"synthetic/{self.episodes[number]}",
                    "contentS3Version": "v1", "contentRevision": 0}
            self._baseline(meta, episode=self.episodes[number])
            self.documents.append(SourceDocument(ref, "EPISODE", meta["contentHash"], "v1", meta))
        for ref, number in (("R3", 1), ("R4", 2)):
            meta = {"sourceKind": "SETTING_BOOK", "settingBookId": str(self.book), "revisionId": str(self.revisions[number]),
                    "revisionNo": number, "contentHash": digest(BOOK[number]), "contentS3Key": f"synthetic/book/{number}",
                    "contentS3Version": "v1", "referenceTimeKind": "WORK_START", "referenceEpisodeNo": None,
                    "currentRevision": number == 2 and book_current}
            self._baseline(meta, revision=self.revisions[number])
            self.documents.append(SourceDocument(ref, "SETTING_BOOK", meta["contentHash"], "v1", meta))
        self.frozen = SimpleNamespace(context=SimpleNamespace(
            member_id=self.member, work_id=self.work, report_id=self.report, baseline_hash="b" * 64,
            target_hash=digest("민수는 양손으로 검을 쥐었다."), job_id=None, lease_token=None, generation=None,
            input_digest=None), sources=tuple(self.documents))

    def _baseline(self, manifest, *, episode=None, revision=None):
        with self.engine.begin() as c:
            c.execute(text("""INSERT INTO validation_baseline_sources (id,report_id,episode_id,setting_book_revision_id,
                source_manifest,created_at,updated_at) VALUES (:id,:report,:episode,:revision,CAST(:manifest AS jsonb),
                localtimestamp,localtimestamp)"""), {"id": uuid4(), "report": self.report, "episode": episode,
                                                         "revision": revision, "manifest": json.dumps(manifest)})

    def rows(self, sql, **params):
        with self.engine.begin() as c:
            return [dict(row) for row in c.execute(text(sql), dict(work=self.work, **params)).mappings()]

    def mutate(self, sql, **params):
        with self.engine.begin() as c:
            c.execute(text(sql), dict(work=self.work, **params))

    def readiness(self):
        from app.validation.search import FrozenRetrievalSearchService

        return asyncio.run(FrozenRetrievalSearchService(self.sessions).readiness(self.frozen))

    def ranked(self, limit=50):
        from app.validation.search import FrozenRetrievalRepository, FrozenRetrievalSearchService

        scope = FrozenRetrievalSearchService._scope(self.frozen)
        with self.sessions() as session:
            return FrozenRetrievalRepository(session, scope, SPEC).frozen_vector([1.0] + [0.0] * 1535, limit=limit)


class Reader:
    """The frozen data API stand-in: full hash-verified text of a manifest source."""

    def __init__(self):
        self.reads = []

    async def read_source(self, document, start=None, end=None):
        self.reads.append(document.ref)
        value = (TEXTS[document.metadata["episodeNo"]] if document.kind == "EPISODE"
                 else BOOK[document.metadata["revisionNo"]])
        assert digest(value) == document.content_hash
        return SourceRead(document, value, 0, len(value), len(value))


class Embedding:
    def __init__(self, *, fail_on=(), cancel_on=()):
        self.calls, self.fail_on, self.cancel_on, self.texts = 0, set(fail_on), set(cancel_on), []

    async def embed(self, document, chunks):
        self.calls += 1
        if self.calls in self.cancel_on:
            raise asyncio.CancelledError()
        if self.calls in self.fail_on:
            raise RuntimeError("synthetic provider 503")
        self.texts.extend(chunk.chunk_text for chunk in chunks)
        return [[1.0] + [0.0] * 1535 for _ in chunks]


def fill(work, embedding, *, batch_size=4):
    return asyncio.run(ValidationIndexFill(work.sessions, embedding, SPEC, batch_size=batch_size)
                       .fill(work.frozen, reader := Reader())), reader


EPISODE_CHUNKS = "SELECT c.id,c.status,c.embedded_at,e.episode_no FROM retrieval_chunks c JOIN episodes e ON e.id=c.episode_id WHERE e.work_id=:work ORDER BY e.episode_no,c.chunk_index"
BOOK_CHUNKS = "SELECT c.id,c.status,c.revision_id FROM retrieval_setting_book_chunks c JOIN retrieval_setting_book_sources s ON s.revision_id=c.revision_id WHERE s.work_id=:work ORDER BY c.chunk_index"


def test_an_empty_index_is_filled_to_full_readiness_and_ranks_episodes_and_the_current_book(database):
    work = Work(database)
    before = work.readiness()
    assert (before.source_count, before.vector_ready_sources, before.vector_chunks) == (3, 0, 0)
    outcome, reader = fill(work, embedding := Embedding())
    assert outcome.status == "COMPLETED" and outcome.counts["gapSources"] == 3
    assert reader.reads == ["R1", "R2", "R4"]  # the old revision R3 is provenance only, never indexed
    episodes, books = work.rows(EPISODE_CHUNKS), work.rows(BOOK_CHUNKS)
    assert episodes and all(row["status"] == "READY" for row in episodes)
    assert books and {row["revision_id"] for row in books} == {work.revisions[2]}
    assert outcome.counts["embeddedChunks"] == len(episodes) + len(books) == len(embedding.texts)
    after = work.readiness()
    assert after.source_count == after.vector_ready_sources == 3 and "VECTOR_INDEX_PARTIAL" not in after.limitations
    ranked = work.ranked()
    assert {chunk.source_kind for chunk in ranked} == {"EPISODE", "SETTING_BOOK"}
    book = next(chunk for chunk in ranked if chunk.source_kind == "SETTING_BOOK")
    assert book.source_id == work.revisions[2] and book.text == BOOK[2] and book.source_hash == digest(BOOK[2])
    # Equal distances rank episodes first, then the book.
    assert [chunk.source_kind for chunk in ranked][-1] == "SETTING_BOOK"


def test_a_second_fill_is_a_no_op_that_reads_and_embeds_nothing(database):
    work = Work(database)
    fill(work, Embedding())
    ids = [row["id"] for row in work.rows(EPISODE_CHUNKS) + work.rows(BOOK_CHUNKS)]
    outcome, reader = fill(work, embedding := Embedding())
    assert outcome.status == "NOT_NEEDED" and reader.reads == [] and embedding.calls == 0
    assert [row["id"] for row in work.rows(EPISODE_CHUNKS) + work.rows(BOOK_CHUNKS)] == ids


def test_chunks_without_embeddings_are_embedded_in_place(database):
    from app.retrieval.chunking import RetrievalSplitter
    from app.retrieval.contracts import Source
    from app.retrieval.store import RetrievalStore

    work = Work(database)
    episode = work.documents[0]
    source = Source(work.work, UUID(episode.metadata["episodeId"]), 1, episode.metadata["contentS3Key"], "v1",
                    episode.content_hash, "AVAILABLE")
    prepared = RetrievalStore(work.sessions).prepare(source, SPEC, RetrievalSplitter().split(TEXTS[1]))
    outcome, _ = fill(work, Embedding())
    assert outcome.status == "COMPLETED"
    first = [row for row in work.rows(EPISODE_CHUNKS) if row["episode_no"] == 1]
    assert [row["id"] for row in first] == [chunk.id for chunk in prepared]  # same rows, now READY
    assert all(row["status"] == "READY" for row in first)


def test_a_partially_embedded_source_embeds_only_its_missing_chunks(database):
    work = Work(database)
    fill(work, Embedding())
    rows = [row for row in work.rows(EPISODE_CHUNKS) if row["episode_no"] == 1]
    assert len(rows) >= 3
    hole = rows[1]["id"]
    work.mutate("UPDATE retrieval_chunks SET embedding=NULL,embedding_model=NULL,embedding_version=NULL,"
                "embedded_at=NULL,status='MISSING' WHERE id=:id", id=hole)
    assert work.readiness().vector_ready_sources == 2
    outcome, reader = fill(work, embedding := Embedding())
    assert outcome.status == "COMPLETED" and reader.reads == ["R1"] and outcome.counts["embeddedChunks"] == 1
    assert embedding.calls == 1 and outcome.counts["keptReadyChunks"] == len(rows) - 1
    after = {row["id"]: row for row in work.rows(EPISODE_CHUNKS)}
    assert all(after[row["id"]]["embedded_at"] == row["embedded_at"] for row in rows if row["id"] != hole)
    assert work.readiness().vector_ready_sources == 3


def test_a_setting_book_without_embeddings_is_filled_while_ready_episodes_are_skipped(database):
    work = Work(database)
    fill(work, Embedding())
    work.mutate("UPDATE retrieval_setting_book_chunks SET embedding=NULL,status='MISSING' "
                "WHERE revision_id=:r", r=work.revisions[2])
    outcome, reader = fill(work, Embedding())
    assert outcome.status == "COMPLETED" and reader.reads == ["R4"]
    assert all(row["status"] == "READY" for row in work.rows(BOOK_CHUNKS))


def test_a_changed_source_header_is_rechunked_never_mixed(database):
    work = Work(database)
    fill(work, Embedding())
    old = {row["id"] for row in work.rows(EPISODE_CHUNKS) if row["episode_no"] == 2}
    work.mutate("UPDATE retrieval_sources SET source_hash=:h WHERE episode_id=:e", h="f" * 64,
                e=work.episodes[2])
    assert work.readiness().vector_ready_sources == 2
    outcome, _ = fill(work, Embedding())
    new = {row["id"] for row in work.rows(EPISODE_CHUNKS) if row["episode_no"] == 2}
    assert outcome.status == "COMPLETED" and new and not new & old
    header = work.rows("SELECT source_hash FROM retrieval_sources WHERE episode_id=:e", e=work.episodes[2])
    assert header == [{"source_hash": digest(TEXTS[2])}] and work.readiness().vector_ready_sources == 3


def test_a_provider_failure_leaves_failed_chunks_and_a_retry_fills_only_those(database):
    work = Work(database)
    outcome, _ = fill(work, Embedding(fail_on={2}), batch_size=2)
    assert (outcome.status, outcome.cause) == ("FAILED", "INDEX_FILL_PROVIDER_FAILED")
    assert "FAILED" in {row["status"] for row in work.rows(EPISODE_CHUNKS)}
    assert work.readiness().vector_ready_sources < 3  # never reported as ready
    saved = {row["id"] for row in work.rows(EPISODE_CHUNKS) if row["status"] == "READY"}
    retry, _ = fill(work, embedding := Embedding(), batch_size=2)
    assert retry.status == "COMPLETED" and retry.counts["keptReadyChunks"] == len(saved)
    assert work.readiness().vector_ready_sources == 3 and len(embedding.texts) == retry.counts["embeddedChunks"]


def test_a_crash_mid_fill_resumes_after_the_saved_batches(database):
    """Lease recovery: the next claim's fill skips every chunk the cancelled one already saved."""
    work = Work(database)
    with pytest.raises(asyncio.CancelledError):
        fill(work, Embedding(cancel_on={3}), batch_size=1)
    saved = {row["id"] for row in work.rows(EPISODE_CHUNKS) if row["status"] == "READY"}
    assert len(saved) == 2
    outcome, _ = fill(work, embedding := Embedding(), batch_size=1)
    total = len(work.rows(EPISODE_CHUNKS)) + len(work.rows(BOOK_CHUNKS))
    assert outcome.status == "COMPLETED" and embedding.calls == total - 2


def test_a_book_not_current_at_intake_is_outside_the_scope(database):
    work = Work(database, book_current=False)
    assert work.readiness().source_count == 2
    outcome, reader = fill(work, Embedding())
    assert outcome.status == "COMPLETED" and reader.reads == ["R1", "R2"] and work.rows(BOOK_CHUNKS) == []


def test_a_book_that_stopped_being_current_is_never_written(database):
    work = Work(database)
    work.mutate("UPDATE upload_files SET current_setting_book_revision_id=:r WHERE id=:id",
                r=work.revisions[1], id=work.book)
    outcome, _ = fill(work, Embedding())
    assert (outcome.status, outcome.cause) == ("FAILED", "INDEX_FILL_SOURCE_CHANGED")
    assert work.rows(BOOK_CHUNKS) == []
