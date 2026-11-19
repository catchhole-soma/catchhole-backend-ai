"""Save independent actual results before opening the separate oracle input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from .evidence import code_identity, counts, digest, save
from .input import parse_cases
from .policy import prepare


def compute(document: dict, *, extended: bool = False) -> tuple[dict, dict, dict]:
    # Engines are imported here so input handling itself has no RDF dependency.
    from . import owl_engine, python_engine

    started = perf_counter()
    cases = parse_cases(document)
    parsed = perf_counter()
    prepared = [prepare(case) for case in cases]
    eligible = perf_counter()
    actuals = {}
    measurements = {
        "sharedParseMs": (parsed - started) * 1000,
        "sharedEligibilityMs": (eligible - parsed) * 1000,
        "engines": {},
    }
    for name, engine in (("B", python_engine), ("C", owl_engine)):
        actuals[name] = []
        measurements["engines"][name] = []
        for item in prepared:
            result, times = engine.run_measured(item, extended=extended)
            actuals[name].append(result)
            measurements["engines"][name].append(
                {"caseId": item.case["caseId"], **times, "counts": counts(result)}
            )
    inputs = {"schemaVersion": 2, "synthetic": True, "cases": list(cases)}
    return inputs, actuals, measurements


def execute(input_path: Path, oracle_path: Path, output: Path, *, extended=False) -> dict:
    if output.exists():
        raise FileExistsError("use a new output directory; never overwrite recorded evidence")
    identity = code_identity()
    document = json.loads(input_path.read_text(encoding="utf-8"))
    inputs, actuals, measurements = compute(document, extended=extended)
    output.mkdir(parents=True)
    save(output / "inputs.json", inputs)
    for name, rows in actuals.items():
        save(output / f"actual-{name}.json", rows)
    save(output / "measurements.json", measurements)
    persisted = {
        name: json.loads((output / f"actual-{name}.json").read_text(encoding="utf-8"))
        for name in actuals
    }
    if persisted != actuals:
        raise ValueError("persisted actuals differ from engine output")
    actuals = persisted
    # Evaluation starts only after both actuals have been durably written.
    from .evaluate import evaluate

    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    if digest(list(parse_cases(oracle))) != digest(inputs["cases"]):
        raise ValueError("oracle and execution inputs differ")
    reports = {name: evaluate(rows, oracle) for name, rows in actuals.items()}
    save(output / "evaluation.json", reports)
    manifest = {
        "formatVersion": 1,
        "profile": "grandchild-v1" if extended else "base-v1",
        "identity": identity,
        "inputHash": digest(inputs),
        "oracleHash": digest(oracle),
        "outputHashes": {name: digest(rows) for name, rows in actuals.items()},
        "caseCount": len(inputs["cases"]),
        "evaluations": reports,
        "sameOutputs": actuals["B"] == actuals["C"],
        "scope": "synthetic offline full recomputation; no production adapter",
    }
    save(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extended", action="store_true")
    args = parser.parse_args()
    manifest = execute(args.input, args.oracle, args.output, extended=args.extended)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "caseCount": manifest["caseCount"],
                "sameOutputs": manifest["sameOutputs"],
                "evaluations": manifest["evaluations"],
            },
            ensure_ascii=False,
        )
    )
    if not manifest["sameOutputs"] or not all(
        report["passed"] for report in manifest["evaluations"].values()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
