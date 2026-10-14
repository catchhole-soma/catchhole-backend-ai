"""GH240 reads against full Java migrations in disposable localhost-only schemas."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.retrieval.contracts import IndexSpec
from app.retrieval.search import RetrievalSearchService
from app.retrieval.search_contracts import SearchScope, SearchScopeUnavailable
from app.retrieval.search_repository import RetrievalSearchRepository

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def search_database():
    url = os.getenv("GH240_TEST_DATABASE_URL")
    checkout = os.getenv("GH240_JAVA_CHECKOUT")
    if not url or not checkout:
        pytest.skip("Explicit GH240_TEST_DATABASE_URL and GH240_JAVA_CHECKOUT required")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or parsed.database != "gh240_test":
        pytest.fail("Only disposable localhost gh240_test is allowed")
    schema = "gh240_search_" + uuid4().hex
    bootstrap = create_engine(url)
    with bootstrap.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
    engine = create_engine(
        url, connect_args={"options": f"-c search_path={schema},public -c timezone=Asia/Seoul"}
    )
    migrations = sorted(
        (Path(checkout) / "src/main/resources/db/migration").glob("V*__*.sql"),
        key=lambda path: int(path.name.split("__")[0][1:]),
    )
    assert any(path.name.startswith("V76__") for path in migrations)
    try:
        with engine.begin() as connection:
            for migration in migrations:
                connection.exec_driver_sql(
                    migration.read_text(), execution_options={"no_parameters": True}
                )
        yield engine
    finally:
        engine.dispose()
        with bootstrap.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        bootstrap.dispose()


class Corpus:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = sessionmaker(engine)
        self.member = uuid4().int % (2**61)
        self.work = uuid4()
        self.spec = IndexSpec()
        self.scope = SearchScope(self.member, self.work)
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO members
                (id,email,password_hash,phone_number,phone_verified,display_name,status,role,created_at,updated_at)
                VALUES (:id,:email,'test',:phone,false,'synthetic','ACTIVE','USER',localtimestamp,localtimestamp)"""),
                dict(id=self.member, email=f"{self.member}@test.invalid", phone=str(self.member)),
            )
            connection.execute(
                text("""INSERT INTO works
                (id,member_id,title,genre,latest_episode_no,created_at,updated_at)
                VALUES (:work,:member,'synthetic','FANTASY',0,localtimestamp,localtimestamp)"""),
                dict(work=self.work, member=self.member),
            )

    def episode(self, number, *, prepared=True):
        episode = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO episodes
                (id,work_id,episode_no,content_s3_key,content_s3_version,content_hash,char_count,
                 status,created_at,updated_at,content_updated_at)
                VALUES (:id,:work,:number,:key,'source-v1',:hash,100,'ANALYZED',
                        localtimestamp,localtimestamp,localtimestamp)"""),
                dict(
                    id=episode,
                    work=self.work,
                    number=number,
                    key=f"synthetic/{episode}",
                    hash="a" * 64,
                ),
            )
            if prepared:
                connection.execute(
                    text("""INSERT INTO retrieval_sources
                    (episode_id,work_id,source_key,source_version,source_hash,chunk_policy,chunk_count,prepared_at)
                    VALUES (:id,:work,:key,'source-v1',:hash,:policy,0,localtimestamp)"""),
                    dict(
                        id=episode,
                        work=self.work,
                        key=f"synthetic/{episode}",
                        hash="a" * 64,
                        policy=self.spec.policy,
                    ),
                )
        return episode

    def chunks(self, episode, contents, *, ready=False, vectors=None):
        ids = []
        with self.engine.begin() as connection:
            start_index = connection.scalar(
                text(
                    "SELECT coalesce(max(chunk_index)+1,0) FROM retrieval_chunks WHERE episode_id=:episode"
                ),
                dict(episode=episode),
            )
            for index, value in enumerate(contents):
                chunk_id = uuid4()
                ids.append(chunk_id)
                embedding = (
                    None
                    if not ready
                    else (vectors[index] if vectors else [1.0, 0.0] + [0.0] * 1534)
                )
                connection.execute(
                    text("""INSERT INTO retrieval_chunks
                    (id,episode_id,chunk_index,chunk_text,start_offset,end_offset,chunk_hash,token_count,
                     embedding,embedding_model,embedding_version,embedded_at,status)
                    VALUES (:id,:episode,:index,:text,:start,:end,:hash,10,CAST(:vector AS vector),
                            :model,:version,:at,:status)"""),
                    dict(
                        id=chunk_id,
                        episode=episode,
                        index=start_index + index,
                        text=value,
                        start=(start_index + index) * 100,
                        end=(start_index + index) * 100 + len(value),
                        hash=sha256(value.encode()).hexdigest(),
                        vector=None if embedding is None else str(embedding),
                        model=self.spec.model if ready else None,
                        version=self.spec.version if ready else None,
                        at="2026-10-13 00:00:00" if ready else None,
                        status="READY" if ready else "MISSING",
                    ),
                )
            connection.execute(
                text("""UPDATE retrieval_sources SET chunk_count=
                (SELECT count(*) FROM retrieval_chunks WHERE episode_id=:episode) WHERE episode_id=:episode"""),
                dict(episode=episode),
            )
        return ids

    def read(self, method, *args, scope=None, spec=None, **kwargs):
        with self.sessions() as session:
            return getattr(
                RetrievalSearchRepository(session, scope or self.scope, spec or self.spec), method
            )(*args, **kwargs)

    def mutate(self, sql, **params):
        with self.engine.begin() as connection:
            connection.execute(text(sql), dict(work=self.work, member=self.member, **params))


@pytest.fixture
def corpus(search_database):
    fixture = Corpus(search_database)
    yield fixture
    fixture.mutate("DELETE FROM episode_source_purge_requests WHERE work_id=:work")
    fixture.mutate("DELETE FROM episodes WHERE work_id=:work")
    fixture.mutate("DELETE FROM works WHERE id=:work")
    fixture.mutate("DELETE FROM members WHERE id=:member")


def test_keyword_and_vector_rank_full_corpus_before_limit(corpus):
    episode = corpus.episode(1)
    corpus.chunks(
        episode, ["ordinary filler"] * 125, ready=True, vectors=[[0.0, 1.0] + [0.0] * 1534] * 125
    )
    last = corpus.chunks(episode, ["rare_needle% exact"], ready=True)[0]
    assert corpus.read("keyword", "rare_needle%", limit=1)[0].chunk_id == last
    assert corpus.read("vector", [1.0, 0.0] + [0.0] * 1534, limit=1)[0].chunk_id == last


def test_keyword_wildcards_are_literal_bound_values(corpus):
    episode = corpus.episode(1)
    literal, other = corpus.chunks(episode, ["100% 능력_a \\ 규칙", "1000 능력Xa x 규칙"])
    assert [c.chunk_id for c in corpus.read("keyword", "능력_a")] == [literal]
    assert [c.chunk_id for c in corpus.read("keyword", "100%")] == [literal]
    assert [c.chunk_id for c in corpus.read("keyword", "\\")] == [literal]
    assert corpus.read("keyword", "' OR 1=1 --") == []


def test_vector_expansion_excludes_prior_hits_before_top_k_through_the_service(corpus):
    first, second = corpus.episode(1), corpus.episode(2)
    prior = corpus.chunks(first, [f"needle seed {index}" for index in range(12)], ready=True)
    novel = corpus.chunks(
        second, ["new explanation"], ready=True, vectors=[[0.8, 0.6] + [0.0] * 1534]
    )[0]
    seeds = corpus.read("keyword", "needle", limit=12)
    assert {chunk.chunk_id for chunk in seeds} == set(prior)

    class Embedding:
        model, version = corpus.spec.model, corpus.spec.version
        calls = 0

        async def create_embedding(self, query):
            self.calls += 1
            return [1.0, 0.0] + [0.0] * 1534

    client = Embedding()
    service = RetrievalSearchService(corpus.sessions)
    original = asyncio.run(service.vector(corpus.scope, "question", client, limit=12))
    assert {chunk.chunk_id for chunk in original.chunks} == set(prior)
    expanded = asyncio.run(
        service.vector(
            corpus.scope,
            "question",
            client,
            limit=12,
            excluded_chunk_ids=tuple(chunk.chunk_id for chunk in seeds),
        )
    )
    assert [chunk.chunk_id for chunk in expanded.chunks] == [novel]
    assert client.calls == 2


def test_missing_embeddings_allow_text_but_partial_vector_readiness(corpus):
    episode = corpus.episode(1)
    ids = corpus.chunks(episode, ["민수의 흑염", "민수의 검"])
    rows = corpus.read("keyword", "민수")
    assert {c.chunk_id for c in rows} == set(ids)
    assert corpus.read("direct", ids) == [
        replace(row, methods=("DIRECT",), score=1.0) for row in rows
    ]
    assert [c.chunk_id for c in corpus.read("neighbors", rows[:1])] == [ids[1]]
    assert not corpus.read("vector", [1.0, 0.0] + [0.0] * 1534)
    readiness = corpus.read("readiness")
    assert readiness.text_ready_sources == 1 and readiness.vector_ready_sources == 0
    assert readiness.limitations == ("VECTOR_INDEX_PARTIAL",)


@pytest.mark.parametrize("seed_count", [6, 12])
def test_neighbors_exclude_seeds_before_limit_and_preserve_current_scope(corpus, seed_count):
    outside, episode = corpus.episode(1), corpus.episode(2)
    outside_ids = corpus.chunks(outside, ["context before", "needle outside", "context after"])
    ids = corpus.chunks(
        episode,
        [f"{'needle' if index % 3 == 1 else 'context'} {index}" for index in range(seed_count * 3)],
    )
    scope = replace(corpus.scope, episode_from=2, episode_to=2)
    seeds = corpus.read("keyword", "needle", scope=scope, limit=seed_count)
    assert len(seeds) == seed_count
    # Invalid adjacent text and an otherwise valid seed outside the episode range
    # must be filtered before the neighbor budget is applied.
    corpus.mutate("UPDATE retrieval_chunks SET chunk_hash=repeat('b',64) WHERE id=:id", id=ids[0])
    outside_seeds = corpus.read("direct", [outside_ids[1]])
    neighbors = corpus.read("neighbors", [*seeds, *outside_seeds], scope=scope, radius=1, limit=6)
    assert [chunk.chunk_index for chunk in neighbors] == [2, 3, 5, 6, 8, 9]
    assert {chunk.episode_id for chunk in neighbors} == {episode}
    assert {chunk.chunk_id for chunk in neighbors}.isdisjoint(chunk.chunk_id for chunk in seeds)
    assert all(chunk.methods == ("NEIGHBOR",) for chunk in neighbors)


def test_invalid_chunks_are_filtered_before_top_k_and_counted_partial(corpus):
    episode = corpus.episode(1)
    bad, valid = corpus.chunks(episode, ["needle needle", "needle"], ready=True)
    corpus.mutate("UPDATE retrieval_chunks SET chunk_hash=repeat('b',64) WHERE id=:id", id=bad)
    assert corpus.read("keyword", "needle", limit=1)[0].chunk_id == valid
    assert corpus.read("vector", [1.0, 0.0] + [0.0] * 1534, limit=1)[0].chunk_id == valid
    assert not corpus.read("direct", [bad])
    assert corpus.read("readiness").limitations == ("TEXT_INDEX_PARTIAL", "VECTOR_INDEX_PARTIAL")
    corpus.mutate("UPDATE retrieval_chunks SET end_offset=end_offset+1 WHERE id=:id", id=valid)
    assert not corpus.read("keyword", "needle")


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE episodes SET content_s3_key='other' WHERE id=:episode",
        "UPDATE episodes SET content_hash=repeat('b',64) WHERE id=:episode",
        "UPDATE episodes SET content_s3_version='other' WHERE id=:episode",
        "UPDATE episodes SET status='ARCHIVED' WHERE id=:episode",
        "UPDATE works SET lifecycle_status='PURGING' WHERE id=:work",
        "UPDATE members SET status='PURGING' WHERE id=:member",
        "UPDATE retrieval_sources SET chunk_policy='other' WHERE episode_id=:episode",
        "UPDATE retrieval_chunks SET embedding_model='other' WHERE episode_id=:episode",
        "UPDATE retrieval_chunks SET embedding_version='other' WHERE episode_id=:episode",
    ],
)
def test_all_query_paths_and_revalidation_apply_same_lifecycle_filters(corpus, mutation):
    episode = corpus.episode(1)
    ids = corpus.chunks(episode, ["needle alpha", "needle beta"], ready=True)
    original = corpus.read("direct", ids)
    corpus.mutate(mutation, episode=episode)
    assert not corpus.read("direct", ids)
    assert not corpus.read("keyword", "needle")
    assert not corpus.read("vector", [1.0, 0.0] + [0.0] * 1534)
    assert not corpus.read("neighbors", original)
    assert not corpus.read("revalidate", original)


def test_trusted_member_work_range_and_neighbors_never_expand_scope(corpus):
    episode1, episode2 = corpus.episode(1), corpus.episode(2)
    first = corpus.chunks(episode1, ["needle first"], ready=True)[0]
    second = corpus.chunks(episode2, ["needle second"], ready=True)[0]
    for scope in [
        replace(corpus.scope, member_id=corpus.member + 1),
        replace(corpus.scope, work_id=uuid4()),
    ]:
        assert corpus.read("direct", [first, second], scope=scope) == []
        assert corpus.read("keyword", "needle", scope=scope) == []
        assert corpus.read("vector", [1.0, 0.0] + [0.0] * 1534, scope=scope) == []
        with pytest.raises(SearchScopeUnavailable):
            corpus.read("readiness", scope=scope)
    scope = replace(corpus.scope, episode_to=1)
    assert [c.chunk_id for c in corpus.read("keyword", "needle", scope=scope)] == [first]
    outside_seed = corpus.read("direct", [second])
    assert corpus.read("neighbors", outside_seed, scope=scope) == []


def test_purge_pending_is_excluded_while_rows_remain_physically_present(corpus):
    episode = corpus.episode(1)
    ids = corpus.chunks(episode, ["needle"], ready=True)
    original = corpus.read("direct", ids)
    corpus.mutate(
        """INSERT INTO episode_source_purge_requests
        (id,episode_id,work_id,previous_episode_no,previous_content_key,retained_content_key,status,
         requested_at,attempt_count,created_at,updated_at)
        VALUES (:id,:episode,:work,1,'old','retained','REQUESTED',localtimestamp,0,localtimestamp,localtimestamp)""",
        episode=episode,
        id=uuid4(),
    )
    assert not corpus.read("keyword", "needle")
    assert not corpus.read("direct", ids)
    assert not corpus.read("vector", [1.0, 0.0] + [0.0] * 1534)
    assert not corpus.read("neighbors", original)
    assert not corpus.read("revalidate", original)
    assert corpus.read("readiness").text_ready_sources == 0


def test_replaced_same_id_or_new_revision_cannot_revalidate_old_passage(corpus):
    episode = corpus.episode(1)
    ids = corpus.chunks(episode, ["needle"], ready=True)
    original = corpus.read("direct", ids)
    corpus.mutate(
        "UPDATE episodes SET content_s3_key='new',content_s3_version=NULL WHERE id=:episode",
        episode=episode,
    )
    corpus.mutate(
        "UPDATE retrieval_sources SET source_key='new',source_version=NULL WHERE episode_id=:episode",
        episode=episode,
    )
    assert corpus.read("keyword", "needle")  # internally current new revision
    assert not corpus.read("revalidate", original)  # old exact reference is invalid
    assert not corpus.read("neighbors", original)


def test_unprepared_episodes_count_as_partial_not_empty_complete_corpus(corpus):
    first = corpus.episode(1)
    corpus.chunks(first, ["needle"], ready=True)
    corpus.episode(2, prepared=False)
    readiness = corpus.read("readiness")
    assert readiness.source_count == 2 and readiness.vector_ready_sources == 1
    assert readiness.partial and readiness.text_chunks == readiness.vector_chunks == 1


def test_edit_during_metered_query_embedding_cannot_return_stale_source(corpus):
    episode = corpus.episode(1)
    corpus.chunks(episode, ["needle"], ready=True)

    class Embedding:
        model, version = corpus.spec.model, corpus.spec.version
        calls = 0

        async def create_embedding(self, text):
            self.calls += 1
            corpus.mutate(
                "UPDATE episodes SET content_hash=repeat('b',64) WHERE id=:episode", episode=episode
            )
            return [1.0, 0.0] + [0.0] * 1534

    client = Embedding()
    result = asyncio.run(
        RetrievalSearchService(corpus.sessions).vector(corpus.scope, "needle", client)
    )
    assert client.calls == 1 and not result.chunks
    assert result.readiness.partial


def test_linked_episode_is_priority_hint_not_an_exclusive_scope(corpus):
    first, linked = corpus.episode(1), corpus.episode(2)
    first_id = corpus.chunks(first, ["needle"], ready=True)[0]
    linked_id = corpus.chunks(linked, ["needle"], ready=True)[0]
    found = corpus.read("keyword", "needle", preferred_episode_ids=[linked])
    assert [c.chunk_id for c in found] == [linked_id, first_id]
    limited_scope = replace(corpus.scope, episode_to=1)
    assert [
        c.chunk_id
        for c in corpus.read(
            "keyword", "needle", preferred_episode_ids=[linked], scope=limited_scope
        )
    ] == [first_id]


def test_matching_item_and_context_outrank_a_generic_linked_episode(corpus):
    relevant, linked = corpus.episode(1), corpus.episode(2)
    relevant_id = corpus.chunks(relevant, ["잔광을 스승에게 배운 장면"], ready=True)[0]
    corpus.chunks(linked, ["스승을 기다리는 장면"], ready=True)
    found = corpus.read("keyword", "잔광 스승", preferred_episode_ids=[linked], limit=1)
    assert [chunk.chunk_id for chunk in found] == [relevant_id]
