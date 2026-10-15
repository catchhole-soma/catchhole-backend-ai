"""Synthetic work-wide regression; scripted judgments are not model accuracy scores."""

import json
import socket
from types import SimpleNamespace
import httpx

import pytest

from app.chatbot.qa_collection import WorkLookup
from app.chatbot.qa_render import AnswerRow
from app.chatbot.replay import replay_case
from app.schemas.setting_tools import SettingToolItem, SettingToolQuery
from pydantic import ValidationError
from tests.test_chat_focused_lookup import (
    answer,
    case,
    catalog_subject,
    item,
    plan,
    tool_queries,
    uid,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Work query tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def work_plan(
    *,
    shape="LIST",
    domain="CHARACTER",
    fact_type=None,
    category=None,
    attribute=None,
    predicate=None,
):
    result = plan("unused")
    result.update(
        subjects=[],
        answerShape=shape,
        collection={
            "domain": domain,
            "factType": fact_type,
            "worldCategory": category,
            "attribute": attribute,
            "predicate": predicate,
        },
    )
    return result


@pytest.mark.anyio
async def test_registered_character_count_uses_server_total_not_page_or_fact_count():
    catalog = [catalog_subject(n, f"인물{n}") for n in range(1, 48)]
    result = await replay_case(case("등록된 인물 몇 명?", work_plan(shape="COUNT")), catalog)
    assert "47명" in answer(result)["text"]
    assert len(result["queries"]) == 1
    assert result["queries"][0]["size"] == 1
    assert len(result["reservations"]) == 1


@pytest.mark.anyio
async def test_world_category_listing_never_searches_work_as_a_subject():
    catalog = []
    for n, category in enumerate(["RACE", "RACE", "LOCATION"], 1):
        subject = catalog_subject(n, f"설정{n}", domain="WORLD")
        subject["subject"]["category"] = category
        catalog.append(subject)
    result = await replay_case(
        case("이 작품의 종족 종류는?", work_plan(domain="WORLD", category="RACE")), catalog
    )
    text = answer(result)["text"]
    assert "2개" in text and "설정1" in text and "설정2" in text and "설정3" not in text
    assert all(q["query"] is None for q in result["queries"])


@pytest.mark.anyio
async def test_semantic_condition_keeps_structured_states_and_unknown_population():
    catalog = [
        catalog_subject(
            1,
            "린",
            current=[
                item(
                    1,
                    "린",
                    category="STATUS",
                    key="status.사망",
                    label="사망",
                    structured={"value": "전투에서 사망", "active": True},
                )
            ],
        ),
        catalog_subject(
            2,
            "테오",
            current=[
                item(
                    2,
                    "테오",
                    category="STATUS",
                    key="status.회복",
                    label="회복",
                    structured={"value": "의식을 회복함", "active": True},
                )
            ],
        ),
        catalog_subject(3, "미아"),
    ]
    judgment = {
        "decisions": [
            {
                "itemRef": "I1",
                "verdict": "MATCH",
                "rowRefs": ["R1"],
                "explanation": "전투에서 사망한 상태예요.",
            },
            {
                "itemRef": "I2",
                "verdict": "NO_MATCH",
                "rowRefs": ["R2"],
                "explanation": "현재는 회복한 상태예요.",
            },
        ]
    }
    result = await replay_case(
        case("현재 사망한 인물", work_plan(fact_type="STATUS", predicate="사망"), judgment), catalog
    )
    text = answer(result)["text"]
    assert "린" in text and "확인한 인물은 1명" in text
    assert "관련 설정이 없는 1명" in text
    queries = tool_queries(result, "CHARACTER_CURRENT_COLLECTION")
    assert len(queries) == 2 and queries[-1]["readToken"]
    assert not tool_queries(result, "FIND_CHARACTERS")
    prompt = result["modelRequests"][1]["input"]
    assert json.loads(prompt["items"][0]["rows"][0]["structuredValue"])["active"] is True


@pytest.mark.anyio
async def test_collection_reads_beyond_two_pages_and_checks_token_without_llm_per_page():
    # One subject, many relevant state rows: one interpretation after all 47 rows.
    rows = [
        item(1, "새 인물", category="STATUS", key=f"status.조건{n}", label=f"조건{n}")
        for n in range(47)
    ]
    decision = {
        "decisions": [
            {
                "itemRef": "I1",
                "verdict": "UNKNOWN",
                "rowRefs": [],
                "explanation": "상태를 확정할 자료가 부족해요.",
            }
        ]
    }
    result = await replay_case(
        case("조건에 맞는 인물", work_plan(fact_type="STATUS", predicate="조건"), decision),
        [catalog_subject(1, "새 인물", current=rows)],
    )
    assert answer(result)["outcome"] == "ANSWER"
    queries = tool_queries(result, "CHARACTER_CURRENT_COLLECTION")
    assert [q["page"] for q in queries] == [0, 1, 2, 0]
    assert len({q["readToken"] for q in queries[1:]}) == 1
    assert len(result["reservations"]) == 2


@pytest.mark.anyio
async def test_condition_rejects_cross_subject_evidence_even_if_reference_exists():
    catalog = [
        catalog_subject(n, f"대상{n}", current=[item(n, f"대상{n}", category="STATUS")])
        for n in (1, 2)
    ]
    bad = {
        "decisions": [
            {"itemRef": "I1", "verdict": "MATCH", "rowRefs": ["R2"], "explanation": "조건 해당"},
            {"itemRef": "I2", "verdict": "NO_MATCH", "rowRefs": ["R2"], "explanation": "조건 다름"},
        ]
    }
    result = await replay_case(
        case("조건 인물", work_plan(fact_type="STATUS", predicate="조건"), bad, bad), catalog
    )
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"


@pytest.mark.anyio
async def test_overview_is_grounded_prose_instead_of_all_rows():
    decision = plan("도윤", fact_type=None)
    decision["answerShape"] = "SUMMARY"
    catalog = [
        catalog_subject(
            1,
            "도윤",
            current=[
                item(1, "도윤", category="PROFILE", key="profile.job", label="직업", value="치료사")
            ],
        )
    ]
    result = await replay_case(
        case(
            "도윤 소개해줘",
            decision,
            {"claims": [{"text": "도윤은 치료사예요.", "rowRefs": ["R1"]}]},
        ),
        catalog,
    )
    assert answer(result)["text"] == "도윤은 치료사예요."


@pytest.mark.anyio
async def test_skill_count_excludes_wishes_and_counts_validated_items_in_code():
    decision = plan("도윤", fact_type="SKILL")
    decision["answerShape"] = "COUNT"
    rows = [
        item(1, "도윤", category="SKILL", key="skill.회복", label="회복", value="사용 가능"),
        item(1, "도윤", category="SKILL", key="skill.검술", label="검술", value="배우고 싶어함"),
    ]
    verdicts = {
        "decisions": [
            {
                "itemRef": "I1",
                "verdict": "MATCH",
                "rowRefs": ["R1"],
                "explanation": "사용 가능한 회복 능력이에요.",
            },
            {
                "itemRef": "I2",
                "verdict": "NO_MATCH",
                "rowRefs": ["R2"],
                "explanation": "습득을 희망할 뿐이에요.",
            },
        ]
    }
    result = await replay_case(
        case("도윤이 가진 스킬 몇 개?", decision, verdicts),
        [catalog_subject(1, "도윤", current=rows)],
    )
    assert "확인한 항목은 1개" in answer(result)["text"]


@pytest.mark.parametrize(
    "args",
    [
        {"tool": "CHARACTER_CURRENT_COLLECTION", "page": 1},
        {"tool": "CHARACTER_CURRENT_COLLECTION", "episodeFrom": 3},
        {"tool": "CHARACTER_CURRENT_COLLECTION", "memberId": 9},
        {"tool": "FIND_CHARACTERS", "readToken": "a" * 64},
        {"tool": "CHARACTER_CURRENT_COLLECTION", "worldCategory": "RACE"},
    ],
)
def test_new_collection_contract_cannot_bypass_scope_or_consistency(args):
    with pytest.raises(ValidationError):
        SettingToolQuery.model_validate(args)


@pytest.mark.anyio
async def test_partial_subject_with_unread_later_status_is_not_a_match():
    rows = [item(1, "린", category="STATUS", key=f"status.{n}") for n in range(21)]
    rows[-1]["value"] = "회복됨"
    result = await replay_case(
        case("사망한 인물", work_plan(fact_type="STATUS", predicate="사망")),
        [catalog_subject(1, "린", current=rows)],
        config={"max_tool_calls": 2},
    )
    text = answer(result)["text"]
    assert "조건 충족 여부가 불확실한 1명" in text and "아직 확인하지 못했어요" in text
    assert "인물은 0명" not in text
    assert len(result["modelRequests"]) == 1  # Incomplete subject never reaches the judge.


@pytest.mark.anyio
async def test_distinct_without_scalar_is_not_zero_types_or_false_truncation():
    result = await replay_case(
        case("종족 종류", work_plan(shape="DISTINCT", fact_type="PROFILE")),
        [
            catalog_subject(
                1, "린", current=[item(1, "린", category="PROFILE", value="요정", structured=None)]
            )
        ],
    )
    data = answer(result)
    assert data["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "0종류" not in data["text"]
    assert not any("잘려" in note for note in data["limitations"])
    assert len(result["queries"]) == 2  # Final consistency check even without a predicate.


@pytest.mark.anyio
async def test_requested_evidence_without_sources_is_explicit():
    decision = work_plan(fact_type="STATUS", predicate="사망")
    decision["includeEvidence"] = True
    verdict = {
        "decisions": [
            {
                "itemRef": "I1",
                "verdict": "MATCH",
                "rowRefs": ["R1"],
                "explanation": "사망이 명시되어 있어요.",
            }
        ]
    }
    result = await replay_case(
        case("사망한 인물과 근거", decision, verdict),
        [catalog_subject(1, "린", current=[item(1, "린", category="STATUS")])],
    )
    assert any("연결된 원문 근거가 없습니다" in note for note in answer(result)["limitations"])


@pytest.mark.anyio
async def test_changed_collection_after_judgment_does_not_publish_old_match():
    def setup(spring):
        original = spring.respond

        def response(request):
            body = json.loads(request.content) if request.content else {}
            if body.get("readToken") and body.get("page") == 0:
                return httpx.Response(409, json={"success": False})
            return original(request)

        spring.respond = response

    verdict = {
        "decisions": [
            {
                "itemRef": "I1",
                "verdict": "MATCH",
                "rowRefs": ["R1"],
                "explanation": "사망이 명시되어 있어요.",
            }
        ]
    }
    result = await replay_case(
        case("사망한 인물", work_plan(fact_type="STATUS", predicate="사망"), verdict),
        [catalog_subject(1, "린", current=[item(1, "린", category="STATUS")])],
        spring_setup=setup,
    )
    data = answer(result)
    assert data["outcome"] == "INSUFFICIENT_EVIDENCE" and "변경" in data["text"]
    assert "린" not in data["text"]


@pytest.mark.anyio
async def test_list_requires_every_logical_item_including_unanswered():
    decision = plan("린", fact_type="SKILL")
    decision["answerShape"] = "LIST"
    rows = [item(1, "린", category="SKILL", key=f"skill.{n}") for n in range(3)]
    bad = {
        "decisions": [
            {"itemRef": "I1", "verdict": "MATCH", "rowRefs": ["R1"], "explanation": "사용 가능"}
        ]
    }
    result = await replay_case(
        case("린 스킬 전부", decision, bad, bad), [catalog_subject(1, "린", current=rows)]
    )
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"


@pytest.mark.anyio
async def test_registered_character_list_uses_people_unit():
    result = await replay_case(
        case("등록된 인물 목록", work_plan()),
        [catalog_subject(1, "린"), catalog_subject(2, "테오")],
    )
    assert answer(result)["text"] == "현재 등록된 인물은 2명이에요.\n- 린\n- 테오"
    assert len(tool_queries(result, "CHARACTER_CURRENT_COLLECTION")) == 2


@pytest.mark.parametrize(
    "difference", [{"category": "FACTION"}, {"scopeName": "상급"}, {"settingKey": "체력"}]
)
def test_distinct_never_combines_different_category_scope_or_key(difference):
    lookup = WorkLookup()
    lookup.cfg = SimpleNamespace(max_answer_characters=12000)
    lookup.codes = []
    first = item(1, "요정", category="RACE", key="마력", label="마력", value="10")
    second = item(2, "드워프", category="RACE", key="마력", label="마력", value="20")
    second.update(difference)
    rows = [
        AnswerRow(f"R{n}", SettingToolItem.model_validate(value), "WORLD")
        for n, value in enumerate([first, second], 1)
    ]
    result = lookup.render_distinct(rows, "등록된 설정").result
    assert result.text.count("1종류") == 2
    assert "2종류" not in result.text
    assert "- 10" in result.text and "- 20" in result.text
    assert result.sources == []


@pytest.mark.anyio
async def test_single_path_distinct_keeps_structured_scalar_contract_and_existing_text():
    rows = [
        item(
            1,
            "린",
            category="PROFILE",
            key="profile.race",
            label="종족",
            structured={"value": "요정"},
        ),
        item(
            2,
            "테오",
            category="PROFILE",
            key="profile.race",
            label="종족",
            structured={"name": "인간"},
        ),
        item(3, "미아", category="PROFILE", key="profile.race", label="종족", structured="요정"),
    ]
    result = await replay_case(
        case("등록된 종족 종류", work_plan(shape="DISTINCT", fact_type="PROFILE")),
        [catalog_subject(n, row["subjectName"], current=[row]) for n, row in enumerate(rows, 1)],
    )
    assert (
        answer(result)["text"]
        == "현재 등록된 인물의 해당 속성에서 확인한 값은 2종류예요.\n- 요정\n- 인간"
    )
    assert len(tool_queries(result, "CHARACTER_CURRENT_COLLECTION")) == 2


def sourced_row(subject, name, number, value):
    row = item(
        subject,
        name,
        category="PROFILE",
        key=f"profile.kind{number}",
        label="종류",
        structured=value,
    )
    ref = {"tool": "CHARACTER_EVIDENCE", "targetId": uid(number)}
    row["sources"] = [ref]
    evidence = {
        **row,
        "kind": "EVIDENCE",
        "id": uid(number),
        "sources": [],
        "evidence": {
            "status": "AVAILABLE",
            "candidateId": None,
            "episodeId": uid(900),
            "sourceEpisodeNo": 1,
            "sourceChunkId": None,
            "sourceHash": "a" * 64,
            "sourceVersion": "v1",
            "matchesCurrentSource": True,
            "quotes": [{"text": "합성 근거", "startOffset": 0, "endOffset": 5}],
            "truncated": False,
        },
    }
    return row, evidence


@pytest.mark.anyio
@pytest.mark.parametrize("with_condition", [False, True])
async def test_distinct_evidence_uses_only_rendered_matching_rows_and_keeps_final_check(
    with_condition,
):
    matching, proof = sourced_row(1, "린", 101, "요정")
    unrelated, unrelated_proof = sourced_row(1, "린", 102, "조건과 무관한 값")
    excluded, excluded_proof = sourced_row(2, "테오", 103, "조건에 맞지 않은 값")
    catalog = [catalog_subject(1, "린", current=[matching])]
    catalog[0]["evidence"] = [proof]
    outputs = []
    if with_condition:
        catalog[0]["current"].append(unrelated)
        catalog[0]["evidence"].append(unrelated_proof)
        catalog.append(catalog_subject(2, "테오", current=[excluded]))
        catalog[1]["evidence"] = [excluded_proof]
        outputs = [
            {
                "decisions": [
                    {
                        "itemRef": "I1",
                        "verdict": "MATCH",
                        "rowRefs": ["R1"],
                        "explanation": "조건 충족",
                    },
                    {
                        "itemRef": "I2",
                        "verdict": "NO_MATCH",
                        "rowRefs": ["R3"],
                        "explanation": "조건과 다름",
                    },
                ]
            }
        ]
    decision = work_plan(
        shape="DISTINCT", fact_type="PROFILE", predicate="조건" if with_condition else None
    )
    decision["includeEvidence"] = True
    result = await replay_case(
        case("조건에 맞는 종류와 근거", decision, *outputs), catalog, config={"max_tool_calls": 3}
    )
    data = answer(result)
    assert data["sources"] == matching["sources"]
    assert "1종류" in data["text"] and "근거 1" in data["text"]
    assert "조건과 무관한 값" not in data["text"] and "조건에 맞지 않은 값" not in data["text"]
    assert [q["targetId"] for q in tool_queries(result, "CHARACTER_EVIDENCE")] == [uid(101)]
    assert result["queries"][-1]["readToken"] and result["queries"][-1]["page"] == 0


@pytest.mark.anyio
async def test_distinct_evidence_budget_still_reserves_collection_revalidation():
    row, proof = sourced_row(1, "린", 101, "요정")
    catalog = [catalog_subject(1, "린", current=[row])]
    catalog[0]["evidence"] = [proof]
    decision = work_plan(shape="DISTINCT", fact_type="PROFILE")
    decision["includeEvidence"] = True
    result = await replay_case(case("종류와 근거", decision), catalog, config={"max_tool_calls": 2})
    assert "1종류" in answer(result)["text"]
    assert answer(result)["sources"] == []
    assert any("근거 조회 한도" in note for note in answer(result)["limitations"])
    assert len(result["queries"]) == 2 and result["queries"][-1]["readToken"]


def world_subject(number, category, *, keys=("마력",), value="10"):
    subject = catalog_subject(
        number,
        f"설정{number}",
        domain="WORLD",
        current=[
            item(number, f"설정{number}", category=category, key=key, label=key, value=value)
            for key in keys
        ],
    )
    subject["subject"]["category"] = category
    return subject


@pytest.mark.anyio
async def test_world_selected_field_binds_actual_category_and_keeps_distinct_populations():
    catalog = [
        world_subject(1, "RACE", value="요정 값"),
        world_subject(2, "FACTION", value="세력 값"),
        world_subject(3, "FACTION", keys=()),
    ]
    result = await replay_case(
        case(
            "마력 종류",
            work_plan(shape="DISTINCT", domain="WORLD", attribute="마력"),
            {"outcome": "ANSWER", "fieldRefs": ["F1", "F2"]},
        ),
        catalog,
    )
    text = answer(result)["text"]
    assert "선택한 분류" in text and text.count("1종류") == 2
    queries = tool_queries(result, "WORLD_CURRENT_COLLECTION")
    assert [q["worldCategory"] for q in queries] == ["FACTION", "RACE", "FACTION", "RACE"]
    assert all(q["settingKey"] == "마력" and q["scopeName"] is None for q in queries)
    assert all(q["readToken"] for q in queries[-2:])


@pytest.mark.anyio
async def test_world_selected_categories_count_population_once_and_exclude_unselected_category():
    catalog = [
        world_subject(1, "FACTION", keys=("마력", "체력")),
        world_subject(2, "FACTION", keys=()),
        world_subject(3, "RACE"),
        world_subject(4, "LOCATION"),
    ]
    result = await replay_case(
        case(
            "관련 속성이 조건에 맞는 설정",
            work_plan(shape="COUNT", domain="WORLD", attribute="관련 속성", predicate="조건"),
            {"outcome": "ANSWER", "fieldRefs": ["F1", "F2", "F4"]},
            {
                "decisions": [
                    {
                        "itemRef": "I1",
                        "verdict": "MATCH",
                        "rowRefs": ["R1", "R2"],
                        "explanation": "조건 충족",
                    },
                    {
                        "itemRef": "I2",
                        "verdict": "MATCH",
                        "rowRefs": ["R3"],
                        "explanation": "조건 충족",
                    },
                ]
            },
        ),
        catalog,
    )
    text = answer(result)["text"]
    assert "설정 주체 3개" in text and "확인한 항목은 2개" in text
    assert "관련 설정이 없는 1개" in text and "설정4" not in text
    assert {q["worldCategory"] for q in tool_queries(result, "WORLD_CURRENT_COLLECTION")} == {
        "FACTION",
        "RACE",
    }


@pytest.mark.anyio
async def test_too_many_selected_fields_asks_to_narrow_instead_of_claiming_no_settings():
    result = await replay_case(
        case(
            "관련 설정 종류",
            work_plan(shape="DISTINCT", domain="WORLD", attribute="관련 설정"),
            {"outcome": "ANSWER", "fieldRefs": [f"F{n}" for n in range(1, 17)]},
        ),
        [world_subject(1, "RACE", keys=tuple(f"속성{n}" for n in range(16)))],
    )
    data = answer(result)
    assert data["outcome"] == "CLARIFICATION" and "좁혀" in data["text"]
    assert "찾지 못" not in data["text"] and "없" not in data["text"]
    assert tool_queries(result, "WORLD_FIELDS") and not tool_queries(
        result, "WORLD_CURRENT_COLLECTION"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["version", "rename", "removed", "same"])
async def test_named_current_summary_rechecks_subject_after_generation(mutation):
    def setup(spring):
        original = spring.respond

        def response(request):
            body = json.loads(request.content) if request.content else {}
            if body.get("tool") == "CHARACTER_CURRENT" and body.get("size") == 1:
                row = spring.catalog[0]["current"][0]
                if mutation == "version":
                    row.update(settingVersion=2, value="42")
                elif mutation == "rename":
                    row["subjectName"] = "바뀐 이름"
                elif mutation == "removed":
                    return httpx.Response(404, json={"success": False})
            return original(request)

        spring.respond = response

    decision = plan("린") | {"answerShape": "SUMMARY"}
    result = await replay_case(
        case(
            "린을 소개해줘",
            decision,
            {"claims": [{"text": "린의 레벨은 7이에요.", "rowRefs": ["R1"]}]},
        ),
        [catalog_subject(1, "린", current=[item(1, "린")])],
        spring_setup=setup,
    )
    data = answer(result)
    if mutation == "same":
        assert data["outcome"] == "ANSWER" and "7" in data["text"]
    else:
        assert data["outcome"] == "INSUFFICIENT_EVIDENCE" and "변경" in data["text"]
        assert "레벨은 7" not in data["text"]
        assert data["sources"] == []


@pytest.mark.anyio
async def test_named_summary_reserves_final_current_check_before_page_exhaustion():
    decision = plan("린", fact_type="SKILL") | {"answerShape": "SUMMARY"}
    rows = [item(1, "린", category="SKILL", key=f"skill.{n}") for n in range(50)]
    result = await replay_case(
        case(
            "린의 스킬 소개",
            decision,
            {"claims": [{"text": "확인한 능력이 있어요.", "rowRefs": ["R1"]}]},
        ),
        [catalog_subject(1, "린", current=rows)],
        config={"max_tool_calls": 3},
    )
    assert answer(result)["outcome"] == "ANSWER"
    assert result["queries"][-1]["size"] == 1
    assert len(result["queries"]) == 3
    assert any("일부" in note for note in answer(result)["limitations"])
