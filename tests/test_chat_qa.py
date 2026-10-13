import copy
import json
import socket
from pathlib import Path

import pytest

from app.chatbot.qa import response_schema
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
    catalog[0]["current"][1]["value"] = "큰 값 " * 400
    result = await replay_case(case, catalog, config={"max_context_characters": 3000})
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
    assert offline_settings().model == "gpt-5.6-terra"


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
async def test_answer_cap_never_returns_a_source_whose_quote_was_omitted():
    case, catalog = scenario()
    row = catalog[0]["current"][0]
    row["value"] = "가" * 940
    result = await replay_case(case, catalog, config={"max_answer_characters": 1000})
    answer = result["finish"]["result"]
    assert len(answer["text"]) <= 1000
    assert answer["sources"] == []
    assert NOTICES["ANSWER_LIMIT"] in answer["limitations"]


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
