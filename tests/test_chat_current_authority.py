"""Synthetic evidence obligations and correction recovery, not planner accuracy."""

import asyncio
import copy
import json
import logging
import socket

import pytest
from pydantic import ValidationError

from app.chatbot.hybrid import _HybridSession
from app.chatbot.qa import response_schema
from app.chatbot.qa_render import NOTICES, notices
from app.schemas.chat_execution import ConversationTurn
from app.schemas.chat_hybrid import HybridPlan
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import answer, claim, search_with

SOURCE = "세린은 자신을 남자라고 소개했다."


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Authority regressions must not call external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def current_case(*, mixed=False, required=True):
    case = scenario()
    case.update(question="현재 세린의 성별은?" + (" 원문에서는 어떻게 나와?" if mixed else ""))
    case["outputs"] = [
        {
            "disposition": "PROCEED",
            "rdbMode": "CURRENT",
            "sourceNeed": "SCENE" if mixed else "NONE",
            "registeredCurrentRequired": required,
            "subjects": [
                {
                    "domain": "CHARACTER",
                    "name": "세린",
                    "focus": {"factType": "PROFILE", "attribute": "성별"},
                }
            ],
            "searchTerms": ["세린"],
            "includeEvidence": mixed,
            "episodeFrom": None,
            "episodeTo": None,
            "sourceFallback": False,
            "requestedFacets": [],
            "requestedParts": [{"ref": "P1", "description": "원문에 나타난 세린의 성별"}]
            if mixed
            else [],
            "includeRegisteredValues": False,
            "eventContext": None,
            "eventScope": None,
        }
    ]
    case.update(keyword=[], vector=[])
    return case


def current_catalog(spring):
    row = item(701, "세린", category="PROFILE", key="profile.gender", label="성별", value="여자")
    row["origin"] = "USER_INPUT"
    spring.catalog = [catalog_subject(701, "세린", current=[row])]


def missing_catalog(spring):
    # An apparently related but differently named character is not an alias proof.
    row = item(
        702, "다른 이름", category="PROFILE", key="profile.gender", label="성별", value="여자"
    )
    spring.catalog = [catalog_subject(702, "다른 이름", current=[row])]


def source_answer():
    return answer(claim(SOURCE, SOURCE, parts=("P1",), event_match=None))


def run(tmp_path, case, setup=current_catalog, **kwargs):
    return asyncio.run(replay(tmp_path, case, spring_setup=setup, **kwargs))


def result_text(result):
    assert result["finish"]["failure"] is None, result["finish"]
    return result["finish"]["result"]["text"]


def test_required_current_decision_is_strict_and_cannot_silently_default():
    plan = current_case()["outputs"][0]
    assert "registeredCurrentRequired" in response_schema(HybridPlan).schema["required"]
    for value in (None, "true", 1):
        with pytest.raises(ValidationError):
            HybridPlan.model_validate({**plan, "registeredCurrentRequired": value})
    del plan["registeredCurrentRequired"]
    with pytest.raises(ValidationError, match="registeredCurrentRequired"):
        HybridPlan.model_validate(plan)


@pytest.mark.parametrize("changes", [{"rdbMode": "NONE"}, {"rdbMode": "HISTORY"}])
def test_current_requirement_cannot_use_an_incompatible_route(changes):
    with pytest.raises(ValidationError, match="Required current information"):
        HybridPlan.model_validate({**current_case(mixed=True)["outputs"][0], **changes})


def test_current_requirement_drops_source_fallback_instead_of_rejecting():
    # 계획기가 등록값 확인 요구에 원문 보완까지 붙이면 거절(실행 실패)이 아니라 보완만 끈다.
    plan = HybridPlan.model_validate(
        {**current_case(mixed=True)["outputs"][0], "sourceFallback": True}
    )
    assert plan.registered_current_required is True and plan.source_fallback is False


def test_current_value_uses_manual_snapshot_without_manuscript_fallback(tmp_path):
    result = run(tmp_path, current_case(), search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    text = result_text(result)
    assert "여자" in text and "직접 입력" in text
    assert "남자" not in text
    assert not result["search"].calls and not result["embedding"].inputs
    assert [q["tool"] for q in result["spring"].queries] == [
        "FIND_CHARACTERS",
        "CHARACTER_FIELDS",
        "CHARACTER_CURRENT",
    ]


def test_missing_current_subject_is_not_replaced_by_source_or_another_character(tmp_path):
    result = run(
        tmp_path,
        current_case(),
        missing_catalog,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
    )
    assert "대상을 찾지 못" in result_text(result)
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert [q["tool"] for q in result["spring"].queries] == ["FIND_CHARACTERS"]
    assert not result["search"].calls and not result["embedding"].inputs


def test_manuscript_only_answer_does_not_inherit_the_current_snapshot(tmp_path):
    case = current_case(mixed=True, required=False)
    case["question"] = "원문에서 세린의 성별은 어떻게 나와?"
    case["outputs"][0]["rdbMode"] = "NONE"
    case["outputs"].append(source_answer())
    result = run(tmp_path, case, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    text = result_text(result)
    assert SOURCE in text and "원문에서 확인한 내용" in text
    assert "여자" not in text and "현재 등록" not in text
    assert not result["spring"].queries


def test_mixed_answer_keeps_manual_current_and_source_conflict_separate(tmp_path):
    case = current_case(mixed=True)
    response = source_answer()
    response.update(
        coverage="PARTIAL",
        conflicts=[
            {
                "rowRefs": ["R1"],
                "evidenceRefs": ["E1"],
                "supports": [{"evidenceRef": "E1", "quote": SOURCE}],
            }
        ],
    )
    case["outputs"].append(response)
    result = run(tmp_path, case, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    text = result_text(result)
    assert SOURCE in text and "현재 성별은 여자" in text
    assert "직접 입력" in text and "등록된 성별 설정과 원문의 내용이 다르게" in text
    payload = result["raw"].requests[-1]["input"]
    assert payload["registeredCurrent"] == {"required": True, "confirmed": True}
    assert payload["rows"][0]["value"] == "여자"
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


def test_mixed_missing_current_remains_unconfirmed_after_complete_source_answer(tmp_path, caplog):
    case = current_case(mixed=True)
    case["outputs"].append(source_answer())
    with caplog.at_level(logging.INFO, logger="app.chatbot.hybrid"):
        result = run(
            tmp_path, case, missing_catalog, search_setup=search_with(keyword=[passage(SOURCE, 1)])
        )
    text = result_text(result)
    assert SOURCE in text and "현재 등록 설정은 확인하지 못" in text
    payload = result["raw"].requests[-1]["input"]
    assert payload["registeredCurrent"] == {"required": True, "confirmed": False}
    assert payload["rows"] == []
    assert "stage=published data={'coverage': 'PARTIAL'" in caplog.text
    assert "다른 이름" not in text


def test_empty_history_does_not_erase_a_confirmed_current_read(tmp_path):
    case = current_case(mixed=True)
    case["outputs"][0]["rdbMode"] = "CURRENT_AND_HISTORY"
    case["outputs"].append(source_answer())
    result = run(tmp_path, case, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    assert "현재 성별은 여자" in result_text(result)
    assert result["raw"].requests[-1]["input"]["registeredCurrent"]["confirmed"] is True


def test_truncated_current_value_cannot_satisfy_current_obligation(tmp_path):
    def truncated(spring):
        current_catalog(spring)
        spring.catalog[0]["current"][0]["valueTruncated"] = True

    case = current_case(mixed=True)
    case["outputs"].append(source_answer())
    result = run(tmp_path, case, truncated, search_setup=search_with(keyword=[passage(SOURCE, 1)]))
    assert "현재 등록 설정은 확인하지 못" in result_text(result)
    assert result["raw"].requests[-1]["input"]["registeredCurrent"]["confirmed"] is False


@pytest.mark.parametrize("per_item", [False, True])
def test_mixed_missing_source_preserves_the_current_answer(tmp_path, per_item):
    case = current_case(mixed=True)
    case["outputs"][0]["perItem"] = per_item
    result = run(tmp_path, case)
    text = result_text(result)
    assert "현재 성별은 여자" in text and "원문 내용은 이번 조회에서 확인하지 못" in text
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_CURRENT") == 2


def correction_catalog(spring):
    current_catalog(spring)
    spring.context = spring.context.model_copy(
        update={
            "conversation": [
                ConversationTurn(
                    question="지금 세린은 남자야 여자야?",
                    answer="남자입니다.",
                )
            ]
        }
    )


@pytest.mark.parametrize("value", ["여자", "남자"])
def test_value_only_correction_repairs_unobserved_attribute_then_maps_real_field(tmp_path, value):
    case = current_case()
    case["question"] = f"{value}일 텐데?"
    invalid = copy.deepcopy(case["outputs"][0])
    repaired = copy.deepcopy(invalid)
    repaired["subjects"][0]["focus"]["attribute"] = value
    case["outputs"] = [invalid, repaired, {"outcome": "ANSWER", "fieldRefs": ["F1"]}]
    result = run(tmp_path, case, correction_catalog)
    assert "여자" in result_text(result)  # The correction's value is not accepted as evidence.
    assert len(result["raw"].requests) == 3
    repair = result["raw"].requests[1]["system"]
    assert "UNOBSERVED_FOCUS_ATTRIBUTE" in repair and "$.subjects[].focus.attribute" in repair
    field_payload = result["raw"].requests[2]["input"]
    assert field_payload["attribute"] == value
    assert field_payload["fields"][0]["displayName"] == "성별"
    assert not result["search"].calls
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3


def test_repeated_focus_failure_drops_the_attribute_without_changing_scope_or_leaking_terms(
    tmp_path, caplog
):
    # 두 번째 시도도 질문에 없는 항목 이름만 틀렸으면 되묻지 않고 그 이름을 비운 뒤 유형(PROFILE)으로
    # 진행한다. 대상·범위는 그대로이고 거절값은 로그·재전달 지침에 남지 않는다.
    case = current_case()
    case["question"] = "여자일 텐데?"
    private = "private-unobserved-attribute"
    case["outputs"][0]["subjects"][0]["focus"]["attribute"] = private
    case["outputs"] *= 2
    with caplog.at_level(logging.WARNING, logger="app.chatbot.qa"):
        result = run(tmp_path, case, correction_catalog)
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert NOTICES["FOCUS_ATTRIBUTE_DROPPED"] in result["finish"]["result"]["limitations"]
    assert [q["tool"] for q in result["spring"].queries][:1] == ["FIND_CHARACTERS"]
    assert all(q.get("factType") in (None, "PROFILE") for q in result["spring"].queries)
    assert not result["search"].calls
    assert len(result["raw"].requests) == 2
    assert private not in caplog.text and private not in result["raw"].requests[1]["system"]
    assert private not in json.dumps(result["spring"].queries, ensure_ascii=False)


def test_ambiguous_registered_name_stops_before_current_or_source_lookup(tmp_path):
    def ambiguous(spring):
        current_catalog(spring)
        spring.catalog.append(catalog_subject(702, "세린"))

    result = run(
        tmp_path,
        current_case(mixed=True),
        ambiguous,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
    )
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "CLARIFICATION"
    assert not result["search"].calls
    assert [q["tool"] for q in result["spring"].queries] == ["FIND_CHARACTERS"]


def test_two_subject_comparison_reads_current_only_and_says_history_was_skipped(tmp_path):
    # 두 인물의 현재 속성 비교에 이력까지 읽으면 항목·페이지 조회가 도구 예산을 넘겨
    # '항목을 모두 확인하지 못함'으로 끝났다(실제 사례). 비교는 현재값만 읽고 이력 생략을 알린다.
    case = current_case()
    plan = case["outputs"][0]
    plan.update(
        question="세린과 다른 이름의 성별을 비교해줘",
        rdbMode="CURRENT_AND_HISTORY",
        subjects=[
            {
                "domain": "CHARACTER",
                "name": "세린",
                "focus": {"factType": "PROFILE", "attribute": "성별"},
            },
            {
                "domain": "CHARACTER",
                "name": "다른 이름",
                "focus": {"factType": "PROFILE", "attribute": "성별"},
            },
        ],
        searchTerms=["세린", "다른 이름"],
    )
    case["question"] = plan.pop("question")

    def both(spring):
        current_catalog(spring)
        row = item(
            702, "다른 이름", category="PROFILE", key="profile.gender", label="성별", value="남자"
        )
        spring.catalog.append(catalog_subject(702, "다른 이름", current=[row]))

    result = run(tmp_path, case, both)
    text = result_text(result)
    assert "여자" in text and "남자" in text
    tools = [q["tool"] for q in result["spring"].queries]
    assert "CHARACTER_HISTORY" not in tools and tools.count("CHARACTER_CURRENT") == 2
    limitations = result["finish"]["result"]["limitations"]
    assert limitations.count(NOTICES["HISTORY_SKIPPED_FOR_COMPARISON"]) == 1


def test_mixed_plan_continues_with_source_when_field_catalog_cannot_be_read_in_budget(tmp_path):
    # 원문 라운드 예약 때문에 항목이 많은 인물의 등록 항목 목록을 다 읽지 못하면 되묻지 않고
    # 등록 현재값은 미확인으로 두고 원문 답변을 계속한다(45화 황금 가면 실제 사례).
    def many_fields(spring):
        rows = [
            item(701, "세린", category="PROFILE", key=f"profile.p{n}", label=f"항목 {n}", value="x")
            for n in range(12)
        ]
        spring.catalog = [catalog_subject(701, "세린", current=rows)]

    case = current_case(mixed=True)
    case["outputs"].append(source_answer())
    result = run(
        tmp_path,
        case,
        many_fields,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        config={"fields_page_size": 1, "max_tool_calls": 12},
    )
    text = result_text(result)
    assert result["finish"]["result"]["outcome"] != "CLARIFICATION"
    assert SOURCE in text and "현재 등록 설정은 확인하지 못" in text
    assert NOTICES["REGISTERED_LOOKUP_INCOMPLETE"] in result["finish"]["result"]["limitations"]
    assert result["search"].calls


def test_each_mixed_subject_with_unreadable_field_catalog_continues_to_source(tmp_path):
    # 항목 목록을 예산 안에 다 읽지 못한 인물이 둘이어도, 두 번째 인물에서 되묻기로 끝나지 않고
    # 두 인물 모두 등록 현재값을 미확인으로 둔 채 원문 답변을 계속한다.
    def many_fields(spring):
        def rows(number, name):
            return [
                item(
                    number,
                    name,
                    category="PROFILE",
                    key=f"profile.p{n}",
                    label=f"항목 {n}",
                    value="x",
                )
                for n in range(12)
            ]

        spring.catalog = [
            catalog_subject(701, "세린", current=rows(701, "세린")),
            catalog_subject(702, "미라", current=rows(702, "미라")),
        ]

    case = current_case(mixed=True)
    case["question"] = "현재 세린과 미라의 성별은? 원문에서는 어떻게 나와?"
    case["outputs"][0]["subjects"].append(
        {
            "domain": "CHARACTER",
            "name": "미라",
            "focus": {"factType": "PROFILE", "attribute": "성별"},
        }
    )
    case["outputs"].append(source_answer())
    result = run(
        tmp_path,
        case,
        many_fields,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        config={"fields_page_size": 1, "max_tool_calls": 14},
    )
    text = result_text(result)
    finished = result["finish"]["result"]
    assert finished["outcome"] != "CLARIFICATION", finished
    assert SOURCE in text
    assert finished["limitations"].count(NOTICES["REGISTERED_LOOKUP_INCOMPLETE"]) == 1
    assert [q["tool"] for q in result["spring"].queries].count("CHARACTER_FIELDS") >= 2
    assert result["search"].calls


def test_mixed_plan_trims_registered_rows_to_keep_room_for_the_source_answer(tmp_path):
    # 원문 답변이 필요한 계획에서 등록 행이 답변 입력을 넘치면 되묻지 않고 우선순위 높은 행만 남긴 뒤
    # 원문 근거 자리를 비워 두고 원문 답변을 계속한다(V2-054 실제 사례).
    def long_rows(spring):
        rows = [
            item(
                701,
                "세린",
                category="PROFILE",
                key=f"profile.p{n}",
                label=f"항목 {n}",
                value="가" * 2000,
            )
            for n in range(10)
        ]
        spring.catalog = [catalog_subject(701, "세린", current=rows)]

    case = current_case(mixed=True)
    case["outputs"][0]["registeredCurrentRequired"] = False
    case["outputs"][0]["subjects"][0]["focus"] = {"factType": "PROFILE", "attribute": None}
    case["outputs"].append(source_answer())
    result = run(
        tmp_path,
        case,
        long_rows,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        config={"max_context_characters": 20000, "max_selected_rows": 10},
        hybrid={"evidence_tokens": 1000},
    )
    finished = result["finish"]["result"]
    assert result["finish"]["failure"] is None, result["finish"]
    assert finished["outcome"] != "CLARIFICATION", finished
    assert NOTICES["REGISTERED_ROWS_TRIMMED"] in finished["limitations"]
    kept = result["raw"].requests[-1]["input"]["rows"]
    assert 0 < len(kept) < 10
    assert SOURCE in finished["text"]


def test_required_registered_current_is_not_trimmed_and_still_asks(tmp_path):
    # 등록 현재값 필수 계획은 같은 인물의 현재값 행 일부가 잘려도 인물 단위로는 확인처럼 보일 수
    # 있으므로 줄이지 않고 기존대로 항목을 좁혀 달라고 되묻는다(PR #124 리뷰).
    def long_rows(spring):
        rows = [
            item(
                701,
                "세린",
                category="PROFILE",
                key=f"profile.p{n}",
                label=f"항목 {n}",
                value="가" * 2000,
            )
            for n in range(10)
        ]
        spring.catalog = [catalog_subject(701, "세린", current=rows)]

    case = current_case(mixed=True)
    case["outputs"][0]["subjects"][0]["focus"] = {"factType": "PROFILE", "attribute": None}
    case["outputs"].append(source_answer())
    result = run(
        tmp_path,
        case,
        long_rows,
        search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        config={"max_context_characters": 20000, "max_selected_rows": 10},
        hybrid={"evidence_tokens": 1000},
    )
    finished = result["finish"]["result"]
    assert result["finish"]["failure"] is None, result["finish"]
    assert finished["outcome"] == "CLARIFICATION", finished
    assert NOTICES["REGISTERED_ROWS_TRIMMED"] not in finished["limitations"]


class _TrimSession:
    """행 줄이기 경계만 보는 가짜 세션. 행 하나는 100자, 안내 문장은 실제 길이로 센다."""

    def __init__(self, rows, limit):
        from types import SimpleNamespace

        self.chosen = [
            SimpleNamespace(
                ref=f"R{n}", item=SimpleNamespace(kind="CURRENT", source_episode_no=None)
            )
            for n in range(1, rows + 1)
        ]
        self.hcfg = SimpleNamespace(evidence_tokens=200)
        self.codes, self.focused_refs, self.limit = [], set(), limit

    def trace(self, *args, **kwargs):
        pass

    def answer_payload(self, plan):
        return {"rows": len(self.chosen), "limitations": notices(self.codes)}

    def fits(self, prompt, payload, schema, *, reserve_tokens=0, reserve_characters=0):
        size = payload["rows"] * 100 + sum(map(len, payload["limitations"]))
        return size + reserve_characters <= self.limit


@pytest.mark.parametrize(("limit", "kept"), [(640, 2), (300, 0)])
def test_trimmed_rows_leave_room_for_the_trimming_notice(limit, kept):
    # 줄인 뒤 붙는 안내 문장도 답변 입력에 들어간다. 행 수를 고른 뒤에 안내를 붙이면 경계에 맞춘 행이
    # 넘쳐 되묻기로 끝났다(V2-054 재실행, 19행). 원문 근거 자리(200토큰 → 320자)를 비우면 행을 하나도
    # 못 남기는 경우에도 마지막 확인은 자리 없이 해서 원문 답변으로 계속한다.
    plan = HybridPlan.model_validate(current_case(mixed=True)["outputs"][0])
    plan = plan.model_copy(update={"registered_current_required": False})
    session = _TrimSession(rows=3, limit=limit)
    assert _HybridSession.trim_rows_for_source_answer(session, plan, schema=None) is True
    assert len(session.chosen) == kept
    assert "REGISTERED_ROWS_TRIMMED" in session.codes


MAZE = "미궁 1층은 7일째에 폐쇄된다."


def world_mixed_case(*, attribute=None):
    case = current_case(mixed=True, required=False)
    case["question"] = "미궁은 층마다 며칠째에 폐쇄돼? 원문 근거도 알려줘."
    case["outputs"][0].update(
        subjects=[
            {
                "domain": "WORLD",
                "name": "미궁",
                "worldCategory": None,
                "focus": {"factType": None, "attribute": attribute},
            }
        ],
        searchTerms=["미궁"],
        requestedParts=[{"ref": "P1", "description": "미궁 층별 폐쇄 시점"}],
    )
    case["outputs"].append(answer(claim(MAZE, MAZE, parts=("P1",), event_match=None)))
    return case


def maze_in_two_categories(spring):
    region = catalog_subject(801, "미궁", domain="WORLD")
    rule = catalog_subject(802, "미궁", domain="WORLD")
    region["subject"]["category"] = "LOCATION"
    rule["subject"]["category"] = "WORLD_RULE_HISTORY"
    spring.catalog = [region, rule]


def test_mixed_plan_answers_from_source_when_world_name_is_registered_in_two_categories(tmp_path):
    # 같은 이름의 세계관이 두 분류에 등록돼 있어도('미궁' 지역·세계 규칙) 원문 계획은 되묻지 않고
    # 등록 정보 없이 원문으로 답한다(V2-007 실제 사례).
    result = run(
        tmp_path,
        world_mixed_case(),
        maze_in_two_categories,
        search_setup=search_with(keyword=[passage(MAZE, 1)]),
    )
    finished = result["finish"]["result"]
    assert result["finish"]["failure"] is None, result["finish"]
    assert finished["outcome"] != "CLARIFICATION", finished
    assert MAZE in finished["text"]
    assert NOTICES["REGISTERED_LOOKUP_AMBIGUOUS"] in finished["limitations"]
    assert result["search"].calls


def test_required_registered_current_still_asks_when_world_name_is_ambiguous(tmp_path):
    case = world_mixed_case()
    case["outputs"][0]["registeredCurrentRequired"] = True
    result = run(
        tmp_path,
        case,
        maze_in_two_categories,
        search_setup=search_with(keyword=[passage(MAZE, 1)]),
    )
    assert result["finish"]["result"]["outcome"] == "CLARIFICATION"
    assert not result["search"].calls


def test_mixed_plan_answers_from_source_when_the_registered_field_is_ambiguous(tmp_path):
    # 대상은 하나로 찾았고 어느 등록 항목인지만 모호하면(세계관의 같은 경로명) 원문 답변을 막지 않는다
    # (V2-054 실제 사례).
    def two_paths(spring):
        rows = [
            item(
                811, "미궁", category="WORLD_RULE_HISTORY", key="closing", label="폐쇄", value="7일"
            ),
            item(
                811,
                "미궁",
                category="WORLD_RULE_HISTORY",
                key="closing",
                label="폐쇄",
                value="10일",
                scope="2층",
            ),
        ]
        maze = catalog_subject(811, "미궁", domain="WORLD", current=rows)
        maze["subject"]["category"] = "WORLD_RULE_HISTORY"
        spring.catalog = [maze]

    case = world_mixed_case(attribute="폐쇄")
    case["outputs"].insert(1, {"outcome": "CLARIFICATION", "fieldRefs": []})
    result = run(tmp_path, case, two_paths, search_setup=search_with(keyword=[passage(MAZE, 1)]))
    finished = result["finish"]["result"]
    assert result["finish"]["failure"] is None, result["finish"]
    assert finished["outcome"] != "CLARIFICATION", finished
    assert MAZE in finished["text"]
    assert NOTICES["REGISTERED_LOOKUP_AMBIGUOUS"] in finished["limitations"]
