"""Measure offline model costs in fresh subprocesses, never production latency."""

import argparse
import gzip
import importlib
import json
import platform
import resource
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from .input import load_path
from .run import code_identity, write_json


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _timed(fn):
    start = perf_counter_ns()
    result = fn()
    return result, (perf_counter_ns() - start) / 1_000_000


def worker(name: str, path: Path, iterations: int, extended: bool) -> dict:
    module, import_ms = _timed(
        lambda: importlib.import_module(
            ".typed_model" if name == "B" else ".rdf_model", __package__
        )
    )
    factory = module.TypedModel if name == "B" else module.RdfModel
    dataset, parse_ms = _timed(lambda: load_path(path))
    models, load_ms = _timed(lambda: [factory(case, extended=extended) for case in dataset.cases])
    validations, validation_ms = _timed(lambda: [model.validate() for model in models])
    if not all(report["conforms"] for report in validations):
        raise ValueError("cannot benchmark an invalid dataset")

    def query_all():
        return [
            model.query(query)
            for model, case in zip(models, dataset.cases, strict=True)
            for query in case.queries
        ]

    actual, first_ms = _timed(query_all)
    repeats = []
    for _ in range(iterations):
        again, duration = _timed(query_all)
        if again != actual:
            raise ValueError("query output changed during benchmark")
        repeats.append(duration)
    serialized = (
        [model.serialize_turtle().encode() for model in models]
        if name == "C"
        else [_json(model.to_case().to_dict()) for model in models]
    )
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports KiB; includes imports and all model work.
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    return {
        "implementation": name,
        "caseCount": len(dataset.cases),
        "queryCount": len(actual),
        "modelImportMs": import_ms,
        "commonParseMs": parse_ms,
        "modelLoadMs": load_ms,
        "validationMs": validation_ms,
        "firstQueryBatchMs": first_ms,
        "repeatedQueryBatchMs": repeats,
        "repeatedQueryBatchMedianMs": median(repeats),
        "processPeakRssBytes": peak_bytes,
        "serializedInputBytes": sum(len(value) for value in serialized),
        "gzipInputBytes": sum(len(gzip.compress(value, mtime=0)) for value in serialized),
        "outputBytes": len(_json(actual)),
        "outputSha256": sha256(_json(actual)).hexdigest(),
        "tripleCount": sum(len(model.graph) for model in models) if name == "C" else None,
    }


def benchmark(path: Path, processes: int = 3, iterations: int = 5, extended: bool = False) -> dict:
    if processes < 1 or iterations < 1:
        raise ValueError("positive repetitions required")
    runs = {"B": [], "C": []}
    for repeat in range(processes):
        for name in ("B", "C") if repeat % 2 == 0 else ("C", "B"):
            command = [
                sys.executable,
                "-m",
                f"{__package__}.benchmark",
                "--worker",
                name,
                "--input",
                str(path.resolve()),
                "--iterations",
                str(iterations),
            ]
            if extended:
                command.append("--extended")
            result = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=180
            )
            runs[name].append(json.loads(result.stdout))
    hashes = {run["outputSha256"] for values in runs.values() for run in values}
    if len(hashes) != 1:
        raise ValueError("benchmark implementations or repetitions returned different results")
    fields = (
        "modelImportMs",
        "commonParseMs",
        "modelLoadMs",
        "validationMs",
        "firstQueryBatchMs",
        "repeatedQueryBatchMedianMs",
        "processPeakRssBytes",
        "serializedInputBytes",
        "gzipInputBytes",
        "outputBytes",
    )
    return {
        "metadata": {
            **code_identity(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processesPerModel": processes,
            "repeatedQueryBatchesPerProcess": iterations,
            "extended": extended,
            "inputSha256": sha256(_json(load_path(path).to_dict())).hexdigest(),
        },
        "scope": {
            "timeUnit": "milliseconds per entire dataset batch, not per query",
            "freshProcess": "fresh interpreter; OS file caches are not flushed",
            "query": "after explicit validation; includes result assembly and integrity guard",
            "memory": "whole-process peak RSS including library imports; not incremental heap",
            "size": "per-case compact JSON(B) or Turtle(C); ontology/shapes/query files excluded",
            "excludes": "HTTP, database, LLM, network, production workload and concurrent load",
        },
        "medians": {
            name: {field: median(run[field] for run in rows) for field in fields}
            for name, rows in runs.items()
        },
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--processes", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--worker", choices=("B", "C"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = worker(args.worker, args.input, args.iterations, args.extended)
    else:
        if args.output is None:
            parser.error("--output is required")
        if args.output.exists():
            parser.error("output already exists")
        result = benchmark(args.input, args.processes, args.iterations, args.extended)
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
