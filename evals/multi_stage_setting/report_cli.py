import argparse
import json
from pathlib import Path
from typing import Any


def build_source_free_summary(report: dict[str, Any]) -> dict[str, Any]:
    """원문·evidence·개별 판단 문장을 제외한 집계값만 artifact로 남긴다."""

    stages = report.get("stages", {})
    return {
        "reportVersion": report.get("reportVersion"),
        "run": report.get("run", {}),
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


def render_markdown_summary(report: dict[str, Any]) -> str:
    summary = build_source_free_summary(report)
    run = summary["run"]
    dataset = summary["dataset"]
    stages = summary["stages"]
    lines = [
        "# 캐릭터·세계관 다단계 평가 결과",
        "",
        f"- 데이터셋: `{dataset.get('name', '-')}` / `{dataset.get('version', '-')}`",
        f"- 모드: `{run.get('mode', '-')}`",
        f"- 도메인: `{', '.join(run.get('domains', [])) or '-'}`",
        f"- 회차: `{dataset.get('episodes', [])}`",
        f"- Fixture hash: `{dataset.get('fixtureHash', '-')}`",
        "",
        "| 도메인 | 단계 | 핵심 지표 | 결과 |",
        "| --- | --- | --- | ---: |",
    ]
    for domain in ("character", "world"):
        stage1 = stages[domain]["stage1"]
        stage2 = stages[domain]["stage2"]
        lines.append(
            f"| {domain.upper()} | 1차 | Candidate F1 | "
            f"{_format_ratio(stage1.get('metrics', {}).get('candidateF1'))} |"
        )
        lines.append(
            f"| {domain.upper()} | 2차 | Full decision accuracy | "
            f"{_format_ratio(stage2.get('metrics', {}).get('fullDecisionAccuracy'))} |"
        )
    end_to_end = summary["endToEnd"]
    lines.extend(
        [
            f"| 전체 | 누적 상태 | After-state F1 | "
            f"{_format_ratio(end_to_end.get('metrics', {}).get('afterStateF1'))} |",
            "",
            f"- 상태 적용 오류: `{end_to_end.get('counts', {}).get('stateApplicationErrors', 0)}`",
            f"- 실패 원인 집계: `{json.dumps(summary['failureCauses'], ensure_ascii=False)}`",
            "",
            "> 점수는 관찰용이며, 자동 workflow는 실행·계약·fixture 검증 실패만 실패로 처리합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def _aggregate_only(
    value: dict[str, Any],
    *,
    include_domains: bool = False,
) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in ("evaluated", "reason", "metrics", "counts")
        if key in value
    }
    if include_domains:
        result["domains"] = value.get("domains", {})
    return result


def _format_ratio(value: Any) -> str:
    return "대상 없음/미판정" if value is None else f"{float(value) * 100:.2f}%"


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
        description="Render source-free aggregate artifacts from a setting-eval/v3 report."
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    main()
