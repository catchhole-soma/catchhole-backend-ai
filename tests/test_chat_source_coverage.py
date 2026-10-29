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


def test_ranked_third_scene_keeps_its_continuation_inside_shared_budget():
    from app.chatbot.source_search import independent_scene_seeds
    from app.retrieval.evidence import compose_evidence

    anchors = [
        passage(f"별도 장면 {i}", 60 + i, episode=10 + i, index=3, offset=300) for i in range(3)
    ]
    neighbors = [
        passage(f"그 장면의 뒤 문장 {i}", 70 + i, episode=10 + i, index=4, offset=400)
        for i in range(3)
    ]
    repeated = passage("첫 장면의 다른 검색 일치", 80, episode=10, index=8, offset=800)
    noise = [passage(f"낮은 순위 후보 {i}", 90 + i, episode=20 + i) for i in range(8)]
    seeds = [anchors[0], repeated, *anchors[1:], *noise]
    # The repository's bounded neighbor result must reach distinct sources even
    # if the first source contributes several lexical matches.
    assert independent_scene_seeds(seeds)[:3] == anchors
    ordered = scene_priority(seeds, neighbors)
    bundle = compose_evidence(
        [*seeds, *neighbors],
        max_items=8,
        per_episode=3,
        priority_chunk_ids=[c.chunk_id for c in ordered],
    )
    assert {c.chunk_id for c in [*anchors, *neighbors]} <= {c.chunk_id for c in bundle.chunks}
    assert len(bundle.chunks) <= 8


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


def test_mixed_planned_route_and_actual_resource_reason_have_independent_scopes():
    from app.chatbot.hybrid_items import apply_event_scope, part_event_scopes
    from app.schemas.chat_hybrid import RequestedPart

    request = plan().model_copy(update={"event_scope": "PLANNED"})
    request.requested_parts[0] = RequestedPart(
        ref="P1", description="예정 경로", eventScope="PLANNED"
    )
    request.requested_parts[1] = RequestedPart(
        ref="P2", description="부족한 자원의 현재 수량", eventScope=None
    )
    parsed = GroundedAnswer.model_validate(
        answer(
            {
                **claim("동쪽으로 갈 계획입니다.", "동쪽으로 갈 계획", parts=("P1",)),
                "eventStatus": "PLANNED",
            },
            {
                **claim("식량이 세 상자 남았습니다.", "식량이 세 상자", parts=("P2",)),
                "eventStatus": "OCCURRED",
            },
        )
    )
    guarded = apply_event_scope(
        parsed, "PLANNED", ["P1", "P2"], part_scopes=part_event_scopes(request)
    )
    assert guarded.coverage == "COMPLETE" and not guarded.missing_facets
    assert [c.event_status for c in guarded.claims] == ["PLANNED", "OCCURRED"]
    # An actual-execution requirement still rejects the same planned claim.
    guarded = apply_event_scope(
        parsed, None, ["P1", "P2"], part_scopes={"P1": "ACTUAL", "P2": None}
    )
    assert guarded.coverage == "PARTIAL" and guarded.missing_facets == ["P1"]
    # Legacy omission retains the old whole-plan guard; explicit null is distinct.
    assert "eventScope" not in RequestedPart(ref="P1", description="경로").model_dump(by_alias=True)
    assert request.requested_parts[1].model_dump(by_alias=True)["eventScope"] is None


def test_source_contract_forbids_nonexistent_rows_without_changing_registered_contract():
    state = session()
    state.chosen = []
    state.logical_items = []
    request = plan()
    _, _, source = state.answer_contract(request)
    for field in ("conflicts", "unansweredRowRefs", "itemAnswers"):
        assert source.schema["properties"][field]["maxItems"] == 0
    assert "ItemAnswer" not in source.schema["$defs"]
    assert source.schema["$defs"]["GroundedClaim"]["properties"]["rowRefs"]["maxItems"] == 0
    _, _, registered = state.answer_contract(request.model_copy(update={"rdb_mode": "CURRENT"}))
    assert registered.schema["properties"]["itemAnswers"]["maxItems"] > 0
    assert registered.schema["$defs"]["GroundedClaim"]["properties"]["rowRefs"]["maxItems"] > 0


def test_explicit_episode_hint_preserves_other_candidates_and_authorized_order():
    from app.chatbot.source_search import explicit_episode_seeds

    request = plan()
    request.requested_parts[0] = request.requested_parts[0].model_copy(
        update={"source_episode_no": 6}
    )
    other, target, last = (
        passage("다른 대목", 61, episode=20),
        passage("지정 대목", 62, episode=6),
        passage("후속 대목", 63, episode=7),
    )
    assert explicit_episode_seeds(request, [other, target, last]) == [target, other, last]


@pytest.mark.parametrize("total", [5, 8])
def test_verified_scene_can_expand_past_initial_episode_share_within_shared_budget(total):
    from unittest.mock import Mock

    from app.chatbot.hybrid import _Evidence
    from tests.test_chat_evidence_budget import answer_context

    state, _, _ = answer_context()
    state.plan = plan()
    state.hcfg = state.hcfg.model_copy(update={"max_evidence": total})
    state.metrics = Mock()
    first = [
        passage(f"확인한 사실 {i}", 101 + i, episode=11, index=i, offset=i * 100) for i in range(3)
    ]
    other = passage("별도 요구의 확인 근거", 110, episode=12)
    state.supported_evidence = tuple(
        _Evidence(f"E{i}", c.text, c.episode_no, chunk=c) for i, c in enumerate([*first, other], 1)
    )
    state.evidences = list(state.supported_evidence)
    extra = [
        passage(f"뒤에 이어진 새 조건 {i}", 120 + i, episode=11, index=i + 3, offset=(i + 3) * 100)
        for i in range(4)
    ]
    state.add_search_evidence(extra, fresh=extra)
    selected = {e.chunk.chunk_id for e in state.evidences}
    assert {c.chunk_id for c in [*first, other, extra[0]]} <= selected
    assert len(selected) <= total
    assert state.evidence_token_count(state.evidences) <= state.hcfg.evidence_tokens


def test_mixed_chapter_requirements_reject_a_contradictory_global_filter():
    request = plan().model_copy(update={"episode_from": 12, "episode_to": 12})
    request.requested_parts[0] = request.requested_parts[0].model_copy(
        update={"source_episode_no": 12}
    )
    with pytest.raises(ChatResponseContractError, match="MIXED_PART_SOURCE_RANGE"):
        validate_part_episodes(request, "12화의 계획과 실제 실행한 장면을 구분해 줘")
    unbounded = request.model_copy(update={"episode_from": None, "episode_to": None})
    validate_part_episodes(unbounded, "12화의 계획과 실제 실행한 장면을 구분해 줘")
    request = request.model_copy(
        update={
            "requested_parts": [
                p.model_copy(update={"source_episode_no": 12}) for p in request.requested_parts
            ]
        }
    )
    validate_part_episodes(request, "12화 안에서 계획과 실행을 구분해 줘")


def test_mixed_source_range_repairs_before_any_scoped_search(tmp_path):
    import copy

    case = parts_case()
    case["question"] = "12화의 봉화를 켠 이유와 이후 행동을 구분해 줘"
    wrong = case["outputs"][0]
    wrong["episodeFrom"] = wrong["episodeTo"] = 12
    wrong["requestedParts"][0]["sourceEpisodeNo"] = 12
    corrected = copy.deepcopy(wrong)
    corrected["episodeFrom"] = corrected["episodeTo"] = None
    case["outputs"] += [
        corrected,
        answer(
            claim(EMOTION, EMOTION, parts=("P1",)),
            claim(BEHAVIOR, BEHAVIOR, ref="E2", parts=("P2",)),
        ),
    ]
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            search_setup=search_with(
                keyword=[passage(EMOTION, 1, episode=12), passage(BEHAVIOR, 2, episode=25)]
            ),
        )
    )
    assert result["finish"]["failure"] is None
    assert len(result["raw"].requests) == 3
    assert "MIXED_PART_SOURCE_RANGE" in result["raw"].requests[1]["system"]
    assert result["raw"].requests[-1]["input"]["plan"]["episodeFrom"] is None
    assert BEHAVIOR in result["finish"]["result"]["text"]
