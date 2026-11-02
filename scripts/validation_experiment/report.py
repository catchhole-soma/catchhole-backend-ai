"""Experiment report: judge agreement (first two judge labels), answer-form errors, flow, cost/latency and typo.

Reads `<out>/judge/<label>/{result.json,ledger.jsonl}` and `<out>/typo/<label>/{result.json,ledger.jsonl}` and
writes `<out>/report/{report.json,report.md,disagreements.csv}`.
"""

import csv
import json
import statistics
from collections import Counter
from pathlib import Path

from scripts.validation_experiment.common import read_json, usd, write_json

OUTCOMES = ("CONFLICT", "SUSPECTED_CONFLICT", "NEEDS_CONTEXT", "NO_CONFLICT", "INCOMPLETE")
FINDINGS = {"CONFLICT", "SUSPECTED_CONFLICT"}
SOFT = ("SPAN_REANCHORED", "TARGET_REF_ALIAS", "REQUIRED_READS_COMPLETED", "CHANGE_SEARCH_RECEIPT_FILLED",
        "FOLLOW_UP_DECISION_FIELDS_IGNORED", "NO_CONFLICT_IMPACT_CLEARED", "TARGET_SPANS_REPLACED",
        "DECIDED_REQUESTS_IGNORED", "COMPARISON_CONTEXT_TARGET_FILLED")


def ledger_rows(directory):
    path = Path(directory) / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def cost(rows):
    by_role = {}
    for row in rows:
        role = by_role.setdefault(row["role"], {"calls": 0, "unknownUsage": 0, "inputTokens": 0,
                                                "cachedInputTokens": 0, "outputTokens": 0, "usd": 0.0,
                                                "latencyMs": [], "models": set(), "efforts": set()})
        role["calls"] += 1
        role["models"].add(row["model"])
        role["efforts"].add(row.get("effort"))
        if row.get("kind") != "SETTLE":
            role["unknownUsage"] += 1
            continue
        role["inputTokens"] += row["inputTokens"]
        role["cachedInputTokens"] += row.get("cachedInputTokens") or 0
        role["outputTokens"] += row["outputTokens"]
        role["usd"] += usd(row["model"], row["inputTokens"], row["outputTokens"]) or 0.0
        role["latencyMs"].append(row["latencyMs"])
    for role in by_role.values():
        latencies = role.pop("latencyMs")
        role.update(models=sorted(role["models"]), efforts=sorted(str(e) for e in role["efforts"]),
                    usd=round(role["usd"], 6), latencyP50Ms=percentile(latencies, 0.5),
                    latencyP95Ms=percentile(latencies, 0.95))
    return by_role


def outcome_of(run, ref):
    decision = run["decisions"].get(ref)
    return decision["outcome"] if decision else "INCOMPLETE"


def judge_summary(run, rows):
    refs = [question["ref"] for question in run["questions"]]
    traces = run.get("traces", {})
    calls = Counter(len(traces.get(ref, [])) for ref in refs)
    follow_ups = sum(any(row.get("action") == "FOLLOW_UP" for row in traces.get(ref, [])) for ref in refs)
    per_call_errors = Counter(row["error"] for ref in refs for row in traces.get(ref, []) if row.get("error"))
    causes = Counter(failure.get("cause") or failure["code"] for failure in run["questionFailures"].values())
    searches = [sum(row.get("searches", 0) for row in traces.get(ref, [])) for ref in refs]
    return {
        "label": run["label"], "model": run["model"], "effort": run["effort"], "status": run["status"],
        "wallSeconds": run["wallSeconds"], "questions": len(refs),
        "outcomes": dict(Counter(outcome_of(run, ref) for ref in refs)),
        "callsPerQuestion": {str(key): value for key, value in sorted(calls.items())},
        "followUpRate": round(follow_ups / len(refs), 4) if refs else None,
        "searchesPerQuestion": round(statistics.mean(searches), 3) if searches else 0,
        "cacheHits": run["normalizations"].get("RAG_CACHE_HITS", 0),
        "vectorSearches": run["normalizations"].get("VECTOR_SEARCHES", 0),
        "changeSearches": run["normalizations"].get("CHANGE_SEARCH_INSERTED", 0),
        "contextRequests": sum(row.get("context_requests", 0) for ref in refs for row in traces.get(ref, [])),
        "hardErrors": {"perQuestion": dict(Counter(f["code"] for f in run["questionFailures"].values())),
                       "perQuestionCause": dict(causes), "perCall": dict(per_call_errors)},
        "softNormalizations": {key: run["normalizations"].get(key, 0) for key in SOFT},
        "cost": cost(rows),
    }


def kappa(pairs):
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[c] * right[c] for c in OUTCOMES) / (n * n)
    return None if expected == 1 else round((observed - expected) / (1 - expected), 4)


def agreement(first, second):
    refs = [question["ref"] for question in first["questions"]
            if question["ref"] in {row["ref"] for row in second["questions"]}]
    pairs = [(outcome_of(first, ref), outcome_of(second, ref)) for ref in refs]
    matrix = {a: {b: 0 for b in OUTCOMES} for a in OUTCOMES}
    for a, b in pairs:
        matrix[a][b] += 1
    both = [ref for ref in refs if ref in first["decisions"] and ref in second["decisions"]]
    flagged = [ref for ref in both if outcome_of(first, ref) in FINDINGS and outcome_of(second, ref) in FINDINGS]
    binary = [(a in FINDINGS, b in FINDINGS) for a, b in pairs]
    return {
        "questions": len(refs),
        "confusion": matrix,
        "percentAgreement": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
        "cohenKappa": kappa(pairs),
        "binaryFindingAgreement": round(sum(a == b for a, b in binary) / len(binary), 4) if binary else None,
        "basisAgreement": round(sum(first["decisions"][r]["basis"] == second["decisions"][r]["basis"] for r in both)
                                / len(both), 4) if both else None,
        "impactAgreementOnBothFlagged": round(sum(first["decisions"][r]["impact"] == second["decisions"][r]["impact"]
                                                  for r in flagged) / len(flagged), 4) if flagged else None,
        "bothFlagged": len(flagged),
    }


def reason_of(run, ref):
    return (run["decisions"].get(ref) or {}).get("reason") or (run["questionFailures"].get(ref) or {}).get("code")


def disagreements(first, second):
    drafts = {question["ref"]: question["draft"] for question in first["questions"]}
    for ref, draft in drafts.items():
        a, b = outcome_of(first, ref), outcome_of(second, ref)
        if a != b:
            yield {"ref": ref, "quote": draft["target_spans"][0]["quote"], "property": draft["property_or_rule"],
                   f"{first['label']}_outcome": a, f"{first['label']}_reason": reason_of(first, ref),
                   f"{second['label']}_outcome": b, f"{second['label']}_reason": reason_of(second, ref)}


def typo_summary(run, rows):
    result = run["result"]
    return {"label": run["label"], "model": run["model"], "effort": run["effort"], "status": result["status"],
            "failureCode": result["failure_code"], "suggestions": len(result["suggestions"]),
            "segments": dict(Counter(row["status"] for row in result["segments"])), "cost": cost(rows),
            "wallSeconds": run["wallSeconds"]}


def typo_agreement(first, second):
    def key(row):
        return row["span"]["start"], row["span"]["end"]
    a = {key(row): row for row in first["result"]["suggestions"]}
    b = {key(row): row for row in second["result"]["suggestions"]}
    same_span = set(a) & set(b)
    overlap = sum(any(x[0] < y[1] and y[0] < x[1] for y in b) for x in a)
    return {"exactSpanAndReplacement": sum(a[k]["replacement"] == b[k]["replacement"] for k in same_span),
            "sameSpan": len(same_span), "overlappingSpans": overlap,
            "categoryAgreementOnSameSpan": sum(a[k].get("category") == b[k].get("category") for k in same_span)}


def markdown(report):
    lines = ["# 검수 실험 보고서", "", f"- 생성: {report['generatedAt']}", (f"- 로직: {report['logicVersion']} / schema "
             f"{report['schemaVersion']} / fixture {report['fixtureSha256'][:12]}"), ""]
    for summary in report["judges"]:
        lines += [f"## 판정 {summary['label']} ({summary['model']}, {summary['effort']})", "",
                  f"- 질문 {summary['questions']}개, 상태 {summary['status']}, 벽시계 {summary['wallSeconds']}초",
                  f"- 결과 분포: {summary['outcomes']}", (f"- 질문당 호출: {summary['callsPerQuestion']}, "
                  f"FOLLOW_UP 비율 {summary['followUpRate']}"),
                  (f"- 검색/질문 {summary['searchesPerQuestion']}, 실제 검색 {summary['vectorSearches']}, 캐시 재사용 "
                  f"{summary['cacheHits']}, 변화 검색 {summary['changeSearches']}, 문맥 요청 {summary['contextRequests']}"),
                  f"- 양식 오류(질문): {summary['hardErrors']['perQuestionCause']}",
                  f"- 양식 오류(호출): {summary['hardErrors']['perCall']}",
                  f"- 정규화: {summary['softNormalizations']}", ""]
        lines += ["| 역할 | 호출 | 입력 | 캐시 입력 | 출력 | USD | p50 ms | p95 ms |", "|---|---|---|---|---|---|---|---|"]
        for role, row in summary["cost"].items():
            lines.append(f"| {role} | {row['calls']} | {row['inputTokens']} | {row['cachedInputTokens']} | "
                         f"{row['outputTokens']} | {row['usd']} | {row['latencyP50Ms']} | {row['latencyP95Ms']} |")
        lines.append("")
    if report.get("agreement"):
        agreement = report["agreement"]
        lines += ["## 판정 일치도", "", (f"- 비교 질문 {agreement['questions']}개, 일치율 {agreement['percentAgreement']}, "
                  f"Cohen κ {agreement['cohenKappa']}"),
                  (f"- 지적 여부 일치 {agreement['binaryFindingAgreement']}, 근거 일치 {agreement['basisAgreement']}, "
                  f"양쪽 지적의 영향 일치 {agreement['impactAgreementOnBothFlagged']} ({agreement['bothFlagged']}개)"), "",
                  "| A \\ B | " + " | ".join(OUTCOMES) + " |", "|---|" + "---|" * len(OUTCOMES)]
        for a in OUTCOMES:
            lines.append(f"| {a} | " + " | ".join(str(agreement["confusion"][a][b]) for b in OUTCOMES) + " |")
        lines.append("")
    for summary in report["typos"]:
        lines += [f"## 오탈자 {summary['label']} ({summary['model']}, {summary['effort']})", "",
                  (f"- 상태 {summary['status']} {summary['failureCode'] or ''}, 제안 {summary['suggestions']}개, "
                  f"구간 {summary['segments']}"), f"- 비용 {summary['cost']}", ""]
    if report.get("typoAgreement"):
        lines += ["## 오탈자 비교", "", f"- {report['typoAgreement']}", ""]
    return "\n".join(lines) + "\n"


def write_report(out, *, judges=(), typos=()):
    out = Path(out)
    runs = [read_json(out / "judge" / label / "result.json") for label in judges]
    rows = {label: ledger_rows(out / "judge" / label) for label in judges}
    typo_runs = [read_json(out / "typo" / label / "result.json") for label in typos]
    typo_rows = {label: ledger_rows(out / "typo" / label) for label in typos}
    first = (runs or typo_runs or [{}])[0]
    report = {"generatedAt": first.get("at"), "logicVersion": first.get("logicVersion"),
              "schemaVersion": first.get("schemaVersion"), "fixtureSha256": first.get("fixtureSha256", ""),
              "aiGit": first.get("aiGit"), "rag": first.get("rag"), "budgets": first.get("budgets"),
              "judges": [judge_summary(run, rows[run["label"]]) for run in runs],
              "typos": [typo_summary(run, typo_rows[run["label"]]) for run in typo_runs]}
    if len(runs) >= 2:
        report["agreement"] = agreement(runs[0], runs[1])
    if len(typo_runs) >= 2:
        report["typoAgreement"] = typo_agreement(typo_runs[0], typo_runs[1])
    directory = out / "report"
    paths = {"json": write_json(directory / "report.json", report)}
    (directory / "report.md").write_text(markdown(report), encoding="utf-8")
    paths["md"] = directory / "report.md"
    if len(runs) >= 2:
        rows_out = list(disagreements(runs[0], runs[1]))
        with (directory / "disagreements.csv").open("w", encoding="utf-8", newline="") as handle:
            fields = ["ref", "quote", "property", f"{runs[0]['label']}_outcome", f"{runs[0]['label']}_reason",
                      f"{runs[1]['label']}_outcome", f"{runs[1]['label']}_reason"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows_out)
        paths["csv"] = directory / "disagreements.csv"
    return paths
