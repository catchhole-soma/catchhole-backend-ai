"""Three independent provenance axes retain source scope and omit model-visible IDs."""

import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.chatbot.qa import _Session
from app.chatbot.qa_render import AnswerRow, provenance_payload, render_answer
from app.chatbot.qa_synthesis import row_data
from app.schemas.chat_qa import QuestionPlan
from app.schemas.setting_tools import SettingToolItem
from tests.test_chat_current_authority import current_case
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay


def source(
    number=800,
    *,
    kind="EPISODE",
    edited=False,
    action="USER_CONFIRM",
    recorded="2026-10-22T16:08:04",
):
    return {
        "reference": {"tool": "CHARACTER_EVIDENCE", "targetId": str(UUID(int=number))},
        "sourceKind": kind,
        "contentEdited": edited,
        "applicationAction": action,
        "recordedAt": recorded,
    }


def row(*sources):
    value = item(701, "세린", category="PROFILE", key="profile.gender", label="성별", value="여자")
    # Legacy aggregate origin must not erase the more precise three independent axes.
    value["origin"] = "USER_INPUT"
    value["sources"] = [s["reference"] for s in sources]
    value["provenance"] = {"sources": list(sources), "sourcesTruncated": False}
    return value


@pytest.mark.parametrize(
    "field", ["sourceKind", "contentEdited", "applicationAction", "recordedAt"]
)
def test_present_provenance_requires_each_axis_without_defaulting_unknown(field):
    value = row(source())
    del value["provenance"]["sources"][0][field]
    with pytest.raises(ValidationError):
        SettingToolItem.model_validate(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("contentEdited", "false"),
        ("sourceKind", "MANUAL_CONFIRM"),
        ("applicationAction", "UPDATE"),
        ("recordedAt", "2026-02-30T12:00:00"),
        ("recordedAt", "2026-10-22T16:08:04Z"),
    ],
)
def test_provenance_rejects_ambiguous_types_and_timestamp_meaning(field, value):
    metadata = source()
    metadata[field] = value
    with pytest.raises(ValidationError):
        SettingToolItem.model_validate(row(metadata))


def test_old_server_response_can_omit_optional_provenance_without_changing_roundtrip():
    value = row(source())
    del value["provenance"]
    decoded = SettingToolItem.model_validate(value)
    assert decoded.model_dump(by_alias=True, mode="json") == value


def test_provenance_is_bound_to_the_same_ordered_source_references_and_limit():
    value = row(source(), source(801, kind="SETTING_BOOK"))
    for mutation in ("reference", "limit"):
        broken = copy.deepcopy(value)
        if mutation == "reference":
            broken["provenance"]["sources"].reverse()
        else:
            broken["provenance"]["sourcesTruncated"] = True
        with pytest.raises(ValidationError, match="Provenance must match"):
            SettingToolItem.model_validate(broken)


def test_user_confirmation_does_not_become_content_edit_or_direct_input():
    value = SettingToolItem.model_validate(row(source()))
    text = render_answer([AnswerRow("R1", value)], [], direct_only=False, maximum=12000).result.text
    assert "현재 성별은 여자" in text
    for unrelated in ("원고", "내용 수정", "확정", "직접 입력", "2026-10-22", "기록 생성"):
        assert unrelated not in text


def test_mixed_current_sources_preserve_automatic_book_edit_and_unknown_axes():
    value = SettingToolItem.model_validate(
        row(
            source(action="AUTOMATIC_CONFIRM"),
            source(801, kind="SETTING_BOOK", edited=True),
            source(802, kind="UNKNOWN", edited=None, action="UNKNOWN", recorded=None),
        )
    )
    sources = provenance_payload(value)["provenance"]["sources"]
    assert [(s["sourceKind"], s["contentEdited"], s["applicationAction"]) for s in sources] == [
        ("EPISODE", False, "AUTOMATIC_CONFIRM"),
        ("SETTING_BOOK", True, "USER_CONFIRM"),
        ("UNKNOWN", None, "UNKNOWN"),
    ]
    assert sources[-1]["recordedAt"] is None


def test_many_values_do_not_repeat_processing_metadata_or_exhaust_display_budget():
    base = row(
        source(edited=None, action="AUTOMATIC_CONFIRM", recorded="2026-10-22T16:08:04.123456")
    )
    rows = [
        AnswerRow(
            f"R{n}",
            SettingToolItem.model_validate({**base, "displayName": f"항목{n}", "value": str(n)}),
        )
        for n in range(1, 31)
    ]
    result = render_answer(rows, [], direct_only=False, maximum=1000).result
    assert "항목30: 30" in result.text
    assert not result.limitations
    for unrelated in ("내용 수정", "자동 확정", "기록 생성", "123456"):
        assert unrelated not in result.text


def test_explicit_processing_question_uses_registered_synthesis_metadata(tmp_path):
    case = current_case()
    case["question"] = "현재 세린의 성별은 어디서 왔고, 내용 수정과 사용자 확정은 어떻게 달라?"
    case["outputs"][0]["answerShape"] = "SUMMARY"
    explanation = (
        "원고에서 나온 값을 내용 변경 없이 사용자가 확정한 기록이에요. 수정 시각은 확인되지 않아요."
    )
    case["outputs"].append({"claims": [{"text": explanation, "rowRefs": ["R1"]}]})

    def setup(spring):
        spring.catalog = [catalog_subject(701, "세린", current=[row(source())])]

    result = asyncio.run(replay(tmp_path, case, spring_setup=setup))
    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["text"] == explanation
    request = result["raw"].requests[-1]
    assert request["prompt_cache_key"] == "chat-registered-answer-v4"
    metadata = request["input"]["rows"][0]["provenance"]["sources"][0]
    assert metadata["contentEdited"] is False and metadata["applicationAction"] == "USER_CONFIRM"
    assert not result["search"].calls
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2


def test_model_payload_preserves_metadata_without_fact_ids_or_source_authority():
    value = SettingToolItem.model_validate(row(source(), source(801, kind="SETTING_BOOK")))
    answer_row = AnswerRow("R1", value)
    session = SimpleNamespace(
        conversation_payload=dict,
        codes=[],
        cfg=SimpleNamespace(max_selected_rows=10),
        rows=[answer_row],
    )
    plan = QuestionPlan(
        intent="CURRENT",
        subjects=[{"domain": "CHARACTER", "name": "세린"}],
        include_evidence=False,
        episode_from=None,
        episode_to=None,
    )
    selected_payload = _Session.payload(session, "세린의 성별은?", plan)["rows"][0]
    for payload in (selected_payload, row_data(answer_row)):
        data = json.dumps(payload)
        assert "targetId" not in data and "reference" not in data
        assert str(UUID(int=800)) not in data and str(UUID(int=801)) not in data
        assert payload["provenance"]["sources"][0]["applicationAction"] == "USER_CONFIRM"
        assert payload["provenance"]["sources"][0]["contentEdited"] is False
        assert payload["provenance"]["sources"][1]["sourceKind"] == "SETTING_BOOK"
