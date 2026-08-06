import argparse
import json
from pathlib import Path
from typing import Any


METRIC_LABELS = (
    ("detectionPrecision", "Detection Precision"),
    ("detectionRecall", "Detection Recall"),
    ("detectionF1", "Detection F1"),
    ("factPrecision", "Fact Precision"),
    ("factRecall", "Fact Recall"),
    ("factF1", "Fact F1"),
    ("weightedDetectionRecall", "중요도 가중 Detection Recall"),
    ("weightedFactRecall", "중요도 가중 Fact Recall"),
    ("valueTypeAccuracy", "valueType Accuracy"),
    ("attributeValueAccuracy", "attributeValue Accuracy"),
    ("structuredValueAccuracy", "structuredValue Accuracy"),
    ("evidenceLocatableRate", "원문에서 찾을 수 있는 근거 비율"),
    ("goldEvidenceCoverageRate", "정답 근거 범위 포괄 비율"),
    ("hardNegativeViolationRate", "DO_NOT_EXTRACT 위반율"),
    ("duplicatePredictionRate", "중복 예측 비율"),
    ("unknownSubjectPredictionRate", "미상 주체 예측 비율"),
    ("subjectOnlyFailureRate", "주체 해소만 실패한 비율"),
)


def main() -> None:
    args = _parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    markdown = render_markdown_summary(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    if args.machine_output is not None:
        args.machine_output.parent.mkdir(parents=True, exist_ok=True)
        args.machine_output.write_text(
            json.dumps(build_machine_summary(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(markdown)


def build_machine_summary(report: dict[str, Any]) -> dict[str, Any]:
    """원문 파생 상세값 없이 비교 가능한 집계 결과만 반환한다."""

    return {
        "run": report.get("run", {}),
        "dataset": report.get("dataset", {}),
        "metrics": report.get("metrics", {}),
        "counts": report.get("counts", {}),
    }


def render_markdown_summary(report: dict[str, Any]) -> str:
    run = report.get("run", {})
    dataset = report.get("dataset", {})
    metrics = report.get("metrics", {})
    counts = report.get("counts", {})
    judge_model = (
        run.get("semanticJudgeModel", "-")
        if run.get("semanticJudgeEnabled")
        else "사용 안 함"
    )
    lines = [
        "# 설정 추출 평가 결과",
        "",
        f"- 데이터셋: `{dataset.get('name', '-')}`",
        f"- 스냅샷 버전: `{dataset.get('version', '-')}`",
        f"- 평가 회차: `{dataset.get('episodeCount', 0)}`개",
        f"- 분석 모델: `{run.get('analysisModel') or '-'}`",
        f"- 의미 판정 모델: `{judge_model}`",
        "- 판정 정책: 낮은 점수만으로 워크플로를 실패시키지 않음",
        "",
        "## 핵심 지표",
        "",
        "| 지표 | 결과 |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {label} | {_format_metric(metrics.get(key))} |"
        for key, label in METRIC_LABELS
    )
    lines.extend(
        [
            "",
            "## 평가 개수",
            "",
            "| 구분 | 개수 |",
            "| --- | ---: |",
            f"| EXTRACT 정답 | {counts.get('goldExtract', 0)} |",
            f"| 채점 대상 예측 | {counts.get('predictions', 0)} |",
            f"| Detection 매칭 | {counts.get('detectionMatches', 0)} |",
            f"| Fact 정답 | {counts.get('factCorrect', 0)} |",
            f"| 의미 판정 대기 | {counts.get('semanticPending', 0)} |",
            f"| REVIEW 제외 예측 | {counts.get('reviewExcludedPredictions', 0)} |",
            f"| 캐릭터 발견 후보 제외 | {counts.get('characterDiscoveryExcluded', 0)} |",
            f"| LLM Judge 입력 토큰 | {counts.get('judgeInputTokens', 0)} |",
            f"| LLM Judge 캐시 입력 토큰 | {counts.get('judgeCachedInputTokens', 0)} |",
            f"| LLM Judge 출력 토큰 | {counts.get('judgeOutputTokens', 0)} |",
            "",
            "> 점수는 관찰·비교용입니다. 인증, Notion 데이터 형식, private 원고 조회, "
            "분석 또는 평가 실행이 실패한 경우에만 GitHub Actions가 실패합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def _format_metric(value: Any) -> str:
    if value is None:
        return "판정 대기/대상 없음"
    if isinstance(value, bool):
        return "완료" if value else "미완료"
    if isinstance(value, (int, float)):
        return f"{value * 100:.2f}%"
    return str(value)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a compact Markdown summary from a setting extraction report."
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--machine-output",
        type=Path,
        default=None,
        help="Write a source-free JSON summary containing dataset, metrics, and counts only.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
