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


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize(
    ("status", "explanation"),
    [
        ("MATCH", "상위 범위 표현은 다르지만 같은 설정을 묶는 범위로 인정했습니다."),
        ("PENDING", "상위 범위: 답지와 의미가 같은지 확인하지 못했습니다."),
        ("MISMATCH", "상위 범위 불일치"),
    ],
)
def test_accepted_item_and_scope_judgment_have_separate_public_explanations(
    stage: str,
    status: str,
    explanation: str,
) -> None:
    case = _case(stage, "MATCH", "SEMANTIC")
    case["expected"]["path"] = "전투 위험성"
    case["actual"]["path"] = "게임 규칙 › 전투 난이도 규칙"
    field = "path" if stage == "stage1" else "proposedPath"
    case["fields"][field] = status
    if status != "MATCH":
        case["result"] = (
            "PARTIAL_MATCH"
            if stage == "stage1"
            else ("SEMANTIC_PENDING" if status == "PENDING" else "DECISION_MISMATCH")
        )
    case["scopeReason"] = "SECRET_SCOPE_REASON"
    report = _report(stage, case)
    public_before = build_public_diagnostics(report)

    markdown = render_markdown_summary(report)

    assert "이름은 다르지만 문맥상 같은 설정 항목으로 판단했습니다." in markdown
    assert explanation in markdown
    assert "답지: 상위 범위 없음 / 모델: 게임 규칙" in markdown
    assert "반영할 범위·설정명 불일치" not in markdown
    assert "설정 범위·세부 항목 불일치" not in markdown
    assert "SECRET_" not in markdown
    assert build_public_diagnostics(report) == public_before
    assert "scopeReason" not in json.dumps(public_before)


@pytest.mark.parametrize("domain", ["WORLD", "CHARACTER"])
def test_partial_match_with_pending_value_explains_unfinished_scoring(domain: str) -> None:
    case = _case("stage1", "MATCH", "EXACT")
    case["result"] = "PARTIAL_MATCH"
    case["fields"]["value"] = "PENDING"
    case["actual"]["path"] = case["expected"]["path"]
    report = _report("stage1", case)
    report["scenarios"][0]["stage1"] = {domain: {"cases": [case]}}

    markdown = render_markdown_summary(report)

    assert "의미 판정이 끝나지 않은 항목이 있어 채점을 완료하지 못했습니다." in markdown
    assert "설정값: 답지와 의미가 같은지 확인하지 못했습니다." in markdown
    assert "설정값 불일치" not in markdown


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_character_dynamic_status_names_explain_contextual_match_without_new_metadata(
    stage: str,
) -> None:
    report = _character_status_report(stage, "status.injured_right_arm")
    public_before = build_public_diagnostics(report)

    markdown = render_markdown_summary(report)

    assert "이름은 다르지만 문맥상 같은 상태 항목으로 판단했습니다." in markdown
    assert "SECRET_" not in markdown
    assert "settingNameMatch" not in json.dumps(public_before)
    assert build_public_diagnostics(report) == public_before


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize("actual_key", [
    "status.right_arm_injury", " STATUS . right_arm_injury ",
    "status.right arm injury", "profile.right_arm_injury", "status.*",
])
def test_character_status_explanation_requires_different_normalized_status_keys(
    stage: str, actual_key: str,
) -> None:
    markdown = render_markdown_summary(_character_status_report(stage, actual_key))

    assert "문맥상 같은 상태 항목" not in markdown


@pytest.mark.parametrize("status", ["PENDING", "MISMATCH"])
def test_character_status_explanation_does_not_claim_an_unaccepted_name(status: str) -> None:
    report = _character_status_report("stage2", "status.injured_right_arm")
    case = report["scenarios"][0]["stage2"][0]
    case["fields"]["canonicalPath"] = status
    case["result"] = "SEMANTIC_PENDING" if status == "PENDING" else "DECISION_MISMATCH"

    markdown = render_markdown_summary(report)

    assert "문맥상 같은 상태 항목" not in markdown


def _character_status_report(stage: str, actual_key: str) -> dict:
    case = _case(stage, "MATCH", "EXACT")
    case.pop("settingNameMatch")
    prefix = "STATUS › " if stage == "stage1" else ""
    case["expected"]["path"] = prefix + "status.right_arm_injury"
    case["actual"]["path"] = prefix + actual_key
    case["expected"]["subject"] = case["actual"]["subject"] = "비요른"
    case["reason"] = "SECRET_PRIVATE_REASON"
    if stage == "stage1":
        report = _report(stage, case)
        report["scenarios"][0]["stage1"] = {"CHARACTER": {"cases": [case]}}
        return report
    case["domain"] = "CHARACTER"
    case["fields"]["canonicalPath"] = case["fields"].pop("proposedPath")
    return _report(stage, case)


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
