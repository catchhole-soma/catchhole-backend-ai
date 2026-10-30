"""합성 회귀: 최초/최후 장면 질문의 회차 순서 원문 검색(plan.ordering).

제공자·임베딩·DB·원고 없이 계획 계약·anchor 선택·검색 호출·답변 입력만 검사한다.
실제 모델이 ordering을 올바르게 고르는지는 측정하지 않는다.
"""

import asyncio
import socket

import pytest
from pydantic import ValidationError

from app.chatbot.qa_render import notices
from app.schemas.chat_hybrid import HybridPlan
from tests.test_chat_evidence_budget import answer_context
from tests.test_chat_hybrid import answer, replay, scenario, source_chunk


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("이 회귀는 외부 서비스에 접근하지 않는다")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def plan_payload(**overrides):
    payload = dict(scenario("lexical_scene")["outputs"][0])
    payload.update(overrides)
    return payload


def test_ordering_defaults_to_none_and_conversation_replies_cannot_order_search():
    assert HybridPlan.model_validate(plan_payload()).ordering == "NONE"
    assert HybridPlan.model_validate(plan_payload(ordering="LATEST")).ordering == "LATEST"
    greeting = {
        "disposition": "GREETING",
        "registeredCurrentRequired": False,
        "rdbMode": "NONE",
        "sourceNeed": "NONE",
        "subjects": [],
        "searchTerms": [],
        "includeEvidence": False,
        "episodeFrom": None,
        "episodeTo": None,
    }
    HybridPlan.model_validate(greeting)
    with pytest.raises(ValidationError):
        HybridPlan.model_validate({**greeting, "ordering": "EARLIEST"})


def test_ordering_anchors_require_subject_names_and_half_of_the_asked_words():
    session, plan, _ = answer_context()
    unfocused = plan.subjects[0]
    assert unfocused.focus.attribute is None
    # 집중 항목이 있으면 인물명(필수) + 그 항목 단어(선택)다.
    focused = unfocused.model_copy(
        update={"focus": unfocused.focus.model_copy(update={"attribute": "검은 창"})}
    )
    assert session.ordering_anchors(plan.model_copy(update={"subjects": [focused]})) == (
        [unfocused.name],
        ["검은", "창"],
    )
    # 집중 항목이 없으면 인물명을 뺀 검색어 단어가 선택 표현이다(최대 4개).
    bare = plan.model_copy(
        update={
            "subjects": [unfocused],
            "search_terms": [unfocused.name, "스킬", "첫 전투", unfocused.name.lower(), "고블린"],
        }
    )
    assert session.ordering_anchors(bare) == ([unfocused.name], ["스킬", "첫", "전투", "고블린"])
    # 검색어가 인물명뿐이면 인물명만 필수다(첫 등장 탐색).
    names_only = bare.model_copy(update={"search_terms": [unfocused.name]})
    assert session.ordering_anchors(names_only) == ([unfocused.name], [])
    # 인물이 없으면 검색어 단어만 선택 표현으로 쓴다.
    no_subject = bare.model_copy(update={"subjects": [], "search_terms": ["반지", "강물"]})
    assert session.ordering_anchors(no_subject) == ([], ["반지", "강물"])
    # 정렬 검색은 원문 검색 몫에 도구 1개를 더 남긴다.
    ordered = plan.model_copy(update={"ordering": "EARLIEST"})
    assert session.source_tool_reserve(ordered) == session.source_tool_reserve(plan) + 1


def test_earliest_ordering_reads_first_mention_episodes_before_lexical_hits(tmp_path):
    case = scenario("lexical_scene")
    case["outputs"][0].update({"ordering": "EARLIEST", "searchTerms": ["아리아", "반지"]})
    # 정렬 검색이 1화 장면을 먼저 돌려주므로 기존 3화 장면은 E2가 된다.
    case["outputs"][1] = answer(ref="E2")

    def setup(search):
        search.ordered_chunks = [source_chunk("weather"), source_chunk("ring")]

    result = asyncio.run(replay(tmp_path, case, search_setup=setup))
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    assert result["search"].calls == ["keyword", "keyword_ordered", "neighbors", "revalidate"]
    assert result["search"].ordered_calls == [((), ("아리아", "반지"), "EARLIEST")]
    request = result["raw"].requests[-1]["input"]
    assert [p["sourceEpisodeNo"] for p in request["passages"]] == [1, 3]
    assert request["searchOrdering"] == {
        "order": "EARLIEST",
        "anchors": ["아리아", "반지"],
        "boundaryEpisode": 1,
        "truncated": False,
    }
    assert notices(["ORDERED_SEARCH_EARLIEST"])[0] in request["limitations"]
    assert [source["sourceEpisodeNo"] for source in finished["retrievalSources"]] == [3]
    assert notices(["ORDERED_SEARCH_EARLIEST"])[0] in finished["limitations"]


def test_latest_ordering_reverses_the_returned_order(tmp_path):
    case = scenario("lexical_scene")
    case["outputs"][0].update({"ordering": "LATEST", "searchTerms": ["반지"]})

    def setup(search):
        search.ordered_chunks = [source_chunk("weather"), source_chunk("ring")]

    result = asyncio.run(replay(tmp_path, case, search_setup=setup))
    assert result["search"].ordered_calls == [((), ("반지",), "LATEST")]
    request = result["raw"].requests[-1]["input"]
    assert [p["sourceEpisodeNo"] for p in request["passages"]] == [3, 1]
    assert request["searchOrdering"]["boundaryEpisode"] == 3
    assert result["finish"]["result"]["outcome"] == "ANSWER"


def test_ordering_without_tool_budget_keeps_the_plain_keyword_round(tmp_path):
    case = scenario("lexical_scene")
    case["outputs"][0].update({"ordering": "EARLIEST", "searchTerms": ["반지"]})
    # 검색 1 + 정렬 1 + 이웃 1 + 영수증 1 + 재검증 1 = 5가 필요한데 4만 허용한다.
    result = asyncio.run(replay(tmp_path, case, config={"max_tool_calls": 4}))
    assert "keyword_ordered" not in result["search"].calls
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert "searchOrdering" not in result["raw"].requests[-1]["input"]
