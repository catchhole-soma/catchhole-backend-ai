"""Harness end to end with the offline fake provider (no network, no DB), and the report's agreement math."""

import json

import pytest

from scripts.validation_experiment import report as report_module
from scripts.validation_experiment.run import main


def test_offline_pipeline_plans_judges_records_contract_errors_and_reports(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)  # the fake provider never needs a key
    out = tmp_path / "experiment"
    assert main(["synthetic", "--out", str(out)]) == 0
    manuscripts, raw = out / "synthetic" / "manuscripts", out / "synthetic" / "baseline-raw.json"
    assert main(["build-fixture", "--out", str(out), "--raw", str(raw), "--manuscripts", str(manuscripts),
                 "--target-no", "3"]) == 0
    fixture = out / "fixture" / "fixture.json"
    common = ["--out", str(out), "--fixture", str(fixture), "--manuscripts", str(manuscripts), "--fake"]
    assert main(["plan", *common]) == 0
    assert main(["judge", *common, "--plan-dir", str(out / "plan"), "--label", "a", "--model", "gpt-6.1-terra"]) == 0
    assert main(["judge", *common, "--plan-dir", str(out / "plan"), "--label", "b", "--model", "gpt-6-luna"]) == 0
    assert main(["typo", *common, "--label", "t", "--model", "gpt-6-luna"]) == 0
    assert main(["report", "--out", str(out), "--judge", "a", "--judge", "b", "--typo", "t"]) == 0

    run = json.loads((out / "judge" / "a" / "result.json").read_text())
    assert run["questionFailures"]["Q3"]["cause"] == "MODEL_CONTRACT_JSON_INVALID"
    # Without a RAG database search is not offered, so the follow-up searches fail their questions only.
    assert {run["questionFailures"][ref]["code"] for ref in ("Q1", "Q2")} == {"VECTOR_NOT_READY"}
    assert set(run["decisions"]) == {"Q4", "Q5", "Q6"}
    calls = [json.loads(line) for line in (out / "judge" / "a" / "calls.jsonl").read_text().splitlines()]
    assert {row["model"] for row in calls} == {"gpt-6.1-terra"} and all("Authorization" not in row for row in calls)
    report = json.loads((out / "report" / "report.json").read_text())
    assert report["agreement"]["percentAgreement"] == 1.0 and report["agreement"]["questions"] == 6
    assert report["judges"][0]["hardErrors"]["perQuestion"]["VECTOR_NOT_READY"] == 2
    assert (out / "report" / "report.md").read_text().startswith("# 검수 실험 보고서")


def test_paid_steps_require_explicit_confirmation(tmp_path, capsys):
    assert main(["plan", "--out", str(tmp_path), "--fixture", "x", "--manuscripts", "y"]) == 2
    assert "--confirm-paid" in capsys.readouterr().err


def test_agreement_counts_kappa_binary_basis_and_impact():
    def run(label, outcomes):
        decisions = {ref: {"outcome": outcome, "basis": "B", "impact": "SEVERE" if outcome != "NO_CONFLICT" else None,
                           "reason": label} for ref, outcome in outcomes.items() if outcome != "INCOMPLETE"}
        return {"label": label, "questions": [{"ref": ref, "draft": {"target_spans": [{"quote": "q"}],
                                                                       "property_or_rule": "p"}} for ref in outcomes],
                "decisions": decisions, "questionFailures": {ref: {"code": "X"} for ref, o in outcomes.items()
                                                             if o == "INCOMPLETE"}}

    first = run("a", {"Q1": "CONFLICT", "Q2": "NO_CONFLICT", "Q3": "SUSPECTED_CONFLICT", "Q4": "INCOMPLETE"})
    second = run("b", {"Q1": "CONFLICT", "Q2": "NO_CONFLICT", "Q3": "CONFLICT", "Q4": "NO_CONFLICT"})
    result = report_module.agreement(first, second)
    assert result["percentAgreement"] == 0.5
    # Q3 SUSPECTED vs CONFLICT are both findings; Q4 INCOMPLETE vs NO_CONFLICT are both non-findings.
    assert result["binaryFindingAgreement"] == 1.0 and result["bothFlagged"] == 2
    assert result["confusion"]["SUSPECTED_CONFLICT"]["CONFLICT"] == 1
    assert result["cohenKappa"] == pytest.approx(0.3333, abs=1e-4)
    rows = list(report_module.disagreements(first, second))
    assert [row["ref"] for row in rows] == ["Q3", "Q4"] and rows[1]["a_reason"] == "X"
