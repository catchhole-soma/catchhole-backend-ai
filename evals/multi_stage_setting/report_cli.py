from __future__ import annotations

import argparse
from collections import Counter
import html
import json
from pathlib import Path
from typing import Any, Callable


_DOMAINS = ("character", "world")
_MAX_ROWS_PER_SECTION = 25
_MAX_DIAGNOSTIC_ROWS = 200
_MAX_SUMMARY_BYTES = 900_000
_MAX_CELL_LENGTH = 180
_STAGE1_FIELD_ORDER = ("subject", "path", "value")
_STAGE2_FIELD_ORDER = (
    "operation",
    "target",
    "value",
    "canonicalPath",
    "temporal",
    "removedSet",
    "structuredValue",
    "consolidation",
    "proposedPath",
    "rootMoveSet",
)
_STAGE1_RESULTS = {"FULL_MATCH", "PARTIAL_MATCH", "MISSED", "EXTRA"}
_STAGE2_RESULTS = {
    "FULL_MATCH",
    "UPSTREAM_BLOCKED",
    "COMPARATOR_MISSING",
    "SEMANTIC_PENDING",
    "DECISION_MISMATCH",
}
_FIELD_STATUSES = {
    "MATCH",
    "MISMATCH",
    "PENDING",
    "NOT_APPLICABLE",
    "MISSING",
    "UNMATCHED",
}
_AXIS_LABELS = {
    "subject": "주체",
    "path": "경로",
    "value": "값",
    "operation": "operation",
    "target": "대상",
    "canonicalPath": "canonical key",
    "temporal": "시점",
    "removedSet": "제거 집합",
    "structuredValue": "valueJson",
    "consolidation": "통합 상태",
    "proposedPath": "제안 경로",
    "rootMoveSet": "root 이동 집합",
}


def build_source_free_summary(report: dict[str, Any]) -> dict[str, Any]:
    """원문·근거·개별 표시값을 제외한 집계 JSON만 artifact로 남긴다."""

    stages = report.get("stages", {})
    run = dict(report.get("run", {}))
    if "runtimeFailures" in run:
        run["runtimeFailures"] = _sanitize_runtime_failures(run["runtimeFailures"])
    return {
        "reportVersion": report.get("reportVersion"),
        "run": run,
        "dataset": report.get("dataset", {}),
        "stages": {
            "character": {
                key: _aggregate_only(stages.get("character", {}).get(key, {}))
                for key in ("stage1", "stage2")
            },
            "world": {
                key: _aggregate_only(stages.get("world", {}).get(key, {}))
                for key in ("stage1", "stage2")
            },
            "macroAverage": stages.get("macroAverage", {}),
        },
        "endToEnd": _aggregate_only(report.get("endToEnd", {}), include_domains=True),
        "failureCauses": report.get("failureCauses", {}),
    }


def build_public_diagnostics(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Allowlist the item fields that may appear in public Markdown."""

    diagnostics = []
    raw_scenarios = report.get("scenarios", [])
    if not isinstance(raw_scenarios, list):
        return diagnostics
    for raw_scenario in raw_scenarios:
        if not isinstance(raw_scenario, dict):
            continue
        stage1: dict[str, dict[str, list[dict[str, Any]]]] = {}
        raw_stage1 = raw_scenario.get("stage1", {})
        if isinstance(raw_stage1, dict):
            for domain in _DOMAINS:
                raw_domain = raw_stage1.get(domain.upper(), {})
                if not isinstance(raw_domain, dict):
                    continue
                cases = _sanitize_cases(raw_domain.get("cases"), _sanitize_stage1_case)
                if cases:
                    stage1[domain] = {"cases": cases}

        stage2 = _sanitize_cases(raw_scenario.get("stage2"), _sanitize_stage2_case)
        diagnostics.append(
            {
                "scenarioId": _text(raw_scenario.get("scenarioId")),
                "episodeNo": _integer(raw_scenario.get("episodeNo")),
                "stage1": stage1,
                "stage2": stage2,
            }
        )
    return diagnostics


def render_markdown_summary(report: dict[str, Any]) -> str:
    summary = build_source_free_summary(report)
    diagnostics = build_public_diagnostics(report)
    run = summary["run"]
    dataset = summary["dataset"]
    stages = summary["stages"]
    lines = [
        "# 캐릭터·세계관 다단계 평가 결과",
        "",
        f"- 데이터셋: `{_inline(dataset.get('name', '-'))}` / "
        f"`{_inline(dataset.get('version', '-'))}`",
        f"- 모드·도메인: `{_inline(run.get('mode', '-'))}` / "
        f"`{_inline(', '.join(run.get('domains', [])) or '-')}`",
        f"- 회차: `{_inline(dataset.get('episodes', []))}`",
        f"- Fixture hash: `{_inline(dataset.get('fixtureHash', '-'))}`",
        f"- 모델: 1차 `{_inline(run.get('analysisModel', '-'))}` · 주체 해소 "
        f"`{_inline(run.get('subjectResolutionModel', '-'))}` · 2차 "
        f"`{_inline(run.get('comparisonModel', '-'))}`",
        f"- 토큰: 입력 `{_count(run.get('inputTokens'))}` (캐시 "
        f"`{_count(run.get('cachedInputTokens'))}`) · 출력 "
        f"`{_count(run.get('outputTokens'))}` · 추정 비용 "
        f"`{_inline(run.get('estimatedCostUsd') or '미설정')}` USD",
        "",
        "> 공개 보고서는 주체·경로·정규화된 표시값만 보여줍니다. 원문, 근거 quote/offset, "
        "raw LLM 응답, valueJson은 포함하지 않습니다.",
        "",
        "## 1차 추출 집계",
        "",
        "Candidate TP는 기존 점수 정의대로 `주체+경로` 일치입니다. 아래 상세의 완전 일치는 "
        "여기에 최종 값 일치까지 요구하므로 두 개수가 다를 수 있습니다.",
        "",
        "| 도메인 | 수량 | Candidate P / R / F1 | 가중 Recall | 주체 / 경로 / 값 | "
        "근거 위치 / 커버리지 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for domain in _DOMAINS:
        stage = stages[domain]["stage1"]
        if not stage.get("evaluated", True):
            lines.append(
                f"| {domain.upper()} | 미평가 | {_cell(stage.get('reason'))} | - | - | - |"
            )
            continue
        metrics = stage.get("metrics", {})
        counts = stage.get("counts", {})
        result_counts = _stage1_result_counts(diagnostics, domain)
        axis_counts = _stage1_field_counts(diagnostics, domain)
        missed = (
            result_counts["MISSED"] if sum(result_counts.values()) else _count(counts.get("missed"))
        )
        quantity = (
            f"Gold {_count(counts.get('gold'))} · 예측 {_count(counts.get('predictions'))} · "
            f"연결 {_count(counts.get('matches'))} · "
            f"TP {_count(counts.get('identityTruePositive'))} · "
            f"누락 {missed} · 과추출 {_count(counts.get('extra'))} · "
            f"추출 금지 위반 {_count(counts.get('hardNegativeHits'))}"
        )
        lines.append(
            f"| {domain.upper()} | {_cell(quantity)} | "
            f"{_format_ratio(metrics.get('candidatePrecision'))} / "
            f"{_format_ratio(metrics.get('candidateRecall'))} / "
            f"{_format_ratio(metrics.get('candidateF1'))} | "
            f"{_format_ratio(metrics.get('weightedRecall'))} | "
            f"{_format_axis_ratio(metrics.get('entityOrSubjectAccuracy'), axis_counts['subject'])} / "
            f"{_format_axis_ratio(metrics.get('pathOrFactAccuracy'), axis_counts['path'])} / "
            f"{_format_axis_ratio(metrics.get('valueAccuracy'), axis_counts['value'])} | "
            f"{_format_ratio(metrics.get('evidenceLocatableRate'))} / "
            f"{_format_ratio(metrics.get('evidenceCoverageRate'))} |"
        )
        if counts.get("semanticPending", 0):
            lines.append(
                f"| ↳ {domain.upper()} 값 의미 판정 | resolved / lower-bound / coverage | "
                f"- | - | {_format_ratio(metrics.get('resolvedValueAccuracy'))} / "
                f"{_format_ratio(metrics.get('valueLowerBoundAccuracy'))} / "
                f"{_format_ratio(metrics.get('valueSemanticCoverage'))} | - |"
            )

    lines.extend(
        [
            "",
            "## 2차 비교 집계",
            "",
            "| 도메인 | 수량 | 도달률 | Full accuracy | operation / 대상 / 값 | "
            "도메인 경로 / 규칙 | 제거 / 이동 |",
            "| --- | --- | ---: | ---: | --- | --- | ---: |",
        ]
    )
    for domain in _DOMAINS:
        stage = stages[domain]["stage2"]
        if not stage.get("evaluated", True):
            lines.append(
                f"| {domain.upper()} | 미평가 | {_cell(stage.get('reason'))} | - | - | - | - |"
            )
            continue
        metrics = stage.get("metrics", {})
        counts = stage.get("counts", {})
        axis_counts = _stage2_field_counts(diagnostics, domain)
        gold_count = _int_or_zero(counts.get("gold"))
        reached_count = _int_or_zero(counts.get("upstreamReached"))
        pending_count = _int_or_zero(counts.get("semanticPending"))
        resolved_count = max(0, reached_count - pending_count)
        resolved_accuracy = metrics.get("resolvedFullDecisionAccuracy")
        if resolved_accuracy is None and pending_count == 0:
            resolved_accuracy = metrics.get("fullDecisionAccuracy")
        full_count = _matched_count(
            resolved_accuracy,
            resolved_count,
        )
        quantity = (
            f"Gold {gold_count} · 도달 {reached_count} · 응답 "
            f"{_count(counts.get('reachedAndCompared'))} · 정답 "
            f"{full_count} · 오답 {resolved_count - full_count} · "
            f"미판정 {pending_count} · 차단 {max(0, gold_count - reached_count)}"
        )
        path_metric = (
            metrics.get("characterCanonicalFactKeyResolutionAccuracy")
            if domain == "character"
            else metrics.get("proposedPathAccuracy")
        )
        rule_metric = (
            metrics.get("temporalAccuracy")
            if domain == "character"
            else metrics.get("consolidationAccuracy")
        )
        removal_metric = (
            metrics.get("removedSnapshotSetAccuracy")
            if domain == "character"
            else metrics.get("existingRootPropertyMoveSetAccuracy")
        )
        lines.append(
            f"| {domain.upper()} | {_cell(quantity)} | "
            f"{_format_ratio(metrics.get('upstreamReachRate'))} | "
            f"{_format_ratio(metrics.get('fullDecisionAccuracy'))} | "
            f"{_format_axis_ratio(metrics.get('operationAccuracy'), axis_counts['operation'])} / "
            f"{_format_axis_ratio(metrics.get('targetAccuracy'), axis_counts['target'])} / "
            f"{_format_axis_ratio(metrics.get('proposedValueAccuracy'), axis_counts['value'])} | "
            f"{_format_axis_ratio(path_metric, axis_counts[_stage2_path_axis(domain)])} / "
            f"{_format_axis_ratio(rule_metric, axis_counts[_stage2_rule_axis(domain)])} | "
            f"{_format_axis_ratio(removal_metric, axis_counts[_stage2_removal_axis(domain)])} |"
        )
        if counts.get("semanticPending", 0):
            lines.append(
                f"| ↳ {domain.upper()} 의미 판정 | resolved / lower-bound / coverage | "
                f"- | {_format_ratio(metrics.get('resolvedFullDecisionAccuracy'))} / "
                f"{_format_ratio(metrics.get('fullDecisionLowerBoundAccuracy'))} / "
                f"{_format_ratio(metrics.get('semanticCoverage'))} | - | - | - |"
            )

    _append_end_to_end(lines, summary)
    _append_diagnostics(lines, diagnostics)
    lines.extend(
        [
            "> 점수는 관찰용이며, 자동 workflow는 실행·계약·fixture 검증 실패만 실패로 처리합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def _append_end_to_end(lines: list[str], summary: dict[str, Any]) -> None:
    end_to_end = summary["endToEnd"]
    lines.extend(
        [
            "",
            "## 누적 상태·전이",
            "",
            "| 범위 | Precision | Recall | F1 | 의미 판정 coverage / pending |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for domain in _DOMAINS:
        value = end_to_end.get("domains", {}).get(domain.upper(), {})
        if not value.get("evaluated", True):
            lines.append(f"| {domain.upper()} | 미평가 | - | - | {_cell(value.get('reason'))} |")
            continue
        lines.append(
            f"| {domain.upper()} after-state | "
            f"{_format_ratio(value.get('afterStatePrecision'))} | "
            f"{_format_ratio(value.get('afterStateRecall'))} | "
            f"{_format_ratio(value.get('afterStateF1'))} | "
            f"{_format_ratio(value.get('semanticCoverage'))} / "
            f"{_count(value.get('semanticPending'))} |"
        )
        if value.get("semanticPending", 0):
            lines.append(
                f"| ↳ {domain.upper()} 의미 판정 | - | - | resolved "
                f"{_format_ratio(value.get('resolvedAfterStateF1'))} / lower-bound "
                f"{_format_ratio(value.get('afterStateLowerBoundF1'))} | "
                f"{_format_ratio(value.get('semanticCoverage'))} / "
                f"{_count(value.get('semanticPending'))} |"
            )
    metrics = end_to_end.get("metrics", {})
    counts = end_to_end.get("counts", {})
    lines.extend(
        [
            f"| 전체 after-state | - | - | {_format_ratio(metrics.get('afterStateF1'))} | - |",
            f"| 상태 전이 ({_count(counts.get('matchedTransitions'))} 일치 / "
            f"{_count(counts.get('expectedTransitions'))} Gold / "
            f"{_count(counts.get('predictedTransitions'))} 예측) | "
            f"{_format_ratio(metrics.get('transitionPrecision'))} | "
            f"{_format_ratio(metrics.get('transitionRecall'))} | "
            f"{_format_ratio(metrics.get('transitionF1'))} | - |",
        ]
    )
    if any(
        value.get("semanticPending", 0)
        for value in end_to_end.get("domains", {}).values()
        if isinstance(value, dict)
    ):
        lines.append(
            "| ↳ 전체 after-state 의미 판정 | - | - | resolved "
            f"{_format_ratio(metrics.get('resolvedAfterStateF1'))} / lower-bound "
            f"{_format_ratio(metrics.get('afterStateLowerBoundF1'))} | - |"
        )
    if counts.get("semanticPendingTransitions", 0):
        lines.append(
            "| ↳ 상태 전이 의미 판정 | "
            f"{_format_ratio(metrics.get('resolvedTransitionPrecision'))} | "
            f"{_format_ratio(metrics.get('resolvedTransitionRecall'))} | resolved "
            f"{_format_ratio(metrics.get('resolvedTransitionF1'))} / lower-bound "
            f"{_format_ratio(metrics.get('transitionLowerBoundF1'))} | - |"
        )
    lines.extend(
        [
            "",
            f"- 상태 적용 오류: `{_count(counts.get('stateApplicationErrors'))}`",
            f"- dependency 상태 적용 오류: "
            f"`{_count(counts.get('dependencyStateApplicationErrors'))}`",
            "- 실패 원인:",
            "",
            "| 원인 | 건수 |",
            "| --- | ---: |",
        ]
    )
    failures = summary.get("failureCauses", {})
    nonzero_failures = (
        [(cause, count) for cause, count in sorted(failures.items()) if count]
        if isinstance(failures, dict)
        else []
    )
    if nonzero_failures:
        for cause, count in nonzero_failures:
            lines.append(f"| {_cell(cause)} | {_count(count)} |")
    else:
        lines.append("| 없음 | 0 |")
    _append_runtime_failures(lines, summary.get("run", {}).get("runtimeFailures"))


def _append_runtime_failures(lines: list[str], value: Any) -> None:
    if not isinstance(value, dict) or not value.get("total"):
        return
    lines.extend(
        [
            "",
            "### 실행 중 복구된 후보 오류",
            "",
            f"총 `{_count(value.get('total'))}`건입니다. 오류 메시지와 응답 본문은 공개하지 않습니다.",
            "",
            "| 분류 | 항목 | 건수 |",
            "| --- | --- | ---: |",
        ]
    )
    for group, label in (("byStage", "단계"), ("byErrorType", "오류 유형")):
        counts = value.get(group, {})
        if not isinstance(counts, dict):
            continue
        for name, count in sorted(counts.items()):
            lines.append(f"| {label} | {_cell(name)} | {_count(count)} |")


def _append_diagnostics(
    lines: list[str],
    diagnostics: list[dict[str, Any]],
) -> None:
    start = len(lines)
    row_limits = (_MAX_DIAGNOSTIC_ROWS, 100, 50, 25, 10, 0)
    for row_limit in row_limits:
        del lines[start:]
        _append_diagnostics_limited(lines, diagnostics, row_limit)
        if len("\n".join(lines).encode("utf-8")) <= _MAX_SUMMARY_BYTES:
            return


def _append_diagnostics_limited(
    lines: list[str],
    diagnostics: list[dict[str, Any]],
    row_limit: int,
) -> None:
    lines.extend(["", "## 항목별 진단", ""])
    if not any(item["stage1"] or item["stage2"] for item in diagnostics):
        lines.extend(["진단 가능한 항목이 없습니다.", ""])
        return
    total_cases = sum(
        len(cases.get("cases", []))
        for scenario in diagnostics
        for cases in scenario.get("stage1", {}).values()
    ) + sum(len(scenario.get("stage2", [])) for scenario in diagnostics)
    remaining_rows = row_limit
    for scenario in diagnostics:
        if remaining_rows <= 0:
            break
        if not scenario["stage1"] and not scenario["stage2"]:
            continue
        title = (
            f"{scenario.get('episodeNo') or '-'}화 · `{_inline(scenario.get('scenarioId') or '-')}`"
        )
        lines.extend([f"### {title}", ""])
        for domain in _DOMAINS:
            if remaining_rows <= 0:
                break
            stage1_cases = scenario.get("stage1", {}).get(domain, {}).get("cases", [])
            stage2_cases = [
                case for case in scenario.get("stage2", []) if case.get("domain") == domain.upper()
            ]
            if not stage1_cases and not stage2_cases:
                continue
            lines.extend([f"#### {domain.upper()}", ""])
            if stage1_cases:
                counts = Counter(case["result"] for case in stage1_cases)
                lines.append(
                    "**1차** · 완전 일치 "
                    f"{counts['FULL_MATCH']} · 부분 일치 {counts['PARTIAL_MATCH']} · "
                    f"누락 {counts['MISSED']} · 과추출 {counts['EXTRA']}"
                )
                lines.append("")
                remaining_rows = _append_case_tables(
                    lines,
                    stage1_cases,
                    _stage1_row,
                    (
                        "판정",
                        "Gold ID",
                        "예측 ID",
                        "주체 (Gold → 예측)",
                        "경로 (Gold → 예측)",
                        "값 (Gold → 예측)",
                        "차이·upstream",
                    ),
                    remaining_rows,
                )
            if stage2_cases:
                counts = Counter(case["result"] for case in stage2_cases)
                lines.append(
                    "**2차** · 완전 일치 "
                    f"{counts['FULL_MATCH']} · 결정 불일치 {counts['DECISION_MISMATCH']} · "
                    f"comparator 누락 {counts['COMPARATOR_MISSING']} · 의미 미판정 "
                    f"{counts['SEMANTIC_PENDING']} · upstream 차단 {counts['UPSTREAM_BLOCKED']}"
                )
                lines.append("")
                remaining_rows = _append_case_tables(
                    lines,
                    stage2_cases,
                    _stage2_row,
                    (
                        "판정",
                        "결정 / 원천",
                        "Gold 결정",
                        "예측 결정",
                        "차이·차단 원인",
                    ),
                    remaining_rows,
                )
    rendered_cases = row_limit - remaining_rows
    if rendered_cases < total_cases:
        lines.extend(
            [
                f"> 공개 요약 크기 제한으로 전체 {total_cases}건 중 "
                f"{rendered_cases}건만 표시했습니다. 전체 수량은 위 집계를 기준으로 확인하세요.",
                "",
            ]
        )


def _append_case_tables(
    lines: list[str],
    cases: list[dict[str, Any]],
    row_builder: Callable[[dict[str, Any]], tuple[str, ...]],
    headers: tuple[str, ...],
    remaining_rows: int,
) -> int:
    failures = [case for case in cases if case["result"] != "FULL_MATCH"]
    successes = [case for case in cases if case["result"] == "FULL_MATCH"]
    if failures and remaining_rows:
        table, rendered = _markdown_table(
            headers,
            failures,
            row_builder,
            remaining_rows,
        )
        lines.extend(table)
        remaining_rows -= rendered
        lines.append("")
    elif not failures:
        lines.extend(["- 실패·미판정 항목 없음", ""])
    if successes and remaining_rows:
        table, rendered = _markdown_table(
            headers,
            successes,
            row_builder,
            remaining_rows,
        )
        lines.extend(
            [
                "<details>",
                f"<summary>완전 일치 {len(successes)}건 보기</summary>",
                "",
                *table,
                "",
                "</details>",
                "",
            ]
        )
        remaining_rows -= rendered
    return remaining_rows


def _markdown_table(
    headers: tuple[str, ...],
    cases: list[dict[str, Any]],
    row_builder: Callable[[dict[str, Any]], tuple[str, ...]],
    remaining_rows: int,
) -> tuple[list[str], int]:
    shown = cases[: min(_MAX_ROWS_PER_SECTION, remaining_rows)]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row_builder(case)) + " |" for case in shown)
    omitted = len(cases) - len(shown)
    if omitted:
        lines.append(
            "| "
            + _cell(f"표 크기 제한으로 {omitted}건 생략")
            + " | "
            + " | ".join("-" for _ in headers[1:])
            + " |"
        )
    return lines, len(shown)


def _stage1_row(case: dict[str, Any]) -> tuple[str, ...]:
    expected = case.get("expected") or {}
    actual = case.get("actual") or {}
    return (
        _cell(_result_label(case["result"])),
        _cell(", ".join(case.get("goldIds", [])) or "-"),
        _cell(case.get("predictionId")),
        _comparison_cell(expected.get("subject"), actual.get("subject")),
        _comparison_cell(expected.get("path"), actual.get("path")),
        _comparison_cell(expected.get("value"), actual.get("value")),
        _cell(_diagnostic_reason(case)),
    )


def _stage2_row(case: dict[str, Any]) -> tuple[str, ...]:
    source_ids = ", ".join(case.get("sourceGoldIds", [])) or "-"
    identifiers = (
        f"{case.get('decisionId') or '-'} / {source_ids} / {case.get('sourceCandidateId') or '-'}"
    )
    return (
        _cell(_result_label(case["result"])),
        _cell(identifiers),
        _cell(_stage2_shape(case.get("expected") or {})),
        _cell(_stage2_shape(case.get("actual") or {})),
        _cell(_diagnostic_reason(case)),
    )


def _stage2_shape(value: dict[str, Any]) -> str:
    if not value:
        return "-"
    parts = [
        str(item)
        for item in (
            value.get("operation"),
            f"대상={value['target']}" if value.get("target") else None,
            value.get("path"),
            value.get("value"),
        )
        if item not in (None, "")
    ]
    if value.get("temporalScope") is not None:
        parts.append(f"시점={value['temporalScope']}")
    if value.get("consolidationStatus") is not None:
        parts.append(f"통합={value['consolidationStatus']}")
    removed_paths = value.get("removedPaths") or []
    if removed_paths:
        parts.append(f"제거=[{', '.join(removed_paths)}]")
    elif value.get("removedCount"):
        parts.append(f"제거={value['removedCount']}건")
    root_move_names = value.get("rootMoveNames") or []
    if root_move_names:
        parts.append(f"root 이동=[{', '.join(root_move_names)}]")
    elif value.get("rootMoveCount"):
        parts.append(f"root 이동={value['rootMoveCount']}건")
    return " · ".join(parts) or "-"


def _diagnostic_reason(case: dict[str, Any]) -> str:
    fields = case.get("fields", {})
    mismatches = [
        _AXIS_LABELS.get(name, name) for name, status in fields.items() if status == "MISMATCH"
    ]
    pending = [
        _AXIS_LABELS.get(name, name) for name, status in fields.items() if status == "PENDING"
    ]
    parts = []
    if mismatches:
        parts.append("불일치=" + ", ".join(mismatches))
    if pending:
        parts.append("미판정=" + ", ".join(pending))
    upstream = case.get("upstreamOutcome")
    if upstream and upstream != "REACHED":
        parts.append(f"upstream={upstream}")
    if case.get("failureCause"):
        parts.append(f"원인={case['failureCause']}")
    if case["result"] == "MISSED":
        parts.append("Gold에 대응하는 예측 없음")
    elif case["result"] == "EXTRA":
        parts.append("예측에 대응하는 Gold 없음")
    return " · ".join(parts) or "모두 일치"


def _comparison_cell(expected: Any, actual: Any) -> str:
    return f"{_cell(expected)}<br>→ {_cell(actual)}"


def _result_label(value: str) -> str:
    return {
        "FULL_MATCH": "완전 일치",
        "PARTIAL_MATCH": "부분 일치",
        "MISSED": "누락 Gold",
        "EXTRA": "추가 예측",
        "UPSTREAM_BLOCKED": "upstream 차단",
        "COMPARATOR_MISSING": "comparator 누락",
        "SEMANTIC_PENDING": "의미 미판정",
        "DECISION_MISMATCH": "결정 불일치",
    }.get(value, value)


def _stage1_result_counts(
    diagnostics: list[dict[str, Any]],
    domain: str,
) -> Counter[str]:
    return Counter(
        case["result"]
        for scenario in diagnostics
        for case in scenario.get("stage1", {}).get(domain, {}).get("cases", [])
    )


def _stage1_field_counts(
    diagnostics: list[dict[str, Any]],
    domain: str,
) -> dict[str, Counter[str]]:
    cases = [
        case
        for scenario in diagnostics
        for case in scenario.get("stage1", {}).get(domain, {}).get("cases", [])
    ]
    return _field_counts(cases, _STAGE1_FIELD_ORDER)


def _stage2_field_counts(
    diagnostics: list[dict[str, Any]],
    domain: str,
) -> dict[str, Counter[str]]:
    cases = [
        case
        for scenario in diagnostics
        for case in scenario.get("stage2", [])
        if case.get("domain") == domain.upper()
    ]
    return _field_counts(cases, _STAGE2_FIELD_ORDER)


def _field_counts(
    cases: list[dict[str, Any]],
    field_order: tuple[str, ...],
) -> dict[str, Counter[str]]:
    return {
        name: Counter(
            case.get("fields", {}).get(name)
            for case in cases
            if case.get("fields", {}).get(name) is not None
        )
        for name in field_order
    }


def _stage2_path_axis(domain: str) -> str:
    return "canonicalPath" if domain == "character" else "proposedPath"


def _stage2_rule_axis(domain: str) -> str:
    return "temporal" if domain == "character" else "consolidation"


def _stage2_removal_axis(domain: str) -> str:
    return "removedSet" if domain == "character" else "rootMoveSet"


def _sanitize_cases(
    value: Any,
    sanitizer: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict) and (sanitized := sanitizer(item)) is not None:
            result.append(sanitized)
    return result


def _sanitize_stage1_case(value: dict[str, Any]) -> dict[str, Any] | None:
    result = _choice(value.get("result"), _STAGE1_RESULTS)
    if result is None:
        return None
    return {
        "result": result,
        "goldIds": _text_list(value.get("goldIds")),
        "predictionId": _text(value.get("predictionId")),
        "expected": _sanitize_fact_summary(value.get("expected")),
        "actual": _sanitize_fact_summary(value.get("actual")),
        "fields": _sanitize_fields(value.get("fields"), _STAGE1_FIELD_ORDER),
        "upstreamOutcome": _text(value.get("upstreamOutcome")),
    }


def _sanitize_stage2_case(value: dict[str, Any]) -> dict[str, Any] | None:
    result = _choice(value.get("result"), _STAGE2_RESULTS)
    domain = _choice(value.get("domain"), {item.upper() for item in _DOMAINS})
    if result is None or domain is None:
        return None
    return {
        "result": result,
        "decisionId": _text(value.get("decisionId")),
        "domain": domain,
        "sourceGoldIds": _text_list(value.get("sourceGoldIds")),
        "sourceCandidateId": _text(value.get("sourceCandidateId")),
        "upstreamOutcome": _text(value.get("upstreamOutcome")),
        "failureCause": _text(value.get("failureCause")),
        "expected": _sanitize_stage2_summary(value.get("expected")),
        "actual": _sanitize_stage2_summary(value.get("actual")),
        "fields": _sanitize_fields(value.get("fields"), _STAGE2_FIELD_ORDER),
    }


def _sanitize_fact_summary(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    return {key: _text(value.get(key)) for key in ("subject", "path", "value")}


def _sanitize_stage2_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    text_keys = (
        "operation",
        "target",
        "path",
        "value",
        "temporalScope",
        "consolidationStatus",
    )
    result: dict[str, Any] = {key: _text(value.get(key)) for key in text_keys}
    result["removedCount"] = _integer(value.get("removedCount"))
    result["removedPaths"] = _text_list(value.get("removedPaths"))
    result["rootMoveCount"] = _integer(value.get("rootMoveCount"))
    result["rootMoveNames"] = _text_list(value.get("rootMoveNames"))
    return result


def _sanitize_runtime_failures(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"total": 0, "byStage": {}, "byErrorType": {}}
    return {
        "total": _integer(value.get("total")) or 0,
        "byStage": _sanitize_count_map(value.get("byStage")),
        "byErrorType": _sanitize_count_map(value.get("byErrorType")),
    }


def _sanitize_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: count
        for key, raw_count in sorted(value.items())
        if isinstance(key, str) and (count := _integer(raw_count)) is not None
    }


def _sanitize_fields(value: Any, allowed: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: status
        for key in allowed
        if (status := _choice(value.get(key), _FIELD_STATUSES)) is not None
    }


def _aggregate_only(
    value: dict[str, Any],
    *,
    include_domains: bool = False,
) -> dict[str, Any]:
    result = {
        key: value[key] for key in ("evaluated", "reason", "metrics", "counts") if key in value
    }
    if include_domains:
        result["domains"] = value.get("domains", {})
    return result


def _choice(value: Any, allowed: set[str]) -> str | None:
    text = _text(value)
    return text if text in allowed else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_or_zero(value: Any) -> int:
    return _integer(value) or 0


def _matched_count(accuracy: Any, total: int) -> int:
    if accuracy is None or total == 0:
        return 0
    return max(0, min(total, round(float(accuracy) * total)))


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", " / ")
    if len(text) > _MAX_CELL_LENGTH:
        text = text[: _MAX_CELL_LENGTH - 1] + "…"
    escaped = html.escape(text, quote=False)
    return escaped.translate(
        str.maketrans(
            {
                "\\": "&#92;",
                "|": "&#124;",
                "[": "&#91;",
                "]": "&#93;",
                "!": "&#33;",
                "`": "&#96;",
            }
        )
    )


def _inline(value: Any) -> str:
    return _cell(value)


def _count(value: Any) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "-"


def _format_ratio(value: Any) -> str:
    return "대상 없음/미판정" if value is None else f"{float(value) * 100:.2f}%"


def _format_axis_ratio(value: Any, counts: Counter[str]) -> str:
    matched = counts["MATCH"]
    mismatched = counts["MISMATCH"]
    pending = counts["PENDING"]
    denominator = matched + mismatched
    if denominator == 0 and pending == 0:
        return _format_ratio(value)
    suffix = f"{matched}/{denominator}"
    if pending:
        suffix += f", 미판정 {pending}"
    return f"{_format_ratio(value)} ({suffix})"


def main() -> None:
    args = _parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    markdown = render_markdown_summary(report)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown, encoding="utf-8")
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(build_source_free_summary(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(markdown)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render aggregate JSON and sanitized Markdown from a setting-eval/v3 report."
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    main()
