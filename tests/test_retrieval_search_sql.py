"""키워드 검색 SQL의 비교 연산자와 가중치 계산 형태를 DB 없이 확인한다.

실제 순위·결과는 tests/test_retrieval_search_postgres.py가 PostgreSQL에서 검사한다.
"""

import re
from types import SimpleNamespace
from uuid import uuid4

from app.retrieval.contracts import IndexSpec
from app.retrieval.search_contracts import SearchScope
from app.retrieval.search_repository import RetrievalSearchRepository


class Session:
    def __init__(self):
        self.sql = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        return SimpleNamespace(mappings=lambda: [])


def repository(*, weighted=True):
    session = Session()
    target = RetrievalSearchRepository(session, SearchScope(1, uuid4()), IndexSpec())
    target.rarity_weighted_keyword = weighted
    return session, target


def comparisons(sql):
    return {param: op for op, param in re.findall(r"c\.chunk_text (I?LIKE) :(\w+) ESCAPE", sql)}


def test_keyword_uses_like_for_caseless_terms_and_ilike_for_cased_terms():
    # ILIKE는 비교마다 본문 전체를 소문자로 바꾼다. 한글·숫자 단어는 LIKE로 같은 결과를 얻는다.
    for weighted in (True, False):
        session, target = repository(weighted=weighted)
        target.keyword("비요른 HP 100% 회복", limit=5)
        assert comparisons(session.sql[-1]) == {
            "pattern_0": "LIKE",
            "pattern_1": "ILIKE",
            "pattern_2": "LIKE",
            "pattern_3": "LIKE",
        }


def test_keyword_ordered_uses_the_same_case_rule():
    session, target = repository()
    target.keyword_ordered(["비요른"], ["Lv", "회복"], order="EARLIEST", limit=5)
    assert comparisons(session.sql[-1]) == {
        "required_0": "LIKE",
        "optional_0": "ILIKE",
        "optional_1": "LIKE",
    }


def test_rarity_weights_are_materialized_once():
    # 한 행짜리 가중치를 펼치면 후보 행마다 전체 청크 집계를 다시 실행할 수 있다.
    session, target = repository()
    target.keyword("비요른 처음 죽인", limit=5)
    assert "weights AS MATERIALIZED (" in session.sql[-1]
