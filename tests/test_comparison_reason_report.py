"""Synthetic report data only; no private manuscript or lost response reconstruction."""

import json
from copy import deepcopy

import pytest

from evals.multi_stage_setting.report_cli import (
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)


def _case(*, actual=None, failure=None):
    case = {
        "result": "COMPARATOR_MISSING" if actual is None else "FULL_MATCH",
        "decisionId": "answer-one",
        "domain": "WORLD",
        "sourceGoldIds": ["gold-one"],
        "sourceCandidateId": "candidate-one",
        "source": {"subject": "MONSTER · 구름 짐승", "path": "사냥 방식 › 함정 사용"},
        "expected": {"operation": "REVIEW_REQUIRED", "path": "사냥 방식 › 함정 사용"},
        "actual": actual,
        "fields": {},
    }
    if failure is not None:
        case["comparisonFailure"] = failure
    return case


def _attempt(reason, *, rule="SCOPE_UNRESOLVED_SOURCE_INVALID", number=1):
    return {
        "attemptNumber": number,
        "ruleCode": rule,
        "stage": "DECISION_VALIDATION",
        "rejectedSourceIds": ["candidate-one"],
        "decision": {
            "operation": "REVIEW_REQUIRED",
            "reviewReason": "SCOPE_UNRESOLVED",
            "comparisonReason": reason,
            "target": "구름 짐승",
            "path": "사냥 방식 › 함정 사용",
            "matchedPath": "사냥 방식 › 함정 사용",
            "matchedPathVerified": True,
        },
    }


def _report(case):
    return {
        "reportVersion": 3,
        "run": {},
        "dataset": {"name": "synthetic-comparison-reasons", "episodes": [1]},
        "scenarios": [{"scenarioId": "synthetic-case", "episodeNo": 1, "stage2": [case]}],
    }


def _row(report):
    return next(
        line for line in render_markdown_summary(report).splitlines()
        if "모델 추출: candidate-one" in line
    )


def test_successful_comparison_reason_is_visible_and_markup_is_inert():
    reason = '기존 설명과 같음 <script>alert("x")</script> | [열기](https://invalid.test) `덧말`'
    report = _report(_case(actual={"operation": "EXCLUDE", "comparisonReason": reason}))

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert public["actual"]["comparisonReason"] == reason
    row = _row(report)
    assert "AI 판단 이유:" in row
    assert "기존 설명과 같음" in row
    assert "&lt;script&gt;" in row and "&#124;" in row
    assert "&#91;열기&#93;" in row and "&#96;덧말&#96;" in row
    assert "<script>" not in row and "[열기]" not in row
    assert "EXCLUDE" not in row


def test_rejected_proposal_has_its_actual_reason_narrow_rule_and_application_impact():
    report = _report(_case(failure={
        "status": "REJECTED", "attempts": [_attempt("범위 관계가 불명확해 사람의 확인이 필요하다.")],
    }))

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert public["comparisonFailure"]["status"] == "REJECTED"
    row = _row(report)
    assert "자동으로 반영하지 않고 검토 요청" in row
    assert "범위 관계가 불명확해 사람의 확인이 필요하다." in row
    assert "새 항목에 범위가 없고 같은 이름의 기존 설정이 특정 범위에 있을 때만" in row
    assert "이번 비교 결과를 확정·반영하지 못함" in row
    for internal in ("REVIEW_REQUIRED", "SCOPE_UNRESOLVED", "DECISION_VALIDATION", "attemptNumber"):
        assert internal not in row


def test_collateral_batch_abort_does_not_claim_an_independent_valid_decision():
    attempt = _attempt("새로운 설명을 기존 항목에 합치면 된다.", rule="FINAL_PATH_DUPLICATED")
    attempt["rejectedSourceIds"] = ["other-candidate"]
    attempt["rejectedSourceNames"] = ["구름 짐승 · 이동 방식"]
    report = _report(_case(failure={"status": "BATCH_ABORTED", "attempts": [attempt]}))

    row = _row(report)
    assert "다른 항목에서 검증이 중단되어 이 항목의 판단은 확정되지 않음" in row
    assert "새로운 설명을 기존 항목에 합치면 된다." in row
    assert "중단을 일으킨 항목: 구름 짐승 · 이동 방식" in row
    assert "이번 비교 결과를 확정·반영하지 못함" in row
    assert "같은 최종 경로" not in row
    assert "정상 판단" not in row and "검증을 통과" not in row


@pytest.mark.parametrize("failure", [None, {"status": "UNKNOWN", "attempts": []}])
def test_old_missing_reason_is_explicitly_unknown(failure):
    report = _report(_case(failure=failure))

    row = _row(report)
    assert "기록이 없어 확인할 수 없음" in row
    assert "범위가 불명확해" not in row
    if failure is not None:
        assert "이번 비교 결과를 확정·반영하지 못함" in row


def test_semantic_judge_reason_is_not_mistaken_for_comparison_reason():
    case = _case(actual={"operation": "ADD"})
    case["settingNameMatch"] = {"status": "MATCH", "method": "SEMANTIC", "reason": "PRIVATE_JUDGE_REASON"}
    case["reason"] = "PRIVATE_JUDGE_REASON"
    report = _report(case)

    row = _row(report)
    assert "기록이 없어 확인할 수 없음" in row
    assert "PRIVATE_JUDGE_REASON" not in render_markdown_summary(report)
    assert "PRIVATE_JUDGE_REASON" not in json.dumps(build_public_diagnostics(report))


def test_changed_earlier_proposal_is_preserved_without_repeating_identical_retries():
    earlier = _attempt("기존 항목에 합치려 했으나 대상 연결을 확인해야 한다.")
    earlier["decision"]["operation"] = "MERGE"
    latest = _attempt("범위를 먼저 확인한 뒤 반영해야 한다.", number=2)
    identical_retry = deepcopy(latest)
    identical_retry["attemptNumber"] = 3
    report = _report(_case(failure={"status": "REJECTED", "attempts": [earlier, latest, identical_retry]}))

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert len(public["comparisonFailure"]["attempts"]) == 3
    row = _row(report)
    assert "기존 항목에 합치려 했으나 대상 연결을 확인해야 한다." not in row
    assert row.count("범위를 먼저 확인한 뒤 반영해야 한다.") == 1
    assert "앞선 제안 1건은 상세 기록에서 확인할 수 있음" in row
    assert "attempt" not in row and "시도 3" not in row


def test_failed_reason_markup_is_escaped_and_unknown_fields_are_not_published():
    attempt = _attempt("줄 하나\n줄 둘 <img src=x> | [링크](https://invalid.test)")
    attempt["rawResponse"] = "PRIVATE_RESPONSE"
    attempt["decision"]["evidenceSpans"] = [{"quote": "PRIVATE_EVIDENCE"}]
    report = _report(_case(failure={"status": "REJECTED", "attempts": [attempt], "raw": "PRIVATE_RAW"}))

    row = _row(report)
    assert "줄 하나 / 줄 둘 &lt;img src=x&gt; &#124; &#91;링크&#93;" in row
    assert "PRIVATE_" not in json.dumps(build_public_diagnostics(report))


def test_unverified_matched_path_is_not_presented_as_an_existing_setting():
    attempt = _attempt("관련 항목을 확인해야 한다.")
    attempt["decision"].update(matchedPath="존재하지 않는 경로", matchedPathVerified=False)
    report = _report(_case(failure={"status": "REJECTED", "attempts": [attempt]}))

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert public["comparisonFailure"]["attempts"][0]["decision"]["matchedPath"] is None
    assert "존재하지 않는 경로" not in _row(report)


def test_rejected_proposed_value_is_bounded_and_visible_as_an_unconfirmed_proposal():
    attempt = _attempt("덧붙인 내용도 반영할 수 있다.")
    attempt["decision"]["value"] = "합성 제안값 <b>강조</b>"
    report = _report(_case(failure={"status": "REJECTED", "attempts": [attempt]}))

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert public["comparisonFailure"]["attempts"][0]["decision"]["value"] == "합성 제안값 <b>강조</b>"
    assert "AI 제안 (미확정)" in _row(report)
    assert "합성 제안값 &lt;b&gt;강조&lt;/b&gt;" in _row(report)


def test_long_proposed_value_keeps_its_final_caveat_and_escapes_html():
    attempt = _attempt("적용 범위가 확실하지 않아 확인이 필요하다.")
    attempt["decision"]["value"] = "합성 설정 설명. " * 40 + "실제 세계에도 적용되는지는 확인되지 않음 <b>확인 필요</b>"
    report = _report(_case(failure={"status": "REJECTED", "attempts": [attempt]}))

    row = _row(report)
    assert "실제 세계에도 적용되는지는 확인되지 않음 &lt;b&gt;확인 필요&lt;/b&gt;" in row
    assert "<b>" not in row


def test_reason_paraphrases_do_not_create_repeated_earlier_proposals():
    attempts = [_attempt(reason, number=number) for number, reason in enumerate(
        ("대상 범위를 살펴야 한다.", "범위 관계의 확인이 필요하다.", "범위를 먼저 확인해야 한다."), 1,
    )]
    report = _report(_case(failure={"status": "REJECTED", "attempts": attempts}))

    row = _row(report)
    assert row.count("범위를 먼저 확인해야 한다.") == 1
    assert "앞선 제안" not in row
    assert "대상 범위를 살펴야 한다." not in row


def test_invalid_internal_identifiers_are_hidden_with_korean_suffixes_without_erasing_reason():
    reason = "C1의 T1.P2와 비교하여 REVIEW_REQUIRED로 제안했다. ABC_GUILD의 기록도 확인한다. "
    reason += "a18b1bb6-2eb5-45b4-a30b-8f0694b0c123의 위치를 확인한다."
    report = _report(_case(failure={"status": "REJECTED", "attempts": [_attempt(reason)]}))

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert public["comparisonFailure"]["attempts"][0]["decision"]["comparisonReason"] == reason
    row = _row(report)
    assert "ABC_GUILD의 기록도 확인한다." in row
    assert "위치를 확인한다." in row
    for internal in ("C1", "T1.P2", "REVIEW_REQUIRED", "a18b1bb6-2eb5-45b4-a30b-8f0694b0c123"):
        assert internal not in row


def test_reason_projection_is_bounded_and_source_free_aggregate_is_unchanged():
    attempt = _attempt("합성 설명" * 5000)
    report = _report(_case(failure={"status": "REJECTED", "attempts": [deepcopy(attempt) for _ in range(100)]}))
    baseline = deepcopy(report)
    baseline["scenarios"][0]["stage2"][0].pop("comparisonFailure")

    public = build_public_diagnostics(report)[0]["stage2"][0]
    assert len(public["comparisonFailure"]["attempts"]) < 100
    assert len(public["comparisonFailure"]["attempts"][-1]["decision"]["comparisonReason"]) <= 4000
    assert build_source_free_summary(report) == build_source_free_summary(baseline)
    assert "comparisonReason" not in json.dumps(build_source_free_summary(report))


def test_mismatched_consolidation_and_time_are_explained_without_internal_enums():
    case = _case(actual={"operation": "ADD", "consolidationStatus": "MERGED", "temporalScope": "PAST"})
    case["result"] = "DECISION_MISMATCH"
    case["expected"].update(consolidationStatus="SINGLE", temporalScope="PRESENT")
    case["fields"] = {"consolidation": "MISMATCH", "temporal": "MISMATCH"}

    row = _row(_report(case))
    assert "답지: 추출값 하나를 사용 / 모델: 여러 추출값을 하나로 합침" in row
    assert "답지: 현재의 정보 / 모델: 과거의 정보" in row
    for internal in ("SINGLE", "MERGED", "PRESENT", "PAST"):
        assert internal not in row


@pytest.mark.parametrize("rule,explanation", [
    ("RESPONSE_SCHEMA_INVALID", "답변 형식 검증 실패. 어느 항목이 원인인지는 특정하지 못함"),
    ("RESPONSE_JSON_INVALID", "답변을 읽을 수 있는 형식이 아님. 어느 항목이 원인인지는 특정하지 못함"),
    ("COMPARISON_VALIDATION_FAILED", "응답 검증이 중단되어 이 항목을 거절한 구체적인 조건은 확인할 수 없음"),
])
def test_batch_level_failure_explains_known_failure_without_assigning_an_unknown_culprit(rule, explanation):
    attempt = _attempt("관련 항목을 확인해야 한다.", rule=rule)
    attempt["rejectedSourceIds"] = []
    report = _report(_case(failure={"status": "UNKNOWN", "attempts": [attempt]}))

    row = _row(report)
    assert explanation in row
    assert "이번 비교 결과를 확정·반영하지 못함" in row
    assert "검증 결과: 기록이 없어" not in row


def test_unscored_report_never_infers_extra_extraction_or_gold_correspondence():
    case = _case(failure={"status": "REJECTED", "attempts": [_attempt("범위를 먼저 확인해야 한다.")]})
    case["result"] = "EXTRA_NO_DECISION"
    case["expected"]["value"] = "아직 비교하지 않은 답지의 값"
    report = _report(case)
    report["dataset"]["scorable"] = False

    rendered = render_markdown_summary(report)
    assert "과추출" not in rendered
    assert "답지 대응 미채점" in rendered
    assert "아직 비교하지 않은 답지의 값" not in rendered
    assert "오답으로 채점" not in rendered


def test_interrupted_prediction_cli_reuses_comparison_reason_projection(tmp_path, monkeypatch, capsys):
    from evals.multi_stage_setting import report_cli
    from evals.multi_stage_setting.contracts import (
        ComparisonAttemptDiagnostic,
        ComparisonDecisionDiagnostic,
        PredictionBundleV3,
        RuntimeFailure,
        ScenarioPrediction,
        WorldStage1Prediction,
    )

    source = WorldStage1Prediction(
        candidate_id="candidate-one", domain="WORLD", category="MONSTER", subject_name="구름 짐승",
        scope_name="사냥 방식", setting_name="함정 사용", source_values=["합성 설정 설명"],
    )
    collateral = source.model_copy(update={"candidate_id": "candidate-two", "setting_name": "은신 방식"})
    attempt = ComparisonAttemptDiagnostic(
        attempt_number=1, rule_code="SCOPE_UNRESOLVED_SOURCE_INVALID", stage="DECISION_VALIDATION",
        attribution="IDENTIFIED", batch_source_ids=["candidate-one", "candidate-two"], rejected_source_ids=["candidate-one"],
        decisions=[ComparisonDecisionDiagnostic(
            decision_index=0, source_candidate_ids=["candidate-one"], operation="REVIEW_REQUIRED",
            review_reason="SCOPE_UNRESOLVED", comparison_reason="범위를 먼저 확인해야 한다.",
            target="구름 짐승", proposed_scope_name="사냥 방식", proposed_setting_name="함정 사용",
        ), ComparisonDecisionDiagnostic(
            decision_index=1, source_candidate_ids=["candidate-two"], operation="REVIEW_REQUIRED",
            review_reason="GENERAL_UNCERTAINTY", comparison_reason="함께 비교한 항목의 검증이 끝나지 않았다.",
            target="구름 짐승", proposed_scope_name="사냥 방식", proposed_setting_name="은신 방식",
        )],
    )
    bundle = PredictionBundleV3(
        fixture_hash="synthetic-fixture", mode="FIXED", evaluation_domains={"WORLD"},
        scenarios=[ScenarioPrediction(
            scenario_id="synthetic-case", stage1=[source, collateral], failures=[RuntimeFailure(
                stage="WORLD_STAGE2", source_id=candidate_id, error_type="ComparisonValidationError",
                message="ComparisonValidationError", comparison_attempts=[attempt],
            ) for candidate_id in ("candidate-one", "candidate-two")],
        )],
    )
    predictions, markdown, aggregate = (tmp_path / name for name in ("predictions.json", "summary.md", "aggregate.json"))
    predictions.write_text(bundle.model_dump_json(by_alias=True), encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "report_cli", "--predictions", str(predictions), "--markdown-output", str(markdown),
        "--json-output", str(aggregate),
    ])

    report_cli.main()

    rendered = markdown.read_text(encoding="utf-8")
    assert "범위를 먼저 확인해야 한다." in rendered
    assert "이번 비교 결과를 확정·반영하지 못함" in rendered
    assert "오답으로 채점" not in rendered
    assert "과추출" not in rendered
    assert "답지 대응 미채점" in rendered
    collateral_row = next(line for line in rendered.splitlines() if "모델 추출: candidate-two" in line)
    assert "중단을 일으킨 항목: 몬스터 · 구름 짐승 · 사냥 방식 › 함정 사용" in collateral_row
    assert "MONSTER" not in collateral_row and "RACE" not in collateral_row
    public = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    assert public["scenarios"][0]["stage2"][0]["comparisonFailure"]["status"] == "REJECTED"
    assert "comparisonReason" not in aggregate.read_text(encoding="utf-8")
    capsys.readouterr()
