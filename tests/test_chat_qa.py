import copy
import json
import socket
from pathlib import Path

import pytest

from app.chatbot.qa import encode, response_schema
from app.chatbot.qa_render import NOTICES
from app.chatbot.replay import check_expected, load_fixture, offline_settings, replay_case
from app.schemas.chat_qa import AnswerSelection, QuestionPlan


FIXTURE = load_fixture(Path(__file__).parent / "fixtures/chat-qa-v1.json")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("QA verification must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def scenario(name="current_ability"):
    case = copy.deepcopy(next(c for c in FIXTURE["cases"] if c["id"] == name))
    return case, copy.deepcopy(FIXTURE["catalogs"][case["catalog"]])


async def run(name="current_ability", **kwargs):
    case, catalog = scenario(name)
    return await replay_case(case, catalog, **kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["id"])
async def test_authored_question_and_evidence_replays(case):
    result = await replay_case(case, FIXTURE["catalogs"][case["catalog"]])
    check_expected(case, result)
    assert len(result["reservations"]) == len(result["modelRequests"])
    assert len(result["usageReports"]) == len(result["modelRequests"])
    assert {r["purpose"] for r in result["reservations"]} <= {"CHAT_INTERPRETATION", "CHAT_ANSWER"}
    assert all(r["kind"] == "SETTLE" for r in result["usageReports"])
    for request in result["modelRequests"]:
        assert "memberId" not in request["input"] and "workId" not in request["input"]
        for row in request["input"].get("rows", []):
            assert "subjectId" not in row and "sources" not in row


@pytest.mark.anyio
@pytest.mark.parametrize(
    "addition",
    [
        {"workId": "foreign-work"},
        {"memberId": 100},
        {"sql": "select * from members"},
        {"tool": "DELETE_SETTINGS"},
        {"url": "https://attacker.invalid"},
    ],
)
async def test_model_cannot_introduce_authority_or_arbitrary_tools(addition):
    case, catalog = scenario()
    bad = case["modelOutputs"][0] | addition
    result = await replay_case(case, catalog, outputs=[bad, bad])
    assert result["finish"] == {"result": None, "failure": "PROVIDER_FAILURE"}
    assert result["queries"] == []
    assert len(result["reservations"]) == 2


@pytest.mark.anyio
async def test_invented_subject_name_is_rejected_before_query():
    case, catalog = scenario()
    bad = case["modelOutputs"][0]
    bad["subjects"][0]["name"] = "질문에 없는 인물"
    result = await replay_case(case, catalog, outputs=[bad, bad])
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert not result["queries"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        {"outcome": "ANSWER", "rowRefs": ["R999"]},
        {"outcome": "ANSWER", "rowRefs": ["R1", "R1"]},
        {"outcome": "ANSWER", "rowRefs": ["R1"], "quote": "조작된 원문"},
        {"outcome": "ANSWER", "rowRefs": ["R1"], "text": "마력은 무한대다"},
    ],
)
async def test_unknown_refs_and_model_authored_quotes_never_reach_success(bad):
    case, catalog = scenario()
    result = await replay_case(case, catalog, outputs=[case["modelOutputs"][0], bad, bad])
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert [q["tool"] for q in result["queries"]] == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]
    assert len(result["usageReports"]) == 3


@pytest.mark.anyio
async def test_schema_retry_reenters_existing_reservation_and_usage_boundary():
    case, catalog = scenario()
    result = await replay_case(case, catalog, outputs=["not json", *case["modelOutputs"]])
    check_expected(case, result)
    assert [r["purpose"] for r in result["reservations"]] == [
        "CHAT_INTERPRETATION",
        "CHAT_INTERPRETATION",
        "CHAT_ANSWER",
    ]
    assert len({r["callId"] for r in result["reservations"]}) == 3
    assert len(result["usageReports"]) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("status", [404, 409, 503])
async def test_query_failure_is_technical_failure_not_absence(status):
    result = await run(spring_setup=lambda spring: setattr(spring, "query_status", status))
    assert result["finish"] == {"result": None, "failure": "NETWORK_FAILURE"}


@pytest.mark.anyio
async def test_query_limit_is_separate_from_account_quota():
    result = await run(spring_setup=lambda spring: setattr(spring, "query_status", 429))
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_quota_denial_never_calls_model_or_tools():
    def reject(spring):
        spring.reserve_status = 409
        spring.reserve_code = "AI_TOKEN_QUOTA_EXHAUSTED"

    result = await run(spring_setup=reject)
    assert result["finish"]["failure"] == "QUOTA_EXHAUSTED"
    assert result["modelRequests"] == result["queries"] == []


@pytest.mark.anyio
async def test_retry_stops_at_execution_call_limit_and_keeps_prior_usage():
    case, catalog = scenario()
    case["maxCalls"] = 1
    result = await replay_case(case, catalog, outputs=["invalid", case["modelOutputs"][0]])
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(result["modelRequests"]) == len(result["usageReports"]) == 1


@pytest.mark.anyio
async def test_tool_budget_stops_before_extra_query():
    result = await run(config={"max_tool_calls": 1})
    assert result["finish"]["failure"] == "EXECUTION_LIMIT_EXCEEDED"
    assert len(result["queries"]) == 1


@pytest.mark.anyio
async def test_partial_subject_page_cannot_be_assumed_unique():
    result = await run("ambiguous_subject", config={"page_size": 1, "max_pages": 1})
    assert result["finish"]["result"]["outcome"] == "CLARIFICATION"
    assert len(result["queries"]) == 1
    assert NOTICES["PARTIAL_RESULTS"] in result["finish"]["result"]["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["current_mana", "world_scope"])
@pytest.mark.parametrize("empty", [False, True])
async def test_server_partial_subject_search_requires_clarification(name, empty):
    def partial_search(spring):
        lookup = spring.lookup

        def partial(query):
            response = lookup(query)
            if query.tool.value.startswith("FIND_"):
                response["partial"] = True
                assert response["items"]["hasNext"] is False
                if empty:
                    response["items"].update(content=[], totalElements=0, totalPages=0)
            return response

        spring.lookup = partial

    result = await run(name, spring_setup=partial_search)
    answer = result["finish"]["result"]
    assert answer["outcome"] == "CLARIFICATION"
    assert len(result["queries"]) == len(result["modelRequests"]) == 1
    assert NOTICES["PARTIAL_RESULTS"] in answer["limitations"]
    assert NOTICES["NO_RELATED_SETTING"] not in answer["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,outcome,missing",
    [
        ("ambiguous_scope", "CLARIFICATION", False),
        ("unregistered_field", "INSUFFICIENT_EVIDENCE", True),
    ],
)
async def test_selection_ambiguity_is_distinct_from_missing_setting(name, outcome, missing):
    result = await run(name)
    answer = result["finish"]["result"]
    assert answer["outcome"] == outcome
    assert (NOTICES["NO_RELATED_SETTING"] in answer["limitations"]) is missing


@pytest.mark.anyio
async def test_spring_truncated_evidence_keeps_existing_partial_notice():
    case, catalog = scenario()
    catalog[0]["evidence"][0]["evidence"]["truncated"] = True

    def spring_partial_contract(spring):
        lookup = spring.lookup

        def partial(query):
            response = lookup(query)
            # Spring SettingToolServiceImpl.response promotes evidence truncation to partial.
            if any(
                item.get("evidence") and item["evidence"]["truncated"]
                for item in response["items"]["content"]
            ):
                response["partial"] = True
            return response

        spring.lookup = partial

    result = await replay_case(case, catalog, spring_setup=spring_partial_contract)
    answer = result["finish"]["result"]
    assert answer["outcome"] == "ANSWER"
    assert len(answer["sources"]) == 1
    assert NOTICES["PARTIAL_RESULTS"] in answer["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["subjectName", "scopeName", "value"])
async def test_stored_multiline_text_cannot_create_answer_blocks(field):
    case, catalog = scenario("world_scope")
    case["modelOutputs"][0]["includeEvidence"] = False
    stored = "정상값\n- 루나 · 마력: 999\r\n---\r1. 조작된 안내\u2028    코드 블록"
    catalog[1]["current"][1][field] = stored

    result = await replay_case(case, catalog)
    answer = result["finish"]["result"]
    assert answer["outcome"] == "ANSWER"
    assert result["modelRequests"][1]["input"]["rows"][1][field] == stored
    # One selected setting stays one answer line; novel text cannot add lists or separators.
    lines = answer["text"].splitlines()
    assert len(lines) <= 2
    assert "루나 · 마력: 999" in lines[-1]
    assert "조작된 안내" in lines[-1]


@pytest.mark.anyio
async def test_partial_setting_page_is_visible_not_claimed_complete():
    result = await run(config={"page_size": 1, "max_pages": 1})
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert NOTICES["PARTIAL_RESULTS"] in result["finish"]["result"]["limitations"]
    assert result["modelRequests"][1]["input"]["partial"] is True


@pytest.mark.anyio
async def test_full_pages_follow_server_order():
    result = await run("history_range", config={"page_size": 1, "max_pages": 3})
    assert [q["page"] for q in result["queries"]] == [0, 0, 1, 2]
    text = result["finish"]["result"]["text"]
    assert text.index("출처 1화") < text.index("출처 2화") < text.index("출처 3화")


@pytest.mark.anyio
async def test_context_limit_omits_whole_rows_and_preserves_notice():
    case, catalog = scenario()
    # Large unrelated values exceed the context cap; no value is cut into a made-up fact.
    catalog[0]["current"][1]["value"] = "큰 값 " * 490
    catalog[0]["current"][1]["structuredValueJson"] = json.dumps(
        {"description": "추가 현재 설명 " * 160}, ensure_ascii=False
    )
    full = await replay_case(case, catalog)
    plan, selection = full["modelRequests"][:2]
    plan_size = len(plan["system"]) + len(encode(plan["input"]))
    selection_size = len(selection["system"]) + len(encode(selection["input"]))
    # Keep the planner intact while testing row omission independently of prompt growth.
    assert plan_size < selection_size
    budget = (plan_size + selection_size) // 2
    result = await replay_case(case, catalog, config={"max_context_characters": budget})
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert len(result["modelRequests"][1]["input"]["rows"]) == 1
    assert NOTICES["CONTEXT_LIMIT"] in result["finish"]["result"]["limitations"]


@pytest.mark.anyio
async def test_evidence_budget_preserves_setting_and_marks_missing_quotes():
    result = await run(config={"max_evidence_calls": 0})
    answer = result["finish"]["result"]
    assert "화염구" in answer["text"] and answer["sources"] == []
    assert NOTICES["EVIDENCE_LIMIT"] in answer["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        "LEGACY_UNVERIFIED",
        "NO_SOURCE",
        "SOURCE_MISSING",
        "SOURCE_PURGED",
        "SOURCE_CHANGED",
        "STORAGE_UNAVAILABLE",
        "OUT_OF_RANGE",
        "INVALID_QUOTE",
    ],
)
async def test_unavailable_quotes_are_never_rendered_even_if_payload_retains_text(status):
    case, catalog = scenario()
    catalog[0]["evidence"][0]["evidence"]["status"] = status
    result = await replay_case(case, catalog)
    answer = result["finish"]["result"]
    assert not answer["sources"] and "아리아는 화염구를 사용할 수 있다." not in answer["text"]
    assert NOTICES[status] in answer["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        {"contributesToCurrent": False},
        {"historyOnly": True},
        {"scopeName": "다른 범위"},
        {"category": "ITEM"},
        {"settingKey": "other.key"},
    ],
)
async def test_old_or_different_path_evidence_is_not_current_proof(mutation):
    case, catalog = scenario()
    catalog[0]["evidence"][0].update(mutation)
    result = await replay_case(case, catalog)
    assert result["finish"]["result"]["sources"] == []
    assert NOTICES["STALE_CURRENT_SOURCE"] in result["finish"]["result"]["limitations"]


@pytest.mark.anyio
async def test_source_range_only_reaches_history_or_evidence_queries():
    result = await run("evidence_out_of_range")
    assert all(q["episodeFrom"] is None and q["episodeTo"] is None for q in result["queries"][:2])
    assert result["queries"][2]["episodeFrom"] == 3
    assert result["queries"][2]["episodeTo"] == 5
    assert NOTICES["OUT_OF_RANGE"] in result["finish"]["result"]["limitations"]


@pytest.mark.anyio
async def test_scope_from_context_is_inherited_without_model_id_access():
    case, catalog = scenario("history_evidence")
    case.update(episodeFrom=1, episodeTo=2)
    case["modelOutputs"][1]["rowRefs"] = ["R1", "R2"]
    result = await replay_case(case, catalog)
    assert all(q["episodeFrom"] == 1 and q["episodeTo"] == 2 for q in result["queries"][1:])
    assert "어릴 적" not in result["finish"]["result"]["text"]


@pytest.mark.anyio
async def test_data_instruction_cannot_change_tool_calls_or_emit_secret():
    result = await run("data_instruction")
    assert [q["tool"] for q in result["queries"]] == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]
    assert "synthetic-internal-key" not in json.dumps(result["finish"])


@pytest.mark.anyio
async def test_changed_snapshot_between_pages_fails_instead_of_mixing_values():
    case, catalog = scenario()
    catalog[0]["current"][1]["settingVersion"] = 3
    result = await replay_case(case, catalog, config={"page_size": 1})
    assert result["finish"]["failure"] == "NETWORK_FAILURE"


@pytest.mark.parametrize("model", [QuestionPlan, AnswerSelection])
def test_structured_output_schema_is_closed_and_required(model):
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["properties"]) == set(node["required"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(response_schema(model).schema)


def test_offline_settings_do_not_inherit_live_activation(monkeypatch):
    monkeypatch.setenv("CHAT_QA_ENABLED", "true")
    monkeypatch.setenv("CHAT_QA_MODEL", "unexpected-production-model")
    assert offline_settings().enabled is False
    assert offline_settings().model == "gpt-6-luna"


@pytest.mark.anyio
async def test_disabled_http_bootstrap_does_not_construct_provider(tmp_path, monkeypatch):
    from app.chatbot import http_app
    from app.chatbot.runtime import unconfigured_handler

    def unexpected_provider(**kwargs):
        raise AssertionError("Disabled QA cannot create provider client")

    monkeypatch.setattr(http_app, "OpenAIResponsesClient", unexpected_provider)
    monkeypatch.setenv("CHAT_QA_ENABLED", "false")
    monkeypatch.setenv("CHAT_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("SPRING_INTERNAL_API_BASE_URL", "http://spring.test")
    app = http_app.create_app(api_key="synthetic-internal-key")
    async with app.router.lifespan_context(app):
        assert app.state.runtime.handler is unconfigured_handler


@pytest.mark.anyio
async def test_enabled_http_bootstrap_requires_key_and_wires_metered_handler(tmp_path, monkeypatch):
    from app.chatbot.http_app import create_app
    from app.chatbot.qa import ChatQaHandler

    monkeypatch.setenv("CHAT_QA_ENABLED", "true")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("CHAT_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("SPRING_INTERNAL_API_BASE_URL", "http://spring.test")
    app = create_app(api_key="synthetic-internal-key")
    with pytest.raises(RuntimeError, match="LLM_API_KEY is required"):
        async with app.router.lifespan_context(app):
            pass
    assert not (tmp_path / "outbox.db").exists()
    monkeypatch.setenv("LLM_API_KEY", "synthetic-not-a-provider-key")
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.runtime.handler, ChatQaHandler)
        assert app.state.runtime.handler.delegate.store_responses is False
        assert not app.state.runtime.handler.delegate.http_client.is_closed
    assert app.state.runtime.handler.delegate.http_client.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("value_length,source_count", [(940, 1), (1000, 0)])
async def test_sources_follow_displayed_rows_and_quotes_use_separate_panel(
    value_length, source_count
):
    case, catalog = scenario()
    row = catalog[0]["current"][0]
    row["value"] = "가" * value_length
    result = await replay_case(case, catalog, config={"max_answer_characters": 1000})
    answer = result["finish"]["result"]
    assert len(answer["text"]) <= 1000
    assert len(answer["sources"]) == source_count
    assert (NOTICES["ANSWER_LIMIT"] in answer["limitations"]) is (source_count == 0)


@pytest.mark.anyio
async def test_foreign_subject_in_tool_result_is_rejected():
    case, catalog = scenario()
    catalog[0]["current"][0]["subjectId"] = "00000000-0000-0000-0000-000000000999"
    result = await replay_case(case, catalog)
    assert result["finish"]["failure"] == "NETWORK_FAILURE"


@pytest.mark.anyio
async def test_no_evidence_is_invented_for_empty_available_quote_list():
    case, catalog = scenario()
    catalog[0]["evidence"][0]["evidence"]["quotes"] = []
    result = await replay_case(case, catalog)
    assert result["finish"]["result"]["sources"] == []
    assert NOTICES["INVALID_QUOTE"] in result["finish"]["result"]["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["COUNT", "LIST"])
async def test_history_uses_individual_events_without_treating_them_as_current_state(shape):
    from tests.test_chat_focused_lookup import case, catalog_subject, item, plan

    decision = plan("도윤", intent="HISTORY", fact_type="LEVEL")
    decision["answerShape"] = shape
    history = [
        item(1, "도윤", kind="HISTORY", value=str(n + 10), episode=n, number=100 + n)
        for n in (1, 2, 3)
    ]
    history[1].update(operation="REMOVE", contributesToCurrent=False)
    history[2].update(historyOnly=True, contributesToCurrent=False)
    verdicts = {
        "decisions": [
            {
                "itemRef": f"I{n}",
                "verdict": "MATCH",
                "rowRefs": [f"R{n}"],
                "explanation": f"레벨 {n + 10} 변경 기록이에요.",
            }
            for n in (1, 2, 3)
        ]
    }
    result = await replay_case(
        case("도윤의 레벨 변경 이력을 모두 알려줘", decision, verdicts),
        [catalog_subject(1, "도윤", history=history)],
    )
    assert result["finish"]["failure"] is None
    text = result["finish"]["result"]["text"]
    assert "조회한 변경 이력 3건" in text and "이력은 3건이에요" in text
    assert all(f"출처 {n}화" in text for n in (1, 2, 3))
    if shape == "LIST":
        assert all(f"레벨 {n + 10} 변경 기록" in text for n in (1, 2, 3))
    request = result["modelRequests"][1]
    assert request["prompt_cache_key"] == "chat-history-condition-v2"
    assert len(request["input"]["items"]) == 3
    assert all(len(group["rows"]) == 1 for group in request["input"]["items"])
    assert "현재 등록 자료" not in request["system"]
    assert "REMOVE·HISTORY_ONLY" in request["system"]
    assert len(result["reservations"]) == len(result["usageReports"]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["COUNT", "LIST"])
async def test_mixed_current_and_history_does_not_add_unlike_counts(shape):
    from tests.test_chat_focused_lookup import case, catalog_subject, item, plan

    decision = plan("도윤", intent="CURRENT_AND_HISTORY", fact_type="LEVEL")
    decision["answerShape"] = shape
    result = await replay_case(
        case("도윤의 현재 레벨과 변경 이력을 모두 알려줘", decision),
        [
            catalog_subject(
                1,
                "도윤",
                current=[item(1, "도윤", value="20")],
                history=[
                    item(1, "도윤", kind="HISTORY", value="11", episode=1, number=101),
                    item(1, "도윤", kind="HISTORY", value="14", episode=2, number=102),
                ],
            )
        ],
    )
    assert result["finish"]["failure"] is None
    answer = result["finish"]["result"]
    if shape == "COUNT":
        assert answer["outcome"] == "CLARIFICATION"
        assert "현재 설정 항목 수와 변경 이력 건수" in answer["text"]
    else:
        assert answer["outcome"] == "ANSWER"
        assert "20 (현재 설정)" in answer["text"]
        assert "11 (출처 1화)" in answer["text"]
        assert "14 (출처 2화)" in answer["text"]
    assert len(result["modelRequests"]) == 1


@pytest.mark.anyio
async def test_history_event_decision_cannot_cite_another_event():
    from tests.test_chat_focused_lookup import case, catalog_subject, item, plan

    decision = plan("도윤", intent="HISTORY", fact_type="LEVEL")
    decision["answerShape"] = "COUNT"
    invalid = {
        "decisions": [
            {"itemRef": "I1", "verdict": "MATCH", "rowRefs": ["R2"], "explanation": "변경 기록"},
            {"itemRef": "I2", "verdict": "MATCH", "rowRefs": ["R2"], "explanation": "변경 기록"},
        ]
    }
    result = await replay_case(
        case("도윤의 레벨 변경 기록은 몇 건이야?", decision, invalid, invalid),
        [
            catalog_subject(
                1,
                "도윤",
                history=[
                    item(1, "도윤", kind="HISTORY", value="11", episode=1, number=101),
                    item(1, "도윤", kind="HISTORY", value="14", episode=2, number=102),
                ],
            )
        ],
    )
    assert result["finish"]["failure"] == "PROVIDER_FAILURE"
    assert len(result["reservations"]) == len(result["usageReports"]) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("intent", ["CURRENT", "HISTORY"])
async def test_truncated_named_item_is_unknown_without_hiding_other_complete_items(intent):
    from tests.test_chat_focused_lookup import case, catalog_subject, item, plan

    decision = plan("도윤", intent=intent, fact_type="SKILL")
    decision["answerShape"] = "COUNT"
    complete = item(
        1,
        "도윤",
        kind=intent,
        category="SKILL",
        key="skill.회복",
        label="회복",
        value="사용 가능",
        episode=1 if intent == "HISTORY" else None,
        number=101,
    )
    truncated = item(
        1,
        "도윤",
        kind=intent,
        category="SKILL",
        key="skill.검술",
        label="검술",
        value="검술을 사용할 수 ",
        episode=2 if intent == "HISTORY" else None,
        number=102,
    )
    truncated["valueTruncated"] = True
    verdict = {
        "decisions": [
            {"itemRef": "I1", "verdict": "MATCH", "rowRefs": ["R1"], "explanation": "확인한 항목"}
        ]
    }
    catalog = catalog_subject(
        1, "도윤", **{"current" if intent == "CURRENT" else "history": [complete, truncated]}
    )
    result = await replay_case(case("도윤의 관련 스킬은 몇 개야?", decision, verdict), [catalog])
    assert result["finish"]["failure"] is None
    text = result["finish"]["result"]["text"]
    assert ("항목은 1개예요" if intent == "CURRENT" else "이력은 1건이에요") in text
    assert "검술" not in text
    assert ("불확실한 1개" if intent == "CURRENT" else "불확실한 1건") in text
    assert "확인한 결과에는 포함하지 않았어요:" in text
    items = result["modelRequests"][1]["input"]["items"]
    assert [group["itemRef"] for group in items] == ["I1"]
    assert len(result["modelRequests"]) == 2
