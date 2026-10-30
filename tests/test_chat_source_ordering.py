"""합성 회귀: 최초/최후 장면 질문의 회차 순서 원문 검색(plan.ordering).

제공자·임베딩·DB·원고 없이 계획 계약·anchor 선택·검색 호출·답변 입력만 검사한다.
실제 모델이 ordering을 올바르게 고르는지는 측정하지 않는다.
"""

import asyncio
import socket
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.chatbot.qa_render import notices
from app.schemas.chat_hybrid import HybridPlan
from tests.test_chat_evidence_budget import answer_context
from tests.test_chat_hybrid import answer, hybrid_settings, replay, scenario, source_chunk


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


def test_only_the_first_ordered_chunks_precede_the_lexical_hits(tmp_path):
    case = scenario("lexical_scene")
    case["outputs"][0].update({"ordering": "EARLIEST", "searchTerms": ["반지"]})
    case["outputs"][1] = answer(ref="E2")

    def setup(search):
        # 정렬 검색이 1화·3화·3화 순으로 셋을 돌려주지만 상한 1개만 keyword 후보 앞에 둔다.
        search.ordered_chunks = [
            source_chunk("weather"),
            source_chunk("ring"),
            source_chunk("raon"),
        ]

    result = asyncio.run(replay(tmp_path, case, search_setup=setup, hybrid={"ordered_top_k": 1}))
    request = result["raw"].requests[-1]["input"]
    passages = [p["sourceEpisodeNo"] for p in request["passages"]]
    # 1화 정렬 후보가 먼저, 그 뒤는 일반 keyword 후보(3화 ring)이며 상한 밖 raon은 끼어들지 않는다.
    assert passages[:2] == [1, 3]
    assert source_chunk("raon").text not in [p["text"] for p in request["passages"]]
    assert request["searchOrdering"]["truncated"] is False


def test_ordered_plans_widen_round_two_vector_search_and_select_the_earliest(tmp_path):
    case = scenario("lexical_scene")
    case["outputs"][0].update({"ordering": "EARLIEST", "searchTerms": ["반지"]})
    case["vector"] = ["ring", "weather"]
    # 1라운드는 답하지 못하고 2라운드 의미 검색으로 넘어간다.
    case["outputs"][1] = answer(coverage="NONE")
    case["outputs"].append(answer(ref="E1", claim="그날 아침에는 비가 내렸다."))
    limits = []

    def setup(search):
        original = search.vector

        async def vector(scope, query, embedding_client, limit, excluded_chunk_ids=()):
            limits.append(limit)
            return await original(scope, query, embedding_client, limit, excluded_chunk_ids)

        search.vector = vector

    # 근거 자리를 하나로 줄이면 선택 우선순위가 그대로 드러난다.
    result = asyncio.run(
        replay(tmp_path, case, search_setup=setup, hybrid={"max_evidence": 1, "per_episode": 1})
    )
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    # 임베딩 1회는 그대로이고 후보 상한만 3배로 넓힌다.
    assert limits == [3 * hybrid_settings().vector_top_k] and len(result["embedding"].inputs) == 1
    request = result["raw"].requests[-1]["input"]
    # 유사도 순서는 ring(3화)이 먼저지만 가장 이른 weather(1화)를 먼저 고른다.
    assert [p["sourceEpisodeNo"] for p in request["passages"]] == [1]
    assert [source["sourceEpisodeNo"] for source in finished["retrievalSources"]] == [1]


def test_first_round_keeps_ordered_hits_ahead_of_full_score_keyword_hits(tmp_path):
    case = scenario("lexical_scene")
    case["outputs"][0].update({"ordering": "LATEST", "searchTerms": ["반지"]})

    def setup(search):
        # 이른 회차의 keyword 후보가 점수 1.0으로 정렬 후보와 동률이면 회차 오름차순에 밀린다.
        search.keyword_chunks = [replace(source_chunk("weather"), score=1.0)]
        search.ordered_chunks = [source_chunk("ring")]

    result = asyncio.run(
        replay(tmp_path, case, search_setup=setup, hybrid={"max_evidence": 1, "per_episode": 1})
    )
    request = result["raw"].requests[-1]["input"]
    assert [p["sourceEpisodeNo"] for p in request["passages"]] == [3]
    assert result["finish"]["result"]["outcome"] == "ANSWER"


def test_ordering_anchors_keep_every_planned_subject_name():
    session, plan, _ = answer_context()
    base = plan.subjects[0]
    subjects = [base.model_copy(update={"name": name}) for name in ("세린", "아리아", "라온")]
    three = plan.model_copy(update={"subjects": subjects, "search_terms": ["만남"]})
    required, optional = session.ordering_anchors(three)
    # 계획이 허용하는 세 인물을 모두 필수 표현으로 남겨 두 명만 있는 장면이 경계가 되지 않게 한다.
    assert required == ["세린", "아리아", "라온"]
    assert optional == ["만남"]


def test_per_item_source_questions_also_use_ordered_search(tmp_path):
    from tests.test_chat_quality_hybrid import (
        item_answer,
        set_source_text,
        setup_catalog,
        skill_case,
        skills,
    )

    case = skill_case()
    case["outputs"][0]["ordering"] = "EARLIEST"
    case["outputs"].append(item_answer(2, text="세린은 스승에게 합성기술001을 배웠습니다."))
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills(2)),
            search_setup=lambda search: set_source_text(
                search, "세린은 스승에게 합성기술001을 배웠다."
            ),
        )
    )
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    # 항목별 경로도 keyword 뒤 정렬 검색을 호출하고, 인물명은 필수·항목 표현은 선택 anchor다.
    assert "keyword_ordered" in result["search"].calls
    required, optional, order = result["search"].ordered_calls[0]
    assert required == ("세린",) and order == "EARLIEST"
    assert optional and all("세린" not in word for word in optional)
