"""Synthetic logical-item/facet regressions. No authored manuscript or model calls."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import socket
from uuid import UUID

import pytest

from app.chatbot.hybrid_items import (
    apply_event_scope,
    group_items,
    merge_item_answers,
    render_item_answers,
    select_context_seeds,
    validate_item_answer,
)
from app.chatbot.qa_render import AnswerRow
from app.schemas.chat_hybrid import GroundedAnswer
from app.schemas.setting_tools import SettingToolItem
from tests.test_chat_focused_lookup import catalog_subject, item, uid
from tests.test_chat_hybrid import replay, scenario, source_chunk
from tests.test_chat_quality_hybrid import item_answer, setup_catalog, skill_case, skills


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Logical item tests cannot call external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def answer_row(ref, **changes):
    value = item(701, "세린", category="SKILL", key="skill.light", label="빛", value="등불을 밝힘")
    value.update(changes)
    return AnswerRow(ref, SettingToolItem.model_validate(value))


def passage(text, number, *, episode=23, offset=0, index=0, method="KEYWORD"):
    return replace(
        source_chunk("ring", method=method),
        chunk_id=UUID(int=6000 + number),
        episode_id=UUID(int=7000 + episode),
        episode_no=episode,
        chunk_index=index,
        text=text,
        start_offset=offset,
        end_offset=offset + len(text),
        chunk_hash=sha256(text.encode()).hexdigest(),
        source_key=f"synthetic/scene/{episode}",
    )


def single_case():
    case = skill_case()
    case["outputs"][0]["includeRegisteredValues"] = False
    return case


def facet_answer(facet, text, ref, *, count=1):
    return item_answer(count, text=text, ref=ref, facet=facet)


def test_logical_identity_preserves_subject_category_scope_and_path():
    rows = [
        answer_row("R1"),
        answer_row("R2", kind="HISTORY"),
        answer_row("R3", scopeName="상급"),
        answer_row("R4", subjectId=uid(702), subjectName="루온"),
        answer_row("R5", category="ITEM"),
        answer_row("R6", settingKey="skill.other"),
        answer_row("R7", settingKey=None),
        answer_row("R8", settingKey=None),
    ]
    items = group_items(rows)
    assert [[r.ref for r in i.rows] for i in items] == [
        ["R1", "R2"],
        ["R3"],
        ["R4"],
        ["R5"],
        ["R6"],
        ["R7"],
        ["R8"],
    ]


def test_current_targets_exclude_removed_historical_items_but_keep_their_own_history():
    items = group_items(
        [
            answer_row("R1"),
            answer_row("R2", kind="HISTORY"),
            answer_row("R3", kind="HISTORY", settingKey="skill.removed", operation="REMOVE"),
        ],
        current_targets=True,
    )
    assert len(items) == 1
    assert [row.ref for row in items[0].rows] == ["R1", "R2"]


def test_current_and_history_are_one_answer_item_through_real_orchestration(tmp_path):
    case = single_case()
    case["outputs"][0]["rdbMode"] = "CURRENT_AND_HISTORY"
    case["outputs"].append(item_answer())

    def setup(spring):
        current = skills()
        history = [
            dict(row, kind="HISTORY", id=uid(920 + n), sourceEpisodeNo=3, operation="ADD")
            for n, row in enumerate(current)
        ]
        subject = catalog_subject(701, "세린", current=current, history=history)
        spring.catalog = [subject]

    result = asyncio.run(replay(tmp_path, case, spring_setup=setup, hybrid={"max_rounds": 1}))
    assert result["finish"]["failure"] is None
    payload = result["raw"].requests[-1]["input"]
    assert [i["rowRefs"] for i in payload["items"]] == [["R1", "R4"], ["R2", "R5"], ["R3", "R6"]]
    assert all(
        result["finish"]["result"]["text"].count(name) == 1
        for name in ["잔광", "바람막이", "길잡이"]
    )


@pytest.mark.parametrize(
    "mutation", ["unknown_item", "missing_facet", "overlap", "false_complete", "free_claim"]
)
def test_item_contract_rejects_reference_only_or_false_facet_coverage(mutation):
    data = item_answer(1)
    if mutation == "unknown_item":
        data["itemAnswers"][0]["itemRef"] = "I999"
    if mutation == "missing_facet":
        data["itemAnswers"][0]["missingFacets"] = ["HOW"]
    if mutation == "overlap":
        data["coverage"] = "PARTIAL"
        data["itemAnswers"][0]["answers"] = [
            {"facet": "TIME", "text": "23화", "evidenceRefs": ["E1"]}
        ]
    if mutation == "false_complete":
        data["coverage"] = "COMPLETE"
    if mutation == "free_claim":
        data["coverage"] = "PARTIAL"
        data["claims"] = [{"text": "빛을 쓰고 싶어 한다.", "evidenceRefs": ["E1"]}]
    with pytest.raises(ValueError):
        validate_item_answer(
            GroundedAnswer.model_validate(data), group_items([answer_row("R1")]), ["TIME", "HOW"]
        )


def test_conflict_retracts_previous_answer_instead_of_counting_coverage():
    items = group_items([answer_row("R1")])
    previous = GroundedAnswer.model_validate(facet_answer("HOW", "스승에게 배웠다.", "E1"))
    latest = item_answer(1)
    latest.update(coverage="PARTIAL", conflicts=[{"rowRefs": ["R1"], "evidenceRefs": ["E2"]}])
    merged = merge_item_answers(
        previous, GroundedAnswer.model_validate(latest), items, ["TIME", "HOW"]
    )
    assert merged.coverage == "PARTIAL"
    assert not merged.item_answers[0].answers
    assert merged.item_answers[0].missing_facets == ["TIME", "HOW"]


@pytest.mark.parametrize("changed", [False, True])
def test_later_partial_adds_missing_facet_and_revalidates_both_rounds(tmp_path, changed):
    case = single_case()
    case["outputs"] += [
        facet_answer("HOW", "스승에게 잔광을 배웠습니다.", "E1"),
        facet_answer("TIME", "겨울 축제 다음 날 배운 것으로 24화에 서술됩니다.", "E2"),
    ]

    def setup(search):
        search.keyword_chunks = [passage("세린은 스승에게 잔광을 배웠다.", 1)]
        search.vector_chunks = [
            passage("잔광을 배운 날은 겨울 축제 다음 날이었다.", 2, episode=24, method="VECTOR")
        ]

    def after_answer(count, spring, search):
        if count == 3:
            search.changed = changed

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()[:1]),
            search_setup=setup,
            model_hook=after_answer,
        )
    )
    final = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert result["raw"].requests[-1]["input"]["previousItemAnswers"][0]["answers"][0][
        "evidenceRefs"
    ] == ["E1"]
    if changed:
        assert final["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert not final.get("retrievalSources")
    else:
        assert final["outcome"] == "ANSWER"
        assert "스승에게" in final["text"] and "축제 다음 날" in final["text"]
        assert final["text"].count("세린 · 잔광") == 1
        assert [r["sourceEpisodeNo"] for r in final["retrievalSources"]] == [23, 24]
        assert "일부만 답변" not in " ".join(final["limitations"])
    assert result["search"].calls[-1] == "revalidate"


def test_item_search_uses_actual_item_and_reads_neighbor_before_answering(tmp_path):
    case = single_case()
    case["outputs"].append(facet_answer("HOW", "스승의 가르침으로 잔광을 배웠습니다.", "E2"))

    def setup(search):
        seed = passage("이것이 잔광의 효과였다.", 1, offset=50, index=4)
        neighbor = passage(
            "세린은 스승의 가르침을 따라 이 빛을 처음 일으켰다.", 2, index=3, method="NEIGHBOR"
        )
        search.keyword_chunks = [seed]
        search.neighbor_chunks = [neighbor]
        search.queries = []
        keyword = search.keyword

        async def record(scope, query, **kwargs):
            search.queries.append(query)
            return await keyword(scope, query, **kwargs)

        search.keyword = record

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()[:1]),
            search_setup=setup,
            hybrid={"max_rounds": 1},
        )
    )
    final = result["finish"]["result"]
    assert result["finish"]["failure"] is None
    assert result["search"].queries == ["잔광"]
    assert result["search"].calls == ["keyword", "neighbors", "revalidate"]
    assert final["retrievalSources"][0]["startOffset"] == 0
    assert "스승의 가르침" in final["text"]
    assert "현재 등록된 설정" not in final["text"]
    assert not any(
        value in str(result["raw"].requests[-1]["input"])
        for value in ["memberId", "workId", uid(701), "skill.synthetic"]
    )


def test_general_source_partial_merges_independent_requested_facets(tmp_path):
    case = scenario()
    case["outputs"][0]["requestedFacets"] = ["TIME", "WHY"]
    case["outputs"][0]["includeRegisteredValues"] = False
    case["vector"] = ["raon"]
    case["outputs"] = [
        case["outputs"][0],
        {
            "coverage": "PARTIAL",
            "claims": [
                {"text": "떠나기 전날이었다.", "evidenceRefs": ["E1"], "answeredFacets": ["TIME"]}
            ],
            "conflicts": [],
            "missingFacets": ["WHY"],
        },
        {
            "coverage": "PARTIAL",
            "claims": [
                {"text": "추적을 피하려고 했다.", "evidenceRefs": ["E2"], "answeredFacets": ["WHY"]}
            ],
            "conflicts": [],
            "missingFacets": ["TIME"],
        },
    ]
    result = asyncio.run(replay(tmp_path, case))
    assert result["finish"]["failure"] is None
    final = result["finish"]["result"]
    assert "떠나기 전날" in final["text"] and "추적을 피하려고" in final["text"]
    assert len(final["retrievalSources"]) == 2
    assert "일부만 답변" not in " ".join(final["limitations"])


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("verdict", ["MATCH", "UNKNOWN"])
@pytest.mark.parametrize("per_item", [True, False])
def test_condition_then_narrative_filters_subjects_and_rechecks_collection(
    tmp_path, changed, verdict, per_item
):
    case = scenario()
    case["question"] = "다친 인물들은 왜 다쳤어?"
    plan = case["outputs"][0]
    plan.update(
        rdbMode="CURRENT",
        sourceNeed="REASON",
        subjects=[],
        searchTerms=["다친"],
        perItem=per_item,
        requestedFacets=["WHY"],
        includeRegisteredValues=False,
        collection={
            "domain": "CHARACTER",
            "factType": "STATUS",
            "worldCategory": None,
            "attribute": None,
            "predicate": "다친",
        },
    )
    case["outputs"] = [
        plan,
        {
            "decisions": [
                {
                    "itemRef": "I1",
                    "verdict": verdict,
                    "rowRefs": ["R1"],
                    "explanation": "부상이 남아 있다.",
                },
                {
                    "itemRef": "I2",
                    "verdict": "NO_MATCH",
                    "rowRefs": ["R2"],
                    "explanation": "이미 회복했다.",
                },
            ]
        },
        {
            "coverage": "COMPLETE",
            "claims": [],
            "conflicts": [],
            "itemAnswers": [
                {
                    "itemRef": "I1",
                    "answers": [
                        {
                            "facet": "WHY",
                            "text": "무너진 다리에서 떨어져 다쳤습니다.",
                            "evidenceRefs": ["E1"],
                        }
                    ],
                    "missingFacets": [],
                },
            ],
        },
    ]
    if not per_item:
        case["outputs"][-1] = {
            "coverage": "COMPLETE",
            "claims": [
                {
                    "text": "무너진 다리에서 떨어져 다쳤습니다.",
                    "evidenceRefs": ["E1"],
                    "rowRefs": ["R1"],
                    "answeredFacets": ["WHY"],
                }
            ],
            "conflicts": [],
        }

    def setup(spring):
        spring.catalog = [
            catalog_subject(
                701,
                "세린",
                current=[
                    item(
                        701,
                        "세린",
                        category="STATUS",
                        key="status.injury",
                        label="다리 부상",
                        value="부상이 남아 있다",
                    )
                ],
            ),
            catalog_subject(
                702,
                "루온",
                current=[
                    item(
                        702,
                        "루온",
                        category="STATUS",
                        key="status.recovery",
                        label="회복",
                        value="모두 나았다",
                    )
                ],
            ),
        ]

    def source(search):
        search.keyword_chunks = [passage("세린은 무너진 다리에서 떨어져 다쳤다.", 1)]

    def mutation(count, spring, search):
        if changed and count == 3:
            spring.catalog[0]["current"][0]["value"] = "회복했다"

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            search_setup=source,
            model_hook=mutation,
            hybrid={"max_rounds": 1},
        )
    )
    queries = [q for q in result["spring"].queries if q["tool"] == "CHARACTER_CURRENT_COLLECTION"]
    assert len(queries) == 2 and queries[-1]["readToken"]
    payload = result["raw"].requests[-1]["input"]
    assert [row["subjectName"] for row in payload["rows"]] == ["세린"]
    assert payload["collectionConditions"] == [{"rowRefs": ["R1"], "verdict": verdict}]
    if changed:
        assert (
            result["finish"]["result"] is None or result["finish"]["result"]["outcome"] != "ANSWER"
        )
        assert "revalidate" not in result["search"].calls
    elif verdict == "MATCH":
        assert result["finish"]["failure"] is None
        assert "떨어져" in result["finish"]["result"]["text"]
        assert result["search"].calls[-1] == "revalidate"
    else:
        assert result["finish"]["failure"] is None
        final = result["finish"]["result"]
        assert final["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert "조건에 해당하는" in final["text"]
        assert "원문 설명을 보류" in final["text"]
        assert "떨어져" not in final["text"]
        assert not final.get("retrievalSources")
        assert "revalidate" not in result["search"].calls


@pytest.mark.parametrize(
    "server_calls,condition_calls,retry", [(8, 4, False), (4, 2, False), (5, 2, True)]
)
def test_condition_batches_leave_one_call_for_grounded_answer(
    tmp_path, server_calls, condition_calls, retry
):
    case = scenario()
    case.update(question="다친 인물들은 왜 다쳤어?", maxCalls=server_calls)
    plan = case["outputs"][0]
    plan.update(
        rdbMode="CURRENT",
        sourceNeed="REASON",
        subjects=[],
        searchTerms=["다친"],
        perItem=True,
        requestedFacets=["WHY"],
        includeRegisteredValues=False,
        collection={
            "domain": "CHARACTER",
            "factType": "STATUS",
            "worldCategory": None,
            "attribute": None,
            "predicate": "다친",
        },
    )
    decisions = [
        {
            "decisions": [
                {
                    "itemRef": f"I{n}",
                    "verdict": "MATCH",
                    "rowRefs": [f"R{n}"],
                    "explanation": "부상이 남아 있다.",
                }
            ]
        }
        for n in range(1, condition_calls + 1)
    ]
    answer = {
        "coverage": "PARTIAL",
        "claims": [],
        "conflicts": [],
        "itemAnswers": [
            {
                "itemRef": f"I{n}",
                "answers": [
                    {"facet": "WHY", "text": "다리에서 떨어져 다쳤습니다.", "evidenceRefs": ["E1"]}
                ]
                if n == 1
                else [],
                "missingFacets": [] if n == 1 else ["WHY"],
            }
            for n in range(1, 7)
        ],
    }
    if retry:
        decisions.insert(len(decisions) - 1, {})
    case["outputs"] = [plan, *decisions, answer]

    def setup(spring):
        spring.catalog = [
            catalog_subject(
                700 + n,
                f"합성 인물{n}",
                current=[
                    item(
                        700 + n,
                        f"합성 인물{n}",
                        category="STATUS",
                        key="status.injury",
                        label="다리 부상",
                        value="부상이 남아 있다",
                    )
                ],
            )
            for n in range(1, 7)
        ]

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            config={"max_output_tokens": 128, "answer_max_output_tokens": 128},
        )
    )
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    requests = result["raw"].requests
    assert len(requests) == condition_calls + 2 + int(retry)
    assert len(requests) == len(result["spring"].reservations) == len(result["spring"].reports)
    assert "passages" in requests[-1]["input"]
    verdicts = requests[-1]["input"]["collectionConditions"]
    assert [v["verdict"] for v in verdicts] == ["MATCH"] * condition_calls + ["UNKNOWN"] * (
        6 - condition_calls
    )
    assert not result["embedding"].inputs


@pytest.mark.parametrize("limit", ["text", "execution"])
def test_general_search_stops_before_embedding_when_no_answer_call_can_follow(tmp_path, limit):
    case = scenario()
    case["outputs"][1]["coverage"] = "PARTIAL"
    if limit == "execution":
        case["maxCalls"] = 3
    result = asyncio.run(
        replay(tmp_path, case, hybrid={"max_model_calls": 2} if limit == "text" else None)
    )
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert result["search"].calls == ["keyword", "neighbors", "revalidate"]
    assert len(result["raw"].requests) == 2
    assert not result["embedding"].inputs


def test_shared_strong_anchor_covers_two_items_without_promoting_weaker_keyword_matches():
    first = passage("첫 항목의 계획", 1, episode=40)
    shared = passage("둘째 셋째 항목의 목록", 2, episode=21)
    repeated = passage("셋째 항목의 사용", 3, episode=20)
    weak = replace(first, score=0.1)
    chosen = select_context_seeds(
        [("I1", [first]), ("I2", [shared]), ("I3", [repeated, shared, weak])], maximum=8
    )
    assert chosen == [shared, first]


def test_joint_context_keeps_shared_anchor_and_acquisition_neighbor_within_default_budget(tmp_path):
    case = single_case()
    case["outputs"].append(item_answer())
    prefix = "합성 배경을 묘사한다."
    first = passage(prefix + " x" * 760, 1, episode=40, index=4, offset=5000)
    first_before = passage(prefix + " y" * 760, 2, episode=40, index=3, method="NEIGHBOR")
    first_after = passage(
        prefix + " z" * 760, 3, episode=40, index=5, offset=10000, method="NEIGHBOR"
    )
    shared = passage(
        "바람막이와 길잡이 능력의 효과표다." + " x" * 760, 4, episode=21, index=4, offset=5000
    )
    acquisition = passage(
        "세린은 수정 두 개를 받았고, 손에 쥐자 새 능력이 깨어났다." + " y" * 740,
        5,
        episode=21,
        index=3,
        method="NEIGHBOR",
    )
    repeated = passage("길잡이를 사용했다." + " z" * 760, 6, episode=20)

    def setup(search):
        async def keyword(scope, query, limit, **kwargs):
            from app.retrieval.search_contracts import SearchResult

            search.record("keyword", scope)
            found = {"잔광": [first], "바람막이": [shared], "길잡이": [repeated, shared]}[query]
            return SearchResult(tuple(found), search.readiness)

        async def neighbors(scope, seeds, radius, limit):
            from app.retrieval.search_contracts import SearchResult

            search.record("neighbors", scope)
            search.seeds = seeds
            return SearchResult((acquisition, first_before, first_after), search.readiness)

        search.keyword, search.neighbors = keyword, neighbors

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()),
            search_setup=setup,
            hybrid={"max_rounds": 1},
        )
    )
    assert result["finish"]["failure"] is None
    assert result["search"].seeds == [shared, first]
    payload = result["raw"].requests[-1]["input"]
    contents = " ".join(p["text"] for p in payload["passages"])
    assert acquisition.text in contents and shared.text in contents and first.text in contents
    assert result["search"].calls == ["keyword", "keyword", "keyword", "neighbors"]
    assert not result["embedding"].inputs


def classified_answer(status, *, facet="HOW", text="스승에게 배우기로 했습니다."):
    raw = facet_answer(facet, text, "E1")
    raw["itemAnswers"][0]["answers"][0]["eventStatus"] = status
    return GroundedAnswer.model_validate(raw)


@pytest.mark.parametrize("status", ["PLANNED", "UNCERTAIN", None])
def test_actual_event_never_counts_plan_uncertainty_or_missing_status_as_coverage(status):
    parsed = classified_answer(status)
    guarded = apply_event_scope(parsed, "ACTUAL", ["TIME", "HOW"])
    assert guarded.item_answers[0].missing_facets == ["TIME", "HOW"]
    assert guarded.coverage == ("NONE" if status is None else "PARTIAL")
    if status is None:
        assert not guarded.item_answers[0].answers
    validate_item_answer(guarded, group_items([answer_row("R1")]), ["TIME", "HOW"])


def test_event_scope_is_backward_compatible_and_planned_question_can_cover_plan():
    old = classified_answer(None)
    assert apply_event_scope(old, None, ["TIME", "HOW"]) is old
    plan = apply_event_scope(classified_answer("PLANNED"), "PLANNED", ["TIME", "HOW"])
    assert plan.item_answers[0].missing_facets == ["TIME"]


@pytest.mark.parametrize("later_status", ["PLANNED", "UNCERTAIN", "OCCURRED"])
def test_later_planned_observation_cannot_replace_actual_event(later_status):
    items = group_items([answer_row("R1")])
    old = apply_event_scope(
        classified_answer("OCCURRED", text="스승에게 배웠습니다."), "ACTUAL", ["TIME", "HOW"]
    )
    new = apply_event_scope(
        classified_answer(later_status, text="새 자료의 설명입니다."), "ACTUAL", ["TIME", "HOW"]
    )
    merged = merge_item_answers(old, new, items, ["TIME", "HOW"], event_scope="ACTUAL")
    assert merged.item_answers[0].answers[0].event_status == "OCCURRED"
    assert merged.item_answers[0].missing_facets == ["TIME"]
    assert merged.item_answers[0].answers[0].text == (
        "새 자료의 설명입니다." if later_status == "OCCURRED" else "스승에게 배웠습니다."
    )


@pytest.mark.parametrize("changed", [False, True])
def test_false_complete_plan_is_downgraded_and_plan_evidence_is_revalidated(tmp_path, changed):
    case = single_case()
    case["outputs"][0]["eventScope"] = "ACTUAL"
    raw = {
        "coverage": "COMPLETE",
        "claims": [],
        "conflicts": [],
        "itemAnswers": [
            {
                "itemRef": "I1",
                "answers": [
                    {
                        "facet": "TIME",
                        "text": "겨울에 배우기로 했습니다.",
                        "evidenceRefs": ["E1"],
                        "eventStatus": "PLANNED",
                    },
                    {
                        "facet": "HOW",
                        "text": "스승의 수련을 받기로 했습니다.",
                        "evidenceRefs": ["E1"],
                        "eventStatus": "PLANNED",
                    },
                ],
                "missingFacets": [],
            }
        ],
    }
    case["outputs"].append(raw)

    def setup(search):
        search.keyword_chunks = [passage("겨울이 오면 스승에게 수련을 받기로 했다.", 1)]
        search.changed = changed

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()[:1]),
            search_setup=setup,
            hybrid={"max_rounds": 1},
        )
    )
    assert result["finish"]["failure"] is None
    final = result["finish"]["result"]
    if changed:
        assert final["outcome"] == "INSUFFICIENT_EVIDENCE" and not final.get("retrievalSources")
    else:
        assert final["outcome"] == "ANSWER"
        assert final["text"].count("계획만 확인") == 1
        assert "실제로 이루어진" in final["text"] and "근거 1" in final["text"]
        assert "경위·방법 정보는" in final["text"]
        assert final["text"].count("(근거 1)") == 1
        assert not final["limitations"]  # The precise unanswered facets are in the answer text.
    assert result["search"].calls[-1] == "revalidate"


def test_general_actual_event_plan_claim_is_observation_not_complete_answer(tmp_path):
    case = scenario()
    case["outputs"][0].update(requestedFacets=["TIME", "HOW"], eventScope="ACTUAL")
    case["outputs"][1] = {
        "coverage": "COMPLETE",
        "claims": [
            {
                "text": "겨울에 스승의 수련을 받기로 했습니다.",
                "evidenceRefs": ["E1"],
                "answeredFacets": ["TIME", "HOW"],
                "eventStatus": "PLANNED",
            }
        ],
        "conflicts": [],
    }
    result = asyncio.run(replay(tmp_path, case, hybrid={"max_rounds": 1}))
    assert result["finish"]["failure"] is None
    text = result["finish"]["result"]["text"]
    assert "계획으로 확인된 내용" in text and "실제로 이루어진" in text
    assert "확정하기 어렵습니다" in text


@pytest.mark.parametrize("same_sources", [True, False])
def test_item_citations_combine_only_when_each_facet_has_the_same_sources(same_sources):
    data = item_answer(1)
    data.update(coverage="COMPLETE")
    data["itemAnswers"][0].update(
        answers=[
            {"facet": "TIME", "text": "겨울 축제 다음 날입니다.", "evidenceRefs": ["E1", "E2"]},
            {
                "facet": "HOW",
                "text": "스승에게 배웠습니다.",
                "evidenceRefs": ["E2", "E1"] if same_sources else ["E3"],
            },
        ],
        missingFacets=[],
    )
    text = "\n".join(
        render_item_answers(
            GroundedAnswer.model_validate(data),
            group_items([answer_row("R1")]),
            {"E1": 1, "E2": 2, "E3": 3},
        )
    )
    if same_sources:
        assert "겨울 축제 다음 날입니다. 스승에게 배웠습니다. (근거 1, 2)" in text
        assert text.count("(근거") == 1
    else:
        assert "겨울 축제 다음 날입니다. (근거 1, 2)" in text
        assert "스승에게 배웠습니다. (근거 3)" in text
