"""File-only CLI: validate/export/score/compare. No .env, DB, network or provider client."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path

from pydantic import ValidationError

from app.chatbot.evaluation.contracts import Dataset, ReviewSet, Run, fingerprint
from app.chatbot.evaluation.scoring import compare, export_inputs, score


def read(path, model):
    return model.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write(path, content):
    # Captures/review templates can contain private answers; default permissions are local only.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def verify_trace(run, path):
    raw = Path(path).read_bytes()
    if sha256(raw).hexdigest() != run.manifest.trace_sha256:
        raise ValueError("Capture trace hash mismatch")
    trace = json.loads(raw)
    observed = [item.model_dump(mode="json") for item in run.observations]
    if not isinstance(trace, dict) or trace.get("observations_sha256") != fingerprint(observed):
        raise ValueError("Normalized observations do not belong to this trace")


def markdown(report):
    reports = report.get("reports", {report.get("manifest", {}).get("mode", "single"): report})
    lines = [
        "# 챗봇 저장 결과 평가",
        "",
        "실행 종류와 검토 범위를 먼저 확인하세요. 이 보고서만으로 베타 출시를 승인하지 않습니다.",
        "AUTHORED_CONTROL은 채점기 대조군, SCRIPTED_REPLAY는 고정 출력 연결 검증입니다.",
        "의미 판정은 별도 검토자의 판단이며 ID/인용 형식 검사만으로 자동 확정하지 않습니다.",
        "",
        "| 방식 | 실행 종류 | 문항 | PASS / FAIL / PENDING | 응답률(답변 가능 문항) | 검토율 | 실제 비용 USD |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for mode, item in reports.items():
        summary, measured = item["summary"], item["measurements"]
        counts = summary["verdicts"]
        response = summary["answerable_response_rate"]
        review = summary["review_coverage"]
        cost = measured["total_cost_usd"]
        lines.append(
            f"| {mode} | {measured['kind']} | {summary['cases']} | "
            f"{counts.get('PASS', 0)} / {counts.get('FAIL', 0)} / {counts.get('PENDING', 0)} | "
            f"{response['numerator']}/{response['denominator']} | "
            f"{review['numerator']}/{review['denominator']} | {cost if cost is not None else '미측정/불완전'} |"
        )
    lines += [
        "",
        "분모가 0인 지표는 JSON에서 null입니다. 미검토·누락 실행은 성공으로 세지 않습니다.",
        "질문 유형·정보 접근 가능성(FULL/LIMITED)·근거 지지·필수 사실 누락은 report.json에서 분리합니다.",
        "실제 provider 계측을 제공하지 않은 실행의 합성 사용량·실행 시간을 실비·지연으로 표시하지 않습니다.",
        "원문/정답/개별 답변은 이 요약에 복사하지 않습니다.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "export-inputs", "score", "compare"):
        sub = commands.add_parser(name)
        sub.add_argument("--dataset", type=Path, required=True)
        if name == "export-inputs":
            sub.add_argument("--output", type=Path, required=True)
        if name in {"score", "compare"}:
            multiple = "+" if name == "compare" else None
            sub.add_argument("--run", type=Path, nargs=multiple, required=True)
            sub.add_argument("--trace", type=Path, nargs=multiple, required=True)
            sub.add_argument("--reviews", type=Path, nargs=multiple)
            sub.add_argument("--output-dir", type=Path, required=True)
    schemas = commands.add_parser("schemas")
    schemas.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "schemas":
            args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            for name, model in (("dataset", Dataset), ("run", Run), ("reviews", ReviewSet)):
                write(args.output_dir / f"{name}.schema.json", json_text(model.model_json_schema()))
            return 0
        dataset = read(args.dataset, Dataset)
        if args.command == "validate":
            print(
                json_text(
                    {
                        "dataset_sha256": fingerprint(dataset),
                        "version": dataset.version,
                        "questions": len(dataset.questions),
                        "evidence": len(dataset.evidence),
                        "gold_status": dataset.gold_status,
                        "categories": sorted({q.category for q in dataset.questions}),
                    }
                ),
                end="",
            )
            return 0
        if args.command == "export-inputs":
            write(args.output, json_text(export_inputs(dataset)))
            return 0
        if args.command == "score":
            run = read(args.run, Run)
            verify_trace(run, args.trace)
            reviews = read(args.reviews, ReviewSet) if args.reviews else None
            report = score(dataset, run, reviews)
        else:
            if len(args.run) != len(args.trace):
                raise ValueError("Each run needs its own raw capture trace")
            runs = [read(path, Run) for path in args.run]
            for run, trace in zip(runs, args.trace, strict=True):
                verify_trace(run, trace)
            review_sets = [read(path, ReviewSet) for path in args.reviews or []]
            by_hash = {r.run_sha256: r for r in review_sets}
            if len(by_hash) != len(review_sets) or not by_hash.keys() <= {
                fingerprint(run) for run in runs
            }:
                raise ValueError("Duplicate or unrelated review set")
            report = compare(
                dataset,
                runs,
                {
                    run.manifest.mode: by_hash[fingerprint(run)]
                    for run in runs
                    if fingerprint(run) in by_hash
                },
            )
        args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        write(args.output_dir / "report.json", json_text(report))
        write(args.output_dir / "summary.md", markdown(report))
        return 0
    except (OSError, ValueError, ValidationError):
        # ValidationError/exception strings can contain entire submitted answers or private paths.
        parser.exit(
            2, "Invalid evaluation input, hash binding or output destination; no score issued.\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
