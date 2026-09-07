import json

import pytest

from evals.multi_stage_setting.report_cli import (
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize(
    ("status", "method", "explanation"),
    [
        (
            "MATCH",
            "ALIAS",
            "정답지에 등록된 별칭으로 같은 설정 항목임을 인정했습니다.",
        ),
        (
            "MATCH",
            "SEMANTIC",
            "이름은 다르지만 문맥상 같은 설정 항목으로 판단했습니다.",
        ),
        ("MISMATCH", "SEMANTIC", "문맥상 다른 설정 항목으로 판단했습니다."),
        ("PENDING", "UNRESOLVED", "설정명의 의미를 확인하지 못했습니다."),
    ],
)
def test_public_setting_name_match_explains_method_without_private_reason(
    stage: str,
    status: str,
    method: str,
    explanation: str,
) -> None:
    case = _case(stage, status, method)
    report = _report(stage, case)
    original = json.dumps(report, sort_keys=True)

    diagnostics = build_public_diagnostics(report)
    public_case = (
        diagnostics[0]["stage1"]["world"]["cases"][0]
        if stage == "stage1"
        else diagnostics[0]["stage2"][0]
    )
    markdown = render_markdown_summary(report)

    assert public_case["settingNameMatch"] == {"status": status, "method": method}
    assert explanation in markdown
    assert "SECRET_" not in json.dumps(diagnostics)
    assert "SECRET_" not in markdown
    assert "settingNameMatch" not in json.dumps(build_source_free_summary(report))
    assert json.dumps(report, sort_keys=True) == original
    if status == "MATCH":
        assert "설정 범위·세부 항목 불일치" not in markdown
        assert "반영할 범위·설정명 불일치" not in markdown
        assert "문맥상 다른 설정 항목으로 판단했습니다." not in markdown


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_public_exact_setting_name_match_keeps_existing_success_explanation(stage: str) -> None:
    case = _case(stage, "MATCH", "EXACT")
    case["actual"]["path"] = case["expected"]["path"]

    markdown = render_markdown_summary(_report(stage, case))

    assert "채점한 항목이 모두 답지와 일치합니다." in markdown
    assert "이름은 다르지만" not in markdown
    assert "SECRET_" not in markdown


def test_public_partial_match_explains_accepted_name_and_actual_value_error() -> None:
    case = _case("stage1", "MATCH", "SEMANTIC")
    case["result"] = "PARTIAL_MATCH"
    case["fields"]["value"] = "MISMATCH"
    case["actual"]["value"] = "다른 내용"

    markdown = render_markdown_summary(_report("stage1", case))

    assert "이름은 다르지만 문맥상 같은 설정 항목으로 판단했습니다." in markdown
    assert "설정값 불일치" in markdown
    assert "설정 범위·세부 항목 불일치" not in markdown


def test_existing_property_name_judgment_is_explained_without_private_reason() -> None:
    case = _case("stage2", "MATCH", "EXACT")
    case["matchedPropertyNameMatch"] = {
        "status": "MATCH", "method": "SEMANTIC", "reason": "SECRET_MANUSCRIPT"
    }
    report = _report("stage2", case)
    public_case = build_public_diagnostics(report)[0]["stage2"][0]
    assert public_case["matchedPropertyNameMatch"] == {"status": "MATCH", "method": "SEMANTIC"}
    markdown = render_markdown_summary(report)
    assert "비교 대상인 기존 설정명: 이름은 다르지만 문맥상 같은 설정 항목" in markdown
    assert "SECRET_" not in markdown


@pytest.mark.parametrize(
    ("status", "method", "explanation"),
    [
        ("MISMATCH", "SEMANTIC", "문맥상 다른 설정 항목으로 판단했습니다."),
        ("PENDING", "UNRESOLVED", "설정명의 의미를 확인하지 못했습니다."),
        (
            "MATCH",
            "ALIAS",
            "정답지에 등록된 별칭으로 같은 설정 항목임을 인정했습니다.",
        ),
    ],
)
def test_upstream_blocked_reason_includes_source_setting_name_judgment(
    status: str,
    method: str,
    explanation: str,
) -> None:
    source = _case("stage1", status, method)
    source["result"] = "PARTIAL_MATCH"
    source["fields"]["value"] = "MISMATCH"
    blocked = _case("stage2", "MATCH", "EXACT")
    blocked.update(
        result="UPSTREAM_BLOCKED",
        upstreamOutcome="UPSTREAM_VALUE_ERROR",
        actual=None,
        fields={},
    )
    blocked.pop("settingNameMatch")
    report = _report("stage1", source)
    report["scenarios"][0]["stage2"] = [blocked]

    markdown = render_markdown_summary(report)
    stage2_markdown = markdown.split("**2차 처리 판단 결과**", 1)[1]

    assert explanation in stage2_markdown
    assert "따라서 이 항목의 2차 판단은 채점에서 제외했습니다." in stage2_markdown
    assert "SECRET_" not in markdown


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_public_setting_name_diagnostics_escape_names_and_drop_unknown_metadata(stage: str) -> None:
    case = _case(stage, "MATCH", "SEMANTIC")
    case["actual"]["path"] = "전투 특성 › <script>이름</script> | ![link](secret)"
    report = _report(stage, case)

    markdown = render_markdown_summary(report)

    assert "<script>" not in markdown
    assert "&lt;script&gt;이름&lt;/script&gt;" in markdown
    assert "&#124;" in markdown
    assert "![link]" not in markdown
    assert "SECRET_" not in markdown

    case["settingNameMatch"]["method"] = "<script>SECRET_METHOD</script>"
    diagnostics = build_public_diagnostics(report)
    public_case = (
        diagnostics[0]["stage1"]["world"]["cases"][0]
        if stage == "stage1"
        else diagnostics[0]["stage2"][0]
    )
    assert "settingNameMatch" not in public_case


def _case(stage: str, status: str, method: str) -> dict:
    name_match = {
        "status": status,
        "method": method,
        "reason": "SECRET_PRIVATE_REASON: 원문 근거를 직접 인용한 설명",
        "quote": "SECRET_ORIGINAL_QUOTE",
        "rawResponse": {"text": "SECRET_RAW_RESPONSE"},
    }
    expected = {"subject": "RACE · 가상 종족", "path": "전투 특성 › 체질", "value": "동일 내용"}
    actual = {"subject": "RACE · 가상 종족", "path": "전투 특성 › 신체 특성", "value": "동일 내용"}
    if stage == "stage1":
        return {
            "result": "FULL_MATCH" if status == "MATCH" else "PARTIAL_MATCH",
            "goldIds": ["W1"],
            "predictionId": "P1",
            "expected": expected,
            "actual": actual,
            "fields": {"subject": "MATCH", "path": status, "value": "MATCH"},
            "settingNameMatch": name_match,
        }
    expected["operation"] = actual["operation"] = "ADD"
    return {
        "result": {
            "MATCH": "FULL_MATCH",
            "MISMATCH": "DECISION_MISMATCH",
            "PENDING": "SEMANTIC_PENDING",
        }[status],
        "decisionId": "D1",
        "domain": "WORLD",
        "sourceGoldIds": ["W1"],
        "sourceCandidateId": "P1",
        "expected": expected,
        "actual": actual,
        "fields": {"operation": "MATCH", "value": "MATCH", "proposedPath": status},
        "settingNameMatch": name_match,
    }


def _report(stage: str, case: dict) -> dict:
    return {
        "scenarios": [
            {
                "scenarioId": "S1",
                "episodeNo": 1,
                "stage1": {"WORLD": {"cases": [case]}} if stage == "stage1" else {},
                "stage2": [case] if stage == "stage2" else [],
            }
        ],
    }
