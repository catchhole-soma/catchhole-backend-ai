from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from evals.setting_extraction.normalization import normalize_fact_key

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
    "pathPreservation",
    "stateApplication",
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
_SETTING_NAME_MATCH_STATUSES = {"MATCH", "MISMATCH", "PENDING"}
_SETTING_NAME_MATCH_METHODS = {"EXACT", "ALIAS", "SEMANTIC", "UNRESOLVED"}
_STAGE2_POLICIES = {"REQUIRED", "WAIT_FOR_CHARACTER_MATCH"}
_AXIS_LABELS = {
    "subject": "설정 대상",
    "path": "설정 분류·세부 항목",
    "value": "설정값",
    "operation": "처리 방식",
    "target": "처리할 기존 설정",
    "canonicalPath": "반영할 설정 분류·세부 항목",
    "temporal": "정보의 시점",
    "removedSet": "종료할 상태",
    "structuredValue": "저장할 세부 값",
    "consolidation": "여러 추출값을 합칠 수 있는지에 대한 판단",
    "proposedPath": "반영할 범위·설정명",
    "rootMoveSet": "새 범위로 함께 옮길 기존 설정",
    "pathPreservation": "수정·병합할 기존 설정 경로 유지",
    "stateApplication": "실제 반영 규칙 준수",
}
_OPERATION_LABELS = {
    "ADD": "새 설정 추가",
    "UPDATE": "기존 설정 수정",
    "MERGE": "기존 설정과 합쳐 반영",
    "REMOVE": "기존 상태 종료",
    "EXCLUDE": "이번 추출 정보를 반영하지 않음",
    "HISTORY_ONLY": "현재 설정에 반영하지 않고 이력에만 기록",
    "REVIEW_REQUIRED": "자동으로 반영하지 않고 검토 요청",
}
_FACT_TYPE_LABELS = {
    "PROFILE": "기본 정보",
    "STATUS": "상태",
    "ITEM": "아이템",
    "STAT": "능력치",
    "LEVEL": "레벨",
    "SKILL": "스킬",
    "RELATIONSHIP": "관계",
    "CHARACTER_DISCOVERY": "인물 발견",
}
_FACT_KEY_LABELS = {
    "profile.species": "종족",
    "profile.gender": "성별",
    "profile.occupation": "직업",
    "profile.attribute": "특성",
    "stats.physique": "육체",
    "stats.mental": "정신",
    "stats.supernatural": "이능",
    "stats.item_level": "아이템 레벨",
    "stats.combat_power": "전투력",
    "level": "레벨",
}
_WORLD_CATEGORY_LABELS = {
    "WORLD_RULE_HISTORY": "세계의 규칙·역사",
    "POWER_SYSTEM": "힘·능력 체계",
    "LOCATION": "장소",
    "RACE": "종족",
    "MONSTER": "몬스터",
    "IMPORTANT_ITEM": "주요 아이템",
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
    episodes = ", ".join(str(episode) for episode in dataset.get("episodes") or [])
    lines = [
        "# 캐릭터·세계관 다단계 평가 결과",
        "",
        f"- 데이터셋: `{_inline(dataset.get('name', '-'))}` / "
        f"`{_inline(dataset.get('version', '-'))}`",
        f"- 모드·도메인: `{_inline(run.get('mode', '-'))}` / "
        f"`{_inline(', '.join(run.get('domains', [])) or '-')}`",
        f"- 회차: {_cell(episodes + '화') if episodes else '정보 없음'}",
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
        (
            "정답지의 설정과 모델이 추출한 설정을 한 쌍씩 묶어 비교합니다. "
            "'비교한 설정 쌍'에는 부분 일치도 포함됩니다. '대상·설정 항목 일치'는 "
            "설정 대상과 항목이 맞는 수이며, 추출한 내용이 맞는지는 '설정 내용 일치율'로 "
            "따로 확인합니다. 상세 결과의 '완전 일치'는 대상·항목·내용이 모두 맞는 경우입니다."
        ),
        "",
        (
            "P와 R의 '맞힘'도 대상·설정 항목 일치를 기준으로 합니다. 예를 들어 추출해야 할 "
            "설정 10개 중 3개만 추출했고 그 3개가 모두 이 기준에 맞으면 P는 100%, R은 30%입니다. "
            "가중 Recall은 정답지의 중요도에 따라 3·2·1의 가중치를 적용합니다."
        ),
        "",
        (
            "설정 대상은 누구·무엇에 관한 설정인지를 뜻하며, 세계관에서는 분류도 포함합니다. "
            "설정 항목은 캐릭터의 설정 종류·항목 이름, 세계관의 설정명·상위 범위를 비교합니다. "
            "내용과 근거의 일치율은 비교한 설정 쌍 중 해당 지표를 평가할 수 있는 항목을 기준으로 합니다. "
            "'추출 금지 항목을 추출한 횟수'는 정답지에서 명시적으로 추출을 금지한 항목을 뽑은 경우입니다."
        ),
        "",
        (
            "| 도메인 | 수량 | P (모델이 추출한 설정 중 맞힌 비율)<br>"
            "R (추출해야 할 전체 설정 중 맞힌 비율)<br>F1 (P와 R을 종합한 점수) | "
            "가중 Recall (중요도를 반영한 정답 검출률) | "
            "설정 대상 일치율 (누구·무엇에 관한 설정인가)<br>"
            "설정 항목 일치율 (설정명·범위 또는 설정 종류·항목 이름이 같은가)<br>"
            "설정 내용 일치율 (기록한 내용이 맞는가) | "
            "인용문 원문 확인율 (인용한 문장이 원문에 있는가)<br>"
            "정답 근거 포함률 (정답지의 근거 문장을 얼마나 찾았는가) |"
        ),
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
            f"정답지의 설정 {_count(counts.get('gold'))} · "
            f"모델이 추출한 설정 {_count(counts.get('predictions'))} · "
            f"비교한 설정 쌍 {_count(counts.get('matches'))} · "
            f"대상·설정 항목 일치 {_count(counts.get('identityTruePositive'))} · "
            f"추출하지 못한 설정 {missed} · "
            f"불필요하게 추출한 설정 {_count(counts.get('extra'))} · "
            f"추출 금지 항목을 추출한 횟수 {_count(counts.get('hardNegativeHits'))}"
        )
        lines.append(
            f"| {domain.upper()} | {_cell(quantity)} | "
            f"P {_format_ratio(metrics.get('candidatePrecision'))}<br>"
            f"R {_format_ratio(metrics.get('candidateRecall'))}<br>"
            f"F1 {_format_ratio(metrics.get('candidateF1'))} | "
            f"{_format_ratio(metrics.get('weightedRecall'))} | "
            f"설정 대상 {_format_axis_ratio(metrics.get('entityOrSubjectAccuracy'), axis_counts['subject'])}<br>"
            f"설정 항목 {_format_axis_ratio(metrics.get('pathOrFactAccuracy'), axis_counts['path'])}<br>"
            f"설정 내용 {_format_axis_ratio(metrics.get('valueAccuracy'), axis_counts['value'])} | "
            f"원문 확인 {_format_ratio(metrics.get('evidenceLocatableRate'))}<br>"
            f"정답 근거 포함 {_format_ratio(metrics.get('evidenceCoverageRate'))} |"
        )
        if counts.get("semanticPending", 0):
            lines.append(
                f"| ↳ {domain.upper()} 값 의미 판정 | resolved / lower-bound / coverage | "
                f"- | - | {_format_ratio(metrics.get('resolvedValueAccuracy'))} / "
                f"{_format_ratio(metrics.get('valueLowerBoundAccuracy'))} / "
                f"{_format_ratio(metrics.get('valueSemanticCoverage'))} | - |"
            )

    _append_stage2_summary(lines, stages)

    _append_end_to_end(lines, summary)
    _append_diagnostics(lines, diagnostics)
    lines.extend(
        [
            "> 점수는 관찰용이며, 자동 workflow는 실행·계약·fixture 검증 실패만 실패로 처리합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def _append_stage2_summary(lines: list[str], stages: dict[str, Any]) -> None:
    stage_by_domain = {domain: stages[domain]["stage2"] for domain in _DOMAINS}
    quantities: dict[str, dict[str, int]] = {}
    for domain, stage in stage_by_domain.items():
        counts = stage.get("counts", {})
        metrics = stage.get("metrics", {})
        gold = _int_or_zero(counts.get("gold"))
        included = _int_or_zero(counts.get("upstreamReached"))
        pending = _int_or_zero(counts.get("semanticPending"))
        resolved = max(0, included - pending)
        accuracy = metrics.get("resolvedFullDecisionAccuracy")
        if accuracy is None and pending == 0:
            accuracy = metrics.get("fullDecisionAccuracy")
        correct = _matched_count(accuracy, resolved)
        quantities[domain] = {
            "gold": gold,
            "waiting": _int_or_zero(counts.get("waitingForCharacterMatch")),
            "included": included,
            "correct": correct,
            "incorrect": resolved - correct,
            "missing": max(0, included - _int_or_zero(counts.get("reachedAndCompared"))),
            "pending": pending,
            "excluded": max(0, gold - included),
        }

    lines.extend([
        "",
        "## 2차 비교 집계",
        "",
        "2차 모델은 추출한 설정을 기존 데이터와 비교해 추가·수정·병합·제외 등을 판단합니다.",
        "오답에는 판단이 틀린 경우와 2차 결과가 없는 경우가 포함됩니다. 채점 제외는 해당 Gold의 채점 여부를 뜻하며 실제 2차 호출 여부를 나타내지 않습니다.",
        "채점 미완료는 표현이 다른 답안의 의미를 추가로 비교해야 하지만, 의미 채점을 실행하지 않는 등 판정 결과가 없어 정오를 확정하지 못한 경우입니다.",
        "",
        "### 수량",
        "",
        "| 항목 | 캐릭터 | 세계관 |",
        "| --- | ---: | ---: |",
    ])
    quantity_rows = (
        ("Gold (정답지의 처리 결정)", "gold"),
        ("인물 연결 대기 (정답지가 2차 진행을 보류한 1차 항목)", "waiting"),
        ("2차 채점에 포함 (필요한 설정이 1차에서 올바르게 추출됨)", "included"),
        ("정답 (처리 방식·대상·내용 등 필요한 판단 항목이 모두 맞음)", "correct"),
        ("오답 (판단이 틀렸거나 2차 결과가 없음)", "incorrect"),
        ("↳ 오답 중 2차 결과가 없는 경우", "missing"),
        ("채점 미완료 (모델 답안은 있으나 정답과 의미가 같은지 확인하지 못함)", "pending"),
        ("2차 채점에서 제외 (필요한 1차 설정이 없거나 잘못 추출됨)", "excluded"),
    )
    for label, key in quantity_rows:
        cells = [
            f"{quantities[domain][key]}개"
            if stage_by_domain[domain].get("evaluated", True)
            else "미평가"
            for domain in _DOMAINS
        ]
        lines.append(f"| {_cell(label)} | {' | '.join(cells)} |")
    if any(quantity["waiting"] for quantity in quantities.values()):
        lines.extend([
            "",
            (
                "인물 연결 대기는 정답지가 정한 정책 수이며, 2차 Gold나 채점 제외 수에 포함되지 않습니다. "
                "실제 추출 성공·누락 여부는 1차 표에서 확인하세요."
            ),
        ])

    lines.extend([
        "",
        "### 정확도",
        "",
        "| 항목 | 캐릭터 | 세계관 |",
        "| --- | ---: | ---: |",
    ])
    metric_rows = (
        ("2차 채점 포함률 (Gold 중 2차 채점에 포함된 비율)", "upstreamReachRate", None),
        ("Full accuracy (채점에 포함된 항목 중 판단 전체가 맞은 비율)", "fullDecisionAccuracy", None),
        ("operation 일치율 (추가·수정·병합·제외 등의 판단이 맞는가)", "operationAccuracy", None),
        ("기존 설정 선택 일치율 (비교하거나 변경할 기존 설정을 올바르게 골랐는가)", "targetAccuracy", None),
        ("반영할 내용 일치율 (최종적으로 기록할 내용이 맞는가)", "proposedValueAccuracy", None),
        ("설정 항목 일치율 (캐릭터의 최종 설정 항목이 맞는가)", "characterCanonicalFactKeyResolutionAccuracy", "character"),
        ("반영할 설정명·범위 일치율 (세계관 설정을 정답과 같은 이름·범위로 정리했는가)", "proposedPathAccuracy", "world"),
        ("시점 판단 일치율 (현재·과거·가정 등을 올바르게 구분했는가)", "temporalAccuracy", "character"),
        ("추출값 통합 판단 일치율 (값이 하나인지, 합칠 수 있는지, 서로 충돌하는지 맞혔는가)", "consolidationAccuracy", "world"),
        ("종료할 상태 선택 일치율 (끝내야 할 부상·중독 등의 상태를 올바르게 골랐는가)", "removedSnapshotSetAccuracy", "character"),
        ("함께 옮길 설정 선택 일치율 (새 범위로 함께 이동할 기존 설정을 올바르게 골랐는가)", "existingRootPropertyMoveSetAccuracy", "world"),
    )
    for label, key, applicable_domain in metric_rows:
        cells = [
            "해당 없음"
            if applicable_domain is not None and domain != applicable_domain
            else _stage2_metric_cell(stage_by_domain[domain], key)
            for domain in _DOMAINS
        ]
        lines.append(f"| {_cell(label)} | {' | '.join(cells)} |")
    for domain, stage in stage_by_domain.items():
        if not stage.get("evaluated", True):
            lines.extend(["", f"- {domain.upper()} 미평가: {_cell(stage.get('reason'))}"])
            continue
        metrics = stage.get("metrics", {})
        if quantities[domain]["pending"]:
            lines.extend([
                "",
                (
                    f"- {domain.upper()} resolved Full accuracy (채점이 끝난 항목만 계산): "
                    f"{_format_ratio(metrics.get('resolvedFullDecisionAccuracy'))}"
                ),
                (
                    f"- {domain.upper()} lower-bound Full accuracy (채점 미완료를 오답으로 계산): "
                    f"{_format_ratio(metrics.get('fullDecisionLowerBoundAccuracy'))}"
                ),
                (
                    f"- {domain.upper()} coverage (채점에 포함된 항목 중 정오 판정을 마친 비율): "
                    f"{_format_ratio(metrics.get('semanticCoverage'))}"
                ),
            ])


def _stage2_metric_cell(stage: dict[str, Any], key: str) -> str:
    if not stage.get("evaluated", True):
        return "미평가"
    metrics = stage.get("metrics", {})
    if key not in metrics:
        return "정보 없음"
    if metrics[key] is not None:
        return _format_ratio(metrics[key])
    if key == "fullDecisionAccuracy" and stage.get("counts", {}).get("semanticPending", 0):
        return "채점 미완료"
    coverage = metrics.get("proposedValueSemanticCoverage")
    if key == "proposedValueAccuracy" and coverage is not None and coverage < 1:
        return "채점 미완료"
    return "평가할 항목 없음"


def _append_end_to_end(lines: list[str], summary: dict[str, Any]) -> None:
    end_to_end = summary["endToEnd"]
    lines.extend(
        [
            "",
            "## 반영 후 데이터·변경 내역 평가",
            "",
            "회차별로 2차 판단을 평가용 데이터에 적용한 뒤, 남은 데이터와 변경 내역을 정답지와 비교합니다.",
            "변경 건수에는 설정값 외에 등장인물 등록·이력·구조화 데이터 등도 포함되므로 추출한 설정 개수와 다를 수 있습니다.",
            "",
            (
                "| 범위 | Precision (처리 후 데이터 중 정답과 일치하는 비율) | "
                "Recall (정답지에서 기대한 데이터 중 올바르게 반영된 비율) | "
                "F1 (Precision과 Recall을 종합한 점수) | "
                "coverage (전체 비교 항목 중 정오 판정이 끝난 비율)<br>"
                "pending (데이터는 있으나 정답과 의미가 같은지 채점하지 못한 항목 수) |"
            ),
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for domain in _DOMAINS:
        value = end_to_end.get("domains", {}).get(domain.upper(), {})
        if not value.get("evaluated", True):
            lines.append(f"| {domain.upper()} | 미평가 | - | - | {_cell(value.get('reason'))} |")
            continue
        lines.append(
            f"| {domain.upper()} after-state "
            f"(회차 처리 후 {'캐릭터' if domain == 'character' else '세계관'} 데이터) | "
            f"{_format_ratio(value.get('afterStatePrecision'))} | "
            f"{_format_ratio(value.get('afterStateRecall'))} | "
            f"{_format_ratio(value.get('afterStateF1'))} | "
            f"coverage {_format_ratio(value.get('semanticCoverage'))}<br>"
            f"pending {_count(value.get('semanticPending'))} |"
        )
        if value.get("semanticPending", 0):
            lines.append(
                f"| ↳ {domain.upper()} 의미 판정 | - | - | resolved (채점이 끝난 항목만 계산) "
                f"{_format_ratio(value.get('resolvedAfterStateF1'))}<br>"
                "lower-bound (채점 미완료를 오답으로 계산) "
                f"{_format_ratio(value.get('afterStateLowerBoundF1'))} | "
                f"{_format_ratio(value.get('semanticCoverage'))} / "
                f"{_count(value.get('semanticPending'))} |"
            )
    metrics = end_to_end.get("metrics", {})
    counts = end_to_end.get("counts", {})
    lines.extend(
        [
            (
                "| 전체 after-state (캐릭터·세계관 F1의 평균) | - | - | "
                f"{_format_ratio(metrics.get('afterStateF1'))} | - |"
            ),
            (
                "| 상태 전이 (처리 전후에 추가·수정·제거된 데이터의 변경 내역)<br>"
                f"Gold {_count(counts.get('expectedTransitions'))}건 · "
                f"모델 판단을 적용한 변경 {_count(counts.get('predictedTransitions'))}건 · "
                f"일치한 변경 {_count(counts.get('matchedTransitions'))}건 | "
                f"{_format_ratio(metrics.get('transitionPrecision'))} | "
                f"{_format_ratio(metrics.get('transitionRecall'))} | "
                f"{_format_ratio(metrics.get('transitionF1'))} | - |"
            ),
        ]
    )
    if any(
        value.get("semanticPending", 0)
        for value in end_to_end.get("domains", {}).values()
        if isinstance(value, dict)
    ):
        lines.append(
            "| ↳ 전체 after-state 의미 판정 | - | - | resolved (채점이 끝난 항목만 계산) "
            f"{_format_ratio(metrics.get('resolvedAfterStateF1'))}<br>"
            "lower-bound (채점 미완료를 오답으로 계산) "
            f"{_format_ratio(metrics.get('afterStateLowerBoundF1'))} | - |"
        )
    if counts.get("semanticPendingTransitions", 0):
        lines.append(
            "| ↳ 상태 전이 의미 판정 | "
            f"{_format_ratio(metrics.get('resolvedTransitionPrecision'))} | "
            f"{_format_ratio(metrics.get('resolvedTransitionRecall'))} | "
            "resolved (채점이 끝난 항목만 계산) "
            f"{_format_ratio(metrics.get('resolvedTransitionF1'))}<br>"
            "lower-bound (채점 미완료를 오답으로 계산) "
            f"{_format_ratio(metrics.get('transitionLowerBoundF1'))} | - |"
        )
    lines.extend(
        [
            "",
            (
                "- 상태 적용 오류 (2차 판단을 평가용 데이터에 반영하지 못한 횟수): "
                f"`{_count(counts.get('stateApplicationErrors'))}`"
            ),
            (
                "- dependency 상태 적용 오류 "
                "(평가 시작 데이터를 준비하는 앞선 회차에서 발생한 반영 오류): "
                f"`{_count(counts.get('dependencyStateApplicationErrors'))}`"
            ),
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
        failure_explanations = {
            "COMPARISON_ERROR": "2차 판단이 틀렸거나 결과가 없음",
            "EXTRACTION_MISS": "필요한 1차 설정을 제대로 확보하지 못해 2차 채점에서 제외됨",
            "UPSTREAM_FALSE_POSITIVE": "불필요하게 추출한 설정",
        }
        for cause, count in nonzero_failures:
            explanation = failure_explanations.get(cause)
            label = f"{cause} ({explanation})" if explanation else cause
            lines.append(f"| {_cell(label)} | {_count(count)} |")
    else:
        lines.append("| 없음 | 0 |")
    _append_runtime_failures(lines, summary.get("run", {}).get("runtimeFailures"))


def _append_runtime_failures(lines: list[str], value: Any) -> None:
    if not isinstance(value, dict) or not value.get("total"):
        return
    lines.extend(
        [
            "",
            "### 실행 중 기록된 오류 (개별 설정 처리 중 발생한 오류)",
            "",
            f"총 `{_count(value.get('total'))}`건입니다. 오류 메시지와 응답 본문은 공개하지 않습니다.",
            "같은 오류를 단계와 오류 유형으로 각각 집계한 표입니다. 두 집계의 건수를 서로 더하지 않습니다. 복구 성공 여부를 나타내는 수치는 아닙니다.",
            "",
            "| 분류 | 항목 | 건수 |",
            "| --- | --- | ---: |",
        ]
    )
    explanations = {
        "CHARACTER_STAGE1": "캐릭터 설정의 1차 추출 단계",
        "CHARACTER_STAGE2": "캐릭터 설정의 2차 비교 단계",
        "WORLD_STAGE1": "세계관 설정의 1차 추출 단계",
        "WORLD_STAGE2": "세계관 설정의 2차 비교 단계",
        "ComparisonValidationError": "2차 비교 결과가 정해진 형식이나 처리 규칙을 충족하지 못함",
    }
    for group, label in (("byStage", "단계"), ("byErrorType", "오류 유형")):
        counts = value.get(group, {})
        if not isinstance(counts, dict):
            continue
        for name, count in sorted(counts.items()):
            explanation = explanations.get(name)
            display_name = f"{name} ({explanation})" if explanation else name
            lines.append(f"| {label} | {_cell(display_name)} | {_count(count)} |")


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
                    "**1차 추출 결과** · 완전 일치 "
                    f"{counts['FULL_MATCH']} · 부분 일치 {counts['PARTIAL_MATCH']} · "
                    f"추출하지 못한 설정 {counts['MISSED']} · "
                    f"불필요하게 추출한 설정 {counts['EXTRA']}"
                )
                lines.append("")
                remaining_rows = _append_case_tables(
                    lines,
                    stage1_cases,
                    lambda case, domain=domain: _stage1_row(case, domain),
                    (
                        "판정",
                        "Gold ID (답지 항목 식별번호)",
                        "추출 ID (모델 추출 항목 식별번호)",
                        "인물 (누구에 관한 정보인가)"
                        if domain == "character"
                        else "설정 대상 (분류·누구 또는 무엇에 관한 정보인가)",
                        "설정 분류·세부 항목" if domain == "character" else "설정 범위·세부 항목",
                        "설정값 (해당 항목의 구체적인 내용)",
                        "판정 이유",
                    ),
                    remaining_rows,
                )
            if stage2_cases:
                counts = Counter(case["result"] for case in stage2_cases)
                lines.append(
                    "**2차 처리 판단 결과** · "
                    + " · ".join(
                        f"{_result_label(result)} {counts[result]}"
                        for result in (
                            "FULL_MATCH",
                            "DECISION_MISMATCH",
                            "COMPARATOR_MISSING",
                            "SEMANTIC_PENDING",
                            "UPSTREAM_BLOCKED",
                        )
                    )
                )
                lines.append("")
                remaining_rows = _append_case_tables(
                    lines,
                    stage2_cases,
                    lambda case, sources=stage1_cases: _stage2_row(case, sources),
                    (
                        "채점 결과",
                        "관련 ID (2차 답지 항목·1차 답지 항목·모델 추출 항목)",
                        "답지에서 기대한 처리",
                        "모델이 판단한 처리",
                        "판정 이유",
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


def _stage1_row(case: dict[str, Any], domain: str = "character") -> tuple[str, ...]:
    expected = case.get("expected") or {}
    actual = case.get("actual") or {}
    return (
        _cell(_result_label(case["result"])),
        _cell(", ".join(case.get("goldIds", [])) or "-"),
        _cell(case.get("predictionId")),
        _comparison_cell(
            _subject_label(expected.get("subject"), domain),
            _subject_label(actual.get("subject"), domain),
        ),
        _comparison_cell(
            _path_label(expected.get("path"), domain),
            _path_label(actual.get("path"), domain),
        ),
        _comparison_cell(expected.get("value"), actual.get("value")),
        _diagnostic_reason(case, domain),
    )


def _stage2_row(
    case: dict[str, Any],
    source_cases: list[dict[str, Any]] | None = None,
) -> tuple[str, ...]:
    domain = str(case.get("domain", "CHARACTER")).lower()
    gold_ids = set(case.get("sourceGoldIds", []))
    source_id = case.get("sourceCandidateId")
    expected_subjects = dict.fromkeys(
        (source.get("expected") or {}).get("subject")
        for source in source_cases or []
        if gold_ids & set(source.get("goldIds", []))
    )
    actual_subjects = dict.fromkeys(
        (source.get("actual") or {}).get("subject")
        for source in source_cases or []
        if source_id and source.get("predictionId") == source_id
    )
    source_ids = ", ".join(case.get("sourceGoldIds", [])) or "-"
    identifiers = "<br>".join(
        _cell(item)
        for item in (
            f"2차 답지: {case.get('decisionId') or '-'}",
            f"1차 답지: {source_ids}",
            f"모델 추출: {case.get('sourceCandidateId') or '2차 답안에 연결된 추출 항목 없음'}",
        )
    )
    return (
        _cell(_result_label(case["result"])),
        identifiers,
        _stage2_shape(
            case.get("expected") or {},
            domain,
            ", ".join(subject for subject in expected_subjects if subject),
        ),
        _stage2_shape(
            case.get("actual") or {},
            domain,
            ", ".join(subject for subject in actual_subjects if subject),
        )
        if case.get("actual")
        else "이 답지 항목과 연결된 2차 결과 없음",
        _diagnostic_reason(case, domain, source_cases),
    )


def _stage2_shape(
    value: dict[str, Any],
    domain: str = "character",
    subject: str = "",
) -> str:
    if not value:
        return "-"
    parts = []
    if subject:
        title = "인물" if domain == "character" else "설정 대상"
        parts.append(f"{title}: {_subject_label(subject, domain)}")
    if value.get("operation"):
        parts.append("처리 방식: " + _explained(value["operation"], _OPERATION_LABELS))
    if value.get("target"):
        parts.append("처리할 기존 설정: " + _subject_label(value["target"], domain))
    if value.get("path"):
        path_title = "설정 분류·세부 항목" if domain == "character" else "설정 범위·세부 항목"
        parts.append(f"{path_title}: {_path_label(value['path'], domain)}")
    if value.get("value") not in (None, ""):
        parts.append(f"설정값: {value['value']}")
    if value.get("temporalScope") is not None:
        parts.append(
            "정보의 시점: "
            + _explained(
                value["temporalScope"],
                {
                    "PRESENT": "현재의 정보",
                    "PAST": "과거의 정보",
                    "HYPOTHETICAL": "가정 속 정보",
                },
            )
        )
    if value.get("consolidationStatus") is not None:
        parts.append(
            "추출값 통합 판단: "
            + _explained(
                value["consolidationStatus"],
                {
                    "SINGLE": "추출값 하나를 사용",
                    "MERGED": "여러 추출값을 하나로 합침",
                    "CONFLICT": "추출값이 서로 충돌하여 하나로 확정하지 못함",
                },
            )
        )
    removed_paths = value.get("removedPaths") or []
    if removed_paths:
        parts.extend("종료할 상태: " + _subject_label(path, domain) for path in removed_paths)
    elif value.get("removedCount"):
        parts.append(f"종료할 상태: {value['removedCount']}건")
    root_move_names = value.get("rootMoveNames") or []
    if root_move_names:
        parts.extend(f"새 범위로 함께 옮길 기존 설정: {name}" for name in root_move_names)
    elif value.get("rootMoveCount"):
        parts.append(f"새 범위로 함께 옮길 기존 설정: {value['rootMoveCount']}건")
    return "<br>".join(_cell(part) for part in parts) or "-"


def _diagnostic_reason(
    case: dict[str, Any],
    domain: str = "character",
    source_cases: list[dict[str, Any]] | None = None,
) -> str:
    result = case["result"]
    if result == "MISSED":
        return "이 답지 항목에 대응하는 모델 추출 결과를 찾지 못했습니다."
    if result == "EXTRA":
        return "모델이 이 정보를 추출했지만 답지에서 대응하는 항목을 찾지 못했습니다."
    if result == "COMPARATOR_MISSING":
        return (
            "필요한 설정은 1차에서 추출됐지만 이 항목의 2차 처리 결과가 없어 오답으로 채점했습니다."
        )
    if result == "UPSTREAM_BLOCKED":
        reasons = {
            "UPSTREAM_MISSING": "답지에서 요구한 설정과 연결된 1차 추출 결과가 없습니다.",
            "UPSTREAM_BLOCKED_SUBJECT": (
                "1차에서 추출한 정보의 인물이 답지와 다릅니다."
                if domain == "character"
                else "1차에서 추출한 정보의 분류·설정 대상이 답지와 다릅니다."
            ),
            "UPSTREAM_VALUE_ERROR": "1차에서 추출한 정보의 분류·항목 또는 설정값이 답지와 다릅니다.",
            "UPSTREAM_PARTIAL": "이 처리에 필요한 1차 설정 중 일부만 답지와 연결되었습니다.",
        }
        parts = [
            reasons.get(
                case.get("upstreamOutcome"),
                "필요한 1차 설정이 이 답지 항목의 채점 조건을 충족하지 못했습니다.",
            )
        ]
        for source in source_cases or []:
            if set(source.get("goldIds", [])) & set(case.get("sourceGoldIds", [])):
                parts.extend(_field_difference_lines(source, domain))
        parts.append("따라서 이 항목의 2차 판단은 채점에서 제외했습니다.")
        return "<br>".join(_cell(part) for part in parts)
    if (
        result == "FULL_MATCH"
        and domain == "character"
        and case.get("stage2Policy") == "WAIT_FOR_CHARACTER_MATCH"
    ):
        return (
            "채점한 1차 항목이 모두 답지와 일치합니다.<br>"
            "인물 연결 대기 (답지는 인물 연결 전 2차 비교를 요구하지 않음)"
        )
    parts = _field_difference_lines(case, domain)
    if result == "PARTIAL_MATCH":
        matched = [
            _axis_label(name, domain)
            for name, status in case.get("fields", {}).items()
            if status == "MATCH"
        ]
        if matched:
            parts.insert(0, "답지와 일치하는 부분: " + ", ".join(matched))
        if "PENDING" in case.get("fields", {}).values():
            parts.append("의미 판정이 끝나지 않은 항목이 있어 채점을 완료하지 못했습니다.")
    if result == "SEMANTIC_PENDING":
        parts.append(
            "2차 답안은 있으나 답지와 의미가 같은지 확인하지 못해 채점을 완료하지 못했습니다."
        )
    return "<br>".join(_cell(part) for part in parts) or "채점한 항목이 모두 답지와 일치합니다."


def _field_difference_lines(case: dict[str, Any], domain: str) -> list[str]:
    fields = case.get("fields", {})
    keys = {
        "canonicalPath": "path",
        "proposedPath": "path",
        "temporal": "temporalScope",
        "consolidation": "consolidationStatus",
    }
    expected, actual = case.get("expected") or {}, case.get("actual") or {}
    parts = []
    if domain == "character" and (name_reason := _character_setting_name_reason(case)):
        parts.append(name_reason)
    if domain == "world" and (name_reason := _setting_name_match_reason(case)):
        parts.append(name_reason)
    if domain == "world" and (
        property_reason := _setting_name_match_reason(case, key="matchedPropertyNameMatch")
    ):
        parts.append(f"비교 대상인 기존 설정명: {property_reason}")
    for name, status in fields.items():
        label = _axis_label(name, domain)
        if domain == "world" and name in {"path", "proposedPath"}:
            scope_reason = _world_scope_match_reason(case, status)
            if scope_reason:
                parts.append(scope_reason)
                continue
        if status == "PENDING":
            parts.append(f"{label}: 답지와 의미가 같은지 확인하지 못했습니다.")
        elif status == "MISMATCH":
            if name == "stateApplication":
                parts.append("모델의 2차 결과가 상태 반영 규칙을 위반해 실제 반영에 실패했습니다.")
                continue
            if name == "pathPreservation":
                parts.append(
                    "기존 설정을 수정·병합할 때는 비교 대상으로 지정한 범위·설정명을 유지해야 합니다."
                )
                continue
            key = keys.get(name, name)
            if key in expected or key in actual:
                gold_value = expected.get(key)
                model_value = actual.get(key)
                if name == "operation":
                    gold_value = _explained(gold_value, _OPERATION_LABELS)
                    model_value = _explained(model_value, _OPERATION_LABELS)
                parts.append(
                    f"{label} 불일치 — 답지: {gold_value or '표시된 내용 없음'} / "
                    f"모델: {model_value or '표시된 내용 없음'}"
                )
            else:
                parts.append(f"{label}: 답지와 다릅니다.")
    return parts


def _character_setting_name_reason(case: dict[str, Any]) -> str | None:
    fields = case.get("fields", {})
    if fields.get("canonicalPath", fields.get("path")) != "MATCH":
        return None
    expected, actual = case.get("expected") or {}, case.get("actual") or {}
    paths = (expected.get("path"), actual.get("path"))
    if not all(isinstance(path, str) for path in paths):
        return None
    keys = [normalize_fact_key(path.rsplit(" › ", 1)[-1]) for path in paths]
    if keys[0] == keys[1] or not all(
        key.startswith("status.") and key.removeprefix("status.") and "*" not in key
        for key in keys
    ):
        return None
    return "이름은 다르지만 문맥상 같은 상태 항목으로 판단했습니다."


def _world_scope_match_reason(case: dict[str, Any], status: str) -> str | None:
    name_match = _sanitize_setting_name_match(case.get("settingNameMatch"))
    if name_match is None or name_match["status"] != "MATCH":
        return None
    expected, actual = case.get("expected") or {}, case.get("actual") or {}
    expected_path, actual_path = expected.get("path"), actual.get("path")
    if not isinstance(expected_path, str) or not isinstance(actual_path, str):
        return None
    expected_scope = expected_path.rpartition(" › ")[0]
    actual_scope = actual_path.rpartition(" › ")[0]
    if expected_scope == actual_scope:
        return None
    comparison = (
        f"답지: {expected_scope or '상위 범위 없음'} / 모델: {actual_scope or '상위 범위 없음'}"
    )
    if status == "MATCH":
        return "상위 범위 표현은 다르지만 같은 설정을 묶는 범위로 인정했습니다. " + comparison
    if status == "PENDING":
        return "상위 범위: 답지와 의미가 같은지 확인하지 못했습니다. " + comparison
    if status == "MISMATCH":
        return "상위 범위 불일치 — " + comparison
    return None


def _setting_name_match_reason(
    case: dict[str, Any], *, key: str = "settingNameMatch"
) -> str | None:
    match = _sanitize_setting_name_match(case.get(key))
    if match is None:
        return None
    if match["status"] == "PENDING":
        return "설정명의 의미를 확인하지 못했습니다."
    if match["method"] == "ALIAS" and match["status"] == "MATCH":
        return "정답지에 등록된 별칭으로 같은 설정 항목임을 인정했습니다."
    if match["method"] == "SEMANTIC":
        if match["status"] == "MATCH":
            return "이름은 다르지만 문맥상 같은 설정 항목으로 판단했습니다."
        return "문맥상 다른 설정 항목으로 판단했습니다."
    return None


def _axis_label(name: str, domain: str) -> str:
    if name == "subject":
        return "인물" if domain == "character" else "분류·설정 대상"
    if name == "path" and domain == "world":
        return "설정 범위·세부 항목"
    return _AXIS_LABELS.get(name, name)


def _explained(value: Any, labels: dict[str, str]) -> str:
    text = str(value or "")
    return f"{text} ({labels[text]})" if text in labels else text


def _path_label(value: Any, domain: str) -> str:
    if not value:
        return ""
    if domain != "character":
        return str(value)
    parts = str(value).split(" › ")
    if len(parts) == 1:
        fact_type = {
            "profile": "PROFILE",
            "status": "STATUS",
            "item": "ITEM",
            "stats": "STAT",
            "skill": "SKILL",
            "level": "LEVEL",
        }.get(parts[0].split(".")[0])
        if fact_type:
            parts.insert(0, fact_type)
    return " › ".join(_explained(part, _FACT_TYPE_LABELS | _FACT_KEY_LABELS) for part in parts)


def _subject_label(value: Any, domain: str) -> str:
    if not value:
        return ""
    parts = str(value).split(" · ")
    if domain == "world" and len(parts) > 1:
        parts[0] = _explained(parts[0], _WORLD_CATEGORY_LABELS)
    elif domain == "character":
        parts = [_path_label(part, domain) if " › " in part else part for part in parts]
    text = " · ".join(parts)
    for suffix in (" (ref에서 canonical 경로 해석)", " (주체, ref에서 해석)"):
        if text.endswith(suffix):
            return text.removesuffix(suffix) + " (설정 식별번호로 확인)"
    return text


def _comparison_cell(expected: Any, actual: Any) -> str:
    return f"답지: {_cell(expected)}<br>모델: {_cell(actual)}"


def _result_label(value: str) -> str:
    return {
        "FULL_MATCH": "완전 일치",
        "PARTIAL_MATCH": "부분 일치",
        "MISSED": "추출하지 못한 설정",
        "EXTRA": "불필요하게 추출한 설정",
        "UPSTREAM_BLOCKED": "2차 채점 제외 (필요한 설정이 1차에서 추출되지 않았거나 잘못 추출됨)",
        "COMPARATOR_MISSING": "2차 답안 없음 (필요한 설정은 1차에서 추출됐지만 2차 처리 결과가 없음)",
        "SEMANTIC_PENDING": "채점 미완료 (2차 답안은 있으나 답지와 의미가 같은지 확인하지 못함)",
        "DECISION_MISMATCH": "판단 불일치 (처리 방식이나 반영할 정보 등이 답지와 다름)",
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
    sanitized = {
        "result": result,
        "goldIds": _text_list(value.get("goldIds")),
        "predictionId": _text(value.get("predictionId")),
        "expected": _sanitize_fact_summary(value.get("expected")),
        "actual": _sanitize_fact_summary(value.get("actual")),
        "fields": _sanitize_fields(value.get("fields"), _STAGE1_FIELD_ORDER),
        "upstreamOutcome": _text(value.get("upstreamOutcome")),
    }
    if (name_match := _sanitize_setting_name_match(value.get("settingNameMatch"))) is not None:
        sanitized["settingNameMatch"] = name_match
    if (policy := _choice(value.get("stage2Policy"), _STAGE2_POLICIES)) is not None:
        sanitized["stage2Policy"] = policy
    return sanitized


def _sanitize_stage2_case(value: dict[str, Any]) -> dict[str, Any] | None:
    result = _choice(value.get("result"), _STAGE2_RESULTS)
    domain = _choice(value.get("domain"), {item.upper() for item in _DOMAINS})
    if result is None or domain is None:
        return None
    sanitized = {
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
    if (name_match := _sanitize_setting_name_match(value.get("settingNameMatch"))) is not None:
        sanitized["settingNameMatch"] = name_match
    if (
        property_match := _sanitize_setting_name_match(value.get("matchedPropertyNameMatch"))
    ) is not None:
        sanitized["matchedPropertyNameMatch"] = property_match
    return sanitized


def _sanitize_setting_name_match(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    status = _choice(value.get("status"), _SETTING_NAME_MATCH_STATUSES)
    method = _choice(value.get("method"), _SETTING_NAME_MATCH_METHODS)
    if status is None or method is None:
        return None
    # Model reasons may quote private evidence; public explanations are fixed copy.
    return {"status": status, "method": method}


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
