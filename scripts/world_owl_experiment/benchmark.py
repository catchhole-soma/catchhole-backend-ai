"""Sequential fresh-process runs; decision and exhaustive explanation are separate."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from .evaluate import digest
from .run import identity, save


def sample(input_path):
    import resource

    from .run import compute

    document = json.loads(input_path.read_text(encoding="utf-8"))
    started = perf_counter()
    actuals, measurements, _ = compute(document)
    elapsed = (perf_counter() - started) * 1000
    if any(row["outcome"] == "ERROR" for row in actuals):
        raise RuntimeError("benchmark has failed cases")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "actualHash": digest(actuals),
        "computeMs": elapsed,
        "peakRssMiB": rss / (1024 * 1024 if sys.platform == "darwin" else 1024),
        "cases": measurements,
        "phaseTotalsMs": {
            key: sum(row[key] for row in measurements)
            for key in (
                "eligibilityMs",
                "engineSetupMs",
                "decisionMs",
                "explanationMs",
                "rdfExportMs",
                "totalMs",
            )
        },
    }


def benchmark(input_path, output, *, repeats=3):
    if not 1 <= repeats <= 10:
        raise ValueError("repeats must be 1..10")
    if output.exists():
        raise FileExistsError("benchmark evidence already exists")
    samples = []
    with tempfile.TemporaryDirectory(prefix="world-owl-benchmark-") as directory:
        for index in range(repeats):
            path = Path(directory) / f"sample-{index}.json"
            started = perf_counter()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.world_owl_experiment.benchmark",
                    "--input",
                    str(input_path.resolve()),
                    "--output",
                    str(path),
                    "--sample",
                ],
                check=True,
                timeout=300,
            )
            row = json.loads(path.read_text())
            row["processMs"] = (perf_counter() - started) * 1000
            samples.append(row)
    if len({row["actualHash"] for row in samples}) != 1:
        raise RuntimeError("fresh processes produced different semantic results")
    report = {
        "schemaVersion": 1,
        "identity": identity(),
        "inputHash": digest(json.loads(input_path.read_text())),
        "repeats": repeats,
        "samples": samples,
        "medians": {
            key: statistics.median(row[key] for row in samples)
            for key in ("computeMs", "processMs", "peakRssMiB")
        },
        "phaseMedianTotalsMs": {
            key: statistics.median(row["phaseTotalsMs"][key] for row in samples)
            for key in samples[0]["phaseTotalsMs"]
        },
        "scope": "whole synthetic suite; fresh JVM per process; fresh ontology/reasoner per decision; full recomputation, no Python baseline",
        "memory": "process peak RSS includes JVM and Python; no before/after allocation claim",
    }
    save(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args()
    if args.sample:
        if args.output.exists():
            raise FileExistsError("sample already exists")
        save(args.output, sample(args.input))
    else:
        print(json.dumps(benchmark(args.input, args.output, repeats=args.repeats)["medians"]))


if __name__ == "__main__":
    main()
