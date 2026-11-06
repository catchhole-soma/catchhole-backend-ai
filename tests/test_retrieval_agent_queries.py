"""원문 탐색용 조회(회차 통째 읽기, 검색어별 회차 분포, 읽을 수 있는 회차 범위)의 계약.

DB 없이 SQL 문과 바인딩 값을 붙잡아 공통 유효성 조건·정렬·상한을 확인한다. 실제 PostgreSQL 결과는
tests/test_retrieval_search_postgres.py의 통합 테스트가 확인한다.
"""

import asyncio
from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.retrieval.contracts import IndexSpec
from app.retrieval.search import RetrievalSearchService
from app.retrieval.search_contracts import SearchReadiness, SearchScope
from app.retrieval.search_repository import CURRENT_FROM, RetrievalSearchRepository
from tests.test_retrieval_search_evidence import chunk


class RecordingSession:
    def __init__(self, rows=()):
        self.rows, self.statements = list(rows), []

    def execute(self, statement, params):
        self.statements.append((str(statement), dict(params)))
        rows = self.rows

        class Result:
            def mappings(self):
                return self

            def __iter__(self):
                return iter(rows)

            def one(self):
                return rows[0]

        return Result()


def repository(rows=(), scope=None):
    session = RecordingSession(rows)
    scope = scope or SearchScope(42, uuid4(), 3, 8)
    return RetrievalSearchRepository(session, scope, IndexSpec()), session, scope


def assert_scoped(sql, params, scope):
    """권한·보관·파기·원문 일치·회차 범위 조건(CURRENT_FROM)이 그대로 들어 있고 서버 범위로 묶인다."""
    assert CURRENT_FROM in sql
    assert (params["work"], params["member"]) == (scope.work_id, scope.member_id)
    assert (params["lo"], params["hi"]) == (scope.episode_from, scope.episode_to)
    assert sql.index(CURRENT_FROM) < sql.index("ORDER BY") if "ORDER BY" in sql else True


def test_whole_episode_read_applies_the_shared_predicates_before_order_and_limit():
    repo, session, scope = repository()
    assert repo.episodes([5, 3, 5], limit=40) == []
    sql, params = session.statements[0]
    assert_scoped(sql, params, scope)
    assert "AND e.episode_no=ANY(CAST(:episodes AS integer[]))" in sql
    # 회차·본문 순서로 읽고, 개수 제한은 유효성 조건 뒤에 건다.
    assert sql.rstrip().endswith("ORDER BY e.episode_no,c.chunk_index,c.id LIMIT :limit")
    assert params["episodes"] == [5, 3] and params["limit"] == 40
    # 회차 번호는 값으로 묶인다. SQL 문에 숫자를 끼워 넣지 않는다.
    assert "5" not in sql.split(CURRENT_FROM)[1]


@pytest.mark.parametrize(
    "numbers", [[], list(range(1, 12)), [0], [-1], [2147483648], [True], ["3"], [3.0], [None]]
)
def test_whole_episode_read_rejects_invalid_episode_numbers_before_any_query(numbers):
    repo, session, _ = repository()
    with pytest.raises(ValueError):
        repo.episodes(numbers)
    assert session.statements == []


@pytest.mark.parametrize("limit", [0, 101, True, "10"])
def test_whole_episode_read_keeps_the_common_limit_bounds(limit):
    repo, session, _ = repository()
    with pytest.raises(ValueError):
        repo.episodes([1], limit=limit)
    assert session.statements == []
    assert repo.episodes(list(range(1, 11)), limit=100) == []


def test_episode_distribution_counts_all_word_matches_without_reading_text():
    repo, session, scope = repository(
        [{"episode_no": 3, "matched": 2}, {"episode_no": 7, "matched": 1}]
    )
    assert repo.keyword_episodes("  등대   문 등대 ") == [(3, 2), (7, 1)]
    sql, params = session.statements[0]
    assert_scoped(sql, params, scope)
    # 본문 열은 읽지 않고 회차 번호와 개수만 고른다. 모든 단어가 함께 든 조각만 센다.
    assert sql.startswith("SELECT e.episode_no AS episode_no,count(*) AS matched")
    assert "c.chunk_text," not in sql.split("FROM retrieval_chunks")[0]
    assert sql.count("c.chunk_text ILIKE") == 2 and " OR " not in sql.split(CURRENT_FROM)[1]
    assert sql.rstrip().endswith("GROUP BY e.episode_no ORDER BY e.episode_no")
    assert (params["pattern_0"], params["pattern_1"]) == ("%등대%", "%문%")


def test_episode_distribution_binds_literal_patterns_and_at_most_twelve_words():
    repo, session, _ = repository()
    repo.keyword_episodes("100%_확실\\한 " + " ".join(f"단어{n}" for n in range(20)))
    sql, params = session.statements[0]
    # %, _, 역슬래시는 와일드카드가 아니라 글자다. 검색어는 SQL 문에 들어가지 않는다.
    assert params["pattern_0"] == "%100\\%\\_확실\\\\한%"
    assert "확실" not in sql and "단어" not in sql
    assert sum(1 for key in params if key.startswith("pattern_")) == 12
    for invalid in ("", "   ", "가" * 1001, None):
        with pytest.raises(ValueError):
            repo.keyword_episodes(invalid)
    assert len(session.statements) == 1


def test_episode_span_reads_only_numbers_from_the_same_scoped_corpus():
    repo, session, scope = repository([{"lo": 3, "hi": 8, "total": 4}])
    assert repo.episode_span() == (3, 8, 4)
    sql, params = session.statements[0]
    assert_scoped(sql, params, scope)
    assert sql.startswith("SELECT min(e.episode_no) AS lo,max(e.episode_no) AS hi,")
    assert "count(DISTINCT e.id) AS total" in sql and "chunk_text," not in sql.split("FROM")[0]


def service_fixture(*, hits=((3, 2),)):
    scope = SearchScope(42, uuid4(), 3, 8)
    found = chunk()
    events = []

    @contextmanager
    def sessions():
        events.append("session-open")
        try:
            yield object()
        finally:
            events.append("session-close")

    class Repository:
        def __init__(self, session, trusted_scope, spec):
            assert trusted_scope == scope
            events.append("repository")

        def readiness(self):
            events.append("readiness")
            return SearchReadiness(1, 1, 1, 1, 1)

        def keyword(self, query, *, limit):
            events.append(("keyword", query, limit))
            return [found]

        def keyword_ordered(self, required, optional, *, order, limit):
            events.append(("keyword_ordered", tuple(required), tuple(optional), order, limit))
            return [found]

        def keyword_episodes(self, query):
            events.append(("keyword_episodes", query))
            return list(hits)

        def episodes(self, numbers, *, limit):
            events.append(("episodes", tuple(numbers), limit))
            return [found] * min(limit, 2)

        def episode_span(self):
            events.append("episode_span")
            return 3, 8, 4

    return RetrievalSearchService(sessions, repository_factory=Repository), scope, events, found


def test_exploration_search_returns_chunks_and_distribution_from_one_scoped_session():
    service, scope, events, found = service_fixture()
    result = asyncio.run(service.keyword_explore(scope, "등대 문", limit=1))
    assert result.chunks == (found,) and result.episode_hits == ((3, 2),)
    assert result.limited is True  # 받은 개수가 상한에 닿으면 더 있을 수 있다
    # 권한 확인(readiness)을 먼저 하고 같은 세션에서 조각과 분포를 읽는다.
    assert events == [
        "session-open",
        "repository",
        "readiness",
        ("keyword", "등대 문", 1),
        ("keyword_episodes", "등대 문"),
        "session-close",
    ]


@pytest.mark.parametrize("order", ["EARLIEST", "LATEST"])
def test_ordered_exploration_search_requires_the_first_three_words(order):
    service, scope, events, _ = service_fixture()
    query = "하나 둘 셋 넷 다섯 여섯 일곱 여덟 하나"
    result = asyncio.run(service.keyword_explore(scope, query, limit=20, order=order))
    assert result.limited is False
    assert ("keyword_ordered", ("하나", "둘", "셋"), ("넷", "다섯", "여섯", "일곱"), order, 20) in (
        events
    )
    assert ("keyword_episodes", query) in events


@pytest.mark.parametrize(
    "query,limit,order",
    [("", 20, "RELEVANCE"), ("등대", 0, "RELEVANCE"), ("등대", 101, "LATEST"), ("등대", 20, "ASC")],
)
def test_invalid_exploration_search_never_opens_a_session(query, limit, order):
    service, scope, events, _ = service_fixture()
    with pytest.raises(ValueError):
        asyncio.run(service.keyword_explore(scope, query, limit=limit, order=order))
    assert events == []


def test_whole_episode_read_and_span_check_authorization_first():
    service, scope, events, found = service_fixture()
    result = asyncio.run(service.episodes(scope, (4, 5), limit=2))
    assert result.chunks == (found, found) and result.limited is True
    assert events[2:4] == ["readiness", ("episodes", (4, 5), 2)]
    events.clear()
    assert asyncio.run(service.episode_span(scope)) == (3, 8, 4)
    assert events == ["session-open", "repository", "readiness", "episode_span", "session-close"]
    with pytest.raises(ValueError):
        asyncio.run(service.episodes(scope, [4], limit=101))
