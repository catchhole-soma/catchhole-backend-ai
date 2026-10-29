"""Synthetic regressions for independent source needs; never contains evaluation Gold."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.chatbot.hybrid_prompts import ANSWER_PROMPT, SOURCE_ANSWER_PROMPT
from app.chatbot.qa import response_schema
from app.chatbot.qa_validation import ChatResponseContractError
from app.chatbot.source_search import missing_scope, scene_priority, validate_part_episodes
from app.retrieval.search_contracts import SearchScope
from app.schemas.chat_hybrid import GroundedAnswer
from tests.test_chat_answer_recovery import offline_only  # noqa: F401
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import (
    BEHAVIOR,
    EMOTION,
    answer,
    claim,
    parts_case,
    search_with,
)
from tests.test_chat_requested_parts_merge import answer as previous_answer
from tests.test_chat_requested_parts_merge import plan, session


def test_source_only_per_item_recovers_independent_valid_claim(tmp_path):
    case = parts_case()
    case["outputs"][0]["perItem"] = True  # No registered item rows exist.
    case["outputs"] += [
        answer(
            claim("잘못된 주장", "존재하지 않는 인용", parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, parts=("P2",)),
        )
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 2},
            search_setup=search_with(keyword=[passage(BEHAVIOR, 1)]),
        )
    )
    assert result["finish"]["failure"] is None
    assert BEHAVIOR in result["finish"]["result"]["text"]
    assert "잘못된 주장" not in result["finish"]["result"]["text"]
    assert result["search"].calls[-1] == "revalidate"


def test_covered_term_rank_survives_evidence_composition(tmp_path):
    case = parts_case()
    case["outputs"] += [answer(claim(EMOTION, EMOTION, ref="E2"))]
    repeated = replace(passage("이미 답한 부분이 반복됩니다.", 1), score=0.9)
    separate = replace(passage(EMOTION, 2, episode=2), score=0.2)
    noise = [replace(passage(f"반복 {i}", i + 10, episode=i + 10), score=0.8) for i in range(12)]
    result = asyncio.run(
        replay(tmp_path, case, search_setup=search_with(keyword=[repeated, separate, *noise]))
    )
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert EMOTION in result["finish"]["result"]["text"]
    request = result["raw"].requests[-1]
    assert request["system"] == SOURCE_ANSWER_PROMPT
    schema = request["response_schema"].schema
    definitions = [
        d["properties"] for d in schema["$defs"].values() if "eventMatch" in d.get("properties", {})
    ]
    assert definitions and all(p["eventMatch"]["type"] == "string" for p in definitions)
    # The generic contract stays nullable for requests without event identity.
    general = response_schema(GroundedAnswer).schema
    assert all(
        "anyOf" in d["properties"]["eventMatch"]
        for d in general["$defs"].values()
        if "eventMatch" in d.get("properties", {})
    )


def test_expansion_does_not_repeat_answered_keyword_bag():
    state = session()
    state.execution.context.question = "이미 답한 화염 장면과 이후 행동을 알려줘"
    draft = previous_answer("심리 확인", ["E1"], ["P1"], "E1", "확인한 인용")
    query = state.expansion_query(plan(), draft, "화염 장면 반복 검색어")
    assert "실제 이후 행동" in query and "동료의 반응" in query
    assert plan().event_context in query
    assert "반복 검색어" not in query and state.execution.context.question not in query


def test_source_only_contract_leaves_more_budget_for_evidence():
    # This checks the cause of input pruning without changing the model/token caps.
    assert len(SOURCE_ANSWER_PROMPT.encode()) < len(ANSWER_PROMPT.encode()) * 0.55


def test_missing_part_can_only_narrow_the_authorized_range():
    request = plan()
    request.requested_parts[0] = request.requested_parts[0].model_copy(
        update={"source_episode_no": 12}
    )
    request.requested_parts[1] = request.requested_parts[1].model_copy(
        update={"source_episode_no": 25}
    )
    scope = SearchScope(1, UUID(int=1), 12, 25)
    validate_part_episodes(request, "12화의 원인과 25화의 해결 방법을 알려줘")
    missing = SimpleNamespace(missing_facets=["P2"])
    assert missing_scope(request, missing, scope) == replace(scope, episode_from=25)
    assert missing_scope(request, SimpleNamespace(missing_facets=["P1", "P2"]), scope) == scope
    assert missing_scope(request, SimpleNamespace(missing_facets=["P3"]), scope) == scope
    request.requested_parts[1] = request.requested_parts[1].model_copy(
        update={"source_episode_no": 26}
    )
    assert missing_scope(request, missing, scope) == scope  # Never widen the trusted scope.
    with pytest.raises(ChatResponseContractError, match="UNOBSERVED_PART_EPISODE"):
        validate_part_episodes(request, "12화의 원인과 25화의 해결 방법을 알려줘")


def test_scene_priority_keeps_two_hits_and_the_first_scenes_continuation():
    before = passage("전투를 시작했다.", 1, episode=3, index=0)
    anchor = passage("주문을 썼다.", 2, episode=3, index=1)
    after = passage("전투가 끝났다.", 3, episode=3, index=2)
    other = passage("별도 요청의 근거다.", 4, episode=7)
    noise = passage("다른 장면이다.", 5, episode=9)
    wrong_version = replace(after, chunk_id=UUID(int=6), source_hash="different")
    ordered = scene_priority([anchor, other, noise], [before, wrong_version, after])
    assert [c.chunk_id for c in ordered] == [
        anchor.chunk_id,
        other.chunk_id,
        after.chunk_id,
        before.chunk_id,
        noise.chunk_id,
    ]
    assert scene_priority([noise, before], [], retained=[anchor])[0] == before


def test_targeted_missing_chapter_uses_remaining_shared_evidence_budget(tmp_path):
    case = parts_case()
    case["question"] = "12화 봉화 이후 감정과 25화 이후 행동을 설명해줘"
    case["outputs"][0]["episodeFrom"] = 12
    case["outputs"][0]["episodeTo"] = 25
    for part, episode in zip(case["outputs"][0]["requestedParts"], [12, 25], strict=True):
        part["sourceEpisodeNo"] = episode
    case["outputs"] += [
        answer(claim(EMOTION, EMOTION, parts=("P1",)), missing=("P2",)),
        answer(
            claim(EMOTION, EMOTION, parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, ref="E8", parts=("P2",)),
        ),
    ]
    initial = passage(EMOTION, 1, episode=12)
    extra = [
        passage(f"새 회차 대목 {i}", 10 + i, episode=25, offset=i * 100, index=i) for i in range(6)
    ]
    extra.append(passage(BEHAVIOR, 16, episode=25, offset=600, index=6))
    scopes = []

    def setup(search):
        search_with(keyword=[initial], vector=extra)(search)
        original = search.vector

        async def vector(scope, *args, **kwargs):
            scopes.append(scope)
            return await original(scope, *args, **kwargs)

        search.vector = vector

    result = asyncio.run(replay(tmp_path, case, search_setup=setup))
    assert scopes[0].episode_from == scopes[0].episode_to == 25
    assert BEHAVIOR in result["finish"]["result"]["text"]
    passages = result["raw"].requests[-1]["input"]["passages"]
    assert sum(len(p["references"]) for p in passages) == 8


def test_scene_window_includes_adjacent_search_hits_before_a_distant_summary():
    from app.retrieval.evidence import compose_evidence

    start = passage("첫 부품은 37원에 샀다.", 21, episode=4, index=3, offset=300)
    middle = passage("다음 가게로 이동했다.", 22, episode=4, index=4, offset=400)
    end = passage("둘째 부품은 58원에 샀다.", 23, episode=4, index=5, offset=500)
    summary = passage("부품 값은 백 원쯤이라고 말했다.", 24, episode=4, index=9, offset=900)
    seeds = [start, summary, middle]
    ordered = scene_priority(seeds, [end])
    bundle = compose_evidence(
        [*seeds, end], max_items=3, per_episode=3, priority_chunk_ids=[c.chunk_id for c in ordered]
    )
    assert {c.chunk_id for c in bundle.chunks} == {start.chunk_id, middle.chunk_id, end.chunk_id}


def test_scene_window_does_not_jump_an_unread_chunk():
    start = passage("시작", 25, episode=4, index=3)
    distant = passage("두 칸 뒤", 26, episode=4, index=5)
    other = passage("다른 요구", 27, episode=5)
    assert distant not in scene_priority([start, other], [distant])


@pytest.mark.parametrize("room", [False, True])
def test_final_search_repairs_one_bad_quote_before_losing_new_information(tmp_path, room):
    case = parts_case()
    bad = answer(
        claim(EMOTION, EMOTION, parts=("P1",)), claim(BEHAVIOR, "인용에 없는 문장", parts=("P2",))
    )
    good = answer(claim(EMOTION, EMOTION, parts=("P1",)), claim(BEHAVIOR, BEHAVIOR, parts=("P2",)))
    case["outputs"] += [bad, good] if room else [bad]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 3 if room else 2},
            search_setup=search_with(keyword=[], vector=[passage(f"{EMOTION} {BEHAVIOR}", 1)]),
        )
    )
    text = result["finish"]["result"]["text"]
    assert EMOTION in text and "인용에 없는 문장" not in text
    assert (BEHAVIOR in text) is room
    assert len(result["raw"].requests) == (3 if room else 2)
    assert len(result["embedding"].inputs) == 1
    assert result["search"].calls[-1] == "revalidate"


@pytest.mark.parametrize("retry_empty", [False, True])
def test_final_quote_repair_failure_keeps_the_verified_subset(tmp_path, retry_empty):
    case = parts_case()
    first = answer(
        claim(EMOTION, EMOTION, parts=("P1",)), claim(BEHAVIOR, "원문에 없는 인용", parts=("P2",))
    )
    second = (
        answer(coverage="NONE", missing=("P1", "P2"))
        if retry_empty
        else answer(claim("거절할 주장", "또 없는 인용"))
    )
    case["outputs"] += [first, second]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 3},
            search_setup=search_with(keyword=[], vector=[passage(EMOTION, 1)]),
        )
    )
    text = result["finish"]["result"]["text"]
    assert EMOTION in text and "거절할 주장" not in text
    assert len(result["raw"].requests) == 3
    assert result["search"].calls[-1] == "revalidate"
