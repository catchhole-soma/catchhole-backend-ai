"""Offline answer-boundary and provenance regressions, not model-quality scores."""

import socket

import pytest

from app.chatbot.qa_render import AnswerRow, NOTICES, render_answer
from app.chatbot.replay import replay_case
from app.schemas.setting_tools import SettingToolItem, SourceReference
from tests.test_chat_focused_lookup import item, uid
from tests.test_chat_qa import scenario


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Rendering regressions must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


async def registered_summary(claims, *, include_evidence=True):
    case, catalog = scenario()
    decision = case["modelOutputs"][0]
    decision["subjects"][0]["focus"] = {"factType": "SKILL", "attribute": None}
    decision["answerShape"] = "SUMMARY"
    decision["includeEvidence"] = include_evidence
    case["modelOutputs"] = [decision, {"claims": claims}]
    result = await replay_case(case, catalog, config={"max_answer_characters": 1000})
    assert result["finish"]["failure"] is None
    assert len(result["modelRequests"]) == 2
    assert [entry["purpose"] for entry in result["reservations"]] == [
        "CHAT_INTERPRETATION",
        "CHAT_ANSWER",
    ]
    assert len(result["usageReports"]) == 2
    assert all(entry["kind"] == "SETTLE" for entry in result["usageReports"])
    # Formatting fallback still passes through the current-value revalidation.
    assert sum(query["tool"] == "CHARACTER_CURRENT" for query in result["queries"]) == 2
    return result["finish"]["result"]


@pytest.mark.anyio
async def test_first_claim_overflow_returns_bounded_guidance_without_orphaned_sources():
    result = await registered_summary([{"text": "가" * 1000, "rowRefs": ["R1"]}])
    assert result["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert 0 < len(result["text"]) <= 1000
    assert "답변 한도" in result["text"]
    assert "가" * 50 not in result["text"] and "근거 1" not in result["text"]
    assert result["sources"] == []
    assert NOTICES["ANSWER_LIMIT"] in result["limitations"]


@pytest.mark.anyio
@pytest.mark.parametrize("include_evidence", [False, True])
async def test_exactly_fitting_claim_and_citation_are_not_dropped(include_evidence):
    suffix = " 근거 1" if include_evidence else ""
    text = "가" * (1000 - len(suffix))
    result = await registered_summary(
        [{"text": text, "rowRefs": ["R1"]}], include_evidence=include_evidence
    )
    assert result["outcome"] == "ANSWER"
    assert result["text"] == text + suffix
    assert len(result["sources"]) == int(include_evidence)
    assert NOTICES["ANSWER_LIMIT"] not in result["limitations"]


@pytest.mark.anyio
async def test_later_claim_overflow_preserves_the_rendered_claim_and_its_source():
    text = "아리아는 화염구를 사용할 수 있어요."
    result = await registered_summary(
        [{"text": text, "rowRefs": ["R1"]}, {"text": "가" * 1000, "rowRefs": ["R1"]}]
    )
    assert result["outcome"] == "ANSWER"
    assert result["text"] == text + " 근거 1"
    assert len(result["sources"]) == 1
    assert NOTICES["ANSWER_LIMIT"] in result["limitations"]


def answer_row(origin, *, kind="CURRENT", value="7", number=1):
    data = item(number, "하린", kind=kind, value=value, episode=3 if kind == "HISTORY" else None)
    data["origin"] = origin
    return AnswerRow(f"R{number}", SettingToolItem.model_validate(data))


@pytest.mark.parametrize("origin", ["UNKNOWN", "USER_INPUT", "CONFIRMED_ANALYSIS"])
@pytest.mark.parametrize("shape", ["scalar", "list", "description", "history"])
def test_origin_remains_distinct_in_scalar_and_list_rendering(origin, shape):
    row = answer_row(
        origin,
        kind="HISTORY" if shape == "history" else "CURRENT",
        value="주변의 기운을 감지해 동료에게 알려 줍니다." if shape == "description" else "7",
    )
    rows = [row]
    if shape == "list":
        rows.append(answer_row("CONFIRMED_ANALYSIS", value="9", number=2))
    result = render_answer(rows, [], direct_only=False, maximum=1000).result
    assert result.outcome == "ANSWER"
    assert row.item.value in result.text
    assert ("출처는 확인되지 않았어요" in result.text) is (
        origin == "UNKNOWN" and shape == "scalar"
    )
    assert ("출처 미상" in result.text) is (origin == "UNKNOWN" and shape != "scalar")
    assert ("직접 입력" in result.text) is (origin == "USER_INPUT")
    assert not any(code in result.text for code in ("UNKNOWN", "USER_INPUT", "CONFIRMED_ANALYSIS"))
    assert result.limitations == []
    if shape == "history":
        assert "출처 3화" in result.text


@pytest.mark.parametrize("cited", [False, True])
def test_unknown_origin_does_not_replace_evidence_state_or_citation(cited):
    row = answer_row("UNKNOWN")
    row.evidence_requested = True
    reference = SourceReference(tool="CHARACTER_EVIDENCE", target_id=uid(900))
    if cited:
        row.evidence = [(reference, row.item)]
    result = render_answer([row], [], direct_only=True, maximum=1000).result
    assert "이 설정의 출처는 확인되지 않았어요." in result.text
    assert result.outcome == ("ANSWER" if cited else "INSUFFICIENT_EVIDENCE")
    assert result.sources == ([reference] if cited else [])
    assert ("근거 1" in result.text) is cited
    assert ("연결된 원문은 확인하지 못했어요." in result.text) is not cited
    assert "직접 입력" not in result.text


def test_origin_annotation_cannot_bypass_the_answer_length_limit():
    row = answer_row("UNKNOWN", value="가" * 970)
    result = render_answer([row], [], direct_only=False, maximum=1000).result
    assert result.outcome == "INSUFFICIENT_EVIDENCE"
    assert len(result.text) <= 1000
    assert result.sources == []
    assert NOTICES["ANSWER_LIMIT"] in result.limitations
