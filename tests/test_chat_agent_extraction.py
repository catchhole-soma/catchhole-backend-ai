"""합성 단위 회귀: 사실 추출의 인용 검증·탈락과 검증된 사실만으로 쓰는 답.

모델·검색·DB 없이 지어낸 조각만 쓴다. 인용문이 원문과 글자 그대로 맞는지, 탈락한 사실이 답에 못
들어가는지, 넓힌 답변 모델이 기존 검사를 그대로 지나는지를 확인한다. 뽑힌 사실이 질문에 맞는지는
측정하지 않는다.
"""

import asyncio
import socket
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.chatbot.answer_recovery import recover_claims
from app.chatbot.client import ChatTransportError
from app.chatbot.hybrid_agent import CITED_CHUNK_SOFT_LIMIT, FACT_LIMIT, _Fact
from app.chatbot.hybrid_items import apply_event_scope
from app.schemas.chat_hybrid import (
    RETRIEVAL_SOURCE_LIMIT,
    AgentGroundedAnswer,
    AgentGroundedClaim,
    ComposedAnswer,
    EvidenceConflict,
    ExtractedFact,
    FactExtraction,
    GroundedAnswer,
    GroundedClaim,
    HybridPlan,
)
from tests.test_chat_agent_exploration import agent_session
from tests.test_chat_agent_retrieval import (
    COMPASS,
    KEY,
    composed,
    plan_output,
    story_chunk,
    story_chunks,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("이 회귀는 외부 서비스에 접근하지 않는다")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def read_session(episodes=(1, 2, 3), **kwargs):
    session = agent_session(**kwargs)
    session.agent_pool_add([c for c in story_chunks() if c.episode_no in episodes])
    return session, session.evidence_passages(session.evidences)


def fact(ref, quote, text="합성 사실"):
    return ExtractedFact(text=text, evidence_ref=ref, quote=quote)


def test_read_passages_are_merged_per_episode_in_episode_and_text_order():
    session = agent_session()
    # 읽은 순서가 뒤섞여도 답변·추출 입력은 회차·본문 순서이고, 이어지는 조각은 한 덩어리다.
    session.agent_pool_add([story_chunk(3, 1), story_chunk(1, 0), story_chunk(3, 0)])
    passages = session.evidence_passages(session.evidences)
    assert [p["sourceEpisodeNo"] for p in passages] == [1, 3]
    assert passages[1]["text"] == story_chunk(3, 0).text + story_chunk(3, 1).text
    assert [r["evidenceRef"] for r in passages[1]["references"]] == ["E3", "E1"]
    # 탐색이 아닌 실행은 기존 근거 상한(조각 12개·9,000토큰)으로 조합한다.
    session.agent_active = False
    assert len(session.evidence_passages(session.evidences)) == 2


def test_fact_quotes_must_be_exact_text_inside_the_cited_passage():
    session, passages = read_session()
    first, second, third = passages
    ref = first["references"][0]["evidenceRef"]
    located, reason = session.agent_locate_fact(fact(ref, COMPASS), passages)
    assert reason is None and located == (ref, COMPASS)
    # 앞뒤 공백은 떼고 찾되, 돌려주는 구절은 원문 그대로다.
    assert session.agent_locate_fact(fact(ref, f"  {COMPASS} "), passages)[0] == (
        ref,
        COMPASS,
    )
    for bad in (
        "낡은 나침반을 금화 세 닢에 샀다",  # 한 단어를 바꿔 씀
        "가온은 나침반을 샀다",  # 줄여 씀
        "낡은 나침반을  은화 세 닢에 샀다",  # 띄어쓰기가 다름
    ):
        located, reason = session.agent_locate_fact(fact(ref, bad), passages)
        assert located is None and reason == "GROUNDING_QUOTE_NOT_FOUND"
    # 덩어리 안에 두 번 나오는 구절은 위치를 정할 수 없어 버린다.
    located, reason = session.agent_locate_fact(fact(ref, "나침반"), passages)
    assert located is None and reason == "GROUNDING_AMBIGUOUS_QUOTE"
    assert session.agent_locate_fact(fact(ref, "   "), passages) == (None, "EMPTY")
    assert third["sourceEpisodeNo"] == 3 and second["sourceEpisodeNo"] == 2


def test_a_wrong_reference_is_corrected_only_inside_the_same_group_and_when_unique():
    session, passages = read_session()
    first, second, third = passages
    wrong, right = first["references"][0]["evidenceRef"], third["references"][0]["evidenceRef"]
    # 참조 번호만 틀리고 구절이 같은 묶음의 다른 덩어리 한 곳에만 있으면 그곳으로 바로잡는다.
    located, reason = session.agent_locate_fact(fact(wrong, KEY), passages)
    assert reason is None and located == (right, KEY)
    assert session.agent_locate_fact(fact("E99", KEY), passages)[0] == (right, KEY)
    # 다른 묶음의 원문으로는 옮기지 않는다.
    located, reason = session.agent_locate_fact(fact(wrong, KEY), [first, second])
    assert located is None and reason == "GROUNDING_QUOTE_NOT_FOUND"
    # 같은 묶음의 두 덩어리에 모두 있는 구절은 어느 쪽인지 알 수 없어 버린다.
    located, reason = session.agent_locate_fact(fact("E99", "가온"), passages)
    assert located is None and reason == "GROUNDING_UNKNOWN_REFERENCE"


def test_a_reference_from_another_group_is_rejected_even_when_the_quote_exists_there():
    session, passages = read_session()
    first, second, third = passages
    other = third["references"][0]["evidenceRef"]
    # 이 추출 호출에는 1·2화만 줬다. 모델이 보지 않은 3화 조각을 참조로 적으면, 그 문장이 실제로
    # 3화에 있어도 받지 않는다(읽지 않은 원문과 회차에 사실을 귀속하지 않는다).
    assert session.agent_locate_fact(fact(other, KEY), [third])[0] == (other, KEY)
    located, reason = session.agent_locate_fact(fact(other, KEY), [first, second])
    assert located is None and reason == "GROUNDING_UNKNOWN_REFERENCE"
    # 같은 문장이 준 묶음 안의 한 곳에도 있으면, 다른 묶음의 참조가 아니라 준 묶음의 그 위치로 바로잡는다.
    shared = "가온에게 나침반을 빌려 달라고 했지만"
    own = second["references"][0]["evidenceRef"]
    copied = {
        "text": "나루는 " + shared + " 소용없었다. ",
        "sourceEpisodeNo": 9,
        "references": [{"evidenceRef": "E90", "start": 0, "end": 30, "methods": ["KEYWORD"]}],
    }
    located, reason = session.agent_locate_fact(fact("E90", shared), [first, second])
    assert reason is None and located == (own, shared)
    assert session.agent_locate_fact(fact("E90", shared), [copied])[0] == ("E90", shared)


def test_extraction_only_keeps_facts_located_inside_the_group_they_were_read_from():
    session, passages = read_session()
    first, second, third = passages
    other = third["references"][0]["evidenceRef"]
    own = first["references"][0]["evidenceRef"]
    pending = [
        # 1·2화 묶음의 호출이 3화 조각 참조를 적은 사실과 제 묶음의 사실을 함께 돌려준다.
        (
            [fact(other, KEY, "3화 참조를 적은 사실"), fact(own, COMPASS, "1화: 나침반을 샀다.")],
            "ok",
        ),
        ([], "ok"),
    ]

    async def extract_group(plan, group):
        return pending.pop(0)

    session.agent_extract_group = extract_group
    session.agent_extract_groups = lambda passages, limit: [[first, second], [third]]
    facts = asyncio.run(session.agent_extract_facts(SimpleNamespace(), {"passages": passages}))
    assert [(f.episode, f.quote) for f in facts] == [(1, COMPASS)]


def test_extraction_groups_follow_reading_order_and_respect_the_group_limit():
    session, passages = read_session(episodes=(1, 2, 3, 4, 5, 6))
    sizes = [session.agent_count_tokens(p["text"]) for p in passages]
    session.hcfg = session.hcfg.model_copy(update={"agent_extract_group_tokens": max(sizes)})
    for limit in (1, 2, 4, 12):
        groups = session.agent_extract_groups(passages, limit)
        assert 1 <= len(groups) <= limit
        # 모든 덩어리가 한 번씩, 읽은 순서 그대로 들어간다.
        assert [p for group in groups for p in group] == passages
    assert len(session.agent_extract_groups(passages, 12)) > 1
    assert session.agent_extract_groups(passages, 1) == [passages]


def facts_for(passages):
    refs = [p["references"][0]["evidenceRef"] for p in passages]
    return [
        _Fact("F1", "1화: 가온이 나침반을 샀다.", refs[0], COMPASS, 1),
        _Fact("F2", "3화: 가온이 열쇠를 건졌다.", refs[2], KEY, 3),
    ]


def test_answer_is_built_only_from_verified_facts_and_their_exact_quotes():
    session, passages = read_session()
    plan = HybridPlan.model_validate(plan_output())
    facts = facts_for(passages)
    output = ComposedAnswer.model_validate(
        composed(
            [
                {
                    "text": "가온은 나침반을 은화 세 닢에 샀습니다.",
                    "factRefs": ["F1", "F1"],
                    "answeredFacets": ["P1"],
                    "rowRefs": ["R7"],
                },
                {
                    "text": "요구 표시 없이 쓴 맥락 문장입니다.",
                    "factRefs": ["F2"],
                    "answeredFacets": [],
                },
            ],
            missing=["P1", "P2"],
            explanations=[("P1", "이미 답한 요구의 설명"), ("P2", "열쇠 설명")],
        )
    )
    answer = session.agent_grounded_from(output, facts, plan)
    assert isinstance(answer, AgentGroundedAnswer)
    first, second = answer.claims
    # 중복 참조·모르는 등록 행은 빠지고, 인용문은 서버가 확인한 사실의 구절이다.
    assert [(s.evidence_ref, s.quote) for s in first.supports] == [(facts[0].evidence_ref, COMPASS)]
    assert first.evidence_refs == [facts[0].evidence_ref] and first.row_refs == []
    # 요구 표시 없는 문장은 관련 맥락으로만 두고 답한 것으로 세지 않는다.
    assert second.answered_facets == [] and second.related_facets == ["P1"]
    # 답함/못함 분할은 서버가 계산한다. 답한 요구를 '못 답함'에 함께 적은 출력은 답한 쪽으로 정리한다.
    assert answer.missing_facets == ["P2"]
    assert [(e.facet, e.text) for e in answer.missing_explanations] == [("P2", "열쇠 설명")]
    assert answer.coverage == "PARTIAL"


def test_any_unknown_fact_reference_rejects_the_whole_answer_for_a_rewrite():
    session, passages = read_session()
    session.chosen = [SimpleNamespace(ref="R1")]
    plan = HybridPlan.model_validate(plan_output())
    facts = facts_for(passages)
    valid = {"text": "가온은 나침반을 샀습니다.", "factRefs": ["F1"], "answeredFacets": ["P1"]}

    def rejected(output):
        with pytest.raises(ValueError, match="Unknown evidence or setting reference"):
            session.agent_grounded_from(ComposedAnswer.model_validate(output), facts, plan)

    # 입력에 없는 사실만 인용한 문장.
    rejected(composed([{"text": "근거 없는 문장", "factRefs": ["F8"], "answeredFacets": ["P1"]}]))
    # 아는 사실과 모르는 사실을 섞은 문장. 모르는 참조만 빼면 F999에만 기대던 주장이 F1의 인용을
    # 달고 남으므로, 문장을 살리지 않고 답 전체를 거절한다. 다른 문장이 멀쩡해도 같다.
    mixed = {
        "text": "가온은 나침반을 샀고 금화 열 닢을 더 냈습니다.",
        "factRefs": ["F1", "F999"],
        "answeredFacets": ["P1"],
    }
    rejected(composed([mixed]))
    rejected(composed([valid, mixed]))
    rejected(composed([{**mixed, "factRefs": ["F999", "F1"]}]))
    # 등록값과의 충돌 표시도 같은 기준이다.
    conflict = {"rowRefs": ["R1"], "factRefs": ["F2", "F999"], "eventMatch": None}
    rejected({**composed([valid]), "conflicts": [conflict]})
    accepted = session.agent_grounded_from(
        ComposedAnswer.model_validate(
            {**composed([valid]), "conflicts": [{**conflict, "factRefs": ["F2"]}]}
        ),
        facts,
        plan,
    )
    assert [c.evidence_refs for c in accepted.conflicts] == [[facts[1].evidence_ref]]


def test_composed_answer_schema_rejects_fact_free_or_oversized_claims():
    ComposedAnswer.model_validate(
        composed([{"text": "가" * 1200, "factRefs": ["F1"] * 12, "answeredFacets": ["P1"]}] * 12)
    )
    for claims in (
        [{"text": "문장", "factRefs": [], "answeredFacets": ["P1"]}],
        [{"text": "문장", "factRefs": ["F1"] * 13, "answeredFacets": ["P1"]}],
        [{"text": "가" * 1201, "factRefs": ["F1"], "answeredFacets": ["P1"]}],
        [{"text": "문장", "factRefs": ["F1"], "answeredFacets": ["P1"]}] * 13,
    ):
        with pytest.raises(ValidationError):
            ComposedAnswer.model_validate(composed(claims))
    FactExtraction.model_validate({"facts": [fact("E1", "구절").model_dump(by_alias=True)] * 60})
    with pytest.raises(ValidationError):
        FactExtraction.model_validate(
            {"facts": [fact("E1", "구절").model_dump(by_alias=True)] * 61}
        )
    with pytest.raises(ValidationError):
        ExtractedFact(text="사실", evidence_ref="E1", quote="가" * 401)


def test_wide_exploration_answers_keep_their_model_through_the_existing_checks():
    refs = [f"E{n}" for n in range(1, 13)]
    wide = AgentGroundedAnswer(
        coverage="COMPLETE",
        claims=[
            AgentGroundedClaim(
                text="가" * 1200,
                evidence_refs=refs,
                answered_facets=["P1"],
                event_status="OCCURRED",
                event_match="MATCH",
            )
        ]
        * 12,
        conflicts=[],
    )
    # 기존 모델은 같은 내용을 받지 않는다(탐색을 끈 실행의 상한은 그대로다).
    for limits in (
        dict(text="가" * 801, evidence_refs=["E1"]),
        dict(text="문장", evidence_refs=refs[:7]),
    ):
        with pytest.raises(ValidationError):
            GroundedClaim(**limits)
    with pytest.raises(ValidationError):
        GroundedAnswer(
            coverage="COMPLETE",
            claims=[GroundedClaim(text="문장", evidence_refs=["E1"])] * 9,
            conflicts=[],
        )
    with pytest.raises(ValidationError):
        AgentGroundedAnswer(coverage="COMPLETE", claims=list(wide.claims) * 2, conflicts=[])

    scoped = apply_event_scope(wide, "ACTUAL", ["P1"], event_context="합성 사건")
    assert type(scoped) is AgentGroundedAnswer and len(scoped.claims) == 12

    session = agent_session()
    session.has_collection_condition = True
    session.collection_conditions = [{"rowRefs": ["R1"], "verdict": "MATCH"}]
    guarded = session.apply_condition_guard(
        wide.model_copy(
            update={"claims": [c.model_copy(update={"row_refs": ["R1"]}) for c in wide.claims]}
        ),
        HybridPlan.model_validate(
            plan_output(requestedParts=[{"ref": "P1", "description": "요구"}])
        ),
    )
    assert type(guarded) is AgentGroundedAnswer and len(guarded.claims) == 12

    def ground(claim):
        if claim.text.startswith("나"):
            raise ValueError("GROUNDING_QUOTE_NOT_FOUND")
        return claim

    mixed = wide.model_copy(
        update={
            "claims": [
                *wide.claims[:11],
                wide.claims[0].model_copy(update={"text": "나쁜 인용", "answered_facets": ["P2"]}),
            ]
        }
    )
    recovered = recover_claims(mixed, ["P1", "P2"], ground, evidence_refs=set(refs), row_refs=set())
    assert type(recovered) is AgentGroundedAnswer
    assert len(recovered.claims) == 11 and recovered.missing_facets == ["P2"]
    # 기존 모델로 들어온 답은 기존 모델로 나온다.
    base = GroundedAnswer(
        coverage="COMPLETE",
        claims=[GroundedClaim(text="문장", evidence_refs=["E1"], answered_facets=["P1"])],
        conflicts=[],
    )
    assert type(apply_event_scope(base, None, ["P1"], event_context="합성 사건")) is GroundedAnswer


def test_cited_chunks_stay_inside_the_result_contract():
    session, _ = read_session()
    plan = HybridPlan.model_validate(plan_output())
    # 조각마다 사실 하나씩, 결과 상한보다 많은 조각을 인용하려는 답.
    facts = [_Fact(f"F{n}", "합성 사실", f"E{n}", f"구절 {n}", 1) for n in range(1, 81)]

    def claims(groups):
        return ComposedAnswer.model_validate(
            composed(
                [
                    {
                        "text": "합성 문장",
                        "factRefs": [f"F{n}" for n in refs],
                        "answeredFacets": ["P1"],
                    }
                    for refs in groups
                ]
            )
        )

    # 문장 다섯 개가 12개씩 60개 조각을 인용하려 해도 여유 한도(56)를 넘는 추가 인용은 얹지 않는다.
    answer = session.agent_grounded_from(
        claims([range(1 + 12 * i, 13 + 12 * i) for i in range(5)]), facts, plan
    )
    cited = {ref for claim in answer.claims for ref in claim.evidence_refs}
    assert len(cited) == CITED_CHUNK_SOFT_LIMIT == 56
    # 문장의 첫 인용은 결과 상한(60)까지 새 조각을 쓸 수 있다.
    answer = session.agent_grounded_from(
        claims([*(range(1 + 12 * i, 13 + 12 * i) for i in range(5)), [61], [62], [63], [64]]),
        facts,
        plan,
    )
    cited = {ref for claim in answer.claims for ref in claim.evidence_refs}
    assert len(cited) == RETRIEVAL_SOURCE_LIMIT == 60
    # 상한을 다 쓴 뒤 새 조각만 인용하는 문장은 받을 수 없어 답을 다시 쓰게 한다.
    with pytest.raises(ValueError, match="Unknown evidence or setting reference"):
        session.agent_grounded_from(
            claims(
                [
                    *(range(1 + 12 * i, 13 + 12 * i) for i in range(5)),
                    [61],
                    [62],
                    [63],
                    [64],
                    [65],
                ]
            ),
            facts,
            plan,
        )


def extraction_session(group_results, **kwargs):
    """추출 호출을 고정 결과로 바꾼 세션. 사실 검증·정렬·상한은 실제 코드다."""
    session, passages = read_session(**kwargs)
    session.execution.calls = 0
    pending = list(group_results)

    async def extract_group(plan, group):
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    session.agent_extract_group = extract_group
    session.hcfg = session.hcfg.model_copy(update={"agent_extract_group_tokens": 1000})
    return session, passages


def test_facts_are_checked_deduplicated_and_ordered_by_episode_then_position():
    session, passages = extraction_session(
        [
            (
                [
                    fact("E5", KEY, "3화: 열쇠를 건졌다."),
                    fact("E1", COMPASS, "1화: 나침반을 샀다."),
                    fact("E1", COMPASS, "같은 구절을 다시 뽑은 사실"),
                    fact("E1", "원문에 없는 구절", "버려질 사실"),
                ],
                "ok",
            )
        ]
    )
    facts = asyncio.run(session.agent_extract_facts(SimpleNamespace(), {"passages": passages}))
    assert [(f.ref, f.episode, f.quote) for f in facts] == [("F1", 1, COMPASS), ("F2", 3, KEY)]
    assert [f.text for f in facts] == ["1화: 나침반을 샀다.", "3화: 열쇠를 건졌다."]
    assert not {"SEARCH_LIMIT", "EVIDENCE_CONTEXT_LIMIT"} & set(session.codes)


@pytest.mark.parametrize(
    "status,code",
    [("INPUT_LIMIT", "EVIDENCE_CONTEXT_LIMIT"), ("ProviderFailure", "SEARCH_LIMIT")],
)
def test_unread_extraction_groups_leave_a_diagnostic_reason(status, code):
    session, passages = extraction_session([([], status)])
    assert asyncio.run(session.agent_extract_facts(SimpleNamespace(), {"passages": passages})) == []
    assert code in session.codes


def test_unexpected_errors_in_one_group_are_raised_after_all_groups_finish():
    session, passages = read_session()
    session.hcfg = session.hcfg.model_copy(update={"agent_extract_group_tokens": 1000})
    finished = []

    async def extract_group(plan, group):
        if not finished:
            finished.append("failed")
            raise ChatTransportError(429, "EXECUTION_LIMIT_EXCEEDED")
        await asyncio.sleep(0)
        finished.append("done")
        return [], "ok"

    session.agent_extract_group = extract_group
    session.agent_extract_groups = lambda passages, limit: [[p] for p in passages]
    with pytest.raises(ChatTransportError):
        asyncio.run(session.agent_extract_facts(SimpleNamespace(), {"passages": passages}))
    # 남은 묶음을 기다린 뒤 올리므로 떠 있는 호출이 없다.
    assert finished == ["failed", "done", "done"]


def test_fact_list_is_capped_in_episode_order_and_the_cut_is_recorded():
    session, _ = read_session()
    base = story_chunk(1, 0)
    sentences = [f"합성 문장 {n:03d}번이다. " for n in range(FACT_LIMIT + 5)]
    text = "".join(sentences)
    chunk = replace(
        base,
        chunk_id=UUID(int=990001),
        episode_id=UUID(int=990002),
        episode_no=9,
        text=text,
        end_offset=len(text),
        chunk_hash=sha256(text.encode()).hexdigest(),
        source_key="synthetic/story/9",
    )
    session.agent_pool_add([chunk])
    passages = session.evidence_passages(session.evidences)
    ref = passages[-1]["references"][0]["evidenceRef"]

    async def extract_group(plan, group):
        return [fact(ref, s.strip(), s.strip()) for s in sentences], "ok"

    session.agent_extract_group = extract_group
    session.agent_extract_groups = lambda passages, limit: [passages]
    facts = asyncio.run(session.agent_extract_facts(SimpleNamespace(), {"passages": passages}))
    assert len(facts) == FACT_LIMIT
    assert facts[0].quote == sentences[0].strip() and facts[-1].ref == f"F{FACT_LIMIT}"
    assert "EVIDENCE_CONTEXT_LIMIT" in session.codes


# ---- 검증된 부분 답의 병합(PR #137 리뷰) ----


def numbered_chunks(count):
    base = story_chunk(1, 0)
    return [
        replace(
            base,
            chunk_id=UUID(int=770000 + n),
            chunk_index=n,
            start_offset=n * 1000,
            end_offset=n * 1000 + len(base.text),
        )
        for n in range(count)
    ]


def partial_answer(claims, missing, conflicts=()):
    return AgentGroundedAnswer(
        coverage="PARTIAL",
        claims=[
            AgentGroundedClaim(text=text, evidence_refs=refs, answered_facets=answered)
            for text, refs, answered in claims
        ],
        conflicts=list(conflicts),
        missing_facets=missing,
    )


def merge_session(count=4):
    session = agent_session()
    session.agent_pool_add(numbered_chunks(count))
    return session, HybridPlan.model_validate(plan_output())


def test_partial_answers_covering_different_parts_are_combined_with_their_evidence():
    session, plan = merge_session()
    first = partial_answer([("첫 시도가 답한 문장", ["E2"], ["P1"])], ["P2"])
    best = session.agent_merge_partial(None, first, plan)
    assert best[0] is first and [e.ref for e in best[1]] == ["E1", "E2", "E3", "E4"]
    second = partial_answer([("둘째 시도가 답한 문장", ["E4", "E2"], ["P2"])], ["P1"])
    merged, evidence = session.agent_merge_partial(best, second, plan)
    # 앞 시도가 답한 요구를 뒤 초안으로 바꿔 잃지 않는다. 탐색 응답 모델 그대로 합친다.
    assert type(merged) is AgentGroundedAnswer and merged.coverage == "COMPLETE"
    assert [c.text for c in merged.claims] == ["첫 시도가 답한 문장", "둘째 시도가 답한 문장"]
    assert merged.missing_facets == []
    # 근거는 인용한 조각만 새 번호로 묶고, 같은 조각은 한 번만 넣는다.
    chunks = numbered_chunks(4)
    assert [(e.ref, e.chunk.chunk_id) for e in evidence] == [
        ("E1", chunks[1].chunk_id),
        ("E2", chunks[3].chunk_id),
    ]
    assert [c.evidence_refs for c in merged.claims] == [["E1"], ["E2", "E1"]]
    # 남은 요구는 합친 답 기준으로 다시 계산한다.
    third_part = HybridPlan.model_validate(
        plan_output(
            requestedParts=[
                {"ref": ref, "description": f"요구 {ref}"} for ref in ("P1", "P2", "P3")
            ]
        )
    )
    still_partial, _ = session.agent_merge_partial(
        (partial_answer([("첫 문장", ["E1"], ["P1"])], ["P2", "P3"]), tuple(session.evidences)),
        partial_answer([("둘째 문장", ["E2"], ["P2"])], ["P1", "P3"]),
        third_part,
    )
    assert still_partial.coverage == "PARTIAL" and still_partial.missing_facets == ["P3"]


def test_merged_answer_uses_the_result_contract_limits_not_the_fixed_round_ones():
    session, plan = merge_session(count=14)
    first = partial_answer([("앞 문장", [f"E{n}" for n in range(1, 8)], ["P1"])], ["P2"])
    second = partial_answer([("뒤 문장", [f"E{n}" for n in range(8, 15)], ["P2"])], ["P1"])
    previous = (first, tuple(session.evidences))
    # 고정 라운드의 기본 한도(근거 12개)로는 합치지 못하고 앞 답을 유지한다.
    assert session.merge_partial_snapshot(previous, second, plan) is previous
    # 탐색은 결과 계약의 인용 상한(60개)까지 합친다.
    merged, evidence = session.agent_merge_partial(previous, second, plan)
    assert merged.coverage == "COMPLETE" and len(evidence) == 14
    # 문장 수가 탐색 응답 모델의 상한(12개)을 넘으면 합치지 않고 앞 답을 유지한다.
    many_first = partial_answer([(f"앞 문장 {n}", ["E1"], ["P1"]) for n in range(7)], ["P2"])
    many_second = partial_answer([(f"뒤 문장 {n}", ["E2"], ["P2"]) for n in range(7)], ["P1"])
    kept = (many_first, tuple(session.evidences))
    session.codes.clear()
    assert session.agent_merge_partial(kept, many_second, plan) is kept
    assert "EVIDENCE_CONTEXT_LIMIT" in session.codes


def test_an_attempt_that_adds_no_newly_answered_part_never_loses_answered_parts():
    session, plan = merge_session()
    first = partial_answer([("앞 문장", ["E1"], ["P1"])], ["P2"])
    best = (first, tuple(session.evidences))
    # 같은 요구를 다시 답했고 문장이 줄지 않았으면 더 많은 원문을 본 뒤 초안을 쓴다.
    same = partial_answer([("뒤 문장", ["E2"], ["P1"]), ("보충 문장", ["E3"], ["P1"])], ["P2"])
    assert session.agent_merge_partial(best, same, plan)[0] is same
    # 답한 요구가 없는 관련 맥락뿐이거나 문장이 줄었으면 앞 답을 유지한다.
    fewer = AgentGroundedAnswer(
        coverage="PARTIAL",
        claims=[AgentGroundedClaim(text="맥락", evidence_refs=["E2"], related_facets=["P2"])],
        conflicts=[],
        missing_facets=["P1", "P2"],
    )
    assert session.agent_merge_partial(best, fewer, plan) is best
    two = (partial_answer([("가", ["E1"], ["P1"]), ("나", ["E2"], ["P1"])], ["P2"]), best[1])
    one = partial_answer([("다", ["E3"], ["P1"])], ["P2"])
    assert session.agent_merge_partial(two, one, plan) is two
    # 앞 답에 등록값과의 충돌 표시가 있으면 조용히 바꾸거나 합치지 않는다.
    conflicted = (
        partial_answer(
            [("앞 문장", ["E1"], ["P1"])],
            ["P2"],
            conflicts=[EvidenceConflict(row_refs=["R1"], evidence_refs=["E1"])],
        ),
        best[1],
    )
    assert session.agent_merge_partial(conflicted, same, plan) is conflicted
    other = partial_answer([("뒤 문장", ["E2"], ["P2"])], ["P1"])
    assert session.agent_merge_partial(conflicted, other, plan) is conflicted
