"""Full real Java migrations, disposable loopback PostgreSQL only."""

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.test_retrieval_search_postgres import Corpus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database():
    url, checkout = os.getenv("GH306_TEST_DATABASE_URL"), os.getenv("GH306_JAVA_CHECKOUT")
    if not url or not checkout:
        pytest.skip("Explicit disposable GH306_TEST_DATABASE_URL and GH306_JAVA_CHECKOUT required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.database == "gh306_test"
    schema = "gh306_" + uuid4().hex
    bootstrap = create_engine(url)
    with bootstrap.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema},public"})
    try:
        with engine.begin() as connection:
            for migration in sorted(
                (Path(checkout) / "src/main/resources/db/migration").glob("V*__*.sql"),
                key=lambda path: int(path.name.split("__")[0][1:]),
            ):
                connection.exec_driver_sql(
                    migration.read_text(), execution_options={"no_parameters": True}
                )
        yield engine
    finally:
        engine.dispose()
        with bootstrap.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        bootstrap.dispose()


@pytest.fixture
def corpus(database):
    from app.validation.search import FrozenRetrievalRepository, FrozenSearchScope

    data = Corpus(database)
    allowed = data.episode(10)
    excluded = data.episode(20)
    target = data.episode(101)
    future = data.episode(102)
    accepted = data.chunks(allowed, ["민수 의수 사용", "민수 의수 작동 조건"], ready=True)
    hidden = data.chunks(excluded, ["민수 의수 사용 의수 의수"], ready=True)
    hidden += data.chunks(target, ["민수 의수 사용"], ready=True)
    hidden += data.chunks(future, ["민수 의수 사용"], ready=True)
    batch, report = uuid4(), uuid4()
    with database.begin() as connection:
        connection.execute(
            text("""INSERT INTO upload_batches (id,work_id,member_id,upload_type,source_type,status,file_count,created_at,updated_at)
            VALUES (:batch,:work,:member,'SINGLE_EPISODE','FILE','COMPLETED',1,localtimestamp,localtimestamp)"""),
            {"batch": batch, "work": data.work, "member": data.member},
        )
        connection.execute(
            text("""INSERT INTO validation_reports
            (id,work_id,member_id,batch_id,episode_id,source_episode_no,source_content_hash,source_content_s3_key,
             source_content_s3_version,request_hash,baseline_seed,baseline_entries,baseline_hash,baseline_status,status,created_at,updated_at)
            VALUES (:report,:work,:member,:batch,:target,101,:hash,:key,'source-v1',:hash,'{}','[]',:baseline,
                    'READY','WAITING_FOR_ERROR_REVIEW',localtimestamp,localtimestamp)"""),
            {
                "report": report,
                "work": data.work,
                "member": data.member,
                "batch": batch,
                "target": target,
                "hash": "a" * 64,
                "key": f"synthetic/{target}",
                "baseline": "b" * 64,
            },
        )
        connection.execute(
            text("""INSERT INTO validation_baseline_sources
            (id,report_id,episode_id,source_manifest,created_at,updated_at)
            VALUES (:id,:report,:episode,CAST(:manifest AS jsonb),localtimestamp,localtimestamp)"""),
            {
                "id": uuid4(),
                "report": report,
                "episode": allowed,
                "manifest": json.dumps(
                    {
                        "sourceKind": "EPISODE",
                        "episodeId": str(allowed),
                        "episodeNo": 10,
                        "contentHash": "a" * 64,
                        "contentS3Key": f"synthetic/{allowed}",
                        "contentS3Version": "source-v1",
                    }
                ),
            },
        )
    scope = FrozenSearchScope(
        data.member, data.work, report_id=report, baseline_hash="b" * 64, target_hash="a" * 64
    )

    def read(method, *args, **kwargs):
        with data.sessions() as session:
            return getattr(FrozenRetrievalRepository(session, scope, data.spec), method)(
                *args, **kwargs
            )

    yield data, read, scope, accepted, hidden
    with database.begin() as connection:
        connection.execute(text("DELETE FROM validation_reports WHERE id=:id"), {"id": report})
        connection.execute(text("DELETE FROM upload_batches WHERE id=:id"), {"id": batch})
        connection.execute(
            text("DELETE FROM episode_source_purge_requests WHERE work_id=:work"),
            {"work": data.work},
        )
        connection.execute(text("DELETE FROM episodes WHERE work_id=:work"), {"work": data.work})
        connection.execute(text("DELETE FROM works WHERE id=:work"), {"work": data.work})
        connection.execute(text("DELETE FROM members WHERE id=:member"), {"member": data.member})


def test_frozen_filter_is_before_keyword_vector_direct_and_neighbor_limits(corpus):
    _data, read, _scope, allowed, hidden = corpus
    assert read("keyword", "의수", limit=1)[0].chunk_id == allowed[0]
    assert read("vector", [1.0, 0.0] + [0.0] * 1534, limit=1)[0].chunk_id == allowed[0]
    assert read("direct", hidden + allowed, limit=1)[0].chunk_id == allowed[0]
    seeds = read("direct", [allowed[0]])
    assert [row.chunk_id for row in read("neighbors", seeds, radius=1, limit=1)] == [allowed[1]]
    readiness = read("readiness")
    assert readiness.source_count == readiness.text_ready_sources == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "update episodes set content_hash='c' || repeat('c',63) where episode_no=10 and work_id=:work",
        "update episodes set status='ARCHIVED' where episode_no=10 and work_id=:work",
        "update validation_baseline_sources set source_manifest=jsonb_set(source_manifest,'{contentS3Version}','\"old\"') where report_id=:report",
    ],
)
def test_stale_deleted_or_wrong_revision_is_never_returned(corpus, mutation):
    data, read, scope, allowed, _hidden = corpus
    data.mutate(mutation, report=scope.report_id)
    assert not read("keyword", "의수")
    assert not read("direct", allowed)
    assert not read("vector", [1.0, 0.0] + [0.0] * 1534)


def test_invalidated_report_target_change_and_other_owner_are_explicit(corpus):
    from app.retrieval.search_contracts import SearchScopeUnavailable
    from app.validation.search import FrozenRetrievalRepository

    data, read, scope, _allowed, _hidden = corpus
    with data.sessions() as session, pytest.raises(SearchScopeUnavailable):
        FrozenRetrievalRepository(
            session, replace(scope, member_id=scope.member_id + 1), data.spec
        ).readiness()
    data.mutate(
        "update episodes set content_hash=repeat('c',64) where work_id=:work and episode_no=101"
    )
    with pytest.raises(SearchScopeUnavailable):
        read("readiness")


def test_actual_partial_vectors_remain_explicit_readiness(corpus):
    data, read, _scope, allowed, _hidden = corpus
    data.mutate(
        "update retrieval_chunks set embedding=null,status='MISSING' where id=:id", id=allowed[1]
    )
    readiness = read("readiness")
    assert readiness.source_count == readiness.text_ready_sources == 1
    assert readiness.vector_chunks == 1 and readiness.vector_ready_sources == 0
    assert "VECTOR_INDEX_PARTIAL" in readiness.limitations
