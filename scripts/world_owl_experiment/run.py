"""Persist real OWL outputs before reading an independently authored oracle."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from .contract import parse_document
from .evaluate import digest, evaluate


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def identity():
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "scripts/world_owl_experiment").glob("*.py")) + [root / "pyproject.toml"]
    return {
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
        ),
        "files": {
            str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest() for path in paths
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def compute(document, *, max_checks=8192):
    from .engine import EngineError
    from .proofs import ProofLimitError
    from .reasoning import run_case

    cases = parse_document(document)
    actuals, measurements, ontologies = [], [], {}
    for case in cases:
        try:
            result, times, rdfxml = run_case(case, max_checks=max_checks)
        except (EngineError, ProofLimitError) as error:
            result = {
                "caseId": case["caseId"],
                "outcome": "ERROR",
                "errorCode": getattr(error, "code", "PROOF_LIMIT"),
                "queries": [],
                "conflictSets": [],
                "excludedStatements": [],
                "explanationComplete": False,
            }
            times, rdfxml = {"caseId": case["caseId"], "failed": True}, None
        actuals.append(result)
        measurements.append(times)
        if rdfxml is not None:
            ontologies[case["caseId"]] = rdfxml
    return actuals, measurements, ontologies


def execute(input_path, oracle_path, output, *, max_checks=8192):
    if output.exists():
        raise FileExistsError("use a fresh output directory; preserve previous evidence")
    provenance = identity()
    document = json.loads(input_path.read_text(encoding="utf-8"))
    started = perf_counter()
    actuals, measurements, ontologies = compute(document, max_checks=max_checks)
    compute_ms = (perf_counter() - started) * 1000
    output.mkdir(parents=True)
    save(output / "inputs.json", document)
    save(output / "actual.json", actuals)
    save(output / "measurements.json", {"computeMs": compute_ms, "cases": measurements})
    (output / "ontologies").mkdir()
    for key, rdfxml in ontologies.items():
        path = output / "ontologies" / f"{key}.owl"
        path.write_text(rdfxml, encoding="utf-8")
        if path.read_bytes() != rdfxml.encode("utf-8"):
            raise ValueError("persisted ontology differs from the engine export")
    persisted = json.loads((output / "actual.json").read_text(encoding="utf-8"))
    if persisted != actuals:
        raise ValueError("persisted output differs from the actual engine output")
    # The oracle is deliberately unopened until actual output has been saved.
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    report = evaluate(persisted, oracle, document)
    save(output / "evaluation.json", report)
    manifest = {
        "schemaVersion": 1,
        "identity": provenance,
        "inputHash": digest(document),
        "oracleHash": digest(oracle),
        "actualHash": digest(persisted),
        "ontologyHashes": {
            key: sha256(value.encode()).hexdigest() for key, value in ontologies.items()
        },
        "evaluation": report,
        "scope": "synthetic, manually formalized, full snapshot recomputation; no LLM or production adapter",
        "maxChecksPerCase": max_checks,
    }
    save(output / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-checks", type=int, default=8192)
    args = parser.parse_args()
    manifest = execute(args.input, args.oracle, args.output, max_checks=args.max_checks)
    print(json.dumps(manifest["evaluation"], ensure_ascii=False, indent=2))
    if manifest["evaluation"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
