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


def test_keyword_rank_weights_rare_words_over_many_common_words(corpus):
    # 흔한 단어 셋이 겹친 무관한 청크보다 희귀한 결정 단어가 든 실제 장면을 먼저 읽는다(#319 X09).
    # 일치 비율만 쓰면 둘 다 3/5로 같아 앞 회차의 무관한 청크가 먼저 나왔다.
    filler = ["비요른 처음 어떻게 걸었다", "비요른 처음 어떻게 먹었다", "비요른 어떻게 잠들었다"]
    corpus.chunks(corpus.episode(1), [text for text in filler for _ in range(8)])
    scene = corpus.chunks(corpus.episode(7), ["처음 사람을 죽인 장면"])[0]
    found = corpus.read("keyword", "비요른 처음 사람을 죽인 어떻게", limit=5)
    assert found[0].chunk_id == scene
    # 모든 단어가 든 청크는 1.0으로 가장 앞서고, 단어가 하나면 기존 일치 순위와 같다.
    full = corpus.chunks(corpus.episode(8), ["비요른 처음 사람을 죽인 어떻게"])[0]
    found = corpus.read("keyword", "비요른 처음 사람을 죽인 어떻게", limit=2)
    assert found[0].chunk_id == full and found[0].score == pytest.approx(1.0)
    single = corpus.read("keyword", "죽인", limit=5)
    assert [c.chunk_id for c in single] == [scene, full]
    assert all(c.score == pytest.approx(1.0) for c in single)


def test_single_character_term_does_not_outweigh_longer_terms(corpus):
    # '레벨 업'이 띄어쓰기로 갈린 한 글자 '업'은 '업적' 안에서도 일치한다. 희귀하다는 이유로 높은 무게를
    # 받으면 '비요른'·'레벨'이 든 실제 장면(22화)이 '레벨'·'업적' 청크에 밀려 후보 밖으로 나갔다(#319 V2-053).
    corpus.chunks(corpus.episode(1), ["비요른 산책"] * 12 + ["업무 회의"] * 3)
    scene = corpus.chunks(corpus.episode(22), ["비요른 레벨 상승"])[0]
    noise = corpus.chunks(corpus.episode(23), ["레벨 업적 달성"])[0]
    ranked = [c.chunk_id for c in corpus.read("keyword", "비요른 레벨 업", limit=20)]
    assert ranked.index(scene) < ranked.index(noise)


def test_keyword_wildcards_are_literal_bound_values(corpus):
    episode = corpus.episode(1)
    literal, other = corpus.chunks(episode, ["100% 능력_a \\ 규칙", "1000 능력Xa x 규칙"])
    assert [c.chunk_id for c in corpus.read("keyword", "능력_a")] == [literal]
    assert [c.chunk_id for c in corpus.read("keyword", "100%")] == [literal]
    assert [c.chunk_id for c in corpus.read("keyword", "\\")] == [literal]
    assert corpus.read("keyword", "' OR 1=1 --") == []


def test_keyword_ignores_case_only_where_terms_have_case(corpus):
    # 한글 단어는 LIKE, 영문 단어는 ILIKE로 비교해도 기존처럼 영문 대소문자를 무시한다.
    upper, lower, korean = corpus.chunks(
        corpus.episode(1), ["HP 물약을 마셨다", "hp가 바닥났다", "체력 물약"]
    )
    assert [c.chunk_id for c in corpus.read("keyword", "hp")] == [upper, lower]
    assert [c.chunk_id for c in corpus.read("keyword", "물약")] == [upper, korean]
    assert [c.chunk_id for c in corpus.read("keyword", "Hp 물약")] == [upper, lower, korean]
    ordered = corpus.read("keyword_ordered", ["hP"], ["물약"], order="EARLIEST")
    assert [c.chunk_id for c in ordered] == [upper]


def test_keyword_ordered_reads_anchor_mentions_in_episode_order_with_rank_scores(corpus):
    first, third, ninth = corpus.episode(1), corpus.episode(3), corpus.episode(9)
    corpus.chunks(first, ["filler without the item", "비요른만 등장하는 장면"])
    early_a, early_b = corpus.chunks(
        third, ["비요른이 메이스를 처음 쥐었다", "메이스만 언급되는 문장"]
    )
    late = corpus.chunks(ninth, ["비요른은 메이스를 휘둘렀다"])[0]
    earliest = corpus.read("keyword_ordered", ["비요른"], ["메이스", "휘둘렀다"], order="EARLIEST")
    # 필수 표현을 모두, 선택 표현을 절반 이상 포함하는 청크만 출처 회차·청크 순서대로 반환한다.
    assert [c.chunk_id for c in earliest] == [early_a, late]
    assert [c.episode_no for c in earliest] == [3, 9]
    assert [c.score for c in earliest] == [1.0, 1.0 - 1 / 10000]
    assert all(c.methods == ("KEYWORD",) for c in earliest)
    latest = corpus.read("keyword_ordered", [], ["메이스"], order="LATEST", limit=2)
    assert [c.chunk_id for c in latest] == [late, early_b]
    assert latest[0].score > latest[1].score
    # 필수 표현만 있으면 그 인물의 첫 등장 순서다.
    assert [c.episode_no for c in corpus.read("keyword_ordered", ["비요른"], [], order="EARLIEST")][
        :2
    ] == [1, 3]
    # 와일드카드는 값으로 묶여 literal이며 방향·빈 표현은 검증한다.
    assert corpus.read("keyword_ordered", [], ["메이스%"], order="EARLIEST") == []
    with pytest.raises(ValueError):
        corpus.read("keyword_ordered", ["메이스"], [], order="RANDOM")
    with pytest.raises(ValueError):
        corpus.read("keyword_ordered", ["   "], [], order="EARLIEST")


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


def test_neighbors_preserve_search_seed_rank_before_episode_order_and_limit(corpus):
    early = []
    for number in range(1, 5):
        episode = corpus.episode(number)
        early.append(corpus.chunks(episode, ["before", "needle", "after"]))
    strongest = corpus.episode(50)
    strongest_ids = corpus.chunks(strongest, ["before", "needle rare", "after"])
    seeds = corpus.read("keyword", "needle rare", limit=5)
    assert seeds[0].chunk_id == strongest_ids[1]
    neighbors = corpus.read("neighbors", seeds, radius=1, limit=6)
    assert [c.chunk_id for c in neighbors] == [
        strongest_ids[0],
        strongest_ids[2],
        early[0][0],
        early[0][2],
        early[1][0],
        early[1][2],
    ]


def test_neighbors_use_distance_to_earliest_seed_and_deduplicate_shared_context(corpus):
    episode = corpus.episode(1)
    ids = corpus.chunks(episode, [f"context {index}" for index in range(7)])
    seeds = corpus.read("direct", [ids[4], ids[1]])
    neighbors = corpus.read("neighbors", [*seeds, seeds[0]], radius=2, limit=6)
    assert [c.chunk_id for c in neighbors] == [ids[3], ids[5], ids[2], ids[6], ids[0]]
    assert [c.score for c in neighbors] == pytest.approx([0.5, 0.5, 1 / 3, 1 / 3, 0.5])
    assert len({c.chunk_id for c in neighbors}) == len(neighbors)


def test_neighbors_discard_stale_high_ranked_seed_before_expansion_limit(corpus):
    stale_episode, current_episode = corpus.episode(50), corpus.episode(60)
    stale = corpus.chunks(stale_episode, ["before", "seed", "after"])
    current = corpus.chunks(current_episode, ["before", "seed", "after"])
    seeds = corpus.read("direct", [stale[1], current[1]])
    corpus.mutate(
        "UPDATE episodes SET content_s3_version='replaced' WHERE id=:episode", episode=stale_episode
    )
    corpus.mutate(
        "UPDATE retrieval_chunks SET chunk_hash=repeat('b',64) WHERE id=:id", id=current[0]
    )
    neighbors = corpus.read("neighbors", seeds, radius=1, limit=1)
    assert [c.chunk_id for c in neighbors] == [current[2]]


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


# ---- 원문 탐색용 조회(#319): 회차 통째 읽기, 검색어별 회차 분포, 읽을 수 있는 회차 범위 ----


def test_whole_episode_read_returns_valid_chunks_in_episode_then_text_order(corpus):
    first, second, third = corpus.episode(1), corpus.episode(2), corpus.episode(3)
    second_ids = corpus.chunks(second, ["둘째 회차 첫 조각", "둘째 회차 둘째 조각"])
    first_ids = corpus.chunks(first, ["첫 회차 조각"])
    third_ids = corpus.chunks(third, ["셋째 회차 조각"])
    # 요청 순서와 무관하게 회차·본문 순서이며, 없는 회차 번호는 오류가 아니라 빈 결과다.
    read = corpus.read("episodes", [2, 1, 9, 2])
    assert [c.chunk_id for c in read] == [*first_ids, *second_ids]
    assert all(c.methods == ("EPISODE",) and c.score == 1.0 for c in read)
    assert [c.episode_no for c in read] == [1, 2, 2]
    # 개수 제한은 정렬 뒤에 걸려 뒤쪽 회차가 빠진다.
    assert [c.chunk_id for c in corpus.read("episodes", [1, 2, 3], limit=2)] == [
        first_ids[0],
        second_ids[0],
    ]
    # 서버가 정한 회차 범위와 회원·작품 밖은 번호를 요청해도 읽지 못한다.
    scope = replace(corpus.scope, episode_from=2, episode_to=3)
    assert [c.chunk_id for c in corpus.read("episodes", [1, 2, 3], scope=scope)] == [
        *second_ids,
        *third_ids,
    ]
    for scope in [
        replace(corpus.scope, member_id=corpus.member + 1),
        replace(corpus.scope, work_id=uuid4()),
    ]:
        assert corpus.read("episodes", [1, 2, 3], scope=scope) == []
    # 유효하지 않은 조각은 통째 읽기에서도 빠진다.
    corpus.mutate(
        "UPDATE retrieval_chunks SET chunk_hash=repeat('b',64) WHERE id=:id", id=second_ids[0]
    )
    assert [c.chunk_id for c in corpus.read("episodes", [2])] == [second_ids[1]]


def test_episode_distribution_counts_chunks_containing_every_word(corpus):
    first, third = corpus.episode(1), corpus.episode(3)
    corpus.chunks(first, ["등대 문 앞에 섰다", "등대만 보이는 조각", "문만 보이는 조각"])
    corpus.chunks(third, ["등대의 문이 열렸다", "등대 문은 100%_확실히 닫혔다"])
    assert corpus.read("keyword_episodes", "등대 문") == [(1, 1), (3, 2)]
    assert corpus.read("keyword_episodes", "등대") == [(1, 2), (3, 2)]
    assert corpus.read("keyword_episodes", "없는단어") == []
    # %, _ 는 와일드카드가 아니라 글자다.
    assert corpus.read("keyword_episodes", "100%_확실") == [(3, 1)]
    assert corpus.read("keyword_episodes", "100_%확실") == []
    assert corpus.read("keyword_episodes", "등_") == []
    assert corpus.read("episode_span") == (1, 3, 2)
    scope = replace(corpus.scope, episode_to=2)
    assert corpus.read("keyword_episodes", "등대", scope=scope) == [(1, 2)]
    assert corpus.read("episode_span", scope=scope) == (1, 1, 1)
    for scope in [
        replace(corpus.scope, member_id=corpus.member + 1),
        replace(corpus.scope, work_id=uuid4()),
    ]:
        assert corpus.read("keyword_episodes", "등대", scope=scope) == []
        assert corpus.read("episode_span", scope=scope) == (None, None, 0)


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
def test_exploration_reads_apply_the_same_lifecycle_filters(corpus, mutation):
    episode = corpus.episode(1)
    corpus.chunks(episode, ["needle alpha", "needle beta"], ready=True)
    assert len(corpus.read("episodes", [1])) == 2
    assert corpus.read("keyword_episodes", "needle") == [(1, 2)]
    assert corpus.read("episode_span") == (1, 1, 1)
    corpus.mutate(mutation, episode=episode)
    assert corpus.read("episodes", [1]) == []
    assert corpus.read("keyword_episodes", "needle") == []
    assert corpus.read("episode_span") == (None, None, 0)


def test_exploration_reads_exclude_purge_pending_episodes(corpus):
    kept, purged = corpus.episode(1), corpus.episode(2)
    kept_ids = corpus.chunks(kept, ["needle kept"])
    corpus.chunks(purged, ["needle purged"])
    corpus.mutate(
        """INSERT INTO episode_source_purge_requests
        (id,episode_id,work_id,previous_episode_no,previous_content_key,retained_content_key,status,
         requested_at,attempt_count,created_at,updated_at)
        VALUES (:id,:episode,:work,2,'old','retained','REQUESTED',localtimestamp,0,localtimestamp,localtimestamp)""",
        episode=purged,
        id=uuid4(),
    )
    assert [c.chunk_id for c in corpus.read("episodes", [1, 2])] == kept_ids
    assert corpus.read("keyword_episodes", "needle") == [(1, 1)]
    assert corpus.read("episode_span") == (1, 1, 1)


def test_exploration_search_through_the_service_returns_chunks_and_distribution(corpus):
    early, late = corpus.episode(2), corpus.episode(7)
    early_ids = corpus.chunks(early, ["등대 문 앞", "등대 이야기"])
    late_ids = corpus.chunks(late, ["등대 문 뒤"])
    service = RetrievalSearchService(corpus.sessions, corpus.spec)
    relevant = asyncio.run(service.keyword_explore(corpus.scope, "등대 문", limit=10))
    # 관련도 순위는 기존 keyword와 같고(모든 단어 일치가 먼저), 분포는 모든 단어가 든 조각만 센다.
    assert [c.chunk_id for c in relevant.chunks][:2] == [early_ids[0], late_ids[0]]
    assert relevant.episode_hits == ((2, 1), (7, 1)) and relevant.limited is False
    latest = asyncio.run(service.keyword_explore(corpus.scope, "등대 문", limit=1, order="LATEST"))
    assert [c.chunk_id for c in latest.chunks] == late_ids and latest.limited is True
    assert latest.episode_hits == ((2, 1), (7, 1))
    whole = asyncio.run(service.episodes(corpus.scope, [7, 2]))
    assert [c.chunk_id for c in whole.chunks] == [*early_ids, *late_ids]
    assert asyncio.run(service.episode_span(corpus.scope)) == (2, 7, 2)
    with pytest.raises(SearchScopeUnavailable):
        asyncio.run(service.episode_span(replace(corpus.scope, member_id=corpus.member + 1)))
    with pytest.raises(SearchScopeUnavailable):
        asyncio.run(service.episodes(replace(corpus.scope, work_id=uuid4()), [2]))
