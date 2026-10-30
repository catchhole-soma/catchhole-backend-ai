"""Synthetic focused-query regressions; no model key, network, or manuscript data.

The scripted planner only chooses a route or an observed field reference. The
assertions cover which real query arguments are issued, whether all matching
pages survive answer selection, and which limits must remain visible.
"""

import copy
import json
import socket
from uuid import UUID

import pytest

from app.chatbot.qa_render import NOTICES
from app.chatbot.replay import replay_case


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Focused lookup tests must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def uid(number):
    return str(UUID(int=number))


def item(
    subject,
    name,
    *,
    kind="CURRENT",
    category="LEVEL",
    key="level",
    label="레벨",
    value="7",
    scope=None,
    episode=None,
    number=1001,
    structured=None,
):
    return {
        "kind": kind,
        "id": uid(subject)
        if kind in {"CHARACTER", "WORLD_SETTING"}
        else (uid(number) if kind == "HISTORY" else None),
        "subjectId": uid(subject),
        "subjectName": name,
        "category": category,
        "scopeName": scope,
        "settingKey": key,
        "displayName": label,
        "value": value,
        "structuredValueJson": json.dumps(structured, ensure_ascii=False)
        if structured is not None
        else None,
        "valueTruncated": False,
        "contributesToCurrent": kind == "CURRENT",
        "operation": "ADD" if kind == "HISTORY" else None,
        "historyOnly": False,
        "settingVersion": 1,
        "effectiveFromEpisodeNo": None,
        "sourceEpisodeId": uid(10000 + episode) if episode else None,
        "sourceEpisodeNo": episode,
        "origin": "CONFIRMED_ANALYSIS",
        "sources": [],
        "sourcesTruncated": False,
        "evidence": None,
    }


def catalog_subject(number, name, *, domain="CHARACTER", current=(), history=()):
    subject = item(number, name, kind="CHARACTER" if domain == "CHARACTER" else "WORLD_SETTING")
    subject.update(
        category=None if domain == "CHARACTER" else "POWER_SYSTEM",
        settingKey=None,
        displayName=None,
        value=None,
    )
    return {"subject": subject, "current": list(current), "history": list(history), "evidence": []}


def plan(name, *, intent="CURRENT", domain="CHARACTER", fact_type="LEVEL", attribute=None):
    return {
        "intent": intent,
        "subjects": [
            {
                "domain": domain,
                "name": name,
                "focus": {"factType": fact_type, "attribute": attribute},
            }
        ],
        "includeEvidence": False,
        "episodeFrom": None,
        "episodeTo": None,
        "characterFilter": None,
    }


def case(question, decision, *extra_outputs):
    return {
        "id": "focused-synthetic",
        "question": question,
        "modelOutputs": [decision, *extra_outputs],
    }


def answer(result):
    assert result["finish"]["failure"] is None, result["finish"]
    return result["finish"]["result"]


def tool_queries(result, tool):
    return [q for q in result["queries"] if q["tool"] == tool]


def unrelated_items(number, name, *, history=False):
    return [
        item(
            number,
            name,
            category="ITEM",
            key=f"item.synthetic-{n}",
            label=f"합성 장비 {n}",
            value=f"무관한 장비 값 {n}",
            kind="HISTORY" if history else "CURRENT",
            episode=1 if history else None,
            number=20000 + n,
        )
        for n in range(300)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("name,number,level", [("세린", 101, "73"), ("타렉", 202, "26")])
async def test_current_level_is_filtered_before_unrelated_settings_pagination(name, number, level):
    catalog = [
        catalog_subject(
            number,
            name,
            current=[
                *unrelated_items(number, name),
                item(number, name, value=level),
            ],
        ),
        catalog_subject(303, "다른 인물", current=[item(303, "다른 인물", value="999")]),
    ]
    result = await replay_case(case(f"{name}의 현재 레벨은?", plan(name)), catalog)
    actual = answer(result)
    assert actual["outcome"] == "ANSWER"
    assert level in actual["text"]
    assert "999" not in actual["text"] and "무관한 장비" not in actual["text"]
    assert [q["tool"] for q in result["queries"]] == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]
    assert tool_queries(result, "CHARACTER_CURRENT")[0]["factType"] == "LEVEL"
    assert tool_queries(result, "CHARACTER_CURRENT")[0]["targetId"] == uid(number)
    assert len(result["modelRequests"]) == 1
    assert NOTICES["PARTIAL_RESULTS"] not in actual["limitations"]
    assert NOTICES["SETTING_BOOK_UNSUPPORTED"] not in actual["limitations"]


@pytest.mark.anyio
async def test_focused_history_exceeds_old_page_and_row_caps_without_losing_records():
    number, name = 104, "로하"
    levels = [
        item(number, name, kind="HISTORY", value=str(1000 + n), episode=n, number=30000 + n)
        for n in range(1, 48)
    ]
    catalog = [
        catalog_subject(
            number,
            name,
            history=[
                *unrelated_items(number, name, history=True),
                *levels,
            ],
        )
    ]
    result = await replay_case(
        case("로하의 레벨 변화 이력을 모두 알려줘", plan(name, intent="HISTORY")),
        catalog,
        config={"page_size": 20, "max_pages": 2, "max_selected_rows": 1, "max_items": 1},
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER"
    queries = tool_queries(result, "CHARACTER_HISTORY")
    assert [q["page"] for q in queries] == [0, 1, 2]
    assert all(q["factType"] == "LEVEL" and q["targetId"] == uid(number) for q in queries)
    positions = [actual["text"].index(row["value"]) for row in levels]
    assert positions == sorted(positions)
    assert len(result["modelRequests"]) == 1
    assert "무관한 장비" not in actual["text"]
    assert NOTICES["PARTIAL_RESULTS"] not in actual["limitations"]
    assert NOTICES["SELECTION_LIMIT"] not in actual["limitations"]


@pytest.mark.anyio
async def test_dynamic_attribute_selects_observed_metadata_before_values_and_hides_ids():
    number, name = 105, "단비"
    catalog = [
        catalog_subject(
            number,
            name,
            current=[
                item(number, name, category="STAT", key="stats.mana", label="마력", value="31"),
                item(number, name, category="STAT", key="stats.resolve", label="의지", value="87"),
            ],
        )
    ]
    decision = plan(name, fact_type="STAT", attribute="정신력")
    result = await replay_case(
        case(
            "단비의 정신력은?",
            decision,
            {"outcome": "ANSWER", "fieldRefs": ["F2"]},
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER" and "87" in actual["text"]
    assert "31" not in actual["text"]
    assert [q["tool"] for q in result["queries"]] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_CURRENT",
    ]
    current = tool_queries(result, "CHARACTER_CURRENT")[0]
    assert current["settingKey"] == "stats.resolve" and current["factType"] == "STAT"
    assert result["modelRequests"][1]["input"]["fields"][1] == {
        "fieldRef": "F2",
        "category": "STAT",
        "displayName": "의지",
        "settingKey": "stats.resolve",
        "scopeName": None,
    }
    for request in result["modelRequests"]:
        payload = json.dumps(request["input"], ensure_ascii=False)
        for private in ("memberId", "workId", "subjectId", "targetId", uid(number), uid(9003)):
            assert private not in payload
    assert (
        len(result["modelRequests"])
        == len(result["reservations"])
        == len(result["usageReports"])
        == 2
    )


@pytest.mark.anyio
async def test_exact_unique_display_name_uses_server_key_without_extra_model_call():
    catalog = [
        catalog_subject(
            106,
            "연서",
            current=[
                item(
                    106,
                    "연서",
                    category="STAT",
                    key="stats.custom-flow",
                    label="기류 감응",
                    value="64",
                ),
            ],
        )
    ]
    result = await replay_case(
        case("연서의 기류 감응은?", plan("연서", fact_type="STAT", attribute="기류 감응")),
        catalog,
    )
    assert "64" in answer(result)["text"]
    assert tool_queries(result, "CHARACTER_CURRENT")[0]["settingKey"] == "stats.custom-flow"
    assert len(result["modelRequests"]) == 1


@pytest.mark.anyio
async def test_unmatched_field_name_falls_back_to_type_values_for_selection():
    # 항목은 있지만 질문 표현('몇 급 마법사')이 등록 표시명('직업')과 달라 FIELD 선택이 비는 경우,
    # 포기하지 않고 그 유형의 현재값을 읽어 선택 모델이 고르게 한다.
    catalog = [
        catalog_subject(
            140,
            "드왈키",
            current=[
                item(
                    140,
                    "드왈키",
                    category="PROFILE",
                    key="profile.occupation",
                    label="직업",
                    value="8급 마법사",
                ),
                item(
                    140,
                    "드왈키",
                    category="PROFILE",
                    key="profile.species",
                    label="종족",
                    value="인간",
                    number=141,
                ),
            ],
        )
    ]
    result = await replay_case(
        case(
            "드왈키는 몇 급 마법사야?",
            plan("드왈키", fact_type="PROFILE", attribute="몇 급 마법사"),
            {"outcome": "INSUFFICIENT_EVIDENCE", "fieldRefs": []},
            {"outcome": "ANSWER", "rowRefs": ["R1"]},
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER" and "8급" in actual["text"]
    assert [q["tool"] for q in result["queries"]] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_CURRENT",
    ]
    assert tool_queries(result, "CHARACTER_CURRENT")[0]["factType"] == "PROFILE"
    assert NOTICES["FOCUS_FALLBACK"] in actual["limitations"]


@pytest.mark.anyio
async def test_missing_type_widens_field_discovery_then_selects_over_type_values():
    # 질문한 유형(STAT)의 항목이 없으면 모든 유형의 항목 목록에서 다시 고르고, 그래도 관련 항목이 없으면
    # 현재값 전체를 선택 모델에 넘긴 뒤 없음으로 끝낸다. 집중 행처럼 무조건 답에 넣지는 않는다.
    catalog = [
        catalog_subject(
            107,
            "수오",
            current=[
                item(107, "수오", category="ITEM", key="item.sword", label="검", value="한 자루"),
            ],
        )
    ]
    result = await replay_case(
        case(
            "수오의 정신력은?",
            plan("수오", fact_type="STAT", attribute="정신력"),
            {"outcome": "INSUFFICIENT_EVIDENCE", "fieldRefs": []},
            {"outcome": "INSUFFICIENT_EVIDENCE", "rowRefs": []},
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert [q["tool"] for q in result["queries"]] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_FIELDS",
        "CHARACTER_CURRENT",
    ]
    assert tool_queries(result, "CHARACTER_FIELDS")[1]["factType"] is None
    assert NOTICES["FOCUS_TYPE_WIDENED"] in actual["limitations"]


@pytest.mark.anyio
async def test_wrong_type_guess_still_finds_the_item_after_widening():
    catalog = [
        catalog_subject(
            150,
            "드왈키",
            current=[
                item(
                    150,
                    "드왈키",
                    category="PROFILE",
                    key="profile.occupation",
                    label="직업",
                    value="8급 마법사",
                ),
            ],
        )
    ]
    result = await replay_case(
        case(
            "드왈키는 몇 급 마법사야?",
            plan("드왈키", fact_type="LEVEL", attribute="몇 급"),
            {"outcome": "ANSWER", "fieldRefs": ["F1"]},
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER" and "8급" in actual["text"]
    assert [q["tool"] for q in result["queries"]] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_FIELDS",
        "CHARACTER_CURRENT",
    ]


def world_catalog():
    return [
        catalog_subject(
            108,
            "결계술",
            domain="WORLD",
            current=[
                item(
                    108,
                    "결계술",
                    category="POWER_SYSTEM",
                    key="소모량",
                    label="소모량",
                    scope=None,
                    value="11",
                ),
                item(
                    108,
                    "결계술",
                    category="POWER_SYSTEM",
                    key="소모량",
                    label="소모량",
                    scope="강화 결계",
                    value="72",
                ),
            ],
        )
    ]


@pytest.mark.anyio
async def test_same_world_property_in_root_and_named_scope_requires_disambiguation():
    result = await replay_case(
        case(
            "결계술의 소모량은?",
            plan("결계술", domain="WORLD", fact_type=None, attribute="소모량"),
            {"outcome": "CLARIFICATION", "fieldRefs": []},
        ),
        world_catalog(),
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert [q["tool"] for q in result["queries"]] == ["FIND_WORLD_SETTINGS", "WORLD_FIELDS"]
    fields = result["modelRequests"][1]["input"]["fields"]
    assert [(f["settingKey"], f["scopeName"]) for f in fields] == [
        ("소모량", None),
        ("소모량", "강화 결계"),
    ]


@pytest.mark.anyio
async def test_selected_world_reference_preserves_exact_scope_and_key():
    result = await replay_case(
        case(
            "결계술의 강화 결계 소모량은?",
            plan("결계술", domain="WORLD", fact_type=None, attribute="소모량"),
            {"outcome": "ANSWER", "fieldRefs": ["F2"]},
        ),
        world_catalog(),
    )
    actual = answer(result)
    assert "72" in actual["text"] and "11" not in actual["text"]
    request = tool_queries(result, "WORLD_CURRENT")[0]
    assert request["settingKey"] == "소모량" and request["scopeName"] == "강화 결계"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        {"outcome": "ANSWER", "fieldRefs": ["F999"]},
        {"outcome": "ANSWER", "fieldRefs": ["F1"], "settingKey": "invented.storage.key"},
        {"outcome": "ANSWER", "fieldRefs": ["F1"], "scopeName": "invented scope"},
        {"outcome": "ANSWER", "fieldRefs": ["F1"], "workId": "foreign-work"},
    ],
)
async def test_unobserved_field_reference_or_invented_query_argument_is_rejected(bad):
    result = await replay_case(
        case(
            "결계술의 소모량은?",
            plan("결계술", domain="WORLD", fact_type=None, attribute="소모량"),
            bad,
            bad,
        ),
        world_catalog(),
    )
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert [q["tool"] for q in result["queries"]] == ["FIND_WORLD_SETTINGS", "WORLD_FIELDS"]


def list_plan(attribute, value):
    return {
        "intent": "CHARACTER_LIST",
        "subjects": [],
        "includeEvidence": False,
        "episodeFrom": None,
        "episodeTo": None,
        "characterFilter": {"factType": "PROFILE", "attribute": attribute, "value": value},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attribute,label,key,value,extra",
    [
        ("종족", "종족", "profile.species", "청동인", []),
        (
            "소속",
            "소속 단체",
            "profile.affiliation",
            "새벽회",
            [
                {"outcome": "ANSWER", "fieldRefs": ["F1"]},
            ],
        ),
    ],
)
async def test_character_condition_uses_observed_work_field_and_current_values(
    attribute,
    label,
    key,
    value,
    extra,
):
    catalog = [
        catalog_subject(
            number,
            name,
            current=[
                item(
                    number,
                    name,
                    category="PROFILE",
                    key=key,
                    label=label,
                    value=stored,
                    structured={"value": stored},
                ),
            ],
            history=[
                item(
                    number,
                    name,
                    kind="HISTORY",
                    category="PROFILE",
                    key=key,
                    label=label,
                    value=value,
                    structured={"value": value},
                    episode=1,
                    number=40000 + number,
                ),
            ],
        )
        for number, name, stored in [
            (110, "하람", value),
            (111, "노엘", value),
            (112, "나린", "다른 값"),
        ]
    ]
    result = await replay_case(
        case(
            f"{attribute}이 {value}인 인물을 모두 알려줘",
            list_plan(attribute, value),
            *extra,
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER"
    assert "하람" in actual["text"] and "노엘" in actual["text"]
    assert "나린" not in actual["text"]
    assert [q["tool"] for q in result["queries"]] == ["CHARACTER_FIELDS", "FIND_CHARACTERS"]
    fields, find = result["queries"]
    assert fields["targetId"] is None and fields["factType"] == "PROFILE"
    assert find["targetId"] is None and find["query"] is None
    assert find["settingKey"] == key and find["factType"] == "PROFILE"
    assert find["valueEquals"] == value


@pytest.mark.anyio
async def test_character_list_follows_all_matching_pages_not_answer_row_cap():
    names = [f"합성인물{n:02}" for n in range(1, 24)]
    catalog = [
        catalog_subject(
            200 + n,
            name,
            current=[
                item(
                    200 + n,
                    name,
                    category="PROFILE",
                    key="profile.species",
                    label="종족",
                    value="청동인",
                    structured={"value": "청동인"},
                ),
            ],
        )
        for n, name in enumerate(names)
    ]
    result = await replay_case(
        case("종족이 청동인인 인물을 모두 알려줘", list_plan("종족", "청동인")),
        catalog,
        config={"page_size": 5, "max_pages": 2, "max_selected_rows": 1},
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER"
    assert all(name in actual["text"] for name in names)
    assert [q["page"] for q in tool_queries(result, "FIND_CHARACTERS")] == [0, 1, 2, 3, 4]
    assert NOTICES["PARTIAL_RESULTS"] not in actual["limitations"]
    assert len(result["modelRequests"]) == 1


@pytest.mark.anyio
async def test_character_condition_matches_structured_scalar_not_display_text():
    catalog = [
        catalog_subject(
            130,
            "스칼라일치",
            current=[
                item(
                    130,
                    "스칼라일치",
                    category="PROFILE",
                    key="profile.species",
                    label="종족",
                    value="외형과 문화에 대한 표시용 설명",
                    structured={"value": "청동인"},
                ),
            ],
        ),
        catalog_subject(
            131,
            "표시만일치",
            current=[
                item(
                    131,
                    "표시만일치",
                    category="PROFILE",
                    key="profile.species",
                    label="종족",
                    value="청동인",
                    structured={"value": "다른 종족"},
                ),
            ],
        ),
        catalog_subject(
            132,
            "설명만존재",
            current=[
                item(
                    132,
                    "설명만존재",
                    category="PROFILE",
                    key="profile.species",
                    label="종족",
                    value="청동인",
                    structured={"description": "청동인"},
                ),
            ],
        ),
    ]
    result = await replay_case(
        case(
            "종족이 청동인인 인물은?",
            list_plan("종족", "청동인"),
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER"
    assert "스칼라일치" in actual["text"]
    assert "표시만일치" not in actual["text"]
    assert "설명만존재" not in actual["text"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "structured,expected,matched",
    [
        ("  SILVER  ", "silver", True),
        ("청동인", "청동인", True),
        ({"value": "청동인", "name": "다른 종족"}, "청동인", True),
        ({"value": None, "name": "청동인"}, "청동인", True),
        ({"name": "청동인"}, "청동인", True),
        ({"description": "청동인"}, "청동인", False),
        ({"value": [], "name": "청동인"}, "청동인", False),
        (["청동인"], "청동인", False),
        (None, "청동인", False),
        ({"value": True}, "true", True),
        ({"value": 17}, "17", True),
        ("Straße", "STRASSE", False),
    ],
)
async def test_scalar_condition_replay_follows_java_value_name_and_normalization(
    structured,
    expected,
    matched,
):
    catalog = [
        catalog_subject(
            133,
            "조건대상",
            current=[
                item(
                    133,
                    "조건대상",
                    category="PROFILE",
                    key="profile.species",
                    label="종족",
                    value=expected,
                    structured=structured,
                ),
            ],
        )
    ]
    result = await replay_case(
        case(
            f"종족이 {expected}인 인물은?",
            list_plan("종족", expected),
        ),
        catalog,
    )
    actual = answer(result)
    assert actual["outcome"] == ("ANSWER" if matched else "INSUFFICIENT_EVIDENCE")
    assert ("조건대상" in actual["text"]) == matched


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [("attribute", "직업"), ("value", "질문에 없는 집단")])
async def test_model_cannot_invent_condition_vocabulary_before_query(field, value):
    decision = list_plan("종족", "청동인")
    decision["characterFilter"][field] = value
    result = await replay_case(
        case(
            "종족이 청동인인 인물은?",
            decision,
            decision,
        ),
        [],
    )
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert result["queries"] == []


@pytest.mark.anyio
async def test_tool_budget_keeps_observed_history_with_explicit_partial_notice():
    name, number = "아란", 120
    catalog = [
        catalog_subject(
            number,
            name,
            history=[
                item(number, name, kind="HISTORY", value=str(2000 + n), episode=n, number=50000 + n)
                for n in range(1, 24)
            ],
        )
    ]
    result = await replay_case(
        case("아란의 레벨 변화 이력은?", plan(name, intent="HISTORY")),
        catalog,
        config={"page_size": 5, "max_tool_calls": 3},
    )
    actual = answer(result)
    assert actual["outcome"] == "ANSWER"
    assert len(result["queries"]) == 3
    assert [q["page"] for q in tool_queries(result, "CHARACTER_HISTORY")] == [0, 1]
    assert all(str(2000 + n) in actual["text"] for n in range(1, 11))
    assert "2011" not in actual["text"]
    assert NOTICES["PARTIAL_RESULTS"] in actual["limitations"]


@pytest.mark.anyio
async def test_incomplete_field_catalog_never_resolves_from_only_the_first_page():
    catalog = [
        catalog_subject(
            121,
            "여울",
            current=[
                item(
                    121,
                    "여울",
                    category="STAT",
                    key=f"stats.path-{n}",
                    label=f"항목 {n}",
                    value="10",
                )
                for n in range(12)
            ],
        )
    ]
    result = await replay_case(
        case("여울의 정신력은?", plan("여울", fact_type="STAT", attribute="정신력")),
        catalog,
        config={"page_size": 5, "max_tool_calls": 3},
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert NOTICES["PARTIAL_RESULTS"] in answer(result)["limitations"]
    assert not tool_queries(result, "CHARACTER_CURRENT")


@pytest.mark.anyio
async def test_server_partial_field_response_cannot_prove_a_unique_display_name():
    catalog = [
        catalog_subject(
            123,
            "미루",
            current=[
                item(123, "미루", category="STAT", key="stats.focus", label="집중력", value="12"),
            ],
        )
    ]

    def partial_fields(spring):
        lookup = spring.lookup

        def partial(query):
            result = lookup(query)
            if query.tool.value == "CHARACTER_FIELDS":
                result["partial"] = True
            return result

        spring.lookup = partial

    result = await replay_case(
        case("미루의 집중력은?", plan("미루", fact_type="STAT", attribute="집중력")),
        catalog,
        spring_setup=partial_fields,
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert NOTICES["PARTIAL_RESULTS"] in answer(result)["limitations"]
    assert not tool_queries(result, "CHARACTER_CURRENT")
    assert len(result["modelRequests"]) == 1


@pytest.mark.anyio
async def test_duplicate_character_labels_are_selected_from_refs_not_first_match():
    catalog = [
        catalog_subject(
            124,
            "이든",
            current=[
                item(
                    124, "이든", category="STAT", key="stats.spell-output", label="마력", value="10"
                ),
                item(
                    124, "이든", category="STAT", key="stats.mana-reserve", label="마력", value="90"
                ),
            ],
        )
    ]
    result = await replay_case(
        case(
            "이든의 마력은?",
            plan("이든", fact_type="STAT", attribute="마력"),
            {"outcome": "CLARIFICATION", "fieldRefs": []},
        ),
        catalog,
    )
    assert answer(result)["outcome"] == "CLARIFICATION"
    assert len(result["modelRequests"][1]["input"]["fields"]) == 2
    assert not tool_queries(result, "CHARACTER_CURRENT")


@pytest.mark.anyio
async def test_returned_wrong_field_cannot_cross_requested_filter_boundary():
    catalog = [catalog_subject(122, "라온", current=[item(122, "라온")])]

    def wrong_field(spring):
        lookup = spring.lookup

        def substituted(query):
            result = lookup(query)
            if query.tool.value == "CHARACTER_CURRENT":
                result = copy.deepcopy(result)
                result["items"]["content"][0]["category"] = "ITEM"
            return result

        spring.lookup = substituted

    result = await replay_case(
        case("라온의 레벨은?", plan("라온")),
        catalog,
        spring_setup=wrong_field,
    )
    assert result["finish"] == {"result": None, "failure": "NETWORK_FAILURE"}
