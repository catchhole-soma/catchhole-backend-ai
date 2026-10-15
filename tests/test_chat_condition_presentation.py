"""Condition-result presentation keeps uncertainty distinct from confirmed members."""

from types import SimpleNamespace

import pytest

from app.chatbot.qa_render import AnswerRow
from app.chatbot.qa_synthesis import ConditionDecision, RegisteredSynthesis
from app.schemas.setting_tools import SettingToolItem, SourceReference
from tests.test_chat_focused_lookup import item, uid


def render(verdicts, *, missing=0, unreviewed=0, codes=(), shape="LIST"):
    session = SimpleNamespace(cfg=SimpleNamespace(max_answer_characters=12000), codes=list(codes))
    groups, decisions = {}, []
    for index, verdict in enumerate(verdicts, 1):
        row = AnswerRow(
            f"R{index}",
            SettingToolItem.model_validate(
                item(index, f"대상{index:02}", category="PROFILE", value="조건 관련 정보")
            ),
        )
        row.evidence = [
            (SourceReference(tool="CHARACTER_EVIDENCE", target_id=uid(1000 + index)), row.item)
        ]
        groups[f"I{index}"] = [row]
        decisions.append(
            ConditionDecision(
                item_ref=f"I{index}",
                verdict=verdict,
                row_refs=[row.ref],
                explanation="조건 충족을 확인했어요."
                if verdict == "MATCH"
                else "판단할 자료가 부족해요.",
            )
        )
    result = RegisteredSynthesis.render_condition(
        session,
        groups,
        decisions,
        "현재 등록된 인물",
        len(verdicts) + missing + unreviewed,
        missing=missing,
        shape=shape,
    )
    return result.result


@pytest.mark.parametrize("shape", ["LIST", "COUNT"])
def test_one_match_is_not_buried_under_unknown_names_or_their_quotes(shape):
    result = render(["UNKNOWN"] * 18 + ["MATCH"], shape=shape)
    assert "확인한 인물은 1명" in result.text
    assert "대상19" in result.text
    assert all(f"대상{index:02}" not in result.text for index in range(1, 19))
    assert "조건 충족 여부가 불확실한 18명" in result.text
    assert result.text.index("대상19") < result.text.index("불확실한 18명")
    assert result.sources == [SourceReference(tool="CHARACTER_EVIDENCE", target_id=uid(1019))]
    assert "근거 1" in result.text and "근거 19" not in result.text


def test_all_unknown_is_not_reported_as_no_matching_people():
    result = render(["UNKNOWN", "UNKNOWN"])
    assert "아직 확인하지 못했어요" in result.text
    assert "조건 충족 여부가 불확실한 2명" in result.text
    assert "0명" not in result.text and "인물이 없어요" not in result.text
    assert result.sources == []


def test_complete_negative_result_is_scoped_to_registered_population():
    result = render(["NO_MATCH", "NO_MATCH"])
    assert "현재 등록된 인물 2명에서는" in result.text
    assert "조건에 맞는 인물이 없어요" in result.text
    assert "아직 확인하지 못했어요" not in result.text


@pytest.mark.parametrize("unknown_kind", ["missing", "unreviewed", "partial"])
def test_incomplete_population_never_becomes_a_complete_negative_result(unknown_kind):
    options = {
        "missing": {"missing": 1},
        "unreviewed": {"unreviewed": 1},
        "partial": {"codes": ["PARTIAL_RESULTS"]},
    }[unknown_kind]
    result = render(["NO_MATCH"], **options)
    assert "아직 확인하지 못했어요" in result.text
    assert "인물이 없어요" not in result.text
    if unknown_kind == "missing":
        assert "관련 설정이 없는 1명" in result.text
    elif unknown_kind == "unreviewed":
        assert "아직 검토하지 못한 1명" in result.text
    else:
        assert "조회가 끝나지 않아" in result.text


def test_scope_counts_distinguish_missing_unknown_and_unreviewed():
    result = render(["MATCH", "UNKNOWN", "NO_MATCH"], missing=2, unreviewed=3)
    assert "확인한 인물은 1명" in result.text
    assert "관련 설정이 없는 2명" in result.text
    assert "조건 충족 여부가 불확실한 1명" in result.text
    assert "아직 검토하지 못한 3명" in result.text
